"""Code-aware helpers for /task and /ask: project context, deterministic
code operations, and running a project's own checks.

Measured against the real on-device model (see
tests/slow/test_agentic_eval.py), /task's coding failures were mostly not
the model's: it never saw file contents while planning, /ask never saw
files at all, operations like "rename this function everywhere" were left
to a model rewriting whole files one at a time, and nothing ever ran the
code to find out whether it worked. This module fills those gaps:

- build_context(): the most relevant files in full plus an outline of
  the rest, fitted to a character budget (the on-device model has a
  4096-token context), so planning, editing, and /ask see real code.
- rename_symbol() / move_functions() / function_blocks(): refactors
  that are bookkeeping, not judgment, done exactly in code.
- detect_checks() / run_check(): the project's own syntax checks and
  tests, so /task can verify its work and feed failures back.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess

from . import taskplan

CODE_EXTS = {
    ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".swift", ".go", ".rs", ".rb", ".java",
    ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".sh", ".html", ".htm", ".css", ".scss",
    ".json", ".toml", ".yml", ".yaml", ".sql", ".vue", ".svelte", ".md",
}
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist", ".build", "target"}
_TEST_FILE_RE = re.compile(r"(?:^|/)(?:test_[^/]+\.py|[^/]+_test\.(?:py|go)|[^/]+\.(?:test|spec)\.[jt]sx?|tests?/[^/]+)$")


def is_test_file(path: str) -> bool:
    return bool(_TEST_FILE_RE.search(path))


def project_files(cwd: str, limit: int = 200) -> list[str]:
    out = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS)
        for f in sorted(files):
            if f.startswith("."):
                continue
            if os.path.splitext(f)[1].lower() in CODE_EXTS or f in ("Makefile", "Dockerfile"):
                out.append(os.path.relpath(os.path.join(root, f), cwd))
                if len(out) >= limit:
                    return out
    return out


def read_text(cwd: str, rel: str, limit: int = 200_000) -> str:
    try:
        with open(os.path.join(cwd, rel), "r", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Outlines and context
# ---------------------------------------------------------------------------

_OUTLINE_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|static\s+|async\s+|final\s+)*"
    r"(?:def|class|function|func|fn|struct|enum|interface|protocol|extension|type|impl|module)\b[^{;]*"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+[\w$]+\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>)[^{;]*"
    r"|^\s*[\w$]+\s*\([^)]*\)\s*\{\s*$"
)


def outline(rel: str, text: str, max_lines: int = 40) -> list[str]:
    """Signature lines ("12: def parse(path):") -- enough to know what a
    file defines and where, at a fraction of its size."""
    out: list[str] = []
    if rel.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            lines = text.splitlines()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    out.append(f"{node.lineno}: {lines[node.lineno - 1].strip()}")
            out.sort(key=lambda s: int(s.split(":", 1)[0]))
            return out[:max_lines]
    for i, line in enumerate(text.splitlines(), 1):
        if _OUTLINE_RE.match(line):
            out.append(f"{i}: {line.strip()[:120]}")
            if len(out) >= max_lines:
                break
    return out


_STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "make", "file", "files",
         "add", "fix", "change", "update", "create", "function", "should", "when", "what", "does",
         "which", "code", "use", "all", "its", "not", "are", "can", "you", "please", "then"}


def request_terms(request: str) -> set[str]:
    words = set(re.findall(r"[A-Za-z_$][\w$]{2,}", request))
    return {w for w in words if w.lower() not in _STOP}


def relevance(rel: str, text: str, terms: set[str], named: set[str]) -> float:
    score = 0.0
    if rel in named or os.path.basename(rel) in named:
        score += 100
    stem = os.path.splitext(os.path.basename(rel))[0].lower()
    for t in terms:
        if t.lower() == stem:
            score += 20
        hits = len(re.findall(rf"(?<![\w$]){re.escape(t)}(?![\w$])", text))
        if hits:
            score += 5 + min(hits, 10)
    return score


def build_context(
    cwd: str,
    request: str,
    budget: int,
    exclude: tuple[str, ...] = (),
    files: list[str] | None = None,
    prefer: tuple[str, ...] = (),
) -> str:
    """The project as the model should see it for `request`, within
    `budget` characters: relevant files in full (most relevant first),
    then outlines of other code files, then the names of the rest."""
    files = files if files is not None else project_files(cwd)
    files = [f for f in files if f not in exclude]
    if not files:
        return ""
    terms = request_terms(request)
    named = set(taskplan.mentioned_files(request, files)) | set(prefer)
    texts = {f: read_text(cwd, f) for f in files}
    ranked = sorted(files, key=lambda f: (-relevance(f, texts[f], terms, named), f))

    parts: list[str] = []
    used = 0
    shown_full: set[str] = set()
    for f in ranked:
        score = relevance(f, texts[f], terms, named)
        body = texts[f]
        block = f"--- {f} ---\n{body.rstrip()}\n"
        if score > 0 and used + len(block) <= budget * 0.75:
            parts.append(block)
            used += len(block)
            shown_full.add(f)
    rest = [f for f in ranked if f not in shown_full]
    for f in rest:
        sig = outline(f, texts[f])
        block = f"--- {f} (outline) ---\n" + "\n".join(sig) + "\n" if sig else ""
        if block and used + len(block) <= budget:
            parts.append(block)
            used += len(block)
            shown_full.add(f)
    names = [f for f in ranked if f not in shown_full]
    if names:
        line = "Other files: " + ", ".join(names)
        parts.append(line[: max(0, budget - used)])
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Deterministic code operations
# ---------------------------------------------------------------------------

def rename_symbol(old: str, new: str, cwd: str, only: list[str] | None = None) -> dict[str, tuple[str, str]]:
    """Whole-word rename of an identifier in every code file that uses it
    (or just `only`). Returns {path: (original, updated)} for files that
    changed; writes nothing."""
    pattern = re.compile(rf"(?<![\w$]){re.escape(old)}(?![\w$])")
    changed = {}
    for rel in (only or project_files(cwd)):
        text = read_text(cwd, rel)
        if pattern.search(text):
            changed[rel] = (text, pattern.sub(new, text))
    return changed


def _py_block_range(lines: list[str], name: str) -> tuple[int, int] | None:
    """[start, end) of a top-level Python def/class `name`, including its
    decorators and not the blank lines after it."""
    for i, line in enumerate(lines):
        if re.match(rf"(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b", line):
            start = i
            while start > 0 and lines[start - 1].startswith("@"):
                start -= 1
            end = i + 1
            while end < len(lines) and (not lines[end].strip() or lines[end][:1] in (" ", "\t")):
                end += 1
            while end > i + 1 and not lines[end - 1].strip():
                end -= 1
            return start, end
    return None


def function_blocks(rel: str, text: str) -> list[tuple[str, int, int]]:
    """(name, start, end) 0-based [start, end) line ranges of every function
    and method in a file -- Python by indentation, brace languages by
    braces -- innermost-last so each can be rewritten on its own."""
    lines = text.splitlines()
    out = []
    if rel.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = node.lineno - 1 - len(node.decorator_list)
                out.append((node.name, start, node.end_lineno))
        return sorted(out, key=lambda b: b[1])
    for i, line in enumerate(lines):
        m = re.match(
            r"^\s*(?:export\s+)?(?:public\s+|private\s+|static\s+|async\s+)*(?:function|func|fn)\s+([\w$]+)", line
        ) or re.match(r"^\s*(?:export\s+)?(?:const|let|var)\s+([\w$]+)\s*=\s*(?:async\s*)?(?:function\b|\()", line)
        if not m:
            continue
        depth, opened = 0, False
        for j in range(i, len(lines)):
            depth += lines[j].count("{") - lines[j].count("}")
            opened = opened or "{" in lines[j]
            if opened and depth <= 0:
                out.append((m.group(1), i, j + 1))
                break
    return out


def move_functions(names: list[str], src: str, dest: str, cwd: str) -> dict[str, tuple[str, str]]:
    """Move top-level Python functions/classes `names` from `src` into
    `dest` (created if missing), carry over the imports they use, and
    import them back into `src` if it still uses them. Returns
    {path: (original, updated)}; writes nothing. Raises ValueError if a
    name isn't a top-level definition in `src`."""
    if not (src.endswith(".py") and dest.endswith(".py")):
        raise ValueError("moving functions between files is only supported for Python")
    src_text = read_text(cwd, src)
    dest_text = read_text(cwd, dest)
    lines = src_text.splitlines()
    ranges = []
    for name in names:
        r = _py_block_range(lines, name)
        if r is None:
            raise ValueError(f"{name} isn't a top-level function or class in {src}")
        ranges.append((name, *r))
    moved_blocks = ["\n".join(lines[s:e]) for _n, s, e in sorted(ranges, key=lambda x: x[1])]
    moved_code = "\n\n\n".join(moved_blocks)

    import_lines = [l for l in lines if re.match(r"(?:import|from)\s+\S+", l)]
    needed = [
        l for l in import_lines
        if any(re.search(rf"(?<![\w.]){re.escape(n)}(?![\w])", moved_code)
               for n in re.findall(r"(?:import|as)\s+([\w, ]+)", l)[-1].replace(" ", "").split(",") if n)
    ]
    dest_imports = [l for l in needed if l not in dest_text]

    drop = set()
    for _n, s, e in ranges:
        drop.update(range(s, e))
    kept = [l for i, l in enumerate(lines) if i not in drop]
    remaining = re.sub(r"\n{3,}", "\n\n\n", "\n".join(kept).strip("\n")) + "\n"

    module = os.path.splitext(dest)[0].replace("/", ".")
    still_used = [n for n in names if re.search(rf"(?<![\w.]){re.escape(n)}(?![\w])", remaining)]
    if still_used:
        import_line = f"from {module} import {', '.join(still_used)}"
        rlines = remaining.splitlines()
        last_import = max((i for i, l in enumerate(rlines) if re.match(r"(?:import|from)\s+\S+", l)), default=-1)
        rlines.insert(last_import + 1, import_line)
        if last_import == -1 and len(rlines) > 1 and rlines[1].strip():
            rlines.insert(1, "")
            rlines.insert(2, "")
        remaining = "\n".join(rlines).rstrip("\n") + "\n"

    new_dest = dest_text.rstrip("\n")
    header = "\n".join(dest_imports)
    if header:
        new_dest = (header + "\n\n\n" + new_dest) if new_dest else header
    new_dest = (new_dest + "\n\n\n" if new_dest else "") + moved_code + "\n"
    return {src: (src_text, remaining), dest: (dest_text, new_dest)}


