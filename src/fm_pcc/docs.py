"""Offline documentation library: download official docs once, search them
locally.

Each doc set is fetched from its official source -- a sparse, shallow git
clone of just the docs folders (MDN, swift-book, react.dev, the Rust Book
and Reference, Go's spec and website), or the Python docs' own plain-text
archive -- cleaned into text, split at headings into passages of at most a
few hundred words, and stored in one SQLite full-text index
(~/.fm-pcc/docs/index.sqlite), each passage with a link to its official
page. After that, searching needs no network.

The model never browses anything: search() ranks passages with SQLite's
BM25 in code, and callers hand the model the top few (or feed many through
map-reduce) -- the same split as the rest of fm-pcc, code for finding,
model for reading.
"""
from __future__ import annotations

import html
import html.parser
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterable

PASSAGE_CHARS = 1600
Progress = Callable[[str], None]


@dataclass
class Page:
    title: str
    url: str
    text: str  # markdown-ish plain text


@dataclass
class DocSet:
    id: str
    name: str
    description: str
    fetch: Callable[[str, Progress], Iterable[Page]] = field(repr=False)
    extensions: tuple[str, ...] = ()   # project files this set is about


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

_FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def front_matter(text: str) -> tuple[dict[str, str], str]:
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, text[m.end():]


_MDN_MACRO_RE = re.compile(r"\{\{\s*([\w-]+)(?:\((.*?)\))?\s*\}\}")


