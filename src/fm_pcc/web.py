"""Web research for /web: search, fetch the top pages, and rank their
passages locally -- the same split as /docs: code finds and ranks, the
on-device model only reads the few passages that fit its window.

Nothing here runs unless the user types /web: /task, /ask and chat never
search on their own, so nothing leaves the device without being asked.

Search uses DuckDuckGo's HTML results page, which needs no account or
key. It isn't an official API, so it can change or refuse requests;
search() raises WebError with a plain reason when it does.
"""
from __future__ import annotations

import concurrent.futures
import html
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

from . import docs

SEARCH_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
FETCH_TIMEOUT = 10
MAX_PAGE_BYTES = 1_500_000
PAGES = 4


class WebError(Exception):
    pass


def _get(url: str, data: bytes | None = None, timeout: float = FETCH_TIMEOUT) -> tuple[str, bytes]:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.headers.get("Content-Type", ""), r.read(MAX_PAGE_BYTES)


def _result_url(href: str) -> str | None:
    """The real address behind a result link (DuckDuckGo can wrap them in
    a /l/?uddg= redirect)."""
    href = html.unescape(href)
    if "uddg=" in href:
        href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [""])[0])
    if href.startswith("//"):
        href = "https:" + href
    if not href.startswith("http") or "duckduckgo.com" in urllib.parse.urlparse(href).netloc:
        return None
    return href


def search(query: str, limit: int = 8) -> list[dict]:
    """[{title, url, snippet}] for `query`, best first."""
    try:
        _type, body = _get(SEARCH_URL, urllib.parse.urlencode({"q": query}).encode())
    except urllib.error.HTTPError as e:
        raise WebError(f"the search service refused the request (HTTP {e.code}) -- try again in a minute")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise WebError(f"couldn't reach the search service ({getattr(e, 'reason', e)}) -- are you online?")
    page = body.decode("utf-8", "replace")
    results, seen = [], set()
    for block in re.split(r'<div[^>]+class="[^"]*\bresult\b', page)[1:]:
        link = re.search(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S) \
            or re.search(r'<a[^>]+href="([^"]+)"[^>]+class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        url = _result_url(link.group(1))
        if not url or url in seen:
            continue
        seen.add(url)
        snippet = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|td|div)>', block, re.S)
        results.append({
            "title": _plain(link.group(2)),
            "url": url,
            "snippet": _plain(snippet.group(1)) if snippet else "",
        })
        if len(results) >= limit:
            break
    if not results and re.search(r"anomaly|captcha|unusual traffic", page, re.I):
        raise WebError("the search service is asking for a captcha (too many searches) -- try again later")
    return results


def _plain(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def fetch(url: str) -> docs.Page | None:
    """A result page as text, or None if it isn't a readable page (a PDF,
    an image, too slow, an error)."""
    try:
        ctype, body = _get(url)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if ctype and not re.search(r"text/(?:html|plain)|application/xhtml", ctype):
        return None
    charset = re.search(r"charset=([\w-]+)", ctype or "")
    text = body.decode(charset.group(1) if charset else "utf-8", "replace")
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    if "<" in text[:2000]:
        text = docs.html_to_text(text)
    text = text.strip()
    if len(text) < 200:
        return None
    return docs.Page(_plain(title.group(1)) if title else url, url, text)


def fetch_all(results: list[dict], limit: int = PAGES) -> list[docs.Page]:
    """The first `limit` readable pages among the results, fetched in
    parallel, in the results' order."""
    candidates = results[: limit * 2]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        pages = list(pool.map(lambda r: fetch(r["url"]), candidates))
    return [p for p in pages if p][:limit]


def rank(question: str, pages: list[docs.Page], results: list[dict] = (), limit: int = 12) -> list[dict]:
    """The pages' passages (and the search snippets) that best match the
    question -- BM25 over an in-memory index, weighted like /docs -- as
    hits in docs.Library.search's shape."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE passages USING fts5(doc_set UNINDEXED, title, heading, url UNINDEXED, body, "
               "tokenize='porter unicode61')")
    rows = []
    for p in pages:
        rows += [("web", p.title, heading, p.url, body) for heading, body in docs.passages(p)]
    rows += [("web", r["title"], r["title"], r["url"], r["snippet"]) for r in results if r.get("snippet")]
    db.executemany("INSERT INTO passages VALUES (?, ?, ?, ?, ?)", rows)
    terms = docs.query_terms(question)
    if not terms:
        return []
    try:
        found = db.execute(
            "SELECT title, heading, url, body, bm25(passages, 0.0, 6.0, 3.0, 0.0, 1.0) AS r FROM passages "
            "WHERE passages MATCH ? ORDER BY r LIMIT ?", (" OR ".join(terms), limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()
    return [{"set": "web", "title": t, "heading": h, "url": u, "body": b, "rank": r} for t, h, u, b, r in found]
