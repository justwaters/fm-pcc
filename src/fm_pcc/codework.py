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
    py_tests = [f for f in files if f.endswith(".py") and is_test_file(f)]
    if py_tests:
        has_pytest = subprocess.run([py, "-c", "import pytest"], capture_output=True).returncode == 0
        if has_pytest:
            checks.append(("tests", [py, "-m", "pytest", "-q"]))
        else:
            # unittest only descends into packages, so a tests/ folder
            # without an __init__.py needs its own discovery root.
            roots = sorted({
                "." if "/" not in f or os.path.exists(os.path.join(cwd, os.path.dirname(f), "__init__.py"))
                else f.split("/", 1)[0]
                for f in py_tests
            })
            for root in roots:
                # (No -t: with a top level set, unittest refuses a start
                # folder that isn't a package. `python -m` already puts
                # the project root on the import path.)
                checks.append(("tests", [py, "-m", "unittest", "discover", "-q", "-s", root, "-p", "*test*.py"]))
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


_NO_TESTS_RE = re.compile(r"NO TESTS RAN|no tests ran|collected 0 items|no test files", re.I)


def no_tests_ran(output: str) -> bool:
    """A test command that found nothing to run hasn't failed."""
    return bool(_NO_TESTS_RE.search(output))


def local_imports(cwd: str, rel: str, files: list[str]) -> list[str]:
    """Project files that `rel` imports (Python `import x` / `from x import`,
    JS `require('./x')` / `import ... from './x'`, Go/other: none)."""
    text = read_text(cwd, rel)
    names = [a or b for a, b in re.findall(r"^\s*from\s+([\w.]+)\s+import|^\s*import\s+([\w.]+)", text, re.MULTILINE)]
    names += [a or b for a, b in re.findall(
        r"require\(\s*['\"](\.{1,2}/[\w./-]+)['\"]\s*\)|from\s+['\"](\.{1,2}/[\w./-]+)['\"]", text)]
    base = os.path.dirname(rel)
    out = []
    for name in names:
        if name.startswith("."):
            stem = os.path.normpath(os.path.join(base, name))
            cands = [stem, f"{stem}.js", f"{stem}.ts", f"{stem}.mjs", f"{stem}/index.js"]
        else:
            stem = name.replace(".", "/")
            cands = [f"{stem}.py", f"{stem}/__init__.py"]
        for cand in cands:
            if cand in files and cand not in out and cand != rel:
                out.append(cand)
                break
    return out


_FOREIGN_PATH_RE = re.compile(r"/(?:lib/python[\d.]*|site-packages|dist-packages|node_modules|Frameworks|"
                              r"\.pyenv|\.venv|venv|Cellar|go/pkg|\.cargo)/|^<frozen|^node:")


_KEEP_STOP = _STOP | {"with", "that", "this", "into", "then", "when", "calls", "call", "show", "shows", "make",
                      "makes", "value", "values", "list", "item", "items", "name", "names",
                      "new", "return", "returns", "self", "func", "def", "let", "var", "class", "struct", "true",
                      "false", "none", "null", "nil", "string", "int", "bool"}


def requested_additions(request: str, before: str, current: str) -> list[str]:
    """Identifiers the task added that the request asks for by name:
    "...with a button that calls store.increment()" -> `Button`,
    `increment` -- in the code now, not in the file before the task, and
    named in the request (any case). A fix for a failing check must keep
    them: measured, to make SwiftUI code compile, a fix round swapped the
    requested Button for .onTapGesture and the task counted as done."""
    words = {w.lower() for w in re.findall(r"[A-Za-z_]\w{2,}", request)} - _KEEP_STOP
    # Names the request writes as code: store.increment(), `count`, 'x'.
    as_code = {m.lower() for m in re.findall(
        r"(?<=\.)([A-Za-z_]\w*)|([A-Za-z_]\w*)(?=\()|[`'\"]([A-Za-z_]\w*)[`'\"]", request) for m in m if m}
    had = set(re.findall(r"[A-Za-z_]\w*", before))
    out = []
    for ident in dict.fromkeys(re.findall(r"[A-Za-z_]\w*", current)):
        if ident in had or ident.lower() not in words:
            continue
        # Plain lowercase words only when written as code -- seen for real:
        # a botched rename pasted "everything" and "uses" from the request
        # into the code, and keeping them blocked every fix.
        if ident.islower() and "_" not in ident and not any(c.isdigit() for c in ident) and ident.lower() not in as_code:
            continue
        out.append(ident)
    return out


def check_kept(names: list[str], updated: str) -> list[str]:
    """Problems when a rewrite dropped any of `names` (see
    requested_additions)."""
    gone = [n for n in names if not re.search(rf"(?<![\w$]){re.escape(n)}(?![\w$])", updated)]
    if not gone:
        return []
    return [f"the fix removed {', '.join(f'`{n}`' for n in gone)}, which the task asked for -- "
            "fix the error while keeping what was requested"]


_DIAGNOSTIC_RE = re.compile(r"^\S[^\n]*?:\d+(?::\d+)?:\s*(error|warning|note|remark)\b", re.MULTILINE)


def errors_only(output: str) -> str:
    """Compiler output with its warning and note blocks dropped when there
    are errors -- measured: swiftc's "no calls to throwing functions occur
    within 'try'" warnings filled the excerpt the model was shown, and the
    one real error never reached it."""
    starts = [(m.start(), m.group(1)) for m in _DIAGNOSTIC_RE.finditer(output)]
    if not any(kind == "error" for _, kind in starts):
        return output
    kept = [output[:starts[0][0]]]
    for i, (start, kind) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(output)
        if kind == "error":
            kept.append(output[start:end])
    return "".join(kept).strip()