def clean_markdown(text: str) -> str:
    """Markdown with the site-specific noise removed: MDN's {{macros}}
    (keeping a readable argument where there is one), DocC directives,
    HTML comments, JSX-ish components, and runs of blank lines."""
    def macro(m: re.Match) -> str:
        name, args = m.group(1), m.group(2) or ""
        if name.lower() in ("cssxref", "jsxref", "htmlelement", "domxref", "glossary", "httpheader", "svgelement",
                            "htmlattrxref", "cssinfo", "jsxref"):
            first = re.findall(r'"([^"]*)"|\'([^\']*)\'', args)
            if first:
                a, b = first[-1] if len(first) > 1 else first[0]
                return f"`{a or b}`"
        return ""
    text = _MDN_MACRO_RE.sub(macro, text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"^@\w+.*$", "", text, flags=re.MULTILINE)                  # DocC @Metadata etc.
    text = re.sub(r"</?(?:Intro|Note|Pitfall|DeepDive|Recipes|Sandpack|Illustration|Diagram|YouWillLearn|"
                  r"Recap|Challenges|Solution|Hint|InlineToc|ConsoleBlock|TerminalBlock|Wip|Canary|RSC)[^>]*>",
                  "", text)                                                   # react.dev components
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)                      # links -> their text
    text = re.sub(r"\s*\{/\*.*?\*/\}", "", text)                                 # react.dev heading anchors
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class _HTMLText(html.parser.HTMLParser):
    """HTML -> text, keeping headings as markdown headings and code as
    code, dropping scripts/styles/navigation."""
    SKIP = {"script", "style", "nav", "head", "footer"}
    BLOCK = {"p", "div", "li", "tr", "br", "section", "article", "table", "ul", "ol", "dl", "dt", "dd", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.pre = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "pre":
            self.pre += 1
            self.out.append("\n```\n")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.out.append("\n```\n")
        elif re.fullmatch(r"h[1-6]", tag) or tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        self.out.append(data if self.pre else re.sub(r"\s+", " ", data))


def html_to_text(markup: str) -> str:
    parser = _HTMLText()
    parser.feed(markup)
    text = "".join(parser.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def passages(page: Page, limit: int = PASSAGE_CHARS) -> list[tuple[str, str]]:
    """(heading, body) passages of a page: split at markdown headings, long
    sections split again at paragraph breaks (never inside a code block)."""
    out: list[tuple[str, str]] = []
    heading, buf = page.title, []
    in_code = False

    def flush():
        body = "\n".join(buf).strip()
        if body:
            out.extend((heading, part) for part in _split_body(body, limit))

    for line in page.text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r"#{1,4}\s+(.+)", line)
        if m:
            flush()
            buf = []
            heading = f"{page.title} — {m.group(1).strip()}" if m.group(1).strip() != page.title else page.title
        else:
            buf.append(line)
    flush()
    return out


def _split_body(body: str, limit: int) -> list[str]:
    if len(body) <= limit:
        return [body]
    parts, current = [], ""
    for para in re.split(r"\n\s*\n", body):
        if current and len(current) + len(para) + 2 > limit:
            parts.append(current)
            current = ""
        if len(para) > limit and "```" not in para:
            for i in range(0, len(para), limit):
                parts.append(para[i:i + limit])
            continue
        current = f"{current}\n\n{para}" if current else para
    if current:
        parts.append(current)
    return parts


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def sparse_clone(repo: str, paths: list[str], dest: str, progress: Progress, branch: str | None = None) -> str:
    """Just `paths` of a GitHub repo at its newest commit, without the rest
    of its history or files. Returns the commit it got."""
    url = f"https://github.com/{repo}"
    args = ["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", url, dest]
    if branch:
        args[4:4] = ["--branch", branch]
    progress(f"downloading {repo}…")
    subprocess.run(args, check=True, capture_output=True, text=True, timeout=600)
    subprocess.run(["git", "-C", dest, "sparse-checkout", "set", "--no-cone", *[f"/{p}" for p in paths]],
                   check=True, capture_output=True, text=True, timeout=600)
    return subprocess.run(["git", "-C", dest, "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def _walk(root: str, exts: tuple[str, ...]) -> Iterable[str]:
    for dirpath, _dirs, files in os.walk(root):
        for f in sorted(files):
            if f.endswith(exts):
                yield os.path.join(dirpath, f)


def _read(path: str) -> str:
    with open(path, errors="replace") as f:
        return f.read()


def _first_heading(text: str, fallback: str) -> str:
    m = re.search(r"^#{1,2}\s+(.+)$", text, re.MULTILINE)
    return (m.group(1).strip().strip("`") if m else fallback)[:120]


def fetch_mdn(section: str) -> Callable[[str, Progress], Iterable[Page]]:
    def fetch(work: str, progress: Progress) -> Iterable[Page]:
        sparse_clone("mdn/content", [f"files/en-us/web/{section}/"], work, progress)
        root = os.path.join(work, "files", "en-us")
        for path in _walk(os.path.join(root, "web", section), (".md",)):
            meta, body = front_matter(_read(path))
            slug = meta.get("slug") or os.path.relpath(os.path.dirname(path), root)
            title = (meta.get("title") or slug.rsplit("/", 1)[-1]).replace("`", "")
            yield Page(title, f"https://developer.mozilla.org/en-US/docs/{slug}", clean_markdown(body))
    return fetch


def fetch_swift(work: str, progress: Progress) -> Iterable[Page]:
    sparse_clone("swiftlang/swift-book", ["TSPL.docc/GuidedTour/", "TSPL.docc/LanguageGuide/",
                                           "TSPL.docc/ReferenceManual/"], work, progress)
    base = "https://docs.swift.org/swift-book/documentation/the-swift-programming-language"
    for path in _walk(os.path.join(work, "TSPL.docc"), (".md",)):
        text = _read(path)
        name = os.path.splitext(os.path.basename(path))[0]
        yield Page(_first_heading(text, name), f"{base}/{name.lower()}", clean_markdown(text))
    # Apple's own AI-ready guides and compiler diagnostics that ship with Xcode.
    xcode = "/Applications/Xcode.app/Contents"
    for folder, url in (
        (f"{xcode}/PlugIns/IDEIntelligenceChat.framework/Versions/A/Resources/AdditionalDocumentation",
         "xcode:AdditionalDocumentation"),
        (f"{xcode}/Developer/Toolchains/XcodeDefault.xctoolchain/usr/share/doc/swift/diagnostics",
         "xcode:swift-diagnostics"),
    ):
        if os.path.isdir(folder):
            progress("adding Apple's guides and compiler diagnostics from Xcode…")
            for path in _walk(folder, (".md",)):
                text = _read(path)
                name = os.path.basename(path)
                yield Page(_first_heading(text, name), f"{url}/{name}", clean_markdown(text))


def latest_python_archive() -> tuple[str, str]:
    """(version, url) of the newest stable Python docs text archive."""
    with urllib.request.urlopen("https://docs.python.org/3/archives/", timeout=30) as r:
        listing = r.read().decode("utf-8", "replace")
    versions = sorted(
        {tuple(int(x) for x in v.split(".")) for v in re.findall(r"python-(3\.\d+(?:\.\d+)?)-docs-text\.tar\.bz2", listing)
         if not re.search(r"[a-z]", v)}
    )
    if not versions:
        raise RuntimeError("couldn't find the Python docs archive")
    v = ".".join(map(str, versions[-1]))
    return v, f"https://docs.python.org/3/archives/python-{v}-docs-text.tar.bz2"


def fetch_python(work: str, progress: Progress) -> Iterable[Page]:
    version, url = latest_python_archive()
    progress(f"downloading the Python {version} docs…")
    archive = os.path.join(work, "python-docs.tar.bz2")
    with urllib.request.urlopen(url, timeout=300) as r, open(archive, "wb") as f:
        shutil.copyfileobj(r, f)
    with tarfile.open(archive) as tar:
        tar.extractall(work, filter="data")
    root = next(os.path.join(work, d) for d in os.listdir(work) if d.startswith("python-") and os.path.isdir(os.path.join(work, d)))
    for path in _walk(root, (".txt",)):
        rel = os.path.relpath(path, root)
        if rel.startswith(("whatsnew/", "changelog")):
            continue
        text = _read(path)
        # reST-style underlined headings -> markdown headings
        text = re.sub(r"^(.+)\n[=*]{3,}\s*$", r"# \1", text, flags=re.MULTILINE)
        text = re.sub(r"^(.+)\n[-~^\"]{3,}\s*$", r"## \1", text, flags=re.MULTILINE)
        yield Page(_first_heading(text, rel), f"https://docs.python.org/{version.rsplit('.', 1)[0] if version.count('.') > 1 else version}/{rel[:-4]}.html", text)


def fetch_go(work: str, progress: Progress) -> Iterable[Page]:
    go_dir = os.path.join(work, "go")
    site_dir = os.path.join(work, "website")
    sparse_clone("golang/go", ["doc/go_spec.html", "doc/go_mem.html"], go_dir, progress)
    sparse_clone("golang/website", ["_content/doc/effective_go.html", "_content/doc/faq.md",
                                    "_content/doc/code.html"], site_dir, progress)
    for path, url in (
        (os.path.join(go_dir, "doc", "go_spec.html"), "https://go.dev/ref/spec"),
        (os.path.join(go_dir, "doc", "go_mem.html"), "https://go.dev/ref/mem"),
        (os.path.join(site_dir, "_content", "doc", "effective_go.html"), "https://go.dev/doc/effective_go"),
        (os.path.join(site_dir, "_content", "doc", "code.html"), "https://go.dev/doc/code"),
    ):
        if os.path.exists(path):
            raw = _read(path)
            meta_title = re.search(r'"Title":\s*"([^"]+)"', raw)
            text = html_to_text(re.sub(r"\A<!--\{.*?\}-->", "", raw, flags=re.DOTALL))
            yield Page(meta_title.group(1) if meta_title else _first_heading(text, url), url, text)
    faq = os.path.join(site_dir, "_content", "doc", "faq.md")
    if os.path.exists(faq):
        meta, body = front_matter(_read(faq))
        yield Page(meta.get("title", "Go FAQ"), "https://go.dev/doc/faq", clean_markdown(body))
    # The standard library, from the installed Go toolchain's own docs.
    if shutil.which("go"):
        pkgs = subprocess.run(["go", "list", "std"], capture_output=True, text=True, timeout=120).stdout.split()
        pkgs = [p for p in pkgs if "internal" not in p and not p.startswith("vendor/")]
        progress(f"adding the standard library ({len(pkgs)} packages) from `go doc`…")
        for pkg in pkgs:
            r = subprocess.run(["go", "doc", "-all", pkg], capture_output=True, text=True, timeout=60)
            if r.returncode == 0 and r.stdout.strip():
                text = re.sub(r"^(FUNCTIONS|TYPES|CONSTANTS|VARIABLES)$", r"## \1", r.stdout, flags=re.MULTILINE)
                text = re.sub(r"^(func|type) (\S+)", r"### \1 \2", text, flags=re.MULTILINE)
                yield Page(f"package {pkg}", f"https://pkg.go.dev/{pkg}", text)


def fetch_rust(work: str, progress: Progress) -> Iterable[Page]:
    book, ref = os.path.join(work, "book"), os.path.join(work, "reference")
    sparse_clone("rust-lang/book", ["src/"], book, progress)
    sparse_clone("rust-lang/reference", ["src/"], ref, progress)
    for root, base in ((book, "https://doc.rust-lang.org/book"), (ref, "https://doc.rust-lang.org/reference")):
        for path in _walk(os.path.join(root, "src"), (".md",)):
            rel = os.path.relpath(path, os.path.join(root, "src"))
            if os.path.basename(rel) == "SUMMARY.md":
                continue
            text = re.sub(r"\{\{#\w+[^}]*\}\}", "", _read(path))  # mdBook include directives
            text = re.sub(r"^r\[[\w.\-]+\]\s*$", "", text, flags=re.MULTILINE)  # Reference rule ids
            yield Page(_first_heading(text, rel), f"{base}/{rel[:-3]}.html", clean_markdown(text))


def fetch_react(work: str, progress: Progress) -> Iterable[Page]:
    sparse_clone("reactjs/react.dev", ["src/content/learn/", "src/content/reference/"], work, progress)
    root = os.path.join(work, "src", "content")
    for path in _walk(root, (".md",)):
        rel = os.path.relpath(path, root)
        meta, body = front_matter(_read(path))
        slug = rel[:-3].removesuffix("/index")
        yield Page(meta.get("title", slug.rsplit("/", 1)[-1]), f"https://react.dev/{slug}", clean_markdown(body))


SETS: dict[str, DocSet] = {s.id: s for s in [
    DocSet("swift", "Swift", "The Swift Programming Language (swift.org), plus Xcode's guides and diagnostics",
           fetch_swift, (".swift",)),
    DocSet("python", "Python", "The official Python docs for the newest release (docs.python.org)",
           fetch_python, (".py",)),
    DocSet("html", "HTML", "MDN's HTML reference and guides", fetch_mdn("html"), (".html", ".htm")),
    DocSet("javascript", "JavaScript", "MDN's JavaScript reference and guides", fetch_mdn("javascript"),
           (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")),
    DocSet("css", "CSS", "MDN's CSS reference and guides", fetch_mdn("css"), (".css", ".scss", ".less")),
    DocSet("go", "Go", "The Go spec, Effective Go, the FAQ, and the standard library (go.dev)", fetch_go, (".go",)),
    DocSet("rust", "Rust", "The Rust Book and the Rust Reference (doc.rust-lang.org)", fetch_rust, (".rs",)),
    DocSet("react", "React", "react.dev's Learn and Reference sections", fetch_react, (".jsx", ".tsx")),
]}


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------

class Library:
    """The downloaded doc sets and their search index, under `root`."""

    def __init__(self, root: str):
        self.root = root
        self.db_path = os.path.join(root, "index.sqlite")
        self.meta_path = os.path.join(root, "installed.json")

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(self.root, exist_ok=True)
        db = sqlite3.connect(self.db_path)
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5("
                   "doc_set UNINDEXED, title, heading, url UNINDEXED, body, tokenize='porter unicode61')")
        return db

    def installed(self) -> dict[str, dict]:
        try:
            with open(self.meta_path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_installed(self, data: dict) -> None:
        os.makedirs(self.root, exist_ok=True)
        tmp = self.meta_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.meta_path)

    def install(self, set_id: str, progress: Progress = lambda _m: None,
                cancelled: Callable[[], bool] = lambda: False) -> dict:
        """Download and index a doc set (replacing any earlier copy). The
        old passages stay searchable until the new ones are ready."""
        doc_set = SETS[set_id]
        work = tempfile.mkdtemp(prefix=f"fm-pcc-docs-{set_id}-")
        try:
            rows = []
            pages = 0
            for page in doc_set.fetch(work, progress):
                if cancelled():
                    raise InterruptedError("cancelled")
                pages += 1
                for heading, body in passages(page):
                    rows.append((set_id, page.title, heading, page.url, body))
                if pages % 200 == 0:
                    progress(f"processed {pages} pages…")
            if not rows:
                raise RuntimeError(f"no {doc_set.name} docs were found in the download")
            progress(f"indexing {len(rows)} passages from {pages} pages…")
            db = self._connect()
            with db:
                db.execute("DELETE FROM passages WHERE doc_set = ?", (set_id,))
                db.executemany("INSERT INTO passages VALUES (?, ?, ?, ?, ?)", rows)
            db.close()
            info = {"name": doc_set.name, "pages": pages, "passages": len(rows),
                    "chars": sum(len(r[4]) for r in rows), "installed": time.strftime("%Y-%m-%d")}
            data = self.installed()
            data[set_id] = info
            self._save_installed(data)
            return info
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def remove(self, set_id: str) -> None:
        db = self._connect()
        with db:
            db.execute("DELETE FROM passages WHERE doc_set = ?", (set_id,))
        db.execute("VACUUM")
        db.close()
        data = self.installed()
        data.pop(set_id, None)
        self._save_installed(data)

    def search(self, query: str, sets: Iterable[str] | None = None, limit: int = 12) -> list[dict]:
        """The passages that best match `query` (BM25, titles and headings
        weighted above body text), from `sets` or every installed set."""
        if not os.path.exists(self.db_path):
            return []
        terms = query_terms(query)
        if not terms:
            return []
        match = " OR ".join(terms)
        sql = ("SELECT doc_set, title, heading, url, body, bm25(passages, 0.0, 6.0, 3.0, 0.0, 1.0) AS rank "
               "FROM passages WHERE passages MATCH ?")
        params: list = [match]
        sets = list(sets) if sets else None
        if sets:
            sql += f" AND doc_set IN ({','.join('?' * len(sets))})"
            params += sets
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        db = self._connect()
        try:
            rows = db.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            db.close()
        return [{"set": r[0], "title": r[1], "heading": r[2], "url": r[3], "body": r[4], "rank": r[5]} for r in rows]


_STOP = {"the", "and", "for", "with", "that", "this", "what", "how", "does", "do", "is", "are", "can", "use",
         "using", "which", "when", "why", "where", "should", "would", "could", "into", "from", "about", "there",
         "their", "your", "you", "a", "an", "of", "in", "on", "to", "it", "be", "or", "i", "my", "me", "we"}


def query_terms(query: str) -> list[str]:
    """FTS5 terms for a natural-language question: identifiers kept whole
    (quoted, so `grid-template-columns` or `str.removeprefix` stay one
    thing), other words minus stopwords, each quoted to neutralize FTS
    syntax."""
    terms = []
    for tok in re.findall(r"[A-Za-z_$][\w$]*(?:[.\-][A-Za-z_$][\w$]*)*", query):
        low = tok.lower()
        if low in _STOP or len(low) < 2:
            continue
        if re.search(r"[.\-]", tok):
            terms.append('"' + " ".join(re.split(r"[.\-]", tok)) + '"')   # a phrase
            terms += [f'"{p}"' for p in re.split(r"[.\-]", tok) if len(p) > 2]
        else:
            terms.append(f'"{tok}"')
    return list(dict.fromkeys(terms))


def sets_for_files(paths: Iterable[str]) -> list[str]:
    """Doc sets that match a project's files by extension."""
    exts = {os.path.splitext(p)[1].lower() for p in paths}
    return [s.id for s in SETS.values() if exts & set(s.extensions)]


# How a question names a language. "go" and "react" are ordinary English
# words ("how do I go about…", "buttons that react"), so those need the
# capitalized name or something unmistakable.
_LANG_WORDS = {
    "swift": (r"\bswift(?:ui)?\b", re.I),
    "python": (r"\bpython\b|\bpip\b|\bpytest\b", re.I),
    "html": (r"\bhtml\b", re.I),
    "css": (r"\bcss\b", re.I),
    "javascript": (r"\bjavascript\b|\bjs\b|\bnode(?:\.js)?\b|\btypescript\b|\bnpm\b", re.I),
    "go": (r"\bGo\b|\bgolang\b|\bgoroutines?\b|\bgo (?:test|build|run|mod)\b", 0),
    "rust": (r"\brust\b|\bcargo\b|\bborrow checker\b", re.I),
    "react": (r"\bReact\b|\bJSX\b|\buse(?:State|Effect|Memo|Ref|Context|Reducer|Callback)\b", 0),
}


def sets_named_in(text: str) -> list[str]:
    """Doc sets a question names ("in CSS…", "React's useEffect")."""
    return [sid for sid, (pattern, flags) in _LANG_WORDS.items() if re.search(pattern, text, flags)]


def format_passage(p: dict) -> tuple[str, str]:
    """(source label, text) for a passage, as map-reduce documents."""
    return f"{p['heading']} <{p['url']}>", p["body"]