def json_set(text: str, key: str, value) -> str:
    """Set `key` (dots for nesting) in a JSON document, keeping its key
    order and indentation."""
    data = json.loads(text)
    indent_m = re.search(r"\n([ \t]+)\"", text)
    indent = indent_m.group(1) if indent_m else "  "
    node = data
    parts = key.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    out = json.dumps(data, indent=indent if indent != "\t" else "\t", ensure_ascii=False)
    return out + ("\n" if text.endswith("\n") else "")


# ---------------------------------------------------------------------------
# Running the project's own checks
# ---------------------------------------------------------------------------

def _python(cwd: str) -> str:
    for venv in (".venv", "venv"):
        p = os.path.join(cwd, venv, "bin", "python")
        if os.path.exists(p):
            return p
    return shutil.which("python3") or "python3"


def detect_checks(cwd: str, changed: list[str], request: str = "") -> list[tuple[str, list[str]]]:
    """(label, argv) checks worth running after /task changed `changed`:
    syntax checks of the changed files, the project's test suite if it has
    one, and anything the request itself says to run ("running main.py
    fails with ...")."""
    checks: list[tuple[str, list[str]]] = []
    py = _python(cwd)
    changed_py = [f for f in changed if f.endswith(".py") and os.path.exists(os.path.join(cwd, f))]
    if changed_py:
        checks.append(("Python syntax", [py, "-m", "py_compile", *changed_py]))
    for f in changed:
        if f.endswith((".js", ".mjs", ".cjs")) and shutil.which("node") and os.path.exists(os.path.join(cwd, f)):
            checks.append((f"node --check {f}", ["node", "--check", f]))
        if f.endswith(".json") and os.path.exists(os.path.join(cwd, f)):
            checks.append((f"valid JSON {f}", [py, "-c", "import json,sys; json.load(open(sys.argv[1]))", f]))
    swift = [f for f in changed if f.endswith(".swift")]
    if swift and shutil.which("swiftc") and not os.path.exists(os.path.join(cwd, "Package.swift")):
        # Type-check the changed files together with the other Swift files
        # beside them (a syntax-only check let a type error through).
        dirs = {os.path.dirname(f) for f in swift}
        together = sorted({f for f in project_files(cwd) if f.endswith(".swift") and os.path.dirname(f) in dirs})
        checks.append(("Swift type check", ["swiftc", "-typecheck", *together]))

    files = project_files(cwd)
    if any(f.endswith(".py") and is_test_file(f) for f in files):
        has_pytest = subprocess.run([py, "-c", "import pytest"], capture_output=True).returncode == 0
        checks.append(("tests", [py, "-m", "pytest", "-q"] if has_pytest
                       else [py, "-m", "unittest", "discover", "-q", "-s", ".", "-p", "*test*.py"]))
    pkg = os.path.join(cwd, "package.json")
    if os.path.exists(pkg):
        try:
            test = json.load(open(pkg)).get("scripts", {}).get("test", "")
        except (OSError, json.JSONDecodeError):
            test = ""
        if test and "no test specified" not in test and shutil.which("npm"):
            checks.append(("tests", ["npm", "test", "--silent"]))
    if os.path.exists(os.path.join(cwd, "Package.swift")) and shutil.which("swift"):
        checks.append(("swift build", ["swift", "build"]))
    if os.path.exists(os.path.join(cwd, "go.mod")) and shutil.which("go"):
        checks.append(("tests", ["go", "test", "./..."]))
    if os.path.exists(os.path.join(cwd, "Cargo.toml")) and shutil.which("cargo"):
        checks.append(("tests", ["cargo", "test", "-q"]))

    # "running main.py fails with ...", "when I run app.js it crashes"
    for m in re.finditer(r"\brun(?:ning)?\s+([\w./-]+\.(?:py|js|mjs))\b", request, re.I):
        target = m.group(1)
        if os.path.exists(os.path.join(cwd, target)):
            argv = [py, target] if target.endswith(".py") else ["node", target]
            checks.append((f"run {target}", argv))
    return checks


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Plain output from every tool: Python 3.13 colors tracebacks by default,
# and the escape codes broke reading which file a crash came from.
_PLAIN_ENV = {"NO_COLOR": "1", "PYTHON_COLORS": "0", "FORCE_COLOR": "0", "TERM": "dumb", "CI": "1"}