def files_in_output(output: str, candidates: list[str], cwd: str | None = None) -> list[str]:
    """Which of `candidates` a failure's output points at (tracebacks,
    compiler errors), most-mentioned first. Paths inside the language's
    own install (Python's unittest/main.py, node_modules, ...) never count
    -- seen for real: unittest's main.py was taken for the project's."""
    roots = {os.path.realpath(cwd), cwd} if cwd else set()
    roots |= {r.replace("/private/", "/", 1) for r in roots}
    counts: dict[str, int] = {}
    for c in candidates:
        base = re.escape(os.path.basename(c))
        for m in re.finditer(rf"(?:^|(?<=[\s\"'(,]))((?:[^\s\"'(,]*/)?{base})(?=[\s\"':),]|$)", output, re.MULTILINE):
            token = m.group(1)
            if _FOREIGN_PATH_RE.search(token):
                continue
            if token.startswith("/"):
                if not any(token.startswith(r + os.sep) for r in roots) and not token.endswith("/" + c):
                    continue
            elif "/" in token and not (token == c or token.endswith("/" + c) or c.endswith(token.lstrip("./"))):
                continue
            counts[c] = counts.get(c, 0) + 1
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
    if out.startswith("timed out"):
        return False, f"it ran for over {int(timeout)} seconds without finishing (too slow, or stuck in a loop)", "timeout"
    origin = smoke_crash_origin(out, name)
    return origin is None, out, origin


_ENTRY_NAMES = ("main.py", "app.py", "__main__.py", "index.js", "app.js", "main.js", "main.swift")


def entry_points(cwd: str, files: list[str]) -> list[str]:
    """The project's runnable programs, by the usual names, at the top or
    in src/: main.py, app.py, index.js, main.swift... (main.swift only
    without a Package.swift, which `swift build` checks instead). A
    Python file only counts if it runs something at the top level."""
    out = []
    for f in files:
        if os.path.basename(f) not in _ENTRY_NAMES or f.count("/") > 1 or (f.count("/") == 1 and not f.startswith("src/")):
            continue
        if f.endswith(".swift") and (os.path.exists(os.path.join(cwd, "Package.swift")) or not shutil.which("swiftc")):
            continue
        if f.endswith(".js") and not shutil.which("node"):
            continue
        if f.endswith(".py") and not runnable_script(f, read_text(cwd, f)):
            continue
        out.append(f)
    return out


def run_entry(cwd: str, rel: str, timeout: float = 30) -> tuple[bool, str]:
    """Run one of the project's programs on a copy of the project (nothing
    it does can touch the real files): `python main.py`, `node index.js`,
    or main.swift compiled with the Swift files beside it. A program that
    times out (a server, say) counts as not ok."""
    import tempfile

    total = 0
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        total += sum(os.path.getsize(os.path.join(root, f)) for f in files if os.path.isfile(os.path.join(root, f)))
        if total > _SMOKE_MAX_BYTES:
            return False, "project too large to copy"
    with tempfile.TemporaryDirectory(prefix="fm-pcc-run-") as tmp:
        copy = os.path.join(os.path.realpath(tmp), "project")
        shutil.copytree(cwd, copy, ignore=shutil.ignore_patterns(*_SKIP_DIRS, ".*"), symlinks=True)
        if rel.endswith(".swift"):
            folder = os.path.dirname(rel)
            sources = sorted(os.path.join(folder, f) for f in os.listdir(os.path.join(copy, folder or "."))
                             if f.endswith(".swift"))
            ok, out = run_check(["swiftc", *sources, "-o", "_fm_pcc_prog"], copy, timeout=240)
            if ok:
                ok, out = run_check(["./_fm_pcc_prog"], copy, timeout=timeout)
        else:
            argv = [_python(cwd), rel] if rel.endswith(".py") else ["node", rel]
            ok, out = run_check(argv, copy, timeout=timeout)
        for prefix in sorted({copy, copy.replace("/private/", "/", 1)}, key=len, reverse=True):
            out = out.replace(prefix + os.sep, "").replace(prefix, ".")
    return ok, out


