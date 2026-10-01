"""Web research for /web: search, fetch the top pages, and rank their
passages locally -- the same split as /docs: code finds and ranks, the
on-device model only reads the few passages that fit its window.

Nothing here runs without the user's say-so: /web <question> searches when
typed, and /task and chat search on their own only after asking ("Allow
agent to search the web?") or once the user chose Allow always.

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


def rank(question: str, pages: list[docs.Page], results: list[dict] = (), limit: int = 12,
         recent: bool = False) -> list[dict]:
    """The pages' passages (and the search snippets) that best match the
    question -- BM25 over an in-memory index, weighted like /docs -- as
    hits in docs.Library.search's shape. With `recent`, passages from the
    search's top results and ones mentioning this year or last count for
    more: measured, research for "the latest AI models" otherwise quoted
    2021 models from an old paper."""
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
    hits = [{"set": "web", "title": t, "heading": h, "url": u, "body": b, "rank": r} for t, h, u, b, r in found]
    if recent:
        import time as _time
        year = int(_time.strftime("%Y"))
        order = {r["url"]: i for i, r in enumerate(results)}
        for h in hits:
            years = [int(y) for y in re.findall(r"\b(20[0-9]{2})\b", h["body"] + " " + h["heading"])]
            if any(y >= year - 1 for y in years):
                h["rank"] -= 3
            elif years and max(years) < year - 2:
                h["rank"] += 3
            h["rank"] -= max(0, 3 - order.get(h["url"], 9))          # the search's own top 3
        hits.sort(key=lambda h: h["rank"])
    return hits


# ---------------------------------------------------------------------------
# Does a request need the web?
# ---------------------------------------------------------------------------

_TIME_WORDS = re.compile(
    r"\b(?:latest|newest|current(?:ly)?|recent(?:ly)?|today'?s?|right now|nowadays|up[- ]to[- ]date|"
    r"this (?:year|season|month|week)'?s?|last (?:month|week|year|season|event|night)'?s?|upcoming|trending|"
    r"yet|so far|anymore|still|20[2-9]\d)\b", re.I)
_NOT_NAMES = {"I", "A", "An", "The", "In", "On", "For", "To", "Of", "And", "Or", "But", "With", "Make", "Build",
              "Add", "Create", "Write", "Research", "What", "Who", "Which", "When", "How", "Is", "Are", "Does", "Do",
              "Can", "Please", "List", "Show", "Find", "Update", "Explain", "Use", "Put", "Give", "Tell"}


def names_something(request: str) -> bool:
    """Whether a request refers to something specific in the world -- a
    product, company, person, version ("OpenAI", "M5", "GPT-5", "Python
    3.14", "Champions League") -- or to a time ("latest", "last month's",
    "upcoming"). Measured: the model's own judgment flagged a bakery
    landing page and a page of the planets as needing the web; neither
    names anything that changes."""
    if _TIME_WORDS.search(request):
        return True
    text = re.sub(r"(?<![\w])[`'\"][^`'\"]*[`'\"](?![\w])", " ", request)   # quoted text is content
    text = re.sub(r"\b[\w./-]+\.[A-Za-z]{1,5}\b", " ", text)           # file names
    for word in re.findall(r"\b[A-Za-z][\w.+-]*", text):
        if word in _NOT_NAMES:
            continue
        if word[0].isupper() or re.search(r"\d", word):
            return True
    return bool(re.search(r"\b\d+(?:\.\d+)+\b", text))                 # a version number


_JUDGE_SCHEMA = {"type": "object", "title": "Verdict", "additionalProperties": False,
                 "properties": {"needs_web": {"type": "boolean"}}, "required": ["needs_web"], "x-order": ["needs_web"]}
_JUDGE_EXAMPLES = (
    "Examples:\n"
    "- \"add a multiply function to calc.py\" -> false\n- \"make the button blue in style.css\" -> false\n"
    "- \"build me an html page with a contact form\" -> false\n- \"commit everything\" -> false\n"
    "- \"how do I reverse a list in Python?\" -> false\n- \"write unit tests for parse.py\" -> false\n"
    "- \"what's the newest version of Python?\" -> true\n- \"who won the last Super Bowl?\" -> true\n"
    "- \"Research the latest ai models and build me an html page with a list of them\" -> true\n"
    "- \"what is the price of bitcoin right now?\" -> true\n- \"what new features came in iOS 26?\" -> true\n")


def needs_web(request: str, structured) -> bool:
    """Whether doing `request` well needs facts from the web: the model's
    judgment (`structured(schema, prompt)`, guided generation) AND the
    request naming something specific or a time (names_something). When
    the model refuses to answer (its guardrails, seen for "members of the
    current US Supreme Court"), names_something decides alone."""
    if not names_something(request):
        return False
    prompt = ("Does this request need up-to-date facts from the web -- recent events, the latest versions or releases, "
              "current prices, people in roles, rankings -- rather than programming work or general knowledge?\n\n"
              f"{_JUDGE_EXAMPLES}\nRequest: {request}")
    try:
        return bool(structured(_JUDGE_SCHEMA, prompt).get("needs_web"))
    except Exception:
        return True