def run_check(argv: list[str], cwd: str, timeout: float = 120) -> tuple[bool, str]:
    env = {**os.environ, **_PLAIN_ENV}
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {int(timeout)}s"
    except OSError as e:
        return False, str(e)
    out = (r.stdout + ("\n" if r.stdout and r.stderr else "") + r.stderr).strip()
    return r.returncode == 0, _ANSI_RE.sub("", out)


def files_in_output(output: str, candidates: list[str]) -> list[str]:
    """Which of `candidates` a failure's output points at (tracebacks,
    compiler errors), most-mentioned first."""
    counts = {}
    for c in candidates:
        n = len(re.findall(rf"(?<![\w.-]){re.escape(os.path.basename(c))}\b", output))
        if n:
            counts[c] = n
    return sorted(counts, key=lambda c: -counts[c])


# ---------------------------------------------------------------------------
# Smoke runs (for projects without a test suite)
# ---------------------------------------------------------------------------

SMOKE_LANGS = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript"}
_SMOKE_MAX_BYTES = 20_000_000


_BROWSER_JS_RE = re.compile(r"\b(?:document|window|localStorage|navigator|alert)\s*[.(]")


def smoke_targets(changed: list[str], cwd: str) -> list[str]:
    """Changed Python/JS source files worth exercising: not tests, still
    there, and (for JS) not browser code, which can't run under Node."""
    out = []
    for f in changed:
        if os.path.splitext(f)[1] not in SMOKE_LANGS or is_test_file(f) or not os.path.isfile(os.path.join(cwd, f)):
            continue
        if f.endswith((".js", ".mjs", ".cjs")) and _BROWSER_JS_RE.search(read_text(cwd, f)):
            continue
        out.append(f)
    return out