def missing_import_fix(cwd: str, output: str, files: list[str]) -> tuple[str, str, str] | None:
    """(file, before, after) adding the import a crash says is missing,
    when exactly one project module defines the name: Python's
    "NameError: name 'User' is not defined" in users.py, with `class User`
    in models.py, gets `from models import User`; JavaScript's
    "ReferenceError: formatTodo is not defined" gets a require of the
    module exporting it. Measured: the model was asked three times to fix
    exactly this and never added the import. None when it isn't that."""
    py = re.search(r"NameError: name '([A-Za-z_]\w*)' is not defined", output)
    js = re.search(r"ReferenceError: ([A-Za-z_$][\w$]*) is not defined", output)
    if py:
        name = py.group(1)
        frames = re.findall(r'File "([^"]+\.py)", line \d+', output[:py.start()])
        crashed = next((f for f in reversed(frames) if f in files), None)
        if not crashed:
            return None
        homes = [f for f in files if f.endswith(".py") and f != crashed and not is_test_file(f)
                 and re.search(rf"^(?:class|def|async def)\s+{re.escape(name)}\b|^{re.escape(name)}\s*=",
                               read_text(cwd, f), re.MULTILINE)]
        if len(homes) != 1:
            return None
        module = homes[0][:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        line = f"from {module} import {name}"
    elif js:
        name = js.group(1)
        located = re.findall(r"([\w./-]+\.(?:js|cjs|mjs)):\d+", output)
        crashed = next((f for f in (os.path.relpath(p, cwd) if os.path.isabs(p) else p for p in located)
                        if f in files), None)
        if not crashed:
            return None
        homes = [f for f in files if f.endswith((".js", ".cjs")) and f != crashed
                 and re.search(rf"module\.exports\s*=\s*\{{[^}}]*\b{re.escape(name)}\b|exports\.{re.escape(name)}\s*=",
                               read_text(cwd, f))]
        if len(homes) != 1:
            return None
        rel = os.path.relpath(os.path.splitext(homes[0])[0], os.path.dirname(crashed) or ".")
        line = f"const {{ {name} }} = require('{rel if rel.startswith('.') else './' + rel}');"
    else:
        return None
    before = read_text(cwd, crashed)
    if line in before:
        return None
    lines = before.splitlines()
    # after the imports at the top
    at = 0
    for i, text in enumerate(lines):
        if re.match(r"^(?:import\s|from\s+\S+\s+import\s|(?:const|let|var)\s+.*=\s*require\()", text):
            at = i + 1
        elif text.strip() and at:
            break
    lines.insert(at, line)
    return crashed, before, "\n".join(lines) + ("\n" if before.endswith("\n") else "")


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


_DEFINES_RE = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:public[ \t]+|private[ \t]+|final[ \t]+|static[ \t]+|async[ \t]+)*"
    r"(?:def|class|function|func|fn|struct|enum|protocol|interface|type|extension)[ \t]+([A-Za-z_$][\w$]*)"
    r"|^(?:export[ \t]+)?(?:const|let|var)[ \t]+([A-Za-z_$][\w$]*)[ \t]*(?::[^=\n]+)?=",
    re.MULTILINE,
)


def defined_names(text: str) -> set[str]:
    """Names a file defines: functions, classes, structs, methods, and
    top-level constants."""
    return {a or b for a, b in _DEFINES_RE.findall(text)}


