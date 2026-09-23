"""Deterministic planning and checking for /task.

A small on-device model is good at *language* -- rewriting a file to add a
line, writing a commit message -- and bad at *bookkeeping*: telling a
rename's source from its destination, noticing a one-step task is already
finished, or keeping commentary out of a filename. Measured on the real
model, the old free-text "pick the next step, or say DONE" loop failed
every case in tests/slow/test_task_reliability.py for exactly those
reasons.

So /task now plans up front, and does the bookkeeping in code:

- parse_task() splits a request into clauses and recognizes the regular
  English shapes of rename/move/commit/push/pull/branch/create requests
  directly, resolving every path against the real filesystem. A clause
  that names exactly one existing file and isn't one of those becomes an
  EDIT of that file, with the clause itself as the instructions -- the
  model then does the actual rewriting.
- Anything it can't recognize is handed to a model planner, whose steps go
  through normalize_model_steps() -- the same filesystem grounding, plus
  dropping steps the request never asked for (the model's most common
  failure: tacking an unrequested commit or a duplicate create onto an
  otherwise correct plan).
- check_edit() / check_new_file() verify a model-written result against
  expectations derived from the request itself ("remove X" -> X is gone,
  "add a line saying Y" -> Y is present and nothing was dropped), so a bad
  rewrite gets retried instead of written.

Nothing here calls a model or touches the filesystem beyond reading the
file/folder lists it's given, so it's all unit-testable offline.
"""
from __future__ import annotations

import difflib
import os
import re

# ---------------------------------------------------------------------------
# Quoted strings
# ---------------------------------------------------------------------------

_QUOTE_PATTERNS = [
    re.compile(r'"([^"\n]+)"'),
    re.compile(r"“([^”\n]+)”"),
    re.compile(r"‘([^’\n]+)’"),
    re.compile(r"`([^`\n]+)`"),
    # Single quotes only at word boundaries, so apostrophes ("don't") aren't
    # mistaken for quotes.
    re.compile(r"(?<![\w])'([^'\n]+)'(?![\w])"),
]
_PLACEHOLDER_RE = re.compile(r"⟦(\d+)⟧")


def protect_quotes(text: str) -> tuple[str, list[str]]:
    """Replace quoted strings with ⟦n⟧ placeholders so their contents (a
    commit message saying "and push", a filename with spaces) can't be
    mistaken for request structure. Returns the text and the quotes.
    """
    quotes: list[str] = []

    def stash(match: re.Match) -> str:
        quotes.append(match.group(1))
        return f"⟦{len(quotes) - 1}⟧"

    for pattern in _QUOTE_PATTERNS:
        text = pattern.sub(stash, text)
    return text, quotes


def restore_quotes(text: str, quotes: list[str], keep_marks: bool = False) -> str:
    def unstash(match: re.Match) -> str:
        value = quotes[int(match.group(1))]
        return f"'{value}'" if keep_marks else value

    return _PLACEHOLDER_RE.sub(unstash, text)


# ---------------------------------------------------------------------------
# Clause splitting
# ---------------------------------------------------------------------------

_CLAUSE_VERBS = (
    r"(?:rename|move|put|place|relocate|commit|stage|push|pull|create|make|add|"
    r"append|prepend|insert|edit|change|replace|remove|delete|update|fix|write|"
    r"set|switch|checkout|check\s+out|sort|git)\b"
)
_SPLIT_RE = re.compile(
    rf"\s*(?:,\s*(?:and\s+)?(?:then\s+)?|;\s*|\.\s+|\s+and\s+then\s+|\s+then\s+|\s+and\s+)"
    rf"(?={_CLAUSE_VERBS})",
    re.IGNORECASE,
)


def split_clauses(text: str) -> list[str]:
    """Split on "and"/"then"/","/";" only where the next word starts a new
    request ("rename a and commit"), not a list ("move a.txt and b.txt").
    """
    text = text.strip().rstrip(".!")
    return [c.strip() for c in _SPLIT_RE.split(text) if c.strip()]


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_NAME = r"(?:⟦\d+⟧|[\w.\-/~]+)"


def _obj(group: str) -> str:
    """A file/folder reference: "notes.txt", "the draft folder", "the
    file called notes.txt", a quoted name."""
    return (
        rf"(?:the\s+)?(?:(?:file|folder|directory|dir)\s+(?:called\s+|named\s+)?)?"
        rf"(?P<{group}>{_NAME})(?:\s+(?:file|folder|directory|dir))?"
    )


def _unquote_name(name: str, quotes: list[str]) -> str:
    return restore_quotes(name, quotes).strip().strip("/")


def resolve_existing(name: str, paths: list[str]) -> str | None:
    """Match `name` to one of `paths`: exact, then case-insensitive, then a
    unique basename ("guide.md" -> "docs/guide.md"), then a unique stem
    ("readme" -> "README.md"). None if nothing (or nothing unique) matches.
    """
    name = name.strip()
    if name.startswith("./"):
        name = name[2:]
    name = name.strip("/")
    if not name:
        return None
    if name in paths:
        return name
    lowered = name.lower()
    for candidate in (
        [p for p in paths if p.lower() == lowered],
        [p for p in paths if os.path.basename(p).lower() == lowered],
        # Stems only when distinctive: "a" must not resolve to a.txt.
        [p for p in paths if len(lowered) >= 3 and os.path.splitext(os.path.basename(p))[0].lower() == lowered],
    ):
        if len(candidate) == 1:
            return candidate[0]
    return None


def mentioned_files(text: str, files: list[str]) -> list[str]:
    """Existing files named anywhere in `text` (by path, basename, or stem
    for names with an extension-less mention like "the readme")."""
    found: list[str] = []
    for token in re.findall(r"[\w.\-/]+", text):
        token = token.strip(".")
        if not token:
            continue
        hit = None
        if "." in token or "/" in token:
            hit = resolve_existing(token, files)
        else:
            stem_hits = [
                f for f in files
                if os.path.splitext(os.path.basename(f))[0].lower() == token.lower()
                and os.path.splitext(f)[1]
            ]
            # Only stems that are distinctive (not "a", "notes" in a
            # sentence about notes...) -- require the file's stem to be at
            # least 3 characters and unique.
            if len(stem_hits) == 1 and len(token) >= 3:
                hit = stem_hits[0]
        if hit and hit not in found:
            found.append(hit)
    return found


# ---------------------------------------------------------------------------
# Clause patterns
# ---------------------------------------------------------------------------

_I = re.IGNORECASE