def has_test_suite(checks: list[tuple[str, list[str]]]) -> bool:
    return any(label == "tests" for label, _ in checks)


def run_smoke(cwd: str, script: str, lang: str, timeout: float = 20) -> tuple[bool, str, str | None]:
    """Run a model-written script that exercises changed code, on a copy of
    the project (so nothing it does can touch the real files). Returns
    (ok, output, origin), `origin` being the project file a crash came
    from. Only a crash *inside the project's own code* counts as a
    failure: an error in the throwaway script itself, or a failed
    assertion (whose expectation the model may simply have gotten wrong),
    is inconclusive and reported as ok."""
    import tempfile

    total = 0
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
        if total > _SMOKE_MAX_BYTES:
            return True, "project too large to copy for a smoke run -- skipped", None

    with tempfile.TemporaryDirectory(prefix="fm-pcc-smoke-") as tmp:
        copy = os.path.join(os.path.realpath(tmp), "project")
        shutil.copytree(cwd, copy, ignore=shutil.ignore_patterns(*_SKIP_DIRS, ".*"), symlinks=True)
        name = "_fm_pcc_smoke.py" if lang == "python" else "_fm_pcc_smoke.js"
        with open(os.path.join(copy, name), "w") as f:
            f.write(script)
        argv = [_python(cwd), name] if lang == "python" else ["node", name]
        ok, out = run_check(argv, copy, timeout=timeout)
        # Longest first: /private/var/... contains /var/...
        for prefix in sorted({copy, copy.replace("/private/", "/", 1)}, key=len, reverse=True):
            out = out.replace(prefix + os.sep, "").replace(prefix, ".")
    if ok:
        return True, out, None
    origin = smoke_crash_origin(out, name)
    return origin is None, out, origin