def files_touched(cwd: str, request: str, files: list[str]) -> dict[str, list[str]]:
    """The project files a change request involves, each with the names
    from the request it defines: files it names ("main.py"), files that
    define the code it names ("Item", "Store.add", "createTodo" -> the
    files defining them), and files whose name it uses as a word ("the
    report" -> report.py). Test files only when the request is about
    tests. Measured on threaded changes ("add a category to inventory
    items: Item gets a field, Store.add takes it, the report shows it"),
    the planner edited one or two of the four files involved."""
    about_tests = re.search(r"\btests?\b", request, re.IGNORECASE)
    candidates = [f for f in files if os.path.splitext(f)[1] in CODE_EXTS and (about_tests or not is_test_file(f))
                  and os.path.splitext(f)[1] not in (".md", ".json", ".toml", ".yml", ".yaml")]
    out: dict[str, list[str]] = {}
    # Text the request asks to write isn't a reference to a file: "add a
    # line to the readme saying Run main.py to start" doesn't involve
    # main.py (seen for real: main.py got edited too).
    request = re.sub(r"\b(?:saying|that\s+says|which\s+says|to\s+say|reading|with\s+the\s+(?:text|words|message))\b.*$",
                     "", request, flags=re.IGNORECASE)
    # (a quote inside a word is an apostrophe: "item's price ... 'Discount'")
    request = re.sub(r"(?<![\w])'[^']*'(?![\w])|\"[^\"]*\"|`[^`]*`", " ", request)
    for f in taskplan.mentioned_files(request, candidates):
        out.setdefault(f, [])
    # code-like names: CamelCase, snake_case, dotted (Store.add), or calls
    named = set()
    for token in re.findall(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*(?:\(\))?", request):
        parts = token.rstrip("()").split(".")
        for part in parts:
            if len(part) > 2 and ("_" in part or any(c.isupper() for c in part) or token.endswith("()")
                                  or len(parts) > 1):
                named.add(part)
    words = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", request)}
    for f in candidates:
        text = read_text(cwd, f)
        defined = defined_names(text)
        hits = sorted(n for n in named if n in defined)
        stem = os.path.splitext(os.path.basename(f))[0].lower()
        if hits:
            out.setdefault(f, []).extend(h for h in hits if h not in out[f])
        elif stem in words and stem not in ("main", "index", "app", "utils", "util", "helpers", "common", "init"):
            out.setdefault(f, [])
    return out


def request_part_for(request: str, rel: str, names: list[str] = ()) -> str:
    """The clauses of a multi-file request about one file: those naming
    the file, its stem as a word ("the report"), or code it defines.
    "...the report adds a line 'Discount: 10%' when one was applied, and
    main.py applies 10% before printing" -> for main.py, "main.py applies
    10% before printing". Empty when no clause singles it out."""
    # Commas and semicolons only: "add and sub functions" is one thing.
    clauses = [c.strip() for c in re.split(r";\s*|,\s*(?:and\s+)?", request) if c and c.strip()]
    base = os.path.basename(rel)
    stem = os.path.splitext(base)[0]
    keys = [re.escape(base), rf"\b{re.escape(stem)}\b"] + [rf"\b{re.escape(n)}\b" for n in names]
    hits = [c for c in clauses if any(re.search(k, c, re.IGNORECASE) for k in keys)]
    return "; ".join(dict.fromkeys(hits)) if hits and len(hits) < len(clauses) else ""


def dependency_order(cwd: str, paths: list[str]) -> list[str]:
    """`paths` with each file after the files whose definitions it uses
    (models before the store that builds them, before the report and the
    main script that use those), so each edit sees what the edits before
    it changed. Ties keep their given order."""
    texts = {p: read_text(cwd, p) for p in paths}
    # Attributes count too (self.items, a dataclass's `name: str`, a Swift
    # `let title`): a report that only reads store.items and item.name
    # still depends on the files that define them.
    attrs = re.compile(r"^[ \t]+(?:self\.|this\.)?([A-Za-z_]\w{2,})[ \t]*(?::[^=\n]+)?=|"
                       r"^[ \t]+([A-Za-z_]\w{2,})[ \t]*:[ \t]*[A-Za-z_\[]|"
                       r"^[ \t]+(?:private(?:\(set\))?[ \t]+)?(?:let|var)[ \t]+([A-Za-z_]\w{2,})", re.MULTILINE)
    defs = {p: defined_names(t) | {a or b or c for a, b, c in attrs.findall(t)} for p, t in texts.items()}
    deps = {p: {q for q in paths if q != p and any(re.search(rf"(?<![\w$]){re.escape(n)}(?![\w$])", texts[p])
                                                   for n in defs[q])} for p in paths}
    out: list[str] = []
    visiting: set[str] = set()

    def visit(p: str) -> None:
        if p in out or p in visiting:
            return
        visiting.add(p)
        for q in paths:
            if q in deps[p]:
                visit(q)
        visiting.discard(p)
        out.append(p)

    for p in paths:
        visit(p)
    return out


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
    # Second pass: settings/constants whose name is mostly the question's
    # own words, in any order ("the base backoff delay in seconds" finds
    # BACKOFF_BASE_SECONDS = 2; "how many times does it retry" finds
    # MAX_RETRIES = 5).
    stems = {w.lower()[:5] for w in words if len(w) > 2}
    assign = re.compile(r"^[ \t]*(?:export[ \t]+)?(?:(?:const|let|var|final|static)[ \t]+)?([A-Za-z_][\w]*)[ \t]*(?::[^=\n]+)?=[^=]", re.MULTILINE)
    hits = []
    for rel in project_files(cwd):
        text = read_text(cwd, rel)
        lines = text.splitlines()
        seen_lines = set()
        for m in pattern.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            seen_lines.add(line_no)
            hits.append(f"{rel}:{line_no}: {lines[line_no - 1].strip()[:160]}")
            if len(hits) >= limit:
                return hits
        for m in assign.finditer(text):
            parts = [p.lower() for p in re.split(r"_|(?<=[a-z])(?=[A-Z])", m.group(1)) if len(p) > 1]
            matched = [p for p in parts if any(len(os.path.commonprefix([p, st])) >= min(4, len(p), len(st)) for st in stems)]
            if matched and len(matched) * 2 >= len(parts):
                line_no = text.count("\n", 0, m.start()) + 1
                if line_no in seen_lines:
                    continue
                seen_lines.add(line_no)
                hits.append(f"{rel}:{line_no}: {lines[line_no - 1].strip()[:160]}")
                if len(hits) >= limit:
                    return hits
    return hits


def excerpt_for(cwd: str, rel: str, request: str, limit: int = 2500) -> str:
    """A file as a model should see it for `request` within `limit`
    characters: all of it if it fits, otherwise the functions the request
    names in full plus an outline of the rest (a 10 KB file's first 2.5 KB
    missed the function that had just changed)."""
    text = read_text(cwd, rel)
    if len(text) <= limit:
        return text
    names = set(re.findall(r"[A-Za-z_$][\w$]*", request))
    lines = text.splitlines()
    parts = ["\n".join(lines[s:e]) for n, s, e in function_blocks(rel, text) if n in names]
    body = "\n\n".join(parts)[:limit]
    sig = "\n".join(outline(rel, text))
    return (f"{body}\n\n(other definitions:)\n{sig}" if body else sig or text[:limit])[: limit + 1500]


# ---------------------------------------------------------------------------
# Definition bookkeeping (checks on a code rewrite)
# ---------------------------------------------------------------------------

def top_level_names(rel: str, text: str) -> set[str]:
    """Names of top-level functions and classes (Python), or functions
    found by function_blocks (brace languages, Go)."""
    if rel.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return set()
        return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    names = {n for n, _s, _e in function_blocks(rel, text)}
    names |= set(re.findall(r"^\s*(?:export\s+)?class\s+([\w$]+)", text, re.MULTILINE))
    names |= {n for n, _s, _e in type_blocks(text)}
    return names


_TYPE_DECL_RE = re.compile(
    r"^(?:@\w+(?:\([^)]*\))?\s+)*(?:export\s+)?(?:public\s+|internal\s+|private\s+|fileprivate\s+|open\s+|final\s+|"
    r"abstract\s+|default\s+)*(?:struct|class|enum|protocol|interface|actor)\s+([A-Za-z_$][\w$]*)[^\n{]*\{",
)


def type_blocks(text: str) -> list[tuple[str, int, int]]:
    """Top-level type declarations in a brace language -- Swift structs,
    classes, enums, protocols; JS/TS classes and interfaces -- as (name,
    first line, end line), 0-based [start, end), attributes above them
    included."""
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        m = _TYPE_DECL_RE.match(lines[i])
        if not m:
            i += 1
            continue
        start = i
        while start > 0 and re.match(r"^@\w+", lines[start - 1]):
            start -= 1
        depth, j, opened = 0, i, False
        while j < len(lines):
            code = re.sub(r'"(?:\\.|[^"\\])*"', '""', lines[j].split("//")[0])
            depth += code.count("{") - code.count("}")
            opened = opened or "{" in code
            j += 1
            if opened and depth <= 0:
                break
        out.append((m.group(1), start, j))
        i = j
    return out


_REMOVING_RE = re.compile(
    r"\b(?:remove|delete|drop|rename|replace|refactor|convert|move|merge|split|extract|inline|rewrite|"
    r"consolidate|combine|get\s+rid\s+of|instead\s+of)\b", re.I
)


def check_definitions(rel: str, instructions: str, original: str, updated: str,
                      elsewhere: set[str] = frozenset()) -> list[str]:
    """Code-rewrite problems a test suite might not catch:
    - a definition that existed before is gone, when the request didn't
      ask to remove/rename/refactor anything (seen for real: asked to add
      f_to_c, the model replaced c_to_f);
    - a definition copied in from another file (seen for real: TestMax
      copied from calc_test.go into calc.go, which Go refuses to build)."""
    before, after = top_level_names(rel, original), top_level_names(rel, updated)
    problems = []
    if not _REMOVING_RE.search(instructions):
        lost = sorted(before - after)
        if lost:
            problems.append(f"existing definitions were removed: {', '.join(lost[:5])} -- keep them and only add or change what was asked")
    copied = sorted((after - before) & set(elsewhere))
    if copied:
        problems.append(f"{', '.join(copied[:5])} already exists in another file -- don't copy it here")
    return problems


def drop_copied_statements(cwd: str, rel: str, before: str, text: str) -> str:
    """For a Swift file other than main.swift: remove top-level lines the
    model copied in from another Swift file beside it (and the block each
    opens). Only main.swift may run statements at the top level -- seen
    for real: main.swift's `let store = TodoStore()` and its `for` loop
    pasted into Models.swift ("statements are not allowed at the top
    level", "invalid redeclaration of 'store'")."""
    folder = os.path.dirname(rel)
    others = set()
    for f in project_files(cwd):
        if f != rel and f.endswith(".swift") and os.path.dirname(f) == folder:
            others |= {l.strip() for l in read_text(cwd, f).splitlines() if l.strip()}
    had = set(before.splitlines())
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line and not line[0].isspace() and line not in had and line.strip() in others \
                and re.search(r"[A-Za-z]\w*.*[A-Za-z(=]", line) and not re.match(
                r"(?:import|@|struct|class|enum|protocol|extension|actor|typealias|func|public|private|final)\b", line):
            depth = line.count("{") - line.count("}")
            i += 1
            while depth > 0 and i < len(lines):
                depth += lines[i].count("{") - lines[i].count("}")
                i += 1
            continue
        out.append(line)
        i += 1
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).rstrip("\n")
    return result + ("\n" if text.endswith("\n") else "")