_RENAME_RE = re.compile(
    rf"^(?:rename|change\s+the\s+name\s+of)\s+{_obj('src')}\s+(?:to|as|into)\s+{_obj('dst')}$", _I
)
_RENAME_SYMBOL_RE = re.compile(
    r"^(?:rename|change\s+the\s+name\s+of)\s+(?:the\s+)?"
    r"(?:(?P<kind>function|method|class|variable|var|constant|const|field|property|parameter|param|symbol|identifier)\s+)?"
    r"(?P<a>[A-Za-z_$][\w$]*)(?:\(\))?(?:\s+(?:function|method|class|variable|constant))?\s+(?:to|as)\s+"
    r"(?P<b>[A-Za-z_$][\w$]*)(?:\(\))?"
    r"(?P<where>\s+(?:everywhere|in\s+all\s+(?:the\s+)?files|across\s+(?:the\s+)?(?:whole\s+)?(?:project|codebase|repo|code)|"
    r"throughout(?:\s+the\s+(?:project|code|codebase))?))?"
    r"(?:\s+in\s+(?P<f>[\w./-]+\.\w+))?$",
    _I,
)
_MOVE_CODE_RE = re.compile(
    r"^(?:move|extract|split\s+out|pull\s+out)\s+(?P<names>.+?)\s+(?:functions?|methods?|classes?|code)?\s*"
    r"(?:from|out\s+of)\s+(?P<src>[\w./-]+\.\w+)\s+(?:in)?to\s+(?:a\s+)?(?:new\s+)?(?:file\s+|module\s+)?"
    r"(?:called\s+|named\s+)?(?P<dest>[\w./-]+\.\w+)(?:\s+(?:and\s+)?import\s+(?:them|it)(?:\s+(?:in|into|back\s+into)\s+\S+)?)?$",
    _I,
)
_MOVE_RE = re.compile(
    r"^(?:move|put|place|relocate|drag)\s+(?P<list>.+?)\s+"
    r"(?:into|in|to|inside|under|over\s+to)\s+(?P<dest>.+)$",
    _I,
)
_BE_NAMED_RE = re.compile(
    rf"^(?:i\s+(?:want|need|would\s+like)\s+|let'?s\s+|please\s+)?{_obj('src')}\s+"
    rf"(?:(?:should|needs\s+to|must|has\s+to|to|will|shall)\s+be\s+(?:called|named|renamed(?:\s+to|\s+as)?)"
    rf"|(?:is|gets)\s+renamed\s+to)\s+{_obj('dst')}$",
    _I,
)
_NEEDS_NAME_RE = re.compile(
    rf"^{_obj('src')}\s+(?:needs|should\s+have|could\s+use|deserves)\s+(?:a\s+)?(?:better|new|different)\s+name"
    rf"\s*[:,\-]?\s*(?:call\s+it\s+|name\s+it\s+|like\s+|such\s+as\s+|how\s+about\s+)?{_obj('dst')}$",
    _I,
)
_BELONGS_RE = re.compile(
    r"^(?P<list>.+?)\s+(?:belongs?|should\s+(?:be|go|live|sit)|needs?\s+to\s+(?:be|go|live)|must\s+(?:be|go)|"
    r"goes|go|lives?)\s+(?:in|into|inside|under)\s+(?P<dest>.+)$",
    _I,
)
_OUT_OF_RE = re.compile(
    rf"^(?:get|take|pull|bring|move)\s+(?P<list>.+?)\s+out\s+of\s+{_obj('folder')}"
    rf"(?:\s+(?:and\s+)?(?:into|to|in)\s+(?P<dest>.+))?$",
    _I,
)
_TOP_LEVEL_RE = re.compile(
    r"^(?:the\s+)?(?:top[\s-]?level|root|project\s+root|repo\s+root|current\s+(?:folder|directory)|"
    r"main\s+(?:folder|directory))(?:\s+(?:folder|directory))?"
    r"(?:\s+of\s+(?:the\s+)?(?:project|repo|repository|directory|folder))?$",
    _I,
)
_NEW_FOLDER_DEST_RE = re.compile(
    rf"^(?:a\s+)?new\s+(?:folder|directory|dir)\s+(?:called\s+|named\s+)?(?P<n>{_NAME})$", _I
)
_COMMIT_RE = re.compile(
    r"^(?:(?:git\s+)?(?:stage|add)(?:\s+(?:all|everything|the\s+changes|my\s+changes|all\s+(?:the\s+)?changes|"
    r"them|it|all\s+files))?\s+and\s+)?(?:git\s+)?(?:commit|check\s+in)\b(?P<rest>.*)$",
    _I,
)
_COMMIT_MESSAGE_RE = re.compile(
    r"(?:message|msg|note|saying|titled|called|as)\s*:?\s*(?P<m>.+)$", _I
)
_SAVE_TO_GIT_RE = re.compile(
    r"^(?:save|record|store|keep|snapshot|put|add)\b.*?\b(?:to|in|into|with)\s+git\b(?P<rest>.*)$", _I
)
_STAGE_RE = re.compile(
    r"^(?:git\s+add|stage|add)(?:\s+(?:all|everything|the\s+changes|all\s+(?:the\s+)?changes|my\s+changes|"
    r"all\s+(?:the\s+)?files|them|it))?(?:\s+(?:to\s+git|for\s+(?:a\s+)?commit))?$",
    _I,
)
_PUSH_RE = re.compile(r"^(?:git\s+)?push\b(?P<rest>.*)$", _I)
_PULL_RE = re.compile(r"^(?:git\s+)?pull\b(?P<rest>.*)$", _I)
_BRANCH_CREATE_RE = re.compile(
    rf"^(?:create|make|start|add|open)\s+(?:a\s+)?(?:new\s+)?(?:git\s+)?branch\s+"
    rf"(?:called\s+|named\s+)?(?P<b>{_NAME})$",
    _I,
)
_BRANCH_SWITCH_RE = re.compile(
    rf"^(?:(?:switch|change|go|move)\s+(?:over\s+)?to|checkout|check\s+out|git\s+checkout|git\s+switch)\s+"
    rf"(?:the\s+)?(?:(?:git\s+)?branch\s+)?(?P<b>{_NAME})(?:\s+branch)?$",
    _I,
)
_CREATE_FOLDER_RE = re.compile(
    rf"^(?:create|make|add)\s+(?:an?\s+)?(?:new\s+)?(?:empty\s+)?(?:folder|directory|dir)\s+"
    rf"(?:called\s+|named\s+)?(?P<n>{_NAME})(?P<rest>.*)$",
    _I,
)
_FOLDER_WITH_FILE_RE = re.compile(
    rf"^\s*(?:with|containing|that\s+contains|and\s+put)\s+(?:the\s+|a\s+|an\s+)?(?:new\s+)?(?:empty\s+)?"
    rf"(?:file\s+)?(?:called\s+|named\s+)?(?P<f>{_NAME})(?:\s+(?:file))?(?:\s+(?:inside|in)(?:\s+(?:it|there))?)?$",
    _I,
)
_FILE_KIND = (
    r"(?:(?:python|py|javascript|js|typescript|ts|node|swift|go|rust|ruby|bash|shell|html|css|json|yaml|"
    r"markdown|text|config|test|unit\s+test|react|web)\s+)?"
    r"(?:file|script|module|program|class|component|page|stylesheet|config(?:uration)?(?:\s+file)?|"
    r"tests?(?:\s+file)?|app|tool|utility|cli)"
)
_CREATE_FILE_RE = re.compile(
    rf"^(?:create|make|add|write|generate|build|set\s+up)\s+(?:an?\s+)?(?:new\s+)?(?:empty\s+|simple\s+|small\s+)?"
    rf"{_FILE_KIND}\s+(?:called\s+|named\s+)?(?P<n>⟦\d+⟧|[\w\-/~]+\.\w+)(?P<rest>.*)$",
    _I,
)
_CREATE_NAMED_FILE_RE = re.compile(
    rf"^(?:create|make|write)\s+(?:an?\s+)?(?:new\s+)?(?:empty\s+)?(?P<n>[\w\-/]+\.\w+|⟦\d+⟧)(?P<rest>.*)$",
    _I,
)
_FILE_LOCATION_RE = re.compile(
    rf"^\s*(?:in|inside|under)\s+(?:the\s+)?(?:(?:folder|directory)\s+)?(?P<d>{_NAME})"
    rf"(?:\s+(?:folder|directory))?(?P<rest>.*)$",
    _I,
)
# "range_sum in util.py is off by one", "isEven in utils.js returns the
# wrong answer", "the test in test_x.py fails": a bug report about a named
# file is a request to fix it. (Naming/location declaratives -- "x.py
# should be called y.py" -- are handled before this and never match.)
_BUG_CUE_RE = re.compile(
    r"\b(?:bug|buggy|broken|wrong|incorrect|off[\s-]by[\s-]one|crash(?:es|ing)?|fails?|failing|errors?|exception|"
    r"doesn'?t\s+work|isn'?t\s+working|not\s+working|should(?:n'?t)?|raises?|typo|mistake)\b",
    _I,
)
_POLITE_LEAD_RE = re.compile(
    r"^(?:(?:please|kindly|now|also|just|then|ok(?:ay)?|hey|so)\s*,?\s+|"
    r"(?:can|could|would|will)\s+you\s+(?:please\s+)?|go\s+ahead\s+and\s+|"
    r"i(?:'d|\s+would)\s+like\s+you\s+to\s+|i\s+(?:want|need)\s+you\s+to\s+)+",
    _I,
)
_POLITE_TAIL_RE = re.compile(r"(?:\s*,?\s+(?:please|thanks|thank\s+you|for\s+me))+$", _I)
_EDIT_LEAD_RE = re.compile(
    r"^(?:(?:please|now|also|then|and|just|can\s+you|could\s+you|would\s+you|go\s+ahead\s+and)\s+)*"
    r"(?:(?:in|inside|within|for|on)\s+(?:the\s+)?[\w.\-/⟦⟧]+(?:\s+file)?\s*,?\s+)?"
    r"(?:add|append|prepend|insert|change|replace|remove|delete|drop|edit|update|fix|set|rewrite|modify|"
    r"make|turn|sort|bump|increase|decrease|increment|decrement|convert|translate|capitali[sz]e|uppercase|"
    r"lowercase|comment|uncomment|document|refactor|clean|improve|correct|write|reword|rephrase|shorten|"
    r"expand|format|swap|reverse|indent|dedent|wrap|include|put|enable|disable|switch\s+(?:on|off)|"
    r"rename\s+(?:the\s+)?(?:variable|function|method|class|key|field|heading|title|constant|parameter|argument)"
    r"|use|mark|check\s+off|strike|number|alphabetize|trim|strip|fill\s+in|complete|finish|implement)\b",
    _I,
)
_DELETE_RE = re.compile(rf"^(?:delete|remove|trash|rm)\s+{_obj('x')}$", _I)
_PRONOUNS = {"it", "them", "that", "there", "this"}


class TaskParseState:
    """The file/folder/branch picture as the plan will leave it, so later
    clauses resolve against earlier ones ("rename a to b and commit it",
    "create a folder x and move y into it")."""

    def __init__(self, files: list[str], folders: list[str]):
        self.files = list(files)
        self.folders = list(folders)
        self.last_folder: str | None = None
        self.last_branch: str | None = None
        self.last_paths: list[str] = []

    def move(self, src: str, dest: str) -> None:
        if src in self.folders:
            prefix = src + "/"
            self.folders = [dest + f[len(src):] if f == src or f.startswith(prefix) else f for f in self.folders]
            self.files = [dest + f[len(src):] if f.startswith(prefix) else f for f in self.files]
            self.last_folder = dest
        elif src in self.files:
            self.files = [dest if f == src else f for f in self.files]
        self._add_parents(dest)

    def add_folder(self, path: str) -> None:
        if path not in self.folders:
            self.folders.append(path)
        self._add_parents(path)
        self.last_folder = path

    def add_file(self, path: str) -> None:
        if path not in self.files:
            self.files.append(path)
        self._add_parents(path)

    def _add_parents(self, path: str) -> None:
        parent = os.path.dirname(path)
        while parent and parent not in self.folders:
            self.folders.append(parent)
            parent = os.path.dirname(parent)


def step(action: str, path: str = "", destination: str = "", details: str = "", **extra) -> dict:
    return {"action": action, "path": path, "destination": destination, "details": details, **extra}


def _parse_move_like(
    verb: str, sources: list[str], dest_phrase: str, quotes: list[str], state: TaskParseState
) -> list[dict] | None:
    dest_phrase = dest_phrase.strip()
    steps: list[dict] = []
    dest_folder: str | None = None
    full_dest: str | None = None

    if _TOP_LEVEL_RE.match(dest_phrase):
        dest_folder = ""
    elif (new_folder := _NEW_FOLDER_DEST_RE.match(dest_phrase)):
        dest_folder = _unquote_name(new_folder.group("n"), quotes)
        if dest_folder not in state.folders:
            steps.append(step("CREATE_FOLDER", dest_folder))
            state.add_folder(dest_folder)
    else:
        match = re.fullmatch(_obj("d"), dest_phrase, _I)
        if not match:
            return None
        raw = _unquote_name(match.group("d"), quotes)
        if raw.lower() in _PRONOUNS:
            if not state.last_folder:
                return None
            dest_folder = state.last_folder
        elif (folder := resolve_existing(raw, state.folders)) is not None:
            dest_folder = folder
        elif resolve_existing(raw, state.files) is not None:
            return None  # can't move something into a file
        elif len(sources) == 1 and os.path.splitext(raw)[1] and sources[0] in state.files:
            full_dest = raw  # "move a.txt to docs/b.txt": a full new path
        else:
            dest_folder = raw  # a folder that doesn't exist yet
            steps.append(step("CREATE_FOLDER", dest_folder))
            state.add_folder(dest_folder)

    for src in sources:
        dest = full_dest if full_dest is not None else os.path.join(dest_folder or "", os.path.basename(src))
        if dest == src:
            continue
        steps.append(step("MOVE" if verb != "rename" else "RENAME", src, dest))
        state.move(src, dest)
    return steps


def _resolve_list(text: str, quotes: list[str], state: TaskParseState, within: str | None = None) -> list[str] | None:
    """Existing paths for "a.txt, b.txt and c.txt" / "the report folder";
    None unless every item resolves. `within` prefers paths inside that
    folder ("data.csv" out of "tmp" -> "tmp/data.csv")."""
    everything = state.files + state.folders
    sources = []
    for item in re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", text.strip(), flags=_I):
        om = re.fullmatch(_obj("x"), item.strip(), _I)
        if not om:
            return None
        raw = _unquote_name(om.group("x"), quotes)
        if raw.lower() in _PRONOUNS and state.last_paths:
            sources.extend(state.last_paths)
            continue
        src = None
        if within:
            src = resolve_existing(os.path.join(within, raw), everything)
        src = src or resolve_existing(raw, everything)
        if src is None:
            return None
        sources.append(src)
    return sources