def smoke_crash_origin(output: str, script_name: str) -> str | None:
    """The project file an exception was raised from, or None if it came
    from the smoke script itself, from outside the project (a library,
    Node internals), or was an AssertionError."""
    if re.search(r"\bAssertionError\b", output):
        return None
    py_frames = re.findall(r'File "([^"]+)", line \d+', output)
    if py_frames:
        last = py_frames[-1]
        if os.path.isabs(last) or last.startswith("<") or os.path.basename(last) == script_name:
            return None
        return last[2:] if last.startswith("./") else last
    # Node: the first stack frame that isn't Node's own.
    for frame in re.findall(r"^\s*at\s+(?:.*?\()?([^\s()]+?):\d+:\d+\)?\s*$", output, re.MULTILINE):
        if frame.startswith("node:") or "node_modules" in frame:
            continue
        if os.path.isabs(frame) or os.path.basename(frame) == script_name:
            return None
        return frame[2:] if frame.startswith("./") else frame
    return None


# ---------------------------------------------------------------------------
# Definition lookup (for /ask)
# ---------------------------------------------------------------------------

def _identifier_forms(words: list[str]) -> set[str]:
    base = "_".join(w.lower() for w in words)
    camel = words[0].lower() + "".join(w.capitalize() for w in words[1:])
    return {base, base.upper(), camel, camel[:1].upper() + camel[1:]}


def find_definitions(cwd: str, question: str, limit: int = 8) -> list[str]:
    """"settings.py:1: TAX_RATE = 0.2" for things the question names --
    identifiers written as-is, or phrases like "tax rate" in the usual
    spellings (TAX_RATE, tax_rate, taxRate, TaxRate). Measured: shown the
    right file, the on-device model named TAX_RATE but never gave its
    value; shown the defining line itself, it can just read it off."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9]*", question)
    names = {w for w in re.findall(r"[A-Za-z_$][\w$]*", question) if "_" in w or any(c.isupper() for c in w[1:])}
    for n in (1, 2, 3):
        for i in range(len(words) - n + 1):
            chunk = words[i:i + n]
            if all(len(w) > 2 for w in chunk):
                names |= _identifier_forms(chunk)
    if not names:
        return []
    alternation = "|".join(sorted((re.escape(n) for n in names), key=len, reverse=True))
    pattern = re.compile(
        rf"^[ \t]*(?:export[ \t]+)?(?:(?:const|let|var|def|class|function|func|fn|struct|enum|type)[ \t]+)?"
        rf"(?:{alternation})\b\s*(?:=|:|\(|\{{)", re.MULTILINE
    )
    hits = []
    for rel in project_files(cwd):
        text = read_text(cwd, rel)
        for m in pattern.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            line = text.splitlines()[line_no - 1].strip()
            hits.append(f"{rel}:{line_no}: {line[:160]}")
            if len(hits) >= limit:
                return hits
    return hits