def check_not_copied(cwd: str, rel: str, before: str, updated: str) -> list[str]:
    """For an edit the request may not need (a file it only mentions):
    a problem when what it added is mostly lines copied verbatim from
    other project files -- seen for real: asked to "return a User from
    models.py", the optional edit of models.py pasted app.py's code into
    it, and the circular import broke `import users`."""
    had = set(before.splitlines())
    added = [l.strip() for l in updated.splitlines() if l.strip() and l not in had and len(l.strip()) > 3]
    if len(added) < 2:
        return []
    elsewhere = set()
    for f in project_files(cwd):
        if f != rel:
            elsewhere |= {l.strip() for l in read_text(cwd, f).splitlines()}
    copied = sum(1 for l in added if l in elsewhere)
    if copied / len(added) >= 0.6:
        return [f"this edit mostly copies code from other files into {rel} -- if {rel} itself needs no change "
                "for the request, return it unchanged"]
    return []


_STUB_BODY_RE = re.compile(
    r"^\s*(?:raise\s+NotImplementedError\b.*|pass|\.\.\.|#\s*TODO.*|//\s*TODO.*|"
    r"throw\s+new\s+Error\(\s*['\"`][^'\"`]*(?:not\s+implemented|todo)[^'\"`]*['\"`]\s*\);?|"
    r"fatalError\(.*\)|return\s+(?:None|null|undefined)?;?|[\"']{3}.*[\"']{3})\s*$",
    re.IGNORECASE,
)


def stub_functions(rel: str, text: str) -> dict[str, tuple[int, int]]:
    """Functions whose body is only a placeholder (`raise
    NotImplementedError`, `pass`, `...`, a TODO, `throw new Error('not
    implemented')`), as {name: (start, end)} line ranges."""
    lines = text.splitlines()
    out = {}
    for name, start, end in function_blocks(rel, text):
        body = [l for l in lines[start + 1:end] if l.strip() and l.strip() not in ("}", "};")]
        if body and all(_STUB_BODY_RE.match(l) for l in body):
            out[name] = (start, end)
    return out