def parse_clause(clause: str, quotes: list[str], state: TaskParseState, previous: list[dict]) -> list[dict] | None:
    """Steps for one clause, or None if it isn't a shape this recognizes."""
    clause = _POLITE_TAIL_RE.sub("", _POLITE_LEAD_RE.sub("", clause.strip().rstrip(".!?"))).strip()
    everything = state.files + state.folders

    if (m := _RENAME_SYMBOL_RE.match(clause)):
        old_name, new_name = m.group("a"), m.group("b")
        scope = m.group("f")
        in_file = resolve_existing(scope, state.files) if scope else None
        is_file = resolve_existing(old_name, everything) is not None or "." in old_name
        if not is_file and (m.group("kind") or m.group("where") or in_file or re.search(r"[_A-Z]", old_name)):
            return [step("RENAME_SYMBOL", in_file or "", old_name, details=new_name)]

    if (m := _MOVE_CODE_RE.match(clause)):
        src = resolve_existing(_unquote_name(m.group("src"), quotes), state.files)
        names = [n for n in re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", m.group("names").strip()) if n]
        names = [re.sub(r"^(?:the\s+)?(?:functions?\s+|methods?\s+|classes?\s+)?|\s*\(\)$", "", n) for n in names]
        if src and names and all(re.fullmatch(r"[A-Za-z_]\w*", n) for n in names):
            dest = _unquote_name(m.group("dest"), quotes)
            state.add_file(dest)
            return [step("MOVE_CODE", src, dest, details=", ".join(names))]

    if (m := _RENAME_RE.match(clause)):
        raw_src = _unquote_name(m.group("src"), quotes)
        src = (state.last_paths[-1] if raw_src.lower() in _PRONOUNS and state.last_paths
               else resolve_existing(raw_src, everything))
        if src is None:
            return None
        dst = _unquote_name(m.group("dst"), quotes)
        if "/" not in dst:
            dst = os.path.join(os.path.dirname(src), dst)
        if dst == src:
            return []
        state.move(src, dst)
        state.last_paths = [dst]
        return [step("RENAME", src, dst)]

    if (m := _BE_NAMED_RE.match(clause)) or (m := _NEEDS_NAME_RE.match(clause)):
        raw_src = _unquote_name(m.group("src"), quotes)
        src = resolve_existing(raw_src, everything)
        if src is not None:
            dst = _unquote_name(m.group("dst"), quotes)
            if "/" not in dst:
                dst = os.path.join(os.path.dirname(src), dst)
            if dst == src:
                return []
            state.move(src, dst)
            state.last_paths = [dst]
            return [step("RENAME", src, dst)]

    if (m := _OUT_OF_RE.match(clause)):
        folder = resolve_existing(_unquote_name(m.group("folder"), quotes), state.folders)
        if folder is not None:
            sources = _resolve_list(m.group("list"), quotes, state, within=folder)
            if sources is not None:
                dest = m.group("dest") or "the top level"
                if not m.group("dest") and os.path.dirname(folder):
                    dest = os.path.dirname(folder)
                result = _parse_move_like("move", sources, dest, quotes, state)
                if result is not None:
                    state.last_paths = [s["destination"] for s in result if s["action"] in ("MOVE", "RENAME")]
                    return result

    if (m := _BELONGS_RE.match(clause)):
        sources = _resolve_list(m.group("list"), quotes, state)
        if sources is not None:
            result = _parse_move_like("move", sources, m.group("dest"), quotes, state)
            if result is not None:
                state.last_paths = [s["destination"] for s in result if s["action"] in ("MOVE", "RENAME")]
                return result

    if (m := _MOVE_RE.match(clause)):
        items = re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", m.group("list").strip(), flags=_I)
        sources = []
        for item in items:
            om = re.fullmatch(_obj("x"), item.strip(), _I)
            if not om:
                return None
            raw = _unquote_name(om.group("x"), quotes)
            if raw.lower() in _PRONOUNS and state.last_paths:
                sources.extend(state.last_paths)
                continue
            src = resolve_existing(raw, everything)
            if src is None:
                return None
            sources.append(src)
        result = _parse_move_like("move", sources, m.group("dest"), quotes, state)
        if result is not None:
            state.last_paths = [s["destination"] for s in result if s["action"] in ("MOVE", "RENAME")]
        return result

    if (m := _COMMIT_RE.match(clause)):
        rest = m.group("rest")
        message = None
        placeholder = _PLACEHOLDER_RE.search(rest)
        if placeholder:
            message = quotes[int(placeholder.group(1))]
        elif (mm := _COMMIT_MESSAGE_RE.search(rest)):
            message = restore_quotes(mm.group("m"), quotes).strip().strip(".")
        return [step("COMMIT", details=message or "")]

    if (m := _SAVE_TO_GIT_RE.match(clause)):
        rest = m.group("rest")
        placeholder = _PLACEHOLDER_RE.search(rest)
        message = quotes[int(placeholder.group(1))] if placeholder else None
        if not message and (mm := _COMMIT_MESSAGE_RE.search(rest)):
            message = restore_quotes(mm.group("m"), quotes).strip().strip(".")
        return [step("COMMIT", details=message or "")]

    if _STAGE_RE.match(clause):
        return [step("STAGE")]

    if (m := _BRANCH_CREATE_RE.match(clause)):
        name = _unquote_name(m.group("b"), quotes)
        state.last_branch = name
        return [step("BRANCH_CREATE", name)]

    if (m := _BRANCH_SWITCH_RE.match(clause)):
        name = _unquote_name(m.group("b"), quotes)
        if name.lower() in _PRONOUNS:
            if not state.last_branch:
                return None
            name = state.last_branch
            # "create branch x and switch to it": creating already switches.
            if previous and previous[-1]["action"] == "BRANCH_CREATE" and previous[-1]["path"] == name:
                return []
        elif resolve_existing(name, everything) is not None:
            return None
        state.last_branch = name
        return [step("BRANCH_SWITCH", name)]

    if (m := _PUSH_RE.match(clause)) and not mentioned_files(m.group("rest"), state.files):
        return [step("PUSH")]

    if (m := _PULL_RE.match(clause)) and not mentioned_files(m.group("rest"), state.files):
        return [step("PULL")]

    if (m := _CREATE_FOLDER_RE.match(clause)):
        folder = _unquote_name(m.group("n"), quotes)
        rest = m.group("rest")
        location = _FILE_LOCATION_RE.match(rest)
        if location:
            parent = _unquote_name(location.group("d"), quotes)
            parent = resolve_existing(parent, state.folders) or parent
            folder = os.path.join(parent, folder)
            rest = location.group("rest")
        steps = []
        if folder not in state.folders:
            steps.append(step("CREATE_FOLDER", folder))
            state.add_folder(folder)
        if rest.strip():
            with_file = _FOLDER_WITH_FILE_RE.match(rest)
            if not with_file:
                return None
            inner = os.path.join(folder, _unquote_name(with_file.group("f"), quotes))
            steps.append(step("CREATE_FILE", inner))
            state.add_file(inner)
        state.last_paths = [folder]
        return steps

    if (m := _CREATE_FILE_RE.match(clause)) or (m := _CREATE_NAMED_FILE_RE.match(clause)):
        path = _unquote_name(m.group("n"), quotes)
        rest = m.group("rest")
        existing = resolve_existing(path, state.files) if "." in path else None
        if existing and not re.match(r"^(?:create|add|generate|set\s+up)\b", clause, _I):
            # "make app.py also print the timeout": app.py exists, so this
            # changes it -- it isn't a new file.
            state.last_paths = [existing]
            return [step("EDIT", existing, details=restore_quotes(clause, quotes, keep_marks=True))]
        # "create a.py with ..., and test_a.py with ...": two files.
        more = re.search(r",?\s+and\s+(?:an?\s+)?(?:new\s+)?(?:file\s+)?(?=[\w\-/]+\.\w+\b)", rest)
        extra: list[dict] = []
        if more:
            tail = "create " + rest[more.end():]
            rest = rest[: more.start()]
            extra = parse_clause(tail, quotes, state, previous) or []
        location = _FILE_LOCATION_RE.match(rest)
        steps = []
        if location:
            raw_parent = _unquote_name(location.group("d"), quotes)
            if raw_parent.lower() in _PRONOUNS and state.last_folder:
                parent = state.last_folder
            else:
                parent = resolve_existing(raw_parent, state.folders) or raw_parent
            path = os.path.join(parent, path)
            rest = location.group("rest")
        details = restore_quotes(rest, quotes, keep_marks=True).strip(" ,")
        details = re.sub(r"^(?:that\s+(?:is|should\s+be)\s+)", "", details, flags=_I)
        if re.fullmatch(r"(?:that\s+is\s+)?empty|with\s+nothing\s+in\s+it|", details, _I):
            details = ""
        steps.insert(0, step("CREATE_FILE", path, details=details))
        state.add_file(path)
        state.last_paths = [path]
        return steps + extra

    if (m := _DELETE_RE.match(clause)):
        raw = _unquote_name(m.group("x"), quotes)
        target = resolve_existing(raw, everything)
        if target is not None:
            return [step("UNSUPPORTED", target, details=f"deleting files or folders ({target})")]

    # "write tests for Stack in stack.py in a new file test_stack.py": the
    # new file is what gets written; stack.py is just what it's about.
    if (nf := re.search(
        rf"\b(?:in|into|as|to)\s+(?:a\s+)?new\s+(?:file|module|script)\s+(?:called\s+|named\s+)?(?P<n>{_NAME})",
        clause, _I,
    )):
        path = _unquote_name(nf.group("n"), quotes)
        if resolve_existing(path, state.files) is None:
            state.add_file(path)
            state.last_paths = [path]
            return [step("CREATE_FILE", path, details=restore_quotes(clause, quotes, keep_marks=True))]

    # An instruction to change contents ("add ...", "in x.txt, replace
    # ...") naming exactly one existing file is an edit of that file,
    # described by the clause itself. It has to *read* as an edit
    # instruction: merely naming a file isn't enough -- measured, that
    # turned "config.toml should be called settings.toml" into a rewrite
    # of config.toml's contents instead of a rename. Anything else goes to
    # the model planner, whose EDITs are likewise dropped unless the
    # request asks for one.
    named = mentioned_files(clause, state.files)
    if not _EDIT_LEAD_RE.match(clause) and not (len(named) == 1 and _BUG_CUE_RE.search(clause)):
        return None
    if len(named) == 1:
        state.last_paths = named
        return [step("EDIT", named[0], details=restore_quotes(clause, quotes, keep_marks=True))]

    # "rename hello.py to greet.py and change it to ...": "it" is whatever
    # the previous clause produced, if that was a single file.
    if not named and re.search(r"\b(?:it|its)\b", clause, _I) and len(state.last_paths) == 1 \
            and state.last_paths[0] in state.files:
        return [step("EDIT", state.last_paths[0], details=restore_quotes(clause, quotes, keep_marks=True))]

    # "change the color to blue in config.txt, and set the size to 20":
    # a follow-on clause naming no file continues the previous edit.
    if not named and previous and previous[-1]["action"] == "EDIT" and not re.match(
        r"^(?:commit|push|pull|stage|rename|move|create|make)\b", clause, _I
    ):
        previous[-1]["details"] += " and " + restore_quotes(clause, quotes, keep_marks=True)
        return []

    return None


def parse_task(task: str, files: list[str], folders: list[str]) -> list[dict]:
    """Deterministic plan for `task`. Returns a list of steps; any clause
    this couldn't recognize becomes {"action": "UNPARSED", "details":
    <clause>} in its position, for the caller to hand to a model planner.
    """
    text, quotes = protect_quotes(task)
    state = TaskParseState(files, folders)
    steps: list[dict] = []
    for clause in split_clauses(text):
        parsed = parse_clause(clause, quotes, state, steps)
        if parsed:
            # "isEven in utils.js is wrong. fix it": two clauses, one edit.
            while parsed and parsed[0]["action"] == "EDIT" and steps and steps[-1]["action"] == "EDIT" \
                    and steps[-1]["path"] == parsed[0]["path"]:
                steps[-1]["details"] += ". " + parsed.pop(0)["details"]
        if parsed is None:
            # Snapshot of the tree as earlier steps will have left it, so a
            # model planning just this clause sees e.g. a renamed file under
            # its new name.
            steps.append(step(
                "UNPARSED", details=restore_quotes(clause, quotes, keep_marks=True),
                files=list(state.files), folders=list(state.folders),
            ))
        else:
            steps.extend(parsed)
    return steps


# ---------------------------------------------------------------------------
# Normalizing a model-written plan
# ---------------------------------------------------------------------------

MODEL_ACTIONS = [
    "EDIT", "CREATE_FILE", "CREATE_FOLDER", "RENAME", "MOVE", "COMMIT",
    "PUSH", "PULL", "BRANCH_CREATE", "BRANCH_SWITCH",
]

_SIGNALS = {
    "COMMIT": r"\b(?:commit|check\s+in|checkpoint|snapshot)|\bgit\b",
    "STAGE": r"\b(?:stage|git\s+add)\b",
    # An EDIT has to be asked for -- seen for real: planning "save my work
    # to git" as an EDIT of the only file, with "save to git" as the change.
    "EDIT": r"\b(?:add|append|prepend|insert|change|replace|remove|delete|edit|update|fix|set|rewrite|"
            r"modify|make|turn|put|write|include|sort|bump|increase|decrease|rename|convert|translate|"
            r"capitali[sz]e|uppercase|lowercase|comment|document|refactor|clean|improve|correct|style|"
            r"theme|restyle|redesign|recolou?r|colou?r)(?:s|es|d|ed|ing)?\b",
    "PUSH": r"\bpush",
    "PULL": r"\bpull",
    "BRANCH_CREATE": r"\bbranch",
    "BRANCH_SWITCH": r"\b(?:branch|switch|checkout|check\s+out)",
    "CREATE_FILE": r"\b(?:create|make|new|write|add\s+(?:a|an)\s+(?:\w+\s+)?file|generate|scaffold|build|website|site|page|project|app|script)",
    # CREATE_FOLDER, RENAME and MOVE aren't gated on wording: they're only
    # planned by a model for requests the parser couldn't read, which are
    # exactly the paraphrases ("all the markdown files belong in docs")
    # no word list anticipates -- and all three are fully undoable, unlike
    # a commit or a content change. Their paths are still grounded below.
}


def _clean_path(value: str) -> str:
    value = (value or "").strip().strip("`'\" ")
    value = value.splitlines()[0].strip() if value.strip() else ""
    if value.startswith("./"):
        value = value[2:]
    value = value.strip("/")
    return "" if value in (".", "..") else value


def normalize_model_steps(steps: list[dict], request: str, files: list[str], folders: list[str]) -> list[dict]:
    """Ground a model-proposed plan in reality and in the request's own
    words. Drops steps of a kind the request never asked for, steps that
    would re-create something that exists, redundant EDITs of something
    being renamed, and resolves every existing path against the real tree.
    Raises ValueError for a step that can't be made valid.
    """
    text, quotes = protect_quotes(request)
    lowered = text.lower()
    state = TaskParseState(files, folders)
    moved_sources: set[str] = set()
    out: list[dict] = []
    seen: set[tuple] = set()

    request_files = mentioned_files(restore_quotes(text, quotes), files)
    request_folders = [f for f in folders if re.search(rf"(?<![\w.\-/]){re.escape(os.path.basename(f))}(?![\w\-])", request, _I)]
    request_existing = list(dict.fromkeys(request_files + request_folders))
    creation_words = re.search(r"\b(?:create|make|new|write|add|generate|scaffold|build|start)\b", lowered)
    new_names = [
        t for t in re.findall(r"[\w\-/]+\.\w+", restore_quotes(text, quotes))
        if resolve_existing(t, files + folders) is None
    ]

    repaired: list[dict] = []
    for raw in steps:
        action = (raw.get("action") or "").upper()
        path = _clean_path(raw.get("path", ""))
        destination = _clean_path(raw.get("destination", ""))
        details = (raw.get("details") or "").strip()
        # R2: "config.toml should be called settings.toml" planned as
        # CREATE_FILE settings.toml -- no creation wording, one existing
        # file named: it's a rename.
        if action == "CREATE_FILE" and not creation_words and len(request_files) == 1 \
                and path and path not in files and path in new_names:
            action, path, destination = "RENAME", request_files[0], path
        # R3: an "edit" whose instructions are really a rename/move.
        if action == "EDIT" and (rm := re.match(
            r"^(?:rename|move)\s+(?:it\s+|this\s+file\s+)?(?:to|into)\s+(?P<d>[\w.\-/]+)$", details, _I
        )):
            action, destination = ("MOVE" if "/" in rm.group("d") else "RENAME"), rm.group("d")
        # R1: a move/rename whose source doesn't exist (or goes nowhere):
        # swapped fields, or the one existing thing the request names.
        if action in ("RENAME", "MOVE"):
            exists_now = resolve_existing(path, files + folders) if path else None
            if exists_now is None and destination and resolve_existing(destination, files + folders) \
                    and path:
                path, destination = destination, path
            elif (exists_now is None or path == destination) and len(request_existing) == 1:
                candidate = request_existing[0]
                if path and path != candidate and path != destination and exists_now is None:
                    destination = path
                elif path == destination and new_names:
                    destination = new_names[0]
                path = candidate
        repaired.append({"action": action, "path": path, "destination": destination, "details": details})
    moved_sources = {s["path"] for s in repaired if s["action"] in ("RENAME", "MOVE")}
    steps = repaired

    for raw in repaired:
        action = raw["action"]
        path, destination, details = raw["path"], raw["destination"], raw["details"]

        signal = _SIGNALS.get(action)
        if signal and not re.search(signal, lowered):
            continue

        if action in ("CREATE_FILE", "CREATE_FOLDER") and not path and destination:
            # Seen for real: CREATE_FOLDER with path "." and the folder's
            # name in destination.
            path, destination = destination, ""

        everything = state.files + state.folders
        if action == "EDIT":
            target = resolve_existing(path, state.files)
            if target is None and path:
                # Seen for real: "styles/liquidglass.css" for liquid_glass.css.
                close = difflib.get_close_matches(path, state.files, n=1, cutoff=0.85)
                target = close[0] if close else None
            if target is None or target in moved_sources:
                continue
            # A request that names files only edits those. One that names
            # none ("make the ui a green and yellow theme") leaves the
            # choice to the planner -- those edits are marked optional: a
            # file that turns out to need no change is skipped, not fatal.
            if request_files and target not in request_files:
                continue
            new = step("EDIT", target, details=details or request, optional=not request_files)
        elif action == "CREATE_FILE":
            if not path or os.path.isabs(path) or path in everything:
                continue
            # A file some move/rename will produce isn't a new file.
            if any(
                s.get("action") in ("RENAME", "MOVE") and _clean_path(s.get("destination", "")) == path
                for s in steps
            ):
                continue
            new = step("CREATE_FILE", path, details=details)
            state.add_file(path)
        elif action == "CREATE_FOLDER":
            if not path or os.path.isabs(path) or path in everything:
                continue
            if os.path.basename(os.path.dirname(path)) == os.path.basename(path):
                continue  # "X/X": a confused duplicate of its own parent
            new = step("CREATE_FOLDER", path)
            state.add_folder(path)
        elif action in ("RENAME", "MOVE"):
            src = resolve_existing(path, everything)
            if src is None and any(
                s["action"] in ("RENAME", "MOVE") and s["path"] == path and s["destination"] == destination
                for s in out
            ):
                continue  # the same move, already planned under the other label
            if src is None:
                # The model sometimes puts the destination in `path`.
                alt = resolve_existing(os.path.basename(path), everything) if path else None
                if alt is None:
                    raise ValueError(f"{action} names something that doesn't exist: {path!r}")
                src, destination = alt, destination or path
            if not destination or len(destination.split()) > 6:
                raise ValueError(f"{action} of {src} has no usable destination: {destination!r}")
            folder = resolve_existing(destination, state.folders)
            if folder is not None and folder != src:
                destination = os.path.join(folder, os.path.basename(src))
            elif action == "RENAME" and "/" not in destination:
                destination = os.path.join(os.path.dirname(src), destination)
            if destination == src:
                continue
            # Make sure the destination's folder exists as a step of its
            # own (so /undo removes it too), whether or not the model
            # planned one correctly.
            parent = os.path.dirname(destination)
            if parent and parent not in state.folders and not os.path.isabs(parent):
                out.append(step("CREATE_FOLDER", parent))
                seen.add(("CREATE_FOLDER", parent, ""))
                state.add_folder(parent)
            new = step(action, src, destination)
            state.move(src, destination)
        elif action == "COMMIT":
            placeholder = _PLACEHOLDER_RE.search(text[text.lower().find("commit"):] if "commit" in lowered else "")
            if placeholder:
                details = quotes[int(placeholder.group(1))]
            else:
                details = clean_commit_message(details)
            new = step("COMMIT", details=details)
        elif action in ("BRANCH_CREATE", "BRANCH_SWITCH"):
            if not path or " " in path:
                continue
            new = step(action, path)
        elif action in ("PUSH", "PULL"):
            new = step(action)
        else:
            continue

        key = (new["action"], new["path"], new["destination"])
        if key in seen:
            continue
        seen.add(key)
        out.append(new)

    # At most one commit, after every file change it's meant to include
    # (but before a push).
    commits = [s for s in out if s["action"] == "COMMIT"]
    if len(commits) > 1 or (commits and any(
        s["action"] in ("EDIT", "CREATE_FILE", "CREATE_FOLDER", "RENAME", "MOVE")
        for s in out[out.index(commits[0]) + 1:]
    )):
        rest = [s for s in out if s["action"] != "COMMIT"]
        tail = [s for s in rest if s["action"] in ("PUSH",)]
        body = [s for s in rest if s["action"] not in ("PUSH",)]
        out = body + [commits[-1]] + tail
    return out


def clean_commit_message(message: str) -> str:
    """First line only, without surrounding quotes or a trailing "DONE"
    (both seen for real from the on-device model)."""
    message = (message or "").strip()
    message = message.splitlines()[0].strip() if message else ""
    message = re.sub(r"\s+DONE$", "", message).strip().strip("`'\"").strip()
    if re.fullmatch(r"(?:done|none|n/a|commit)?", message, _I):
        return ""
    return message


# ---------------------------------------------------------------------------
# Checking model-written file contents
# ---------------------------------------------------------------------------

_ADDITIVE_RE = re.compile(r"^\s*(?:please\s+)?(?:add|append|insert|prepend|include)\b", _I)
_SAYS_RE = re.compile(
    r"\b(?:that\s+says|says|saying|that\s+reads|reads|reading|with\s+the\s+text|"
    r"containing(?:\s+the\s+(?:word|text|line|phrase))?|the\s+(?:word|line|text|phrase))\s+"
    r"(?P<w>⟦\d+⟧|[^\s,⟦]+(?:\s+[^\s,⟦]+)*?)(?=\s+(?:to|in|into|at|on|after|before|below|above|under|from|with)\s|\s*$|\s*,)",
    _I,
)
_REPLACE_RE = re.compile(r"\breplace\s+(?:all\s+)?(?P<a>.+?)\s+with\s+(?P<b>.+?)$", _I)
_CHANGE_RE = re.compile(r"\b(?:change|set|rename|update)\s+(?P<a>.+?)\s+to\s+(?P<b>.+?)$", _I)
_INSTEAD_RE = re.compile(r"(?P<b>⟦\d+⟧|[\w.\-]+)\s+instead\s+of\s+(?P<a>⟦\d+⟧|[\w.\-]+)", _I)
_REMOVE_RE = re.compile(r"\b(?:remove|delete|drop|take\s+out)\s+(?P<x>.+?)(?:\s+(?:from|in|out\s+of)\s+.+)?$", _I)
_TRAILING_LOCATION_RE = re.compile(r"\s+(?:in|on|inside|within|of)\s+(?:the\s+)?[\w.\-/]+(?:\s+file)?$", _I)
_CLEAR_RE = re.compile(r"\b(?:clear|empty|wipe|erase|delete\s+everything|remove\s+everything|rewrite|start\s+over)\b", _I)


def _phrase(value: str, quotes: list[str]) -> str:
    value = _TRAILING_LOCATION_RE.sub("", value.strip())
    value = restore_quotes(value, quotes).strip().strip(".,")
    value = re.sub(r"^(?:the|a|an)\s+", "", value, flags=_I)
    return value


def _literal_in(needle: str, haystack: str) -> bool:
    if not needle:
        return False
    return re.search(rf"(?<![\w]){re.escape(needle)}(?![\w])", haystack, _I) is not None


def edit_expectations(instructions: str, original: str, code: bool = False) -> tuple[list[str], list[str]]:
    """(must_contain, must_not_contain) implied by an edit request, only
    where the request is unambiguous about it. For `code`, only quoted
    text counts as required wording: "return None when b is zero instead
    of crashing" is satisfied by `if b == 0`, not by the word "zero"."""
    text, quotes = protect_quotes(instructions)
    present: list[str] = []
    absent: list[str] = []

    for m in _SAYS_RE.finditer(text):
        present.append(_phrase(m.group("w"), quotes))

    pairs = assignment_pairs(text, quotes)
    if len(pairs) > 1:
        # "set port to 443 and mode to prod": every value must land, and
        # each key's old value line must be gone.
        for key, value in pairs:
            present.append(value)
            for line in original.splitlines():
                km = _key_line_re(key).match(line)
                if km and km.group("val").strip("\"'") and km.group("val").strip("\"'") != value:
                    absent.append(line.strip())
        return list(dict.fromkeys(present)), list(dict.fromkeys(absent))

    for pattern in (_REPLACE_RE, _INSTEAD_RE, _CHANGE_RE):
        m = pattern.search(text)
        if not m:
            continue
        old, new = _phrase(m.group("a"), quotes), _phrase(m.group("b"), quotes)
        # "so it returns 'hello' instead of 'hi'" -- keep just the quoted
        # value when the phrase wraps one.
        new_q = _PLACEHOLDER_RE.findall(m.group("b"))
        if len(new_q) == 1:
            new = quotes[int(new_q[0])]
        old_q = _PLACEHOLDER_RE.findall(m.group("a"))
        if len(old_q) == 1:
            old = quotes[int(old_q[0])]
        # A short value ("blue", "New Title", "1.1.0") must appear
        # verbatim; a longer phrase ("to print hello world") describes the
        # change rather than quoting it, so it can't be checked literally.
        max_words = 6 if pattern is _REPLACE_RE else 3
        if new and (len(new_q) == 1 or len(new.split()) <= max_words) and not re.match(
            r"(?:print|return|use|say|show|output|display|log|be|do|call|make)\b", new, _I
        ):
            present.append(new)
        # "change the color to blue" names a *setting* whose value changes
        # -- "color" stays. Only an explicitly quoted old value (or
        # replace/instead-of, which always name the old text) has to go.
        must_go = pattern is not _CHANGE_RE or len(old_q) == 1 or _is_literal_change(m, quotes, original)
        if must_go and old and _literal_in(old, original) and not _literal_in(old, new):
            absent.append(old)
        break

    m = _REMOVE_RE.search(text)
    if m and not _CLEAR_RE.search(text):
        target = _phrase(m.group("x"), quotes)
        target = re.sub(r"\s+(?:item|line|entry|row|bullet|word|text|task|section|part)s?$", "", target, flags=_I)
        target = re.sub(r"^(?:line|item|entry|word|bullet)\s+", "", target, flags=_I)
        if _literal_in(target, original):
            absent.append(target)

    # "delete the line X": X is named to be removed, not required. And a
    # filename is never required text: "use slugify from utils.py instead"
    # is satisfied by `from utils import slugify`.
    present = [
        p for p in dict.fromkeys(present)
        if p and not any(_literal_in(a, p) for a in absent) and not re.fullmatch(r"[\w./-]+\.\w{1,5}", p)
    ]
    if code:
        quoted = set(quotes)
        present = [p for p in present if p in quoted]
    return present, list(dict.fromkeys(absent))


def _content_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def check_edit(
    instructions: str, original: str, updated: str, code: bool = False, expectations: bool = True
) -> list[str]:
    """Problems with `updated` as the result of applying `instructions` to
    `original` -- empty if it looks right. `code` relaxes the "an addition
    keeps every existing line" rule, which is right for prose but wrong for
    code ("add a parameter" has to change the signature line); code is
    judged by the project's own checks instead. `expectations=False` skips
    the text-derived must/mustn't-contain checks (for an edit that's one
    part of a request describing several files)."""
    problems: list[str] = []
    if _content_lines(updated) == _content_lines(original):
        return ["the file came back unchanged -- the requested change wasn't made"]
    if updated.split() == original.split() and not re.search(
        r"\b(?:format|indent|whitespace|spacing|spaces|tabs|blank\s+lines?|line\s+breaks?|wrap|tidy|pretty)\b",
        instructions, _I,
    ):
        # Seen for real: a "make it green" rewrite of an HTML file that
        # only reshuffled indentation and blank lines.
        return ["only whitespace changed -- the requested change wasn't made"]
    if not updated.strip() and original.strip() and not _CLEAR_RE.search(instructions):
        return ["the file came back empty"]

    present, absent = edit_expectations(instructions, original, code=code) if expectations else ([], [])
    for p in present:
        if p.lower() not in updated.lower():
            problems.append(f"the result should contain {p!r} but doesn't")
    for a in absent:
        if _literal_in(a, updated):
            problems.append(f"the result should no longer contain {a!r} but still does")

    original_lines = _content_lines(original)
    updated_set = set(_content_lines(updated))
    kept = [line for line in original_lines if line in updated_set]
    if _ADDITIVE_RE.search(instructions) and not code:
        dropped = [line for line in original_lines if line not in updated_set]
        if dropped:
            problems.append(
                f"this was an addition, but existing lines were lost or changed: {dropped[:3]!r}"
            )
    elif len(original_lines) >= (15 if code else 4) and len(kept) < len(original_lines) / 2 \
            and not _CLEAR_RE.search(instructions):
        # (For code, only a sizable file: a refactor legitimately shrinks
        # a small function -- a 5-line loop becomes a 2-line comprehension.)
        problems.append("most of the existing lines were lost -- only the requested change should differ")
    return problems


def check_new_file(instructions: str, content: str, expectations: bool = True) -> list[str]:
    """Problems with a freshly written file's `content`."""
    text, quotes = protect_quotes(instructions)
    problems = []
    if instructions.strip() and not content.strip():
        problems.append("the file came back empty")
    for m in (_SAYS_RE.finditer(text) if expectations else ()):
        want = _phrase(m.group("w"), quotes)
        if want and want.lower() not in content.lower():
            problems.append(f"the file should contain {want!r} but doesn't")
    return problems


def strip_code_fence(text: str, original: str = "") -> str:
    """Drop a ``` fence the model wrapped the whole file in (unless the
    original itself started with one)."""
    stripped = text.strip("\n")
    if original.lstrip().startswith("```"):
        return text
    m = re.fullmatch(r"```[\w+\-.]*\n(.*?)\n?```", stripped, re.DOTALL)
    return m.group(1) if m else text


# ---------------------------------------------------------------------------
# Literal edits that don't need a model
# ---------------------------------------------------------------------------

_LINE_WORDS_RE = re.compile(r"\b(?:line|item|entry|row|bullet|task)s?\b", _I)


def literal_edit(instructions: str, original: str) -> str | None:
    """Apply `instructions` exactly, without a model, when it's an
    unambiguous literal operation on text that's really in the file:
    "remove/delete X" (a whole line, or just the words) and "replace A
    with B" / "B instead of A". None when the request isn't one of those,
    or the text it names isn't in the file -- the model handles it then.

    The on-device model is unreliable at exactly these: measured, it
    would repeatedly return a file unchanged when asked to delete a line.
    """
    text, quotes = protect_quotes(instructions)
    lines = original.splitlines()
    ending = "\n" if original.endswith("\n") else ""

    assigned = _assign_values(text, quotes, lines)
    if assigned is not None:
        return "\n".join(assigned) + ending

    case = _CASE_RE.search(re.sub(r"\s+(?:in|of)\s+(?:the\s+)?[\w\-/]+\.\w+(?:\s+file)?", "", text, flags=_I))
    if case:
        kind = (case.group("c") or case.group("c2")).lower().replace(" ", "")
        if kind.startswith(("upper", "allcaps", "capital")):
            return original.upper()
        if kind.startswith("lower"):
            return original.lower()
        if kind.startswith("title"):
            return "\n".join(line.title() for line in lines) + ending

    block = _REMOVE_BLOCK_RE.search(text)
    if block:
        name = block.group("n1") or block.group("n2")
        kept = _remove_block(name, lines)
        if kept is not None:
            return "\n".join(kept) + (ending if kept else "")

    for pattern in (_REPLACE_RE, _INSTEAD_RE, _CHANGE_RE):
        m = pattern.search(text)
        if not m:
            continue
        if pattern is _CHANGE_RE and not _is_literal_change(m, quotes, original):
            return None
        old_q = _PLACEHOLDER_RE.findall(m.group("a"))
        new_q = _PLACEHOLDER_RE.findall(m.group("b"))
        old = quotes[int(old_q[0])] if len(old_q) == 1 else _phrase(m.group("a"), quotes)
        new = quotes[int(new_q[0])] if len(new_q) == 1 else _phrase(m.group("b"), quotes)
        if pattern is _INSTEAD_RE and not (old_q and new_q) and len(new.split()) > 1:
            return None
        if not old or not new or len(old.split()) > 6 or len(new.split()) > 8:
            return None
        if not _literal_in(old, original):
            return None
        return re.sub(rf"(?<![\w]){re.escape(old)}(?![\w])", lambda _m: new, original)

    m = _REMOVE_RE.search(text)
    if m and not _CLEAR_RE.search(text):
        raw = m.group("x")
        target = _phrase(raw, quotes)
        target = re.sub(r"\s+(?:item|line|entry|row|bullet|word|text|task)s?$", "", target, flags=_I)
        target = re.sub(r"^(?:line|item|entry|word|bullet|task)\s+", "", target, flags=_I)
        if not target or not _literal_in(target, original):
            return None
        hits = [i for i, line in enumerate(lines) if _literal_in(target, line)]
        wants_line = bool(_LINE_WORDS_RE.search(raw))

        def is_whole_line(line: str) -> bool:
            rest = re.sub(rf"(?<![\w]){re.escape(target)}(?![\w])", "", line, flags=_I)
            return not re.sub(r"[\s\-*+•#>.,;:0-9)(\[\]]", "", rest)

        whole = [i for i in hits if is_whole_line(lines[i])]
        if whole:
            kept = [line for i, line in enumerate(lines) if i not in whole]
        elif wants_line and len(hits) == 1:
            kept = [line for i, line in enumerate(lines) if i != hits[0]]
        elif not wants_line:
            kept = [
                re.sub(r"[ \t]{2,}", " ", re.sub(rf"(?<![\w]){re.escape(target)}(?![\w])", "", line, flags=_I))
                .replace(" .", ".").replace(" ,", ",").rstrip()
                if i in hits else line
                for i, line in enumerate(lines)
            ]
        else:
            return None
        return "\n".join(kept) + (ending if kept else "")

    return None


def restore_layout(original: str, updated: str) -> str:
    """Re-apply `original`'s whitespace/line layout to `updated`.

    Seen for real: asked to change one value in a 4-line CSS rule, the
    on-device model returned the right change but collapsed the whole
    rule onto one line -- every retry, too. Aligning the two as word
    sequences keeps the model's actual changes (replaced/added/removed
    words) while every unchanged word keeps the whitespace it had, so the
    file's formatting survives.
    """
    parts = re.findall(r"\s+|\S+", original)
    lead = parts.pop(0) if parts and parts[0].isspace() else ""
    toks: list[str] = []
    gaps: list[str] = []  # whitespace after each token
    for part in parts:
        if part.isspace():
            gaps[-1] += part
        else:
            toks.append(part)
            gaps.append("")
    new = updated.split()
    out = [lead]
    matcher = difflib.SequenceMatcher(None, toks, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend(toks[k] + gaps[k] for k in range(i1, i2))
        elif tag == "replace":
            for n, j in enumerate(range(j1, j2)):
                gap = gaps[i1 + n] if n < i2 - i1 - 1 and j < j2 - 1 else (gaps[i2 - 1] if j == j2 - 1 else " ")
                out.append(new[j] + gap)
        elif tag == "insert":
            out.extend(new[j] + " " for j in range(j1, j2))
        elif tag == "delete":
            last_gap = gaps[i2 - 1]
            if "\n" in last_gap and len(out) > 1 and "\n" not in out[-1]:
                out[-1] = out[-1].rstrip() + last_gap
    return "".join(out)


def looks_reflowed(original: str, updated: str) -> bool:
    """True if `updated` keeps most of `original`'s words but lost much of
    its line structure -- a reformat, not a real rewrite."""
    orig_lines, new_lines = _content_lines(original), _content_lines(updated)
    if len(orig_lines) < 2 or len(new_lines) >= len(orig_lines) * 0.75:
        return False
    ratio = difflib.SequenceMatcher(None, original.split(), updated.split(), autojunk=False).ratio()
    return ratio >= 0.5


_ASSIGN_VERB_RE = re.compile(r"\b(?:set|change|update|make|bump)\s+(?:the\s+)?(?P<pairs>.+)$", _I)
_ASSIGN_PAIR_RE = re.compile(
    r"^(?:the\s+)?(?P<k>[\w.\-$]+)(?:\s+(?:value|setting|option|field|key))?\s+(?:to|=)\s+(?P<v>⟦\d+⟧|[^\s,]+)$", _I
)


def _key_line_re(key: str) -> re.Pattern:
    # `key` as the assigned name on a key/value line: "port=80",
    # "port = 80", "port: 80", '"version": "1.0.0",', "const PORT = 3000;".
    return re.compile(
        rf"^(?P<pre>\s*(?:[\w$]+\s+)*?[\"']?{re.escape(key)}[\"']?\s*[:=]\s*)"
        rf"(?P<val>.*?)(?P<post>\s*[;,]?\s*)$",
        _I,
    )


def assignment_pairs(text: str, quotes: list[str]) -> list[tuple[str, str]]:
    """("port", "443"), ("mode", "prod") from "set port to 443 and mode to
    prod" -- only if *every* part of the request is such a pair."""
    m = _ASSIGN_VERB_RE.search(text)
    if not m:
        return []
    # Drop file mentions wherever they sit: "the version in package.json
    # to 1.1.0", "port to 443 in app.cfg".
    body = re.sub(
        r"\s+(?:in|of|inside)\s+(?:the\s+)?[\w\-/]+\.\w+(?:\s+file)?", "", m.group("pairs").strip().rstrip("."), flags=_I
    )
    pairs = []
    for chunk in re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", body, flags=_I):
        pm = _ASSIGN_PAIR_RE.match(chunk.strip())
        if not pm:
            return []
        pairs.append((pm.group("k"), restore_quotes(pm.group("v"), quotes)))
    return pairs


def _assign_values(text: str, quotes: list[str], lines: list[str]) -> list[str] | None:
    """Set each named key's value on its own key/value line, keeping the
    line's quoting and punctuation. None unless every key is on exactly
    one line."""
    pairs = assignment_pairs(text, quotes)
    if not pairs:
        return None
    out = list(lines)
    for key, value in pairs:
        pattern = _key_line_re(key)
        hits = [i for i, line in enumerate(out) if pattern.match(line)]
        if len(hits) != 1:
            return None
        m = pattern.match(out[hits[0]])
        old = m.group("val")
        quote = old[0] if len(old) >= 2 and old[0] == old[-1] and old[0] in "\"'" else ""
        if quote and not (value.startswith(quote) and value.endswith(quote)):
            value = f"{quote}{value}{quote}"
        out[hits[0]] = m.group("pre") + value + m.group("post")
    return out


_REMOVE_BLOCK_RE = re.compile(
    r"\b(?:remove|delete|drop|get\s+rid\s+of)\s+(?:the\s+)?(?:(?:unused|old|dead|deprecated|obsolete|legacy|"
    r"duplicate|redundant|empty)\s+)*"
    r"(?:(?:function|method|class|def|func|fn)\s+(?P<n1>[\w$]+)|(?P<n2>[\w$]+)(?:\(\))?\s+(?:function|method|class))\b",
    _I,
)


def _remove_block(name: str, lines: list[str]) -> list[str] | None:
    """Remove the definition of `name` (a Python def/class by indentation,
    or a brace-delimited function/class/method), with its decorators.
    None unless exactly one definition is found."""
    n = re.escape(name)
    py = re.compile(rf"^(?P<ind>\s*)(?:async\s+)?(?:def|class)\s+{n}\b")
    brace = re.compile(
        rf"(?:\b(?:function|class|func|fn|def)\s+{n}\b|\b{n}\s*[=:]\s*(?:async\s*)?(?:function\b|\()|"
        rf"^\s*(?:[\w<>\[\],*&]+\s+)*{n}\s*\([^;]*$)"
    )
    starts = [i for i, line in enumerate(lines) if py.match(line) or brace.search(line)]
    if len(starts) != 1:
        return None
    start = starts[0]

    if (pm := py.match(lines[start])):
        indent = len(pm.group("ind"))
        end = start + 1
        while end < len(lines) and (
            not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > indent
        ):
            end += 1
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1  # leave the blank lines that separate what follows
        while start > 0 and lines[start - 1].strip().startswith("@"):
            start -= 1
    else:
        depth, end, opened = 0, None, False
        for i in range(start, len(lines)):
            depth += lines[i].count("{") - lines[i].count("}")
            opened = opened or "{" in lines[i]
            if opened and depth <= 0:
                end = i + 1
                break
        if end is None:
            return None

    kept = lines[:start] + lines[end:]
    # Don't leave a pile-up of blank lines where the block was.
    while start < len(kept) and start > 0 and not kept[start].strip() and not kept[start - 1].strip():
        del kept[start]
    while start == 0 and kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


_CASE_WHAT = r"(?:all\s+(?:of\s+)?)?(?:the\s+)?(?:text|contents?|everything|file|it|words|lines)"
_CASE_RE = re.compile(
    rf"\b(?:make|convert|change|turn|put|set|transform)\s+{_CASE_WHAT}\s+(?:to\s+|into\s+|in\s+)?(?:all\s+)?"
    rf"(?P<c>upper\s*case|lower\s*case|all\s+caps|caps|capitals|capital\s+letters|title\s*case)\b"
    rf"|\b(?P<c2>uppercase|lowercase)\s+{_CASE_WHAT}\b",
    _I,
)


def _is_literal_change(m: re.Match, quotes: list[str], original: str) -> bool:
    """"change World to Universe": the old text is really in the file, in
    exactly that case, and isn't a setting's *name* ("change the color to
    blue" keeps "color=" and changes its value) -- so it's a replacement."""
    old_q = _PLACEHOLDER_RE.findall(m.group("a"))
    old = quotes[int(old_q[0])] if len(old_q) == 1 else _phrase(m.group("a"), quotes)
    if not old or len(old.split()) > 4:
        return False
    if not re.search(rf"(?<![\w]){re.escape(old)}(?![\w])", original):
        return False
    return not any(_key_line_re(old).match(line) for line in original.splitlines())


# ---------------------------------------------------------------------------
# Structure checks for rewrites of code/stylesheets
# ---------------------------------------------------------------------------

_BRACE_EXTS = {".css", ".scss", ".less", ".js", ".jsx", ".ts", ".tsx", ".json", ".java", ".c", ".cpp", ".swift", ".go", ".rs"}
_CSS_VAR_DEF_RE = re.compile(r"(--[\w-]+)\s*:")


def check_structure(filename: str, instructions: str, original: str, updated: str, whole_file: str = "") -> list[str]:
    """Problems that break a file no matter what was asked -- measured for
    real on a stylesheet rewrite that renamed/dropped custom properties the
    rest of the file still used and lost a comment's closing */.
    `whole_file` is the full file when only a section is being rewritten,
    so "still used elsewhere" can be checked."""
    ext = os.path.splitext(filename)[1].lower()
    problems = []
    if ext in _BRACE_EXTS or ext in (".html", ".htm"):
        if updated.count("/*") - updated.count("*/") != original.count("/*") - original.count("*/"):
            problems.append("a /* comment */ was left unclosed (or a stray */ added)")
    if ext in _BRACE_EXTS:
        if updated.count("{") - updated.count("}") != original.count("{") - original.count("}"):
            problems.append("the { } braces no longer balance")
    if ext in (".html", ".htm", ".xml", ".svg", ".vue"):
        def balance(text: str) -> dict[str, int]:
            out: dict[str, int] = {}
            for closing, tag in re.findall(r"<(/?)([a-zA-Z][\w-]*)\b[^>]*?(?<!/)>", text):
                out[tag.lower()] = out.get(tag.lower(), 0) + (-1 if closing else 1)
            return out
        before, after = balance(original), balance(updated)
        off = [t for t in set(before) | set(after) if before.get(t, 0) != after.get(t, 0)
               and t not in ("br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "col", "base")]
        if off:
            problems.append(f"HTML tags no longer balance: <{'>, <'.join(sorted(off)[:4])}>")
    if ext in (".css", ".scss", ".less") and not re.search(r"\b(?:rename|remove|delete|drop)\b", instructions, _I):
        context = whole_file or original
        used = set(re.findall(r"var\(\s*(--[\w-]+)", context))
        lost = [
            name for name in dict.fromkeys(_CSS_VAR_DEF_RE.findall(original))
            if name in used and name not in _CSS_VAR_DEF_RE.findall(updated)
        ]
        if lost:
            problems.append(f"CSS variables still used elsewhere were renamed or removed: {', '.join(lost[:4])}")
    return problems


def match_indentation(original: str, updated: str) -> str:
    """If `original` indents with tabs and the rewrite switched to spaces,
    switch it back (4 or 2 spaces per level, whichever fits)."""
    orig_tabs = sum(1 for line in original.splitlines() if line.startswith("\t"))
    new_tabs = sum(1 for line in updated.splitlines() if line.startswith("\t"))
    new_spaces = [len(line) - len(line.lstrip(" ")) for line in updated.splitlines() if line.startswith(" ")]
    if not orig_tabs or new_tabs or not new_spaces:
        return updated
    unit = 4 if all(n % 4 == 0 for n in new_spaces) else 2
    out = []
    for line in updated.splitlines(keepends=True):
        n = len(line) - len(line.lstrip(" "))
        out.append("\t" * (n // unit) + " " * (n % unit) + line[n:] if n else line)
    return "".join(out)


# ---------------------------------------------------------------------------
# Stylesheet palettes: the model picks values, code keeps the structure
# ---------------------------------------------------------------------------

_NAMED_COLORS = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0), "green": (0, 128, 0),
    "blue": (0, 0, 255), "yellow": (255, 255, 0), "orange": (255, 165, 0), "purple": (128, 0, 128),
    "pink": (255, 192, 203), "gray": (128, 128, 128), "grey": (128, 128, 128), "teal": (0, 128, 128),
    "navy": (0, 0, 128), "gold": (255, 215, 0), "lime": (0, 255, 0), "olive": (128, 128, 0),
    "brown": (165, 42, 42), "cyan": (0, 255, 255), "magenta": (255, 0, 255), "maroon": (128, 0, 0),
    "transparent": None,
}
_HEX_RE = re.compile(r"^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$", _I)
_FUNC_COLOR_RE = re.compile(r"^(?:rgb|rgba|hsl|hsla)\(\s*[\d.%\s,/-]+\)$", _I)
_PALETTE_DECL_RE = re.compile(r"^(?P<pre>\s*(?P<name>--[\w-]+)\s*:\s*)(?P<val>[^;]+?)(?P<post>\s*;.*)$")


def is_css_color(value: str) -> bool:
    value = value.strip()
    return bool(_HEX_RE.match(value) or _FUNC_COLOR_RE.match(value) or value.lower() in _NAMED_COLORS)


def css_color_rgb(value: str) -> tuple[int, int, int] | None:
    value = value.strip().lower()
    if value in _NAMED_COLORS:
        return _NAMED_COLORS[value]
    if _HEX_RE.match(value):
        h = value[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h[:3])
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    m = re.match(r"rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)", value)
    if m:
        return tuple(int(x) for x in m.groups())
    return None


_HUE_RANGES = {  # color word -> hue ranges in degrees
    "red": [(345, 360), (0, 15)], "orange": [(15, 40)], "yellow": [(40, 70)], "gold": [(40, 60)],
    "green": [(70, 170)], "lime": [(70, 110)], "teal": [(160, 200)], "cyan": [(170, 200)],
    "blue": [(195, 255)], "navy": [(215, 250)], "purple": [(255, 300)], "violet": [(255, 300)],
    "pink": [(300, 345)], "magenta": [(285, 330)],
}


def color_matches_word(value: str, word: str) -> bool:
    import colorsys
    rgb = css_color_rgb(value)
    if rgb is None:
        return False
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
    word = word.lower()
    if word in ("black", "white", "gray", "grey"):
        return {"black": l < 0.2, "white": l > 0.9}.get(word, s < 0.15)
    if s < 0.2 or l < 0.08 or l > 0.97:
        return False
    hue = h * 360
    return any(lo <= hue < hi for lo, hi in _HUE_RANGES.get(word, []))


def requested_color_words(instructions: str) -> list[str]:
    words = re.findall(r"\b(" + "|".join(list(_HUE_RANGES) + ["black", "white", "gray", "grey"]) + r")\b",
                       instructions, _I)
    return list(dict.fromkeys(w.lower() for w in words))


def palette_variables(content: str) -> list[tuple[int, str, str]]:
    """(line index, --name, value) for every custom property whose value is
    a plain color."""
    out = []
    for i, line in enumerate(content.splitlines()):
        m = _PALETTE_DECL_RE.match(line)
        if m and is_css_color(m.group("val")):
            out.append((i, m.group("name"), m.group("val").strip()))
    return out


def apply_palette(content: str, new_values: dict[str, str]) -> str:
    """Replace only the values of the named color variables -- names,
    comments, and layout are untouched by construction."""
    lines = content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = _PALETTE_DECL_RE.match(line.rstrip("\n"))
        if m and m.group("name") in new_values:
            ending = "\n" if line.endswith("\n") else ""
            lines[i] = m.group("pre") + new_values[m.group("name")] + m.group("post") + ending
    return "".join(lines)


def check_palette(instructions: str, old_values: dict[str, str], new_values: dict[str, str]) -> list[str]:
    changed = {k: v for k, v in new_values.items() if k in old_values and v.strip().lower() != old_values[k].lower()}
    if not changed:
        return ["no colors were changed"]
    bad = [f"{k}: {v!r}" for k, v in changed.items() if not is_css_color(v)]
    if bad:
        return [f"these aren't valid CSS colors: {', '.join(bad)}"]
    final = {**old_values, **changed}
    problems = []
    for word in requested_color_words(instructions):
        if not any(color_matches_word(v, word) for v in changed.values()):
            if not any(color_matches_word(v, word) for v in final.values()):
                problems.append(f"the palette should include {word}, but none of the new colors are {word}")
    return problems


# ---------------------------------------------------------------------------
# Whole-theme recoloring
# ---------------------------------------------------------------------------

_TARGET_HUE = {
    "red": 0, "orange": 30, "yellow": 50, "gold": 48, "green": 140, "lime": 95, "teal": 180,
    "cyan": 188, "blue": 215, "navy": 228, "purple": 275, "violet": 275, "pink": 330, "magenta": 305,
}
_FAMILIES = ["red", "orange", "yellow", "green", "teal", "blue", "purple", "pink"]
_COLOR_LITERAL_RE = re.compile(
    r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![0-9a-zA-Z_-])"
    r"|rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*(?:,\s*[\d.]+%?\s*)?\)"
)
_THEME_WORDS_RE = re.compile(
    r"\b(?:theme|themed|colou?r\s*scheme|palette|colou?rs|look|ui|site|website|app|page|design|style)\b", _I
)


def is_theme_request(instructions: str) -> bool:
    """"make the ui a green and yellow theme", "use a purple color scheme"
    -- a request to re-color everything, not one specific thing."""
    words = [w for w in requested_color_words(instructions) if w in _TARGET_HUE]
    return bool(words) and (bool(_THEME_WORDS_RE.search(instructions)) or len(words) >= 2)


def _family(h: float) -> str:
    hue = h * 360
    for fam in _FAMILIES:
        if any(lo <= hue < hi for lo, hi in _HUE_RANGES[fam]):
            return fam
    return "red"


def recolor_theme(content: str, instructions: str) -> str | None:
    """Shift every chromatic color in `content` to the requested theme
    colors, keeping each color's lightness and saturation -- so dark stays
    dark, light stays light, and contrast survives. The most common color
    family goes to the first requested color, the next to the second, and
    so on; a family that already *is* a requested color stays put, and
    grays/whites/blacks and any leftover families (an error red) are left
    alone. None if nothing would change. No model involved: a theme swap is
    exactly the kind of consistent, file-wide bookkeeping a small model
    gets wrong (measured: it re-colored a few variables at random and
    produced white text on a light-green background).
    """
    import colorsys

    wanted = [w for w in requested_color_words(instructions) if w in _TARGET_HUE]
    if not wanted:
        return None

    def hls(rgb):
        return colorsys.rgb_to_hls(*(c / 255 for c in rgb))

    counts: dict[str, int] = {}
    for m in _COLOR_LITERAL_RE.finditer(content):
        rgb = css_color_rgb(m.group())
        if rgb is None:
            continue
        h, l, s = hls(rgb)
        if s >= 0.2 and 0.08 < l < 0.97:
            fam = _family(h)
            counts[fam] = counts.get(fam, 0) + 1
    if not counts:
        return None

    mapping: dict[str, str] = {}
    remaining = list(wanted)
    for fam in counts:
        if fam in remaining:
            mapping[fam] = fam
            remaining.remove(fam)
    for fam in sorted(counts, key=lambda f: -counts[f]):
        if fam not in mapping and remaining:
            mapping[fam] = remaining.pop(0)
    if all(fam == target for fam, target in mapping.items()):
        return None

    def replace(m: re.Match) -> str:
        text = m.group()
        rgb = css_color_rgb(text)
        if rgb is None:
            return text
        h, l, s = hls(rgb)
        if not (s >= 0.2 and 0.08 < l < 0.97):
            return text
        target = mapping.get(_family(h))
        if target is None or target == _family(h):
            return text
        r, g, b = (round(c * 255) for c in colorsys.hls_to_rgb(_TARGET_HUE[target] / 360, l, s))
        if text.startswith("#"):
            digits = text[1:]
            alpha = digits[6:8] if len(digits) == 8 else (digits[3] * 2 if len(digits) == 4 else "")
            out = f"#{r:02x}{g:02x}{b:02x}{alpha}"
            return out.upper() if digits.isupper() else out
        alpha = re.match(r"rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*(,\s*[\d.]+%?\s*)?\)", text).group(1)
        func = "rgba" if text.lower().startswith("rgba") else "rgb"
        return f"{func}({r}, {g}, {b}{alpha.rstrip() if alpha else ''})"

    updated = _COLOR_LITERAL_RE.sub(replace, content)
    return updated if updated != content else None


_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "into", "from", "make", "change", "update", "set",
    "add", "use", "all", "its", "your", "you", "our", "them", "then", "text", "file", "files",
}


def check_relevant(instructions: str, original: str, updated: str) -> list[str]:
    """For an edit the planner chose on its own (the request named no
    file): the change has to actually involve the request -- one of its
    words, or a color when colors were asked for. Seen for real: a "make
    the ui green" rewrite of index.html that only moved tags around."""
    old_tokens, new_tokens = original.split(), updated.split()
    added = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            added.extend(new_tokens[j1:j2])
    added_text = " ".join(added).lower()
    keywords = {w.lower() for w in re.findall(r"[A-Za-z][\w-]{2,}", instructions)} - _STOPWORDS
    if any(k in added_text for k in keywords):
        return []
    if requested_color_words(instructions) and _COLOR_LITERAL_RE.search(added_text):
        return []
    return ["the change doesn't touch anything the request is about"]


# ---------------------------------------------------------------------------
# Plain chat -> /task
# ---------------------------------------------------------------------------

_AFFIRM_RE = re.compile(
    r"^(?:(?:yes|yeah|yep|yup|sure|ok(?:ay)?|please|go\s+ahead|make\s+it\s+so|sounds\s+good|"
    r"(?:(?:can|could|would|will)\s+)?you\s+(?:do|make|change|apply)\s+(?:it|that|this|them|those|the\s+change)|"
    r"(?:do|apply|make)\s+(?:it|that|this|so|them|those|the\s+changes?)|let'?s\s+do\s+(?:it|that)|"
    r"go\s+for\s+it|for\s+me|yourself|then|now|y|thanks?)\b[\s,.!?]*)+$",
    _I,
)
_ACTION_CUE_RE = re.compile(
    r"\b(?:commit|push|pull|branch|rename|move|create|make|add|append|change|edit|update|fix|replace|"
    r"remove|delete|write|set|theme|restyle|recolou?r|style)\b|\bi\s+(?:want|need|would\s+like|'d\s+like)\b",
    _I,
)


def is_affirmation(text: str) -> bool:
    """"yes, do that", "go ahead", "ok do it" -- a go-ahead for whatever
    change was just discussed."""
    return bool(_AFFIRM_RE.match(text.strip()))


def is_direct_action(text: str, files: list[str], folders: list[str]) -> bool:
    """True if a plain chat message is a request /task fully understands
    on its own ("can you please commit and push", "rename a.txt to
    b.txt"). Questions, and anything with a part the parser can't read,
    stay chat -- so talking *about* a change never makes one."""
    if text.rstrip().endswith("?"):
        return False
    steps = parse_task(text, files, folders)
    return bool(steps) and all(s["action"] != "UNPARSED" for s in steps)


def looks_like_action(text: str) -> bool:
    """A chat message that reads like a change request, but that the
    parser couldn't fully read -- worth offering to run it as /task."""
    return not text.rstrip().endswith("?") and bool(_ACTION_CUE_RE.search(text))


# ---------------------------------------------------------------------------
# Requests with special handling in the editor
# ---------------------------------------------------------------------------

_JSON_SET_RE = re.compile(
    r"\b(?:add|set|change|update|put)\s+(?:a\s+|an\s+|the\s+)?(?:new\s+)?"
    r"(?:(?:setting|key|field|property|option|value|entry)\s+)?[\"'⟦]?(?P<k>[A-Za-z_][\w.-]*)[\"'⟧]?\s+"
    r"(?:(?:setting|key|field|property|option|value|entry)\s+)?"
    r"(?:of|to|=|:|as|with\s+(?:a\s+)?value\s+(?:of\s+)?)\s*(?P<v>⟦\d+⟧|[^\s,]+)",
    _I,
)


def json_assignment(instructions: str) -> tuple[str, object] | None:
    """("timeout", 30) for "add a timeout setting of 30 to config.json"."""
    text, quotes = protect_quotes(instructions)
    m = _JSON_SET_RE.search(text)
    if not m:
        return None
    key = restore_quotes(m.group("k"), quotes)
    raw = restore_quotes(m.group("v"), quotes).rstrip(".;")
    if raw.lower() in ("true", "on", "yes"):
        value: object = True
    elif raw.lower() in ("false", "off", "no"):
        value = False
    elif raw.lower() in ("null", "none"):
        value = None
    else:
        try:
            value = int(raw)
        except ValueError:
            try:
                value = float(raw)
            except ValueError:
                value = raw
    return key, value


_EVERY_BLOCK_RE = re.compile(
    r"\b(?:every|each|all(?:\s+(?:of\s+)?the)?)\s+(?:of\s+the\s+)?(?:functions?|methods?|defs?)\b", _I
)


def applies_to_every_function(instructions: str) -> bool:
    """"add a docstring to every function": one edit per function, which
    the model does far better than several at once."""
    return bool(_EVERY_BLOCK_RE.search(instructions))


# ---------------------------------------------------------------------------
# Cleaning up generated code
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"(?:^|\n)(`{3,}|~{3,})[^\n`]*\n(.*?)\n?\1[ \t]*(?=\n|$)", re.DOTALL)


def extract_code_block(reply: str) -> str:
    """The code from a plain-text reply: the longest fenced block, or an
    unterminated one's contents, or else the whole reply."""
    blocks = [m.group(2) for m in _FENCE_RE.finditer(reply)]
    if blocks:
        return max(blocks, key=len)
    m = re.match(r"\s*(`{3,}|~{3,})[^\n]*\n(.*)$", reply, re.DOTALL)
    if m:
        return m.group(2).rstrip("`~ \n")
    return reply.strip("\n")


def unescape_literal_newlines(original: str, text: str) -> str:
    """Seen for real through guided generation: a whole multi-line file
    returned as one line full of literal "\\n" sequences. If the reply has
    no real line breaks but does have escaped ones, decode them."""
    if "\n" in text.strip() or "\\n" not in text:
        return text
    if original and "\n" not in original.strip() and "\\n" in original:
        return text  # the file genuinely is one line with "\n" in it
    return text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')


_COMMENT_MARKERS = {
    ".py": ("#",), ".sh": ("#",), ".rb": ("#",), ".yml": ("#",), ".yaml": ("#",), ".toml": ("#",),
    ".js": ("//", "/*"), ".mjs": ("//", "/*"), ".cjs": ("//", "/*"), ".jsx": ("//", "/*"), ".ts": ("//", "/*"),
    ".tsx": ("//", "/*"), ".swift": ("//", "/*"), ".go": ("//", "/*"), ".rs": ("//", "/*"), ".java": ("//", "/*"),
    ".kt": ("//", "/*"), ".c": ("//", "/*"), ".cpp": ("//", "/*"), ".cs": ("//", "/*"), ".php": ("//", "#", "/*"),
    ".css": ("/*",), ".scss": ("//", "/*"), ".html": ("<!--",), ".htm": ("<!--",), ".sql": ("--",),
}


def check_comment_request(filename: str, instructions: str, original: str, updated: str) -> list[str]:
    """"Add a comment above X" in code means a real comment -- seen for
    real: a docstring written instead of the requested # comment."""
    if not re.search(r"\b(?:add|write|put|insert)\s+(?:a\s+|an\s+|some\s+)?(?:short\s+|brief\s+)?comments?\b",
                     instructions, _I):
        return []
    markers = _COMMENT_MARKERS.get(os.path.splitext(filename)[1].lower())
    if not markers:
        return []
    if sum(updated.count(mk) for mk in markers) <= sum(original.count(mk) for mk in markers):
        return [f"no new {' or '.join(markers)} comment was added"]
    return []