_LITERAL = r"""(?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|True|False|None|true|false|null|\[[^\[\]]*\]|\{[^{}]*\})"""
_GIVES = r"(?:gives|returns|should\s+(?:give|return|be)|becomes|is|equals|==|->|=>|→)"
_CALL_EXAMPLE_RE = re.compile(rf"(?P<call>[A-Za-z_$][\w$]*\([^()]*\))\s+(?:still\s+)?{_GIVES}\s+(?P<want>{_LITERAL})")
_ARG_EXAMPLE_RE = re.compile(rf"(?<![\w(])(?P<arg>{_LITERAL})\s+{_GIVES}\s+(?P<want>{_LITERAL})")
_PRINTS_RE = re.compile(r"\b(?:should\s+)?(?:print|output|show|display)s?\s+(?P<want>'[^']+'|\"[^\"]+\")")
_INPUTS_RE = re.compile(rf"\b(?:like|such\s+as|e\.g\.|for\s+example|inputs?\s+like)\s+(?P<list>{_LITERAL}(?:\s*,\s*(?:or\s+|and\s+)?{_LITERAL}|\s+(?:or|and)\s+{_LITERAL})*)")


def _literal(text: str):
    """A Python value for a literal written in a request (JS spellings
    too: true/false/null); raises ValueError if it isn't one."""
    text = {"true": "True", "false": "False", "null": "None"}.get(text, text)
    return ast.literal_eval(text)


def request_examples(request: str, functions: dict[str, int]) -> dict:
    """What a request says the code should do, in checkable form:
    - "calls": [(call, expected)] -- "apply_discount(50, 150) gives 0";
      or, for the one changed function taking one argument, "'1h30m'
      gives 90" / "1994 is 'MCMXCIV'" -> ("to_roman(1994)", "MCMXCIV");
    - "inputs": calls that must at least not crash -- "strings like
      '1h30m', '45m' or '2h'";
    - "prints": text the program's output must contain -- "it should
      print 'Total: $10.80'".
    `functions` maps the changed functions to their parameter counts.
    Only what the request itself spells out; nothing is guessed."""
    calls, inputs, prints = [], [], []
    single = [n for n, k in functions.items() if k == 1]
    for m in _CALL_EXAMPLE_RE.finditer(request):
        name = m.group("call").split("(")[0]
        if name not in functions:
            continue
        try:
            calls.append((m.group("call"), _literal(m.group("want"))))
        except (ValueError, SyntaxError):
            continue
    if len(single) == 1:
        taken = {m.span() for m in _CALL_EXAMPLE_RE.finditer(request)}
        for m in _ARG_EXAMPLE_RE.finditer(request):
            if any(a <= m.start() < b for a, b in taken):
                continue
            try:
                _literal(m.group("arg"))
                want = _literal(m.group("want"))
            except (ValueError, SyntaxError):
                continue
            calls.append((f"{single[0]}({m.group('arg')})", want))
        for m in _INPUTS_RE.finditer(request):
            for lit in re.findall(_LITERAL, m.group("list")):
                try:
                    _literal(lit)
                except (ValueError, SyntaxError):
                    continue
                call = f"{single[0]}({lit})"
                if call not in [c for c, _ in calls]:
                    inputs.append(call)
    for m in _PRINTS_RE.finditer(request):
        prints.append(_literal(m.group("want")))
    return {"calls": _dedupe(calls), "inputs": list(dict.fromkeys(inputs)), "prints": prints}


def _dedupe(pairs):
    out = []
    for p in pairs:
        if p not in out:
            out.append(p)
    return out


def function_params(rel: str, text: str, names: set[str]) -> dict[str, int]:
    """{name: number of parameters} for the functions `names` in a file."""
    out = {}
    if rel.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return out
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
                out[node.name] = len(node.args.args) - (1 if node.args.args and node.args.args[0].arg == "self" else 0)
        return out
    for name in names:
        m = re.search(rf"(?:function\s+{re.escape(name)}\s*|{re.escape(name)}\s*=\s*(?:async\s*)?(?:function\s*)?)\(([^)]*)\)", text)
        if m:
            out[name] = len([p for p in m.group(1).split(",") if p.strip()])
    return out


def examples_script(rel: str, examples: dict) -> tuple[str, str] | None:
    """(language, script) that runs the request's examples against the
    module `rel`: prints each mismatch ("parse_duration('2h') raised
    ValueError: ..., the request says it should give 120") and exits 1.
    None when there's nothing to run."""
    if not examples["calls"] and not examples["inputs"]:
        return None
    if rel.endswith(".py"):
        module = os.path.splitext(rel)[0].replace("/", ".")
        lines = ["import math, sys", f"from {module} import *", "_bad = []",
                 "def _same(a, b):",
                 "    if isinstance(a, float) or isinstance(b, float):",
                 "        try:\n            return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)\n        except TypeError:\n            return False",
                 "    return a == b"]
        for call, want in examples["calls"]:
            lines.append(f"try:\n    _got = {call}\n    if not _same(_got, {want!r}):\n"
                         f"        _bad.append({call!r} + ' returned ' + repr(_got) + ', but the request says it should give ' + {repr(want)!r})\n"
                         f"except Exception as _e:\n    _bad.append({call!r} + ' raised ' + type(_e).__name__ + ': ' + str(_e) + "
                         f"', but the request says it should give ' + {repr(want)!r})")
        for call in examples["inputs"]:
            lines.append(f"try:\n    {call}\nexcept Exception as _e:\n"
                         f"    _bad.append({call!r} + ' raised ' + type(_e).__name__ + ': ' + str(_e) + ', but the request says it takes inputs like that')")
        lines.append("if _bad:\n    print('\\n'.join(_bad))\n    sys.exit(1)")
        return "python", "\n".join(lines) + "\n"
    if rel.endswith((".js", ".cjs")):
        path = "./" + rel
        lines = [f"const _m = require({json.dumps(path)});", "const _bad = [];",
                 "const _same = (a, b) => JSON.stringify(a) === JSON.stringify(b);"]
        for call, want in examples["calls"]:
            lines.append(f"try {{ const _got = _m.{call}; if (!_same(_got, {json.dumps(want)})) _bad.push({json.dumps(call)} + "
                         f"' returned ' + JSON.stringify(_got) + ', but the request says it should give ' + {json.dumps(json.dumps(want))}); }} "
                         f"catch (_e) {{ _bad.push({json.dumps(call)} + ' threw ' + _e + ', but the request says it should give ' + {json.dumps(json.dumps(want))}); }}")
        for call in examples["inputs"]:
            lines.append(f"try {{ _m.{call}; }} catch (_e) {{ _bad.push({json.dumps(call)} + ' threw ' + _e + "
                         f"', but the request says it takes inputs like that'); }}")
        lines.append("if (_bad.length) { console.log(_bad.join('\\n')); process.exit(1); }")
        return "javascript", "\n".join(lines) + "\n"
    return None


def run_examples(cwd: str, rel: str, examples: dict, timeout: float = 20) -> tuple[bool, str]:
    """Run the request's examples for `rel` on a copy of the project."""
    made = examples_script(rel, examples)
    if not made:
        return True, ""
    lang, script = made
    import tempfile

    with tempfile.TemporaryDirectory(prefix="fm-pcc-examples-") as tmp:
        copy = os.path.join(os.path.realpath(tmp), "project")
        shutil.copytree(cwd, copy, ignore=shutil.ignore_patterns(*_SKIP_DIRS, ".*"), symlinks=True)
        name = "_fm_pcc_examples.py" if lang == "python" else "_fm_pcc_examples.js"
        with open(os.path.join(copy, name), "w") as f:
            f.write(script)
        argv = [_python(cwd), name] if lang == "python" else ["node", name]
        ok, out = run_check(argv, copy, timeout=timeout)
        for prefix in sorted({copy, copy.replace("/private/", "/", 1)}, key=len, reverse=True):
            out = out.replace(prefix + os.sep, "").replace(prefix, ".")
    if out.startswith("timed out"):
        return False, "the request's examples ran for over 20 seconds without finishing (too slow, or stuck in a loop)"
    return ok, out


def undefined_names(rel: str, text: str, functions: set[str]) -> list[tuple[str, str]]:
    """(function, name) for names the Python functions `functions` use
    that nothing in the file defines -- not a parameter, local, import,
    global, class, or builtin -- so calling them crashes with a NameError.
    Measured: a cache written as `rates.rates[currency]` inside rates.py
    passed the syntax check and the smoke run (which never reached it)."""
    if not rel.endswith(".py"):
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    import builtins

    defined = set(dir(builtins)) | {"__name__", "__file__", "__doc__", "__builtins__", "__spec__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if any(a.name == "*" for a in node.names):
                return []        # a star import could define anything
            defined |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            defined |= set(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, ast.MatchAs) and node.name:
            defined.add(node.name)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load) and inner.id not in defined:
                    if (node.name, inner.id) not in out:
                        out.append((node.name, inner.id))
    return out


def names_defined_elsewhere(cwd: str, rel: str) -> set[str]:
    """Top-level names defined in other files where a duplicate would
    clash: the same directory for Go (one package), and test functions
    anywhere (never meant to be copied into code)."""
    ext = os.path.splitext(rel)[1]
    out: set[str] = set()
    for other in project_files(cwd):
        if other == rel or os.path.splitext(other)[1] != ext:
            continue
        names = top_level_names(other, read_text(cwd, other))
        if ext == ".go" and os.path.dirname(other) == os.path.dirname(rel):
            out |= names
        elif ext == ".swift" and os.path.dirname(other) == os.path.dirname(rel):
            # One module, one namespace: a type or top-level function copied
            # in from a file beside it won't compile ("invalid redeclaration
            # of 'TodoStore'"). Types and top-level functions only -- a
            # method sharing a name with one elsewhere is fine.
            text = read_text(cwd, other)
            out |= {n for n, _s, _e in type_blocks(text)}
            out |= set(re.findall(r"^(?:public\s+|private\s+)?func\s+([A-Za-z_]\w*)", text, re.MULTILINE))
        elif is_test_file(other):
            out |= {n for n in names if n.lower().startswith("test")}
    return out


# ---------------------------------------------------------------------------
# Documented examples as checks
# ---------------------------------------------------------------------------

_ARROW_EXAMPLE_RE = re.compile(r"""((?:'[^']*'|"[^"]*"|-?\d+(?:\.\d+)?|\[[^\]]*\]|\([^)]*\)))\s*(?:->|=>|→)\s*"""
                               r"""('[^']*'|"[^"]*"|-?\d+(?:\.\d+)?|True|False|None|\[[^\]]*\]|\{[^}]*\})""")


def docstring_examples_script(rel: str, text: str, names: set[str]) -> str | None:
    """A script that checks the documented examples of Python functions in
    `names`: real doctests (>>>), and "input -> output" pairs in a
    docstring ("'1h30m' -> 90, '45m' -> 45"). Prints a mismatch and exits
    1 if the code disagrees with its own documentation. None if there are
    no examples to check."""
    if not rel.endswith(".py"):
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    module = os.path.splitext(rel)[0].replace("/", ".")
    checks = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name not in names:
            continue
        doc = ast.get_docstring(node) or ""
        if ">>>" in doc:
            checks.append(f"_doctest({node.name!r})")
            continue
        if len(node.args.args) != 1:
            continue
        for arg, want in _ARROW_EXAMPLE_RE.findall(doc):
            checks.append(f"_check({node.name!r}, {arg}, {want})")
    if not checks:
        return None
    return (
        "import doctest, sys\n"
        f"import {module} as _m\n"
        "_bad = []\n"
        "def _check(name, arg, want):\n"
        "    got = getattr(_m, name)(arg)\n"
        "    if got != want:\n"
        "        _bad.append(f'{name}({arg!r}) returned {got!r}, but its docstring says {want!r}')\n"
        "def _doctest(name):\n"
        "    f = getattr(_m, name)\n"
        "    r = doctest.run_docstring_examples(f, {**vars(_m)}, name=name, verbose=False)\n"
        + "".join(f"{c}\n" for c in checks)
        + "if _bad:\n"
        "    print('\\n'.join(_bad))\n"
        "    sys.exit(1)\n"
    )


def runnable_script(rel: str, text: str) -> bool:
    """A standalone Python script that can simply be run: it has top-level
    code or a __main__ block, and needs no arguments or input."""
    if not rel.endswith(".py") or is_test_file(rel):
        return False
    if re.search(r"\bsys\.argv\b|\bargparse\b|\binput\(|\bclick\b|\btyper\b|while\s+True", text):
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    return any(
        not isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))
        and not (isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant))
        for n in tree.body
    )


def changed_functions(rel: str, before: str, after: str) -> set[str]:
    """Names of functions whose text differs between two versions of a
    file (including ones that are new)."""
    def blocks(text):
        lines = text.splitlines()
        return {n: "\n".join(lines[s:e]) for n, s, e in function_blocks(rel, text)}
    old, new = blocks(before), blocks(after)
    return {n for n, body in new.items() if old.get(n) != body}


def drop_definitions(rel: str, text: str, names: set[str]) -> str:
    """Remove the top-level definitions `names` from `text` -- used to undo
    a model copying a definition in from another file (it kept copying a
    Go test function into the package, even when told not to)."""
    lines = text.splitlines()
    ranges = [(s, e) for n, s, e in function_blocks(rel, text) + type_blocks(text) if n in names]
    if not ranges:
        return text
    drop = set()
    for s, e in ranges:
        drop.update(range(s, e))
    kept = [l for i, l in enumerate(lines) if i not in drop]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).rstrip("\n")
    return _drop_unused_imports(rel, out) + ("\n" if text.endswith("\n") else "")


def _drop_unused_imports(rel: str, text: str) -> str:
    """After removing code, remove imports nothing uses any more -- Go
    refuses to build with one ("testing" imported and not used)."""
    if rel.endswith(".go"):
        def used(pkg: str) -> bool:
            name = pkg.rsplit("/", 1)[-1]
            body = re.sub(r"(?s)^import\s*\(.*?\)|^import\s+\"[^\"]+\"", "", text, flags=re.MULTILINE)
            return re.search(rf"\b{re.escape(name)}\.", body) is not None
        text = re.sub(r'^import\s+"([^"]+)"\n?', lambda m: m.group(0) if used(m.group(1)) else "", text, flags=re.MULTILINE)
        def block(m):
            pkgs = re.findall(r'^\s*(?:\w+\s+)?"([^"]+)"', m.group(1), re.MULTILINE)
            keep = [p for p in pkgs if used(p)]
            if not keep:
                return ""
            return "import (\n" + "".join(f'\t"{p}"\n' for p in keep) + ")"
        text = re.sub(r"(?s)^import\s*\((.*?)\)", block, text, flags=re.MULTILINE)
        return re.sub(r"\n{3,}", "\n\n", text)
    if rel.endswith(".py"):
        lines = text.splitlines()
        body = "\n".join(l for l in lines if not re.match(r"(?:import|from)\s", l))
        out = []
        for l in lines:
            m = re.match(r"import\s+([\w.]+)(?:\s+as\s+(\w+))?\s*$", l)
            if m and not re.search(rf"\b{re.escape(m.group(2) or m.group(1).split('.')[0])}\b", body):
                continue
            out.append(l)
        return "\n".join(out)
    return text


def crash_function(output: str) -> str | None:
    """The function a Python/Node crash happened in (innermost frame)."""
    py = re.findall(r'File "[^"]+", line \d+, in ([\w<>]+)', output)
    if py:
        return py[-1]
    js = re.findall(r"^\s*at\s+(?:\S+\.)?([\w$]+)\s+\(", output, re.MULTILINE)
    return js[0] if js else None


_FAILED_TEST_RE = re.compile(
    r"^(?:FAIL|ERROR): (\w+) \(([\w.]+)\)"            # unittest
    r"|^FAILED ([\w/.:\[\]-]+)"                        # pytest -q summary
    r"|^--- FAIL: (\w+)"                              # go test
    r"|^not ok \d+ - (.+)$"                           # TAP / node --test
    r"|^\s*✖ (.+?)(?: \(\d+(?:\.\d+)?ms\))?$",        # node --test spec reporter
    re.MULTILINE,
)


def failing_tests(output: str) -> set[str]:
    """Identifiers of the tests a test run reports as failing."""
    return {next(g for g in m.groups() if g) + (f" ({m.group(2)})" if m.group(1) else "")
            for m in _FAILED_TEST_RE.finditer(output)}
