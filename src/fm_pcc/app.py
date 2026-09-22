"""Chat TUI for Apple's on-device and Cloud Pro Foundation Models.

On-device goes through the `fm` CLI, using its own --resume/--save-transcript
flags for real multi-turn context. Cloud Pro has no public CLI/API yet, so it
shells out to a saved Shortcut (Receive input -> Use Cloud Pro model, bound to
Shortcut Input -> Stop and Output Response) via `shortcuts run`, stripping the
RTF it returns. Shortcuts has no scriptable session concept, so multi-turn
context there is approximated by re-sending prior turns as plain text.

The first time Cloud Pro is actually used, if the bridge shortcut isn't
installed yet, this opens its iCloud share link so the person can add it
through the normal Shortcuts "Add Shortcut" confirmation -- there's no way
(or business) silently writing into the Shortcuts library without that.
"""
import argparse
import difflib
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Callable

from rich.console import Group
from rich.markdown import Markdown
from rich.markup import escape as rich_escape
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from . import __version__

MODEL_LABELS = {
    "on-device": "on-device",
    "cloud": "cloud",
    "cloud-pro": "cloud pro",
    "ollama": "ollama",
}
MODEL_COLORS = {
    "on-device": "#f0b429",
    "cloud": "#20c997",
    "cloud-pro": "#38d9c9",
    "ollama": "#9775fa",
}
MODEL_ORDER = ["on-device", "cloud", "cloud-pro", "ollama"]


def model_label(model: str) -> str:
    """Display label for a model id -- including a specific "ollama:<tag>"."""
    if model in MODEL_LABELS:
        return MODEL_LABELS[model]
    if model.startswith("ollama:"):
        return model.split(":", 1)[1]
    return model


def model_color(model: str) -> str:
    """Accent color for a model id -- any "ollama:<tag>" gets ollama's color."""
    if model in MODEL_COLORS:
        return MODEL_COLORS[model]
    if model.startswith("ollama:"):
        return MODEL_COLORS["ollama"]
    return MODEL_COLORS["on-device"]


def model_family(model: str) -> str:
    """Collapse a specific "ollama:<tag>" back to the general "ollama" family."""
    return "ollama" if model.startswith("ollama:") else model


STATUS_ACCENTS = {
    "on-device": "#f0b429",
    "pcc": "#38d9c9",
    "ollama": "#9775fa",
}


def status_accent(model: str) -> str:
    """Coarser 3-way accent for the statusline: on-device, pcc (cloud and
    cloud-pro share one color here), or ollama -- unlike model_color(),
    which distinguishes all four model tiers individually for the input
    border and prompt glyph.
    """
    family = model_family(model)
    if family in ("cloud", "cloud-pro"):
        return STATUS_ACCENTS["pcc"]
    return STATUS_ACCENTS.get(family, STATUS_ACCENTS["on-device"])

# Each of these is a "Use Model" Shortcut (Receive input -> Use <tier> model,
# bound to Shortcut Input -> Stop and Output Response) sharing the same
# shape, just pointed at a different model tier.
CLOUD_SHORTCUTS = {
    "cloud": {
        "name": "PCC-Cloud",
        "url": "https://www.icloud.com/shortcuts/9f4e45968b974ef7ad8d29eb06f98a9b",
    },
    "cloud-pro": {
        "name": "PCC-CloudPro",
        "url": "https://www.icloud.com/shortcuts/7f7f8e41dfee459a89c28ca0b8c60984",
    },
}

_FILE_REF_RE = re.compile(r"@(\S+)")
MAX_FILE_CHARS = 8000
README_NAMES = ["README.md", "README.rst", "README.txt", "README", "readme.md", "Readme.md"]


def find_readme(cwd: str) -> str | None:
    for name in README_NAMES:
        path = os.path.join(cwd, name)
        if os.path.isfile(path):
            return path
    return None


def expand_file_references(prompt: str, cwd: str) -> tuple[str, list[str]]:
    """Inline the contents of any @path/to/file mentions found in `prompt`.

    Paths are resolved relative to `cwd` unless already absolute (~ is
    expanded too). `@readme` is a special alias that resolves to whichever
    actual README variant exists in `cwd` (README.md, README, etc.), since
    the real filename/extension varies by project. Mentions that don't
    resolve to a readable file are left alone -- so stray "@" text (an
    email, a handle) is harmless. Returns the prompt with file contents
    appended, and the list of resolved labels for display.
    """
    attachments: list[str] = []
    seen: set[str] = set()
    extra = ""

    for ref in _FILE_REF_RE.findall(prompt):
        if ref.lower() == "readme":
            path = find_readme(cwd)
            if path is None:
                continue
        else:
            path = os.path.expanduser(ref)
            if not os.path.isabs(path):
                path = os.path.join(cwd, path)
            path = os.path.normpath(path)

        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)

        try:
            with open(path, "r", errors="replace") as f:
                content = f.read(MAX_FILE_CHARS + 1)
        except OSError:
            continue

        truncated = len(content) > MAX_FILE_CHARS
        content = content[:MAX_FILE_CHARS]
        label = os.path.relpath(path, cwd)
        attachments.append(label)
        suffix = "\n… (truncated)" if truncated else ""
        extra += f"\n\n--- {label} ---\n{content}{suffix}\n--- end {label} ---"

    return prompt + extra, attachments


ENVIRONMENT_HEADER_MAX_ENTRIES = 200


def build_environment_header(cwd: str) -> str:
    """A deterministic, real snapshot of the working directory -- top-level
    names only (matching /task's own directory scope: no subdirectory
    recursion, no dotfiles), not file contents (that's what @file is for).

    Without this, the model has zero actual filesystem awareness and will
    confidently guess a plausible-sounding but made-up cwd/listing -- verified
    directly against `fm respond` itself, not just fm-pcc's own prompting.
    Injected once, into the first message of a conversation (see
    on_input_submitted), rather than every turn: each backend's own
    multi-turn memory (on-device's --resume transcript, the cloud tiers'
    resend-as-text history, Ollama's native messages array) keeps it in view
    for the rest of that conversation without paying the cost again.
    """
    try:
        entries = sorted(e for e in os.listdir(cwd) if not e.startswith("."))
    except OSError as e:
        return f"Current directory: {cwd} (couldn't list contents: {e})"

    shown = entries[:ENVIRONMENT_HEADER_MAX_ENTRIES]
    listing = ", ".join(
        f"{e}/" if os.path.isdir(os.path.join(cwd, e)) else e for e in shown
    )
    if len(entries) > ENVIRONMENT_HEADER_MAX_ENTRIES:
        listing += f", … ({len(entries) - ENVIRONMENT_HEADER_MAX_ENTRIES} more not shown)"

    return f"Current directory: {cwd}\nContents: {listing or '(empty)'}"


class EditError(Exception):
    pass


_EDIT_SCHEMA_ARGS = [
    "schema", "object", "--name", "EditProposal",
    "--string", "summary", "--description", "One-sentence summary of the change",
    "--integer", "anchor_line",
    "--description", "The 1-based line number (from the numbered listing) to act on",
    "--boolean", "insert_after",
    "--description", "true to insert new_lines after anchor_line, false to replace anchor_line with new_lines",
    "--string", "new_lines", "--description", "The new line(s) of text, freshly written (not copied)",
]


def propose_edit(
    path: str, instructions: str, cwd: str, line_range: tuple[int, int] | None = None
) -> dict:
    """Ask the on-device model for a line-anchored edit to `path`.

    Guided generation on a small on-device model is unreliable at copying
    multi-line source text verbatim into a JSON string -- it reliably mangles
    escaping and truncates. So instead of asking for an old/new text snippet,
    this shows the file as numbered lines and asks for a line number plus
    freshly-*written* replacement text, which the model is much better at.
    We do the actual line lookup ourselves, so there's no verbatim-matching
    step to fail. On-device only for now -- Cloud Pro (via Shortcuts) has no
    schema control to constrain output the same way.

    `line_range` (1-based, inclusive) restricts both what's shown to the
    model and where it's allowed to anchor, so a large file can be edited a
    section at a time without the whole thing needing to fit in context.
    """
    full_path = os.path.expanduser(path)
    if not os.path.isabs(full_path):
        full_path = os.path.join(cwd, full_path)
    full_path = os.path.normpath(full_path)
    label = os.path.relpath(full_path, cwd)

    if not os.path.isfile(full_path):
        raise EditError(f"no such file: {label}")

    with open(full_path, "r", errors="replace") as f:
        original = f.read()

    lines = original.splitlines()
    lo, hi = line_range if line_range else (1, len(lines))
    excerpt_lines = lines[lo - 1 : hi]
    numbered = "\n".join(f"{lo + i}: {line}" for i, line in enumerate(excerpt_lines))

    if len(numbered) > MAX_FILE_CHARS:
        raise EditError(
            f"{label}{f' (lines {lo}-{hi})' if line_range else ''} is too "
            f"large to edit on-device (over {MAX_FILE_CHARS} characters)"
        )

    schema = subprocess.run(
        ["fm", *_EDIT_SCHEMA_ARGS], capture_output=True, text=True, check=True
    ).stdout

    with tempfile.TemporaryDirectory() as tmp:
        schema_path = os.path.join(tmp, "schema.json")
        with open(schema_path, "w") as f:
            f.write(schema)

        prompt = (
            f"Here is {label}, with line numbers:\n\n{numbered}\n\n"
            f"Apply this change: {instructions}\n\n"
            f"Pick the single line number (anchor_line) this change belongs "
            f"at. Set insert_after to true to add new_lines after that line "
            f"and leave it in place, or false to replace that line with "
            f"new_lines. Write new_lines yourself -- don't copy existing "
            f"lines verbatim unless they belong in the result."
        )
        result = _run(
            ["fm", "respond", "--model", "system", "--no-stream", "--greedy",
             "--schema", schema_path, prompt]
        )

    if result.returncode != 0:
        raise EditError(result.stderr.strip() or "fm respond failed")

    try:
        proposal = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise EditError("model didn't return a valid edit proposal")

    anchor = proposal.get("anchor_line")
    insert_after = bool(proposal.get("insert_after"))
    new_lines = proposal.get("new_lines") or ""
    summary = proposal.get("summary") or "(no summary)"

    if not isinstance(anchor, int) or not (lo <= anchor <= max(hi, lo)):
        raise EditError(f"model picked an out-of-range line ({anchor!r})")

    # When inserting after a line (e.g. after a `def` line), new lines belong
    # at the block's body indent -- best approximated by the line that
    # already follows, not the anchor line itself.
    indent_source = lines[anchor] if insert_after and anchor < len(lines) else lines[anchor - 1]
    anchor_indent = re.match(r"[ \t]*", indent_source).group() if lines else ""
    new_split = [
        line if line.startswith((" ", "\t")) or not line else anchor_indent + line
        for line in new_lines.splitlines()
    ]

    if insert_after:
        updated_lines = lines[:anchor] + new_split + lines[anchor:]
    else:
        updated_lines = lines[: anchor - 1] + new_split + lines[anchor:]
    updated = "\n".join(updated_lines) + ("\n" if original.endswith("\n") else "")

    return {
        "path": full_path,
        "label": label,
        "original": original,
        "updated": updated,
        "summary": summary,
    }


_NEW_FILE_SCHEMA_ARGS = [
    "schema", "object", "--name", "NewFileProposal",
    "--string", "summary", "--description", "One-sentence summary of the new file",
    "--string", "content", "--description", "The complete contents of the new file, freshly written",
]


def propose_new_file(path: str, instructions: str, cwd: str) -> dict:
    """Ask the on-device model to write a brand new file from scratch.

    Returns the same shape propose_edit() does (path, label, original,
    updated, summary), with `original` always "", so /task's write/diff/undo
    plumbing doesn't need to distinguish creating a file from editing one.
    Restricted to a plain top-level filename inside `cwd` -- no
    subdirectories, no escaping the working directory -- and refuses to
    clobber a file that already exists (that's /edit's or /task's edit path's
    job, not this one's).
    """
    if os.path.basename(path) != path or path in ("", ".", ".."):
        raise EditError(f"refusing to create an invalid filename: {path!r}")

    full_path = os.path.normpath(os.path.join(cwd, path))
    label = os.path.relpath(full_path, cwd)

    if os.path.exists(full_path):
        raise EditError(f"{label} already exists -- edit it instead of creating it")

    schema = subprocess.run(
        ["fm", *_NEW_FILE_SCHEMA_ARGS], capture_output=True, text=True, check=True
    ).stdout

    with tempfile.TemporaryDirectory() as tmp:
        schema_path = os.path.join(tmp, "schema.json")
        with open(schema_path, "w") as f:
            f.write(schema)

        prompt = (
            f"Create a new file at {label}.\n\n"
            f"Instructions: {instructions}\n\n"
            f"Write the complete contents of this file, from scratch."
        )
        result = _run(
            ["fm", "respond", "--model", "system", "--no-stream", "--greedy",
             "--schema", schema_path, prompt]
        )

    if result.returncode != 0:
        raise EditError(result.stderr.strip() or "fm respond failed")

    try:
        proposal = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise EditError("model didn't return a valid file proposal")

    content = proposal.get("content") or ""
    summary = proposal.get("summary") or "(no summary)"

    return {
        "path": full_path,
        "label": label,
        "original": "",
        "updated": content,
        "summary": summary,
    }


def diff_preview(original: str, updated: str) -> str:
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
    )
    return "".join(diff)


_BRACE_LANGUAGES = {".css", ".js", ".jsx", ".ts", ".tsx", ".json", ".java", ".c", ".cpp", ".swift"}
_SECTION_CHUNK_LINES = 40


def split_sections(content: str, filename: str) -> list[dict]:
    """Split a file into named, 1-based-inclusive line-range sections.

    This is deliberately not model-driven: brace-delimited languages have
    unambiguous top-level block boundaries a few lines of Python can find
    reliably, which is exactly the kind of mechanical task a small model is
    bad at (see propose_edit's own docstring). Anything else falls back to
    fixed-size chunks -- cruder, but still gives the picker something
    bounded to choose between.
    """
    lines = content.splitlines()
    if not lines:
        return [{"name": "(empty file)", "start": 1, "end": 1}]

    ext = os.path.splitext(filename)[1].lower()
    if ext in _BRACE_LANGUAGES:
        sections = _split_top_level_braces(lines)
        if sections:
            return sections

    return [
        {
            "name": f"lines {i + 1}-{min(i + _SECTION_CHUNK_LINES, len(lines))}",
            "start": i + 1,
            "end": min(i + _SECTION_CHUNK_LINES, len(lines)),
        }
        for i in range(0, len(lines), _SECTION_CHUNK_LINES)
    ]


def _split_top_level_braces(lines: list[str]) -> list[dict]:
    sections = []
    depth = 0
    start = None
    for i, line in enumerate(lines):
        if depth == 0 and start is None and line.strip():
            start = i
        depth += line.count("{") - line.count("}")
        if depth <= 0 and start is not None:
            sections.append(
                {"name": lines[start].strip()[:48], "start": start + 1, "end": i + 1}
            )
            start = None
            depth = 0
    if start is not None:
        sections.append({"name": lines[start].strip()[:48], "start": start + 1, "end": len(lines)})
    return sections


STALL_SIMILARITY_THRESHOLD = 0.6
STALL_LOOKBACK = 3


def is_stalling(instructions: str, recent_instructions: list[str]) -> bool:
    """True if `instructions` looks like a near-repeat of a recent step.

    Empirically, a /task loop that isn't converging (e.g. a cleanup task
    where each step only adds more instead of removing the mess from the
    last one) shows up as Cloud Pro asking for essentially the same fix
    again with slightly different wording, not identical text. A fuzzy
    similarity check against the last few steps' instructions catches that
    where an exact match wouldn't.
    """
    lowered = instructions.lower()
    for prior in recent_instructions[-STALL_LOOKBACK:]:
        if difflib.SequenceMatcher(None, lowered, prior.lower()).ratio() > STALL_SIMILARITY_THRESHOLD:
            return True
    return False


def plan_next_step(
    task: str, candidates: list[str], cwd: str, history: list[str], backend: "Backend", model: str
) -> dict | None:
    """Ask a cloud model to pick the next single edit/create step, or declare
    done.

    This is the orchestrator half of the /task loop: it sees the whole task,
    the full content of every candidate file, and a running log of what's
    already been changed, and decides either that nothing more is needed or
    exactly one concrete next step (a file plus specific instructions for
    just that change). The on-device model never sees this -- it only ever
    executes one bounded, already-decided edit or file creation at a time.

    The named file doesn't have to already exist -- if it doesn't match any
    candidate, /task treats it as a new file to create, which is how an
    empty directory can be bootstrapped from nothing.
    """
    listing = []
    for name in candidates:
        try:
            with open(os.path.join(cwd, name), "r", errors="replace") as f:
                content = f.read(MAX_FILE_CHARS)
        except OSError:
            content = "(unreadable)"
        listing.append(f"--- {name} ---\n{content}")

    progress = "\n".join(f"- {h}" for h in history) if history else "(nothing yet)"

    prompt = (
        f"Task: {task}\n\nFiles in this directory:\n\n"
        + ("\n\n".join(listing) if listing else "(empty directory -- no files yet)")
        + f"\n\nSteps already taken:\n{progress}\n\n"
        "If the task is now fully done, reply with exactly: DONE\n"
        "Otherwise reply with exactly two lines:\n"
        "FILE: <filename to work on next -- name one of the existing files "
        "above to edit it, or a new filename with no subdirectories to "
        "create it>\n"
        "INSTRUCTIONS: <specific instructions for just this one change>"
    )
    reply = backend.classify(prompt, model).strip()
    if reply.upper().startswith("DONE"):
        return None

    file_match = re.search(r"FILE:\s*(.+)", reply)
    instr_match = re.search(r"INSTRUCTIONS:\s*(.+)", reply, re.DOTALL)
    if not file_match or not instr_match:
        raise EditError(f"couldn't parse the next step from the model's reply: {reply!r}")

    filename = file_match.group(1).strip().strip("`'\" .")
    instructions = instr_match.group(1).strip()

    # Only ever touch a plain top-level filename inside cwd -- rejects
    # absolute paths, "..", and subdirectories, whether the file is being
    # edited or created.
    if not filename or os.path.basename(filename) != filename or filename in (".", ".."):
        raise EditError(f"model picked an invalid filename: {filename!r}")

    matched = next(
        (c for c in candidates if c.lower() == filename.lower() or c.lower() in filename.lower()),
        None,
    )
    return {"file": matched or filename, "instructions": instructions}


def pick_section(task: str, filename: str, sections: list[dict], backend: "Backend", model: str) -> dict:
    """Ask a cloud model which (deterministically-split) section to edit."""
    listing = "\n".join(
        f"{i}: {s['name']} (lines {s['start']}-{s['end']})" for i, s in enumerate(sections)
    )
    prompt = (
        f"Task: {task}\n\n{filename} has these sections:\n{listing}\n\n"
        f"Which section number is most relevant to this task? "
        f"Reply with just the number, nothing else."
    )
    reply = backend.classify(prompt, model).strip()
    match = re.search(r"\d+", reply)
    index = int(match.group()) if match else -1
    if not (0 <= index < len(sections)):
        raise EditError(f"couldn't tell which section to edit from the model's reply: {reply!r}")
    return sections[index]


ASK_MAX_SUBQUESTIONS = 8


def decompose_question(question: str, backend: "Backend", model: str) -> dict:
    """Ask the "planning" role to either answer directly, or split into
    sub-questions for the "building" role to research first.

    Returns {"answer": str} if it answered directly, or
    {"subquestions": [str, ...]} if it decomposed. If the reply doesn't
    match either expected shape, it's treated as a direct answer rather
    than raising -- a plain but usable reply beats a hard failure here.
    """
    prompt = (
        f"Question: {question}\n\n"
        "If this is simple enough to answer directly and completely, reply "
        "with exactly:\nANSWER: <your answer>\n\n"
        "If it would be answered better by researching a few simpler "
        "sub-questions first, reply with exactly:\nSUBQUESTIONS:\n"
        "1. <sub-question>\n2. <sub-question>\n"
        f"(as many as needed, no more than {ASK_MAX_SUBQUESTIONS})"
    )
    reply = backend.classify(prompt, model).strip()
    upper = reply.upper()

    if upper.startswith("ANSWER:"):
        return {"answer": reply.split(":", 1)[1].strip()}

    if upper.startswith("SUBQUESTIONS:"):
        # Numbering was requested but isn't always honored -- treat every
        # non-empty line as one sub-question, stripping a leading
        # "1.", "2)", "-", or "*" if present rather than requiring one.
        subquestions = []
        for line in reply.split(":", 1)[1].splitlines():
            line = re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", line).strip()
            if line:
                subquestions.append(line)
        if subquestions:
            return {"subquestions": subquestions[:ASK_MAX_SUBQUESTIONS]}

    return {"answer": reply}


def synthesize_answer(
    question: str, subanswers: list[tuple[str, str]], backend: "Backend", model: str
) -> str:
    """Ask the "planning" role to combine sub-answers into one final answer."""
    research = "\n\n".join(f"Q: {q}\nA: {a}" for q, a in subanswers)
    prompt = (
        f"Original question: {question}\n\nResearch:\n{research}\n\n"
        "Using this research, write one clear, complete final answer to the "
        "original question."
    )
    return backend.classify(prompt, model).strip()


def _gradient(text: str, start: str, end: str) -> Text:
    """Render `text` with a per-character color ramp from `start` to `end`."""
    sr, sg, sb = (int(start[i : i + 2], 16) for i in (1, 3, 5))
    er, eg, eb = (int(end[i : i + 2], 16) for i in (1, 3, 5))
    out = Text()
    span = max(len(text) - 1, 1)
    for i, ch in enumerate(text):
        t = i / span
        r = round(sr + (er - sr) * t)
        g = round(sg + (eg - sg) * t)
        b = round(sb + (eb - sb) * t)
        out.append(ch, style=f"bold #{r:02x}{g:02x}{b:02x}")
    return out


class GenerationCancelled(Exception):
    pass


class ICloudPlusRequired(RuntimeError):
    """Raised when a Shortcuts-backed cloud tier fails specifically because
    the signed-in account lacks iCloud+ -- Apple's "Use Model" action's own
    error text for this ("...you must be signed in to an iCloud+ account
    to use the <tier> model") is specific enough to match on reliably.
    """
    pass


_op_lock = threading.Lock()
_active_op: subprocess.Popen | http.client.HTTPConnection | None = None
_cancel_requested = False


def _begin_op(op: subprocess.Popen | http.client.HTTPConnection) -> None:
    global _active_op, _cancel_requested
    with _op_lock:
        _active_op = op
        _cancel_requested = False


def _end_op(op: subprocess.Popen | http.client.HTTPConnection) -> bool:
    """Unregister `op`. Returns True if it had been cancelled."""
    global _active_op
    with _op_lock:
        was_cancelled = _cancel_requested and _active_op is op
        if _active_op is op:
            _active_op = None
    return was_cancelled


def cancel_active_process() -> bool:
    """Interrupt whatever fm/shortcuts subprocess or Ollama request is running.

    Only one of these runs at a time in this app (a single background
    worker per turn), so a single module-level slot is enough. A subprocess
    gets terminated; an HTTP connection gets closed out from under its
    blocked read, which raises in the thread that's waiting on it.
    """
    global _cancel_requested
    with _op_lock:
        op = _active_op
        if op is None:
            return False
        _cancel_requested = True
    if isinstance(op, subprocess.Popen):
        if op.poll() is None:
            op.terminate()
    else:
        try:
            op.close()
        except OSError:
            pass
    return True


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """subprocess.run-alike whose Popen is registered so Esc-Esc can kill it."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _begin_op(proc)
    try:
        stdout, stderr = proc.communicate()
    finally:
        cancelled = _end_op(proc)
    if cancelled:
        raise GenerationCancelled()
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


OLLAMA_HOST_DEFAULT = "http://localhost:11434"
SESSIONS_DIR = os.path.expanduser("~/.fm-pcc/sessions")
STATE_PATH = os.path.expanduser("~/.fm-pcc/state.json")
NOTIFY_MIN_SECONDS = 5.0
ON_DEVICE_CONTEXT_TOKENS = 4096  # documented limit for the on-device system model
CLOUD_CONTEXT_TOKENS_ESTIMATE = 32000  # no published figure for cloud/cloud-pro -- a guess
UPDATE_CHECK_URL = "https://api.github.com/repos/justwaters/fm-pcc/releases/latest"


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = []
    for piece in version.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    return _version_tuple(candidate) > _version_tuple(current)


def fetch_latest_version(
    url: str = UPDATE_CHECK_URL, timeout: float = 4.0
) -> tuple[str | None, str | None]:
    """Check GitHub's "latest release" for this repo. Returns (version,
    None) on success or (None, error) on failure (offline, timeout, no
    releases yet, unexpected content) -- never raises, so the startup check
    can just skip showing a button on failure, while /update can surface
    the actual error for a human to debug instead of failing silently.
    """
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        return None, str(e)
    tag = data.get("tag_name") or ""
    version = tag[1:] if tag.startswith("v") else tag
    if not version:
        return None, f"unexpected response: {data!r}"[:200]
    return version, None


def _git_repo_name(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return os.path.basename(result.stdout.strip().rstrip("/"))


def git_branch(cwd: str) -> str | None:
    """Current branch name, or None if `cwd` isn't a git repo or HEAD is
    detached (an empty branch name isn't worth showing in the statusline).
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _readme_first_line(path: str) -> str | None:
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                stripped = line.strip().lstrip("#").strip()
                if stripped:
                    return stripped
    except OSError:
        return None
    return None


def launch_context_hint(cwd: str) -> str | None:
    """Best-effort one-line orientation shown on startup: the git repo name
    and a file count if `cwd` is inside a repo, plus the README's first
    non-empty line if one exists. Never raises; returns None if there's
    nothing worth showing (not a repo, no README).
    """
    parts = []
    repo_name = _git_repo_name(cwd)
    if repo_name:
        try:
            file_count = sum(
                1 for f in os.listdir(cwd)
                if os.path.isfile(os.path.join(cwd, f)) and not f.startswith(".")
            )
            parts.append(f"git repo '{repo_name}' ({file_count} files here)")
        except OSError:
            parts.append(f"git repo '{repo_name}'")

    readme_path = find_readme(cwd)
    if readme_path:
        first_line = _readme_first_line(readme_path)
        if first_line:
            parts.append(f"README: {first_line}")

    return " · ".join(parts) if parts else None


def _applescript_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, message: str) -> None:
    """Best-effort desktop notification -- never raises, never blocks the
    caller on anything but the (short, timed-out) osascript call itself.
    """
    try:
        message = message.strip().replace("\n", " ")
        if len(message) > 200:
            message = message[:197] + "…"
        script = (
            f"display notification {_applescript_string(message)} "
            f"with title {_applescript_string(title)}"
        )
        subprocess.run(
            ["osascript", "-e", script], check=False, capture_output=True, timeout=5
        )
    except Exception:
        pass


def _ollama_connect(host: str) -> http.client.HTTPConnection:
    parsed = urllib.parse.urlparse(host)
    return http.client.HTTPConnection(parsed.hostname or "localhost", parsed.port or 11434, timeout=180)


def _ollama_list_models(host: str) -> list[str]:
    conn = _ollama_connect(host)
    try:
        conn.request("GET", "/api/tags")
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
    except OSError as e:
        raise RuntimeError(f"couldn't reach Ollama at {host} -- run 'ollama serve'") from e
    finally:
        conn.close()
    if status != 200:
        raise RuntimeError(f"Ollama returned HTTP {status}")
    return [m["name"] for m in json.loads(data).get("models", [])]


def _ollama_chat(model: str, messages: list[dict], host: str) -> tuple[str, int | None]:
    """POST to Ollama's /api/chat, cancellable the same way as _run().

    Uses http.client directly (stdlib, no new dependency) rather than
    `ollama run <model>` so multi-turn history can be passed natively as
    a messages list instead of re-stuffing prior turns into a text prompt
    the way the Shortcuts-backed cloud tiers have to.

    Returns (reply text, total tokens now in context) -- the second value
    is `prompt_eval_count + eval_count` from Ollama's own accounting (real
    tokenizer counts, not an estimate), or None if either is missing from
    the response.
    """
    conn = _ollama_connect(host)
    _begin_op(conn)
    try:
        body = json.dumps({"model": model, "messages": messages, "stream": False})
        conn.request("POST", "/api/chat", body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
    except OSError as e:
        cancelled = _end_op(conn)
        conn.close()
        if cancelled:
            raise GenerationCancelled()
        raise RuntimeError(f"couldn't reach Ollama at {host} -- run 'ollama serve'") from e

    cancelled = _end_op(conn)
    conn.close()
    if cancelled:
        raise GenerationCancelled()

    if status != 200:
        raise RuntimeError(f"Ollama returned HTTP {status}: {data.decode('utf-8', 'replace')[:200]}")
    payload = json.loads(data)
    if "error" in payload:
        raise RuntimeError(f"Ollama error: {payload['error']}")

    prompt_tokens = payload.get("prompt_eval_count")
    eval_tokens = payload.get("eval_count")
    total_tokens = (
        prompt_tokens + eval_tokens
        if prompt_tokens is not None and eval_tokens is not None
        else None
    )
    return payload["message"]["content"], total_tokens


def _ollama_context_length(model: str, host: str) -> int | None:
    """Look up `model`'s context window from /api/show. The key is always
    "<architecture>.context_length" (e.g. "llama.context_length",
    "qwen2.context_length") -- the architecture prefix varies by model
    family, so this matches on the suffix generically rather than trying
    to enumerate every architecture name.
    """
    conn = _ollama_connect(host)
    try:
        body = json.dumps({"name": model})
        conn.request("POST", "/api/show", body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
    except OSError:
        return None
    finally:
        conn.close()
    if status != 200:
        return None
    try:
        info = json.loads(data).get("model_info", {})
    except json.JSONDecodeError:
        return None
    for key, value in info.items():
        if key.endswith("context_length") and isinstance(value, int):
            return value
    return None


def _installed_shortcuts() -> set[str]:
    result = subprocess.run(["shortcuts", "list"], capture_output=True, text=True)
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def ensure_shortcut_installed(
    name: str,
    url: str,
    on_status: Callable[[str], None],
    timeout: float = 90.0,
) -> bool:
    """Make sure `name` is installed, prompting the Shortcuts add-flow if not."""
    if name in _installed_shortcuts():
        return True

    on_status(f"'{name}' shortcut not found — opening its install link…")
    subprocess.run(["open", url])

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if name in _installed_shortcuts():
            on_status(f"'{name}' installed.")
            return True
        time.sleep(1.5)

    on_status(
        f"Still waiting on '{name}'. On-device chat works without it — "
        f"try again once you've added it."
    )
    return False


def load_icloud_plus_unavailable() -> set[str]:
    """Which cloud tiers are known to require iCloud+ this account doesn't
    have -- persisted so this survives restarts and updates, not just the
    current session (once seen, that error isn't going away until the
    account itself changes).
    """
    try:
        with open(STATE_PATH, "r") as f:
            data = json.load(f)
        return set(data.get("icloud_plus_unavailable", []))
    except (OSError, json.JSONDecodeError):
        return set()


def save_icloud_plus_unavailable(unavailable: set[str]) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump({"icloud_plus_unavailable": sorted(unavailable)}, f, indent=2)
    except OSError:
        pass


class Backend:
    """Talks to fm (on-device), Shortcuts (cloud, cloud pro), and Ollama."""

    def __init__(
        self,
        shortcut_overrides: dict[str, str] | None = None,
        on_status: Callable[[str], None] = print,
        ollama_model: str | None = None,
        ollama_host: str = OLLAMA_HOST_DEFAULT,
    ):
        self.on_status = on_status
        self._shortcut_overrides = shortcut_overrides or {}
        self._shortcut_ready: dict[str, bool] = {}
        self._transcript_path: str | None = None
        self._cloud_history: dict[str, list[tuple[str, str]]] = {
            model: [] for model in CLOUD_SHORTCUTS
        }
        self.ollama_model = ollama_model
        self.ollama_host = ollama_host
        self._ollama_resolved_model: str | None = None
        self._ollama_history: dict[str, list[dict[str, str]]] = {}
        self._ollama_context_tokens: dict[str, int] = {}
        self._ollama_context_length: dict[str, int] = {}
        self._icloud_plus_unavailable: set[str] = load_icloud_plus_unavailable()

    def shortcut_name(self, model: str) -> str:
        return self._shortcut_overrides.get(model, CLOUD_SHORTCUTS[model]["name"])

    def reset(self) -> None:
        self._transcript_path = None
        self._ollama_context_tokens.clear()
        for history in self._cloud_history.values():
            history.clear()
        self._ollama_history.clear()

    def respond(self, prompt: str, model: str) -> str:
        if model == "on-device":
            return self._respond_on_device(prompt)
        if model == "ollama" or model.startswith("ollama:"):
            return self._respond_ollama(prompt, model)
        return self._respond_cloud(prompt, model)

    def _resolve_ollama_model(self, model: str = "ollama") -> str:
        """`model` is either the generic "ollama" (auto-resolve) or a
        specific "ollama:<tag>" picked directly, e.g. from the /model menu.
        """
        if model.startswith("ollama:"):
            return model.split(":", 1)[1]
        if self.ollama_model:
            return self.ollama_model
        if self._ollama_resolved_model:
            return self._ollama_resolved_model
        models = _ollama_list_models(self.ollama_host)
        if not models:
            raise RuntimeError(
                "no Ollama models installed -- run e.g. 'ollama pull llama3.2'"
            )
        self._ollama_resolved_model = models[0]
        return self._ollama_resolved_model

    def _respond_ollama(self, prompt: str, model: str) -> str:
        tag = self._resolve_ollama_model(model)
        history = self._ollama_history.setdefault(tag, [])
        history.append({"role": "user", "content": prompt})
        try:
            text, total_tokens = _ollama_chat(tag, history, self.ollama_host)
        except Exception:
            history.pop()  # don't keep a failed turn in context
            raise
        history.append({"role": "assistant", "content": text})
        if total_tokens is not None:
            self._ollama_context_tokens[tag] = total_tokens
        return text

    def _respond_on_device(self, prompt: str) -> str:
        if self._transcript_path is None:
            self._transcript_path = os.path.join(
                tempfile.gettempdir(), f"fm-pcc-{uuid.uuid4().hex}.json"
            )

        args = ["fm", "respond", "--no-stream"]
        if os.path.exists(self._transcript_path):
            args += ["--resume", self._transcript_path]
        args += ["--save-transcript", self._transcript_path, prompt]

        result = _run(args)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "fm respond failed")
        return result.stdout.strip()

    def _ensure_ready(self, model: str) -> None:
        if not self._shortcut_ready.get(model):
            self._shortcut_ready[model] = ensure_shortcut_installed(
                self.shortcut_name(model), CLOUD_SHORTCUTS[model]["url"], self.on_status
            )

    def _run_shortcut(self, model: str, prompt: str) -> str:
        """Raw one-shot Shortcuts call -- no chat history involved."""
        shortcut = self.shortcut_name(model)
        self._ensure_ready(model)

        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, "input.txt")
            out_path = os.path.join(tmp, "output.rtf")
            with open(in_path, "w") as f:
                f.write(prompt)

            result = _run(["shortcuts", "run", shortcut, "-i", in_path, "-o", out_path])
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                if "icloud+" in detail.lower():
                    self._icloud_plus_unavailable.add(model)
                    save_icloud_plus_unavailable(self._icloud_plus_unavailable)
                    raise ICloudPlusRequired(detail)
                raise RuntimeError(
                    f"Shortcut '{shortcut}' failed: {detail}\n"
                    f"Check it exists ('shortcuts list') and its 'Use Model' "
                    f"action is bound to Shortcut Input."
                )
            if not os.path.exists(out_path):
                raise RuntimeError(f"Shortcut '{shortcut}' produced no output.")

            return subprocess.run(
                ["textutil", "-convert", "txt", "-stdout", out_path],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

    def classify(self, prompt: str, model: str) -> str:
        """One-shot, history-free call for routing/classification, not chat."""
        if model == "on-device":
            result = _run(["fm", "respond", "--no-stream", "--greedy", prompt])
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "fm respond failed")
            return result.stdout.strip()
        if model == "ollama" or model.startswith("ollama:"):
            text, _ = _ollama_chat(
                self._resolve_ollama_model(model),
                [{"role": "user", "content": prompt}],
                self.ollama_host,
            )
            return text
        return self._run_shortcut(model, prompt)

    def _respond_cloud(self, prompt: str, model: str) -> str:
        history = self._cloud_history[model]
        full_prompt = prompt
        if history:
            context = "\n\n".join(f"User: {u}\nAssistant: {a}" for u, a in history)
            full_prompt = f"{context}\n\nUser: {prompt}"

        text = self._run_shortcut(model, full_prompt)
        history.append((prompt, text))
        return text

    def context_usage(self, model: str) -> tuple[int | None, int | None]:
        """Return (tokens used, context window size) for `model`'s current
        session, or (None, None)/(None, max) when a piece is unknown.

        - on-device: exact, via `fm count-tokens --transcript` against the
          documented 4,096-token session limit.
        - ollama: exact, from the real token counts Ollama returns with
          each chat response, against that model's own context length
          (from /api/show) -- both real numbers, not estimates.
        - cloud/cloud-pro: no API exposes either number for Apple's
          PCC-backed tiers, so this is a chars/4 estimate against a
          guessed context size, clearly a rough approximation.
        """
        family = model_family(model)

        if family == "on-device":
            if not self._transcript_path or not os.path.isfile(self._transcript_path):
                return 0, ON_DEVICE_CONTEXT_TOKENS
            try:
                result = subprocess.run(
                    ["fm", "count-tokens", "--quiet", "--transcript", self._transcript_path],
                    capture_output=True, text=True, timeout=15,
                )
                used = int(result.stdout.strip())
            except (OSError, ValueError, subprocess.TimeoutExpired):
                return None, ON_DEVICE_CONTEXT_TOKENS
            return used, ON_DEVICE_CONTEXT_TOKENS

        if family == "ollama":
            try:
                tag = self._resolve_ollama_model(model)
            except Exception:
                return None, None
            used = self._ollama_context_tokens.get(tag)
            max_tokens = self._ollama_context_length.get(tag)
            if max_tokens is None:
                max_tokens = _ollama_context_length(tag, self.ollama_host)
                if max_tokens is not None:
                    self._ollama_context_length[tag] = max_tokens
            return used, max_tokens

        # cloud / cloud-pro
        history = self._cloud_history.get(model, [])
        chars = sum(len(u) + len(a) for u, a in history)
        return chars // 4, CLOUD_CONTEXT_TOKENS_ESTIMATE


@dataclass
class Message:
    role: str  # "user" | "assistant" | "system" | "thinking"
    text: str


class MessageWidget(Static):
    """A single turn, rendered as flowing text (no bubble/border chrome)."""

    def __init__(self, message: Message, accent: str):
        if message.role == "user":
            body = Text.from_markup(f"[#7b838a]you ›[/] {rich_escape(message.text)}")
        elif message.role == "assistant":
            prefix = Text("fm-pcc ›", style=f"bold {accent}")
            # Cloud/Ollama replies routinely come back as markdown (headers,
            # lists, bold); plain-text replies render through Markdown
            # unchanged, so this is a safe default for either.
            body = Group(prefix, Markdown(message.text))
        elif message.role == "thinking":
            body = Text.from_markup(f"[{accent}]· thinking…[/]")
        else:
            body = Text.from_markup(f"[#7b838a italic]{rich_escape(message.text)}[/]")
        super().__init__(body)
        self.add_class(f"msg-{message.role}")


class ChatApp(App):
    CSS = """
    Screen { background: #14181c; }
    #banner { padding: 1 2 0 2; }
    #subtitle { padding: 0 2 1 2; color: #7b838a; }
    #log { padding: 0 2; }
    .msg-user { margin: 1 0 0 0; }
    .msg-assistant { margin: 1 0 0 0; }
    .msg-system { margin: 1 0 0 0; }
    .msg-thinking { margin: 1 0 0 0; }
    #palette {
        display: none;
        height: auto;
        max-height: 8;
        margin: 0 2 0 2;
        border: round #7b838a;
        background: #1b2126;
    }
    #palette > .option-list--option-highlighted { background: #2a3138; }
    #inputbar { height: 3; border: round #7b838a; margin: 0 2 0 2; padding: 0 1; }
    #prompt-glyph { width: 2; content-align: center middle; }
    #input { border: none; background: transparent; }
    #input:focus { border: none; }
    #statusbar { height: auto; margin: 0 2 1 2; }
    #status { width: 1fr; padding: 0 1; content-align: left middle; }
    #update-button {
        display: none;
        min-width: 0;
        height: 1;
        border: none;
        padding: 0 1;
        background: #1b2126;
        color: #ffd43b;
        text-style: bold;
    }
    #update-button:hover { background: #2a3138; }
    """

    BINDINGS = [
        ("ctrl+t", "toggle_model", "Toggle model"),
        ("ctrl+r", "reset", "Reset chat"),
        # priority=True so this fires even though the focused Input has its
        # own ctrl+c binding (copy) that would otherwise intercept it first.
        Binding("ctrl+c", "attempt_quit", "Quit", priority=True),
    ]

    model: reactive[str] = reactive("on-device")

    def __init__(
        self,
        initial_model: str = "on-device",
        shortcut_overrides: dict[str, str] | None = None,
        ollama_model: str | None = None,
        ollama_host: str = OLLAMA_HOST_DEFAULT,
    ):
        super().__init__()
        self.backend = Backend(
            shortcut_overrides,
            on_status=lambda msg: self.call_from_thread(
                self._add_message, Message("system", msg)
            ),
            ollama_model=ollama_model,
            ollama_host=ollama_host,
        )
        self.set_reactive(ChatApp.model, initial_model)
        self.turn = 0
        self._thinking: MessageWidget | None = None
        self._pending_edit: dict | None = None
        self._model_picker_active = False
        self._history: list[str] = []
        self._history_index: int | None = None
        self._history_draft = ""
        self._suppress_palette_once = False
        self._last_escape_time = 0.0
        self._last_quit_time = 0.0
        self._loop_running = False
        self._loop_cancel_requested = False
        self.subagent_roles: dict[str, str] = {"planning": "cloud-pro", "building": "on-device"}
        self._message_log: list[Message] = []
        self._undo_stack: list[dict] = []
        self._branch = git_branch(os.getcwd())
        self._previous_model: str | None = None
        self._latest_version: str | None = None
        self._last_task_description: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="banner")
        yield Static(id="subtitle")
        yield VerticalScroll(id="log")
        yield OptionList(id="palette")
        with Horizontal(id="inputbar"):
            yield Static("❯", id="prompt-glyph")
            yield Input(placeholder="Message fm-pcc… (/ for commands)", id="input")
        with Horizontal(id="statusbar"):
            yield Static(id="status")
            yield Button("", id="update-button")

    def on_mount(self) -> None:
        self.query_one("#banner", Static).update(
            _gradient(
                f" fm-pcc v{__version__} — Apple Foundation Models Chat",
                "#f0b429", "#38d9c9",
            )
        )
        self.query_one(Input).focus()
        self._update_chrome()
        hint = launch_context_hint(os.getcwd())
        if hint:
            self._add_message(Message("system", hint))
        self._check_for_update()

    @work(thread=True)
    def _check_for_update(self) -> None:
        latest, _error = fetch_latest_version()
        if latest and is_newer(latest, __version__):
            self.call_from_thread(self._show_update_button, latest)

    def _show_update_button(self, latest: str) -> None:
        self._latest_version = latest
        button = self.query_one("#update-button", Button)
        button.label = f"Update (v{__version__} -> v{latest})"
        button.display = True

    @work(thread=True)
    def _run_manual_update_check(self) -> None:
        latest, error = fetch_latest_version()
        self.call_from_thread(self._manual_update_check_finished, latest, error)

    def _manual_update_check_finished(self, latest: str | None, error: str | None) -> None:
        self._enable_input()
        if error:
            self._add_message(Message("system", f"update check failed: {error}"))
        elif latest and is_newer(latest, __version__):
            self._show_update_button(latest)
            self._add_message(Message("system", f"update available: v{__version__} -> v{latest}"))
        else:
            self._add_message(Message("system", f"up to date (v{__version__})"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "update-button":
            self._run_update()

    @work(thread=True)
    def _run_update(self) -> None:
        self.call_from_thread(self._update_started)
        try:
            result = subprocess.run(
                ["uv", "tool", "upgrade", "fm-pcc"],
                capture_output=True, text=True, timeout=180,
            )
            success = result.returncode == 0
            detail = (result.stderr or result.stdout or "").strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            success = False
            detail = str(e)
        self.call_from_thread(self._update_finished, success, detail)

    def _update_started(self) -> None:
        button = self.query_one("#update-button", Button)
        button.disabled = True
        button.label = "Updating…"
        self._add_message(Message("system", "updating fm-pcc…"))

    def _update_finished(self, success: bool, detail: str) -> None:
        button = self.query_one("#update-button", Button)
        if success:
            button.label = f"Updated to v{self._latest_version} -- restart fm-pcc"
            self._add_message(
                Message("system", f"updated to v{self._latest_version} — restart fm-pcc to use it")
            )
        else:
            button.disabled = False
            button.label = f"Update (v{__version__} -> v{self._latest_version})"
            self._add_message(Message("system", f"update failed: {detail[:300]}"))

    def watch_model(self, old_value: str, _new_value: str) -> None:
        self._previous_model = old_value
        self._update_chrome()

    NORMAL_INPUT_COLOR = "#e7e5dd"

    def _status_line(self) -> str:
        directory = os.path.basename(os.getcwd()) or os.getcwd()
        location = f"{directory} ({self._branch})" if self._branch else directory

        used, max_tokens = self.backend.context_usage(self.model)
        if used is None or not max_tokens:
            context = "Context:n/a"
        else:
            context = f"Context:{min(100, round(100 * used / max_tokens))}%"

        return f"{location} | {model_label(self.model)} | {context}"

    def _update_chrome(self) -> None:
        accent = model_color(self.model)
        self.query_one("#subtitle", Static).update(
            f" model: {model_label(self.model)} · /help for help"
        )
        status = self.query_one("#status", Static)
        status.update(self._status_line())
        status.styles.color = status_accent(self.model)
        self.query_one("#inputbar").styles.border = ("round", accent)
        self.query_one("#prompt-glyph", Static).update(Text("❯", style=f"bold {accent}"))
        self.query_one(Input).styles.color = self.NORMAL_INPUT_COLOR

    def action_toggle_model(self) -> None:
        next_index = (MODEL_ORDER.index(model_family(self.model)) + 1) % len(MODEL_ORDER)
        self._select_model(MODEL_ORDER[next_index])

    def _ollama_available(self) -> bool:
        """Live check -- covers both "ollama serve isn't running" and "it's
        running but nothing is pulled," since either way there's nothing to
        actually talk to.
        """
        try:
            return bool(_ollama_list_models(self.backend.ollama_host))
        except Exception:
            return False

    def _select_model(self, model: str) -> None:
        if model == self.model:
            return
        if model in self.backend._icloud_plus_unavailable:
            self._add_message(
                Message(
                    "system",
                    f"{model_label(model)} requires iCloud+ on this account — not "
                    f"switching (run /model reset if that's changed)",
                )
            )
            return
        if model_family(model) == "ollama" and not self._ollama_available():
            self._add_message(
                Message(
                    "system",
                    f"{model_label(model)} isn't reachable — Ollama doesn't seem "
                    f"to be running, or has no models installed",
                )
            )
            return
        self.model = model
        self._add_message(Message("system", f"switched to {model_label(self.model)}"))

    def _reset_icloud_plus_unavailable(self) -> None:
        if not self.backend._icloud_plus_unavailable:
            self._add_message(Message("system", "no models are currently marked as requiring iCloud+"))
            return
        cleared = sorted(self.backend._icloud_plus_unavailable)
        self.backend._icloud_plus_unavailable.clear()
        save_icloud_plus_unavailable(self.backend._icloud_plus_unavailable)
        labels = ", ".join(model_label(m) for m in cleared)
        self._add_message(
            Message("system", f"cleared the iCloud+ restriction for: {labels} — they'll be retried")
        )

    def _open_model_picker(self) -> None:
        palette = self.query_one("#palette", OptionList)
        palette.clear_options()

        try:
            ollama_models = _ollama_list_models(self.backend.ollama_host)
        except Exception:
            ollama_models = []

        # `None` marks a non-selectable group header -- only "ollama" gets
        # one, to show its locally installed models as a tree underneath.
        entries: list[str | None] = []
        for key in MODEL_ORDER:
            if key == "ollama" and ollama_models:
                entries.append(None)
                entries.extend(f"ollama:{tag}" for tag in ollama_models)
            else:
                entries.append(key)

        for i, entry in enumerate(entries):
            if entry is None:
                palette.add_option(Option("ollama", id="_header_ollama", disabled=True))
                continue
            marker = "● " if entry == self.model else "○ "
            if entry in self.backend._icloud_plus_unavailable:
                unavailable, note = True, "Requires iCloud+"
            elif entry == "ollama" and not ollama_models:
                unavailable, note = True, "Not running"
            else:
                unavailable, note = False, None
            label = model_label(entry)
            if note:
                label = f"{label} ({note})"
            if entry.startswith("ollama:"):
                branch = "└─" if i == len(entries) - 1 else "├─"
                palette.add_option(
                    Option(f"{branch} {marker}{label}", id=entry, disabled=unavailable)
                )
            else:
                palette.add_option(Option(f"{marker}{label}", id=entry, disabled=unavailable))

        selectable = [e for e in entries if e is not None]
        highlight_target = self.model if self.model in selectable else selectable[0]
        palette.highlighted = next(i for i, e in enumerate(entries) if e == highlight_target)
        palette.display = True
        palette.focus()
        self._model_picker_active = True
        self.query_one(Input).disabled = True

    def _close_model_picker(self) -> None:
        self._model_picker_active = False
        self.query_one("#palette", OptionList).display = False
        input_widget = self.query_one(Input)
        input_widget.disabled = False
        input_widget.focus()

    def action_reset(self) -> None:
        self.backend.reset()
        self.turn = 0
        self._message_log = []
        self.query_one("#log", VerticalScroll).remove_children()
        self._update_chrome()
        self._add_message(Message("system", "conversation reset"))

    def action_attempt_quit(self) -> None:
        now = time.monotonic()
        if self._last_quit_time and now - self._last_quit_time < 1.5:
            cancel_active_process()  # don't hang waiting on a blocked worker thread
            self.exit()
        else:
            self._last_quit_time = now
            self.query_one("#status", Static).update("press ctrl+c again to quit…")
            self.set_timer(1.5, self._update_chrome)

    UNDO_STACK_MAX = 20

    def _push_undo(self, path: str, label: str, original: str, existed_before: bool = True) -> None:
        self._undo_stack.append(
            {"path": path, "label": label, "original": original, "existed_before": existed_before}
        )
        if len(self._undo_stack) > self.UNDO_STACK_MAX:
            self._undo_stack.pop(0)

    def _add_message(self, message: Message) -> MessageWidget:
        log = self.query_one("#log", VerticalScroll)
        widget = MessageWidget(message, model_color(self.model))
        log.mount(widget)
        log.scroll_end(animate=False)
        if message.role in ("user", "assistant"):
            self._message_log.append(message)
        return widget

    COMMANDS = {
        "help": "show this help",
        "model": "open a menu to switch models, /model <name> directly, or /model reset to clear iCloud+ restrictions",
        "edit": "propose an edit: /edit <path> <instructions> (on-device only)",
        "task": "run a multi-step edit loop, writing as it goes: /task <description>",
        "ask": "research a question via cloud/core subagents: /ask <question>",
        "subagents": "show or set the planning/building roles: /subagents [planning|building] <model>",
        "compare": "ask every model the same question: /compare <question>",
        "save": "save the conversation: /save <name>",
        "resume": "resume a saved conversation, or list saved ones: /resume [name]",
        "undo": "revert the last file write made by /edit or /task",
        "push": "commit and push the current changes to git",
        "update": "check for a newer version of fm-pcc",
        "apply": "write the pending proposed edit",
        "discard": "discard the pending proposed edit",
        "clear": "start a new conversation",
        "quit": "exit fm-pcc",
    }
    COMMAND_ALIASES = {"?": "help", "reset": "clear", "exit": "quit", "q": "quit"}

    def _handle_command(self, raw: str) -> None:
        parts = raw[1:].strip().split(maxsplit=1)
        name = self.COMMAND_ALIASES.get(parts[0].lower(), parts[0].lower()) if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if name == "help":
            commands = "\n".join(f"  /{cmd:<7} {desc}" for cmd, desc in self.COMMANDS.items())
            shortcuts = (
                "  ctrl+t          cycle on-device / cloud / cloud pro / ollama\n"
                "  ctrl+r          start a new conversation\n"
                "  enter           send your message\n"
                "  ↑ / ↓           step through what you've sent\n"
                "  esc esc         stop the current response/edit/task\n"
                "  ctrl+c ctrl+c   quit"
            )
            note = (
                "note: fm-pcc's chat is text-only, but the underlying `fm` CLI "
                "can also analyze images directly -- `fm respond --image "
                "photo.jpg --tool ocr \"...\"` for reliable text extraction, or "
                "`--tool barcode` to actually decode a barcode/QR code (without "
                "the tool, the model just guesses at unreadable pixel patterns "
                "like that). Not wired into fm-pcc's chat yet."
            )
            self._add_message(
                Message(
                    "system",
                    f"commands:\n{commands}\n\nshortcuts:\n{shortcuts}\n\n{note}",
                )
            )
        elif name == "model":
            if not arg:
                self._open_model_picker()
            elif arg == "reset":
                self._reset_icloud_plus_unavailable()
            elif arg in MODEL_LABELS or arg.startswith("ollama:"):
                self._select_model(arg)
            else:
                choices = ", ".join(MODEL_ORDER)
                self._add_message(
                    Message(
                        "system",
                        f"unknown model '{arg}' — try one of: {choices}, "
                        f"or ollama:<name> for a specific local model",
                    )
                )
        elif name == "ask":
            if not arg:
                self._add_message(Message("system", "usage: /ask <question>"))
                return
            self.query_one(Input).disabled = True
            self._run_ask(arg)
        elif name == "compare":
            if not arg:
                self._add_message(Message("system", "usage: /compare <question>"))
                return
            self.query_one(Input).disabled = True
            self._run_compare(arg)
        elif name == "save":
            self._handle_save(arg)
        elif name == "resume":
            self._handle_resume(arg)
        elif name == "undo":
            self._handle_undo()
        elif name == "subagents":
            self._handle_subagents(arg)
        elif name == "edit":
            edit_parts = arg.split(maxsplit=1)
            if len(edit_parts) < 2:
                self._add_message(Message("system", "usage: /edit <path> <instructions>"))
                return
            path, instructions = edit_parts
            self.query_one(Input).disabled = True
            self._thinking = self._add_message(Message("thinking", ""))
            self._propose_edit(path, instructions)
        elif name == "task":
            if not arg:
                self._add_message(Message("system", "usage: /task <description>"))
                return
            self._last_task_description = arg
            self.query_one(Input).disabled = True
            self._run_task(arg)
        elif name == "push":
            self._handle_push()
        elif name == "update":
            self.query_one(Input).disabled = True
            self._run_manual_update_check()
        elif name == "apply":
            self._apply_pending_edit()
        elif name == "discard":
            if self._pending_edit:
                self._pending_edit = None
                self._add_message(Message("system", "edit discarded"))
            else:
                self._add_message(Message("system", "no pending edit"))
        elif name == "clear":
            self.action_reset()
        elif name == "quit":
            self.exit()
        else:
            self._add_message(Message("system", f"unknown command '/{name}' — try /help"))

    @work(thread=True)
    def _propose_edit(self, path: str, instructions: str) -> None:
        try:
            proposal = propose_edit(path, instructions, os.getcwd())
        except GenerationCancelled:
            self.call_from_thread(self._finish_cancelled)
            return
        except EditError as e:
            self.call_from_thread(self._finish_turn, None, str(e))
            return
        except Exception as e:
            self.call_from_thread(self._finish_turn, None, f"edit failed: {e}")
            return
        diff = diff_preview(proposal["original"], proposal["updated"])
        self.call_from_thread(self._show_edit_proposal, proposal, diff)

    TASK_MAX_STEPS = 15

    @work(thread=True)
    def _run_task(self, task: str) -> None:
        """Orchestrator/worker loop: Cloud Pro plans each step, on-device
        executes and writes it immediately -- no per-step /apply. Stops when
        Cloud Pro says the task is done, Esc-Esc is pressed, or a safety cap
        on step count is hit.
        """
        cwd = os.getcwd()
        self._loop_running = True
        self._loop_cancel_requested = False
        history: list[str] = []
        recent_instructions: list[str] = []
        start_time = time.monotonic()
        try:
            candidates = sorted(
                f for f in os.listdir(cwd)
                if os.path.isfile(os.path.join(cwd, f)) and not f.startswith(".")
            )

            for step in range(1, self.TASK_MAX_STEPS + 1):
                if self._loop_cancel_requested:
                    self.call_from_thread(self._log_progress, "stopped")
                    break

                self.call_from_thread(
                    self._log_progress, f"step {step}: deciding what to do next…"
                )
                plan = plan_next_step(
                    task, candidates, cwd, history, self.backend, self.subagent_roles["planning"]
                )
                if plan is None:
                    if history:
                        done_message = f"done after {step - 1} step(s) -- run /push to commit and push these changes."
                    else:
                        done_message = "nothing to do -- task already satisfied."
                    self.call_from_thread(self._log_progress, done_message)
                    break

                filename, instructions = plan["file"], plan["instructions"]

                if is_stalling(instructions, recent_instructions):
                    self.call_from_thread(
                        self._log_progress,
                        f"stopped: step {step} looks like a repeat of a recent "
                        f"step, not real progress -- {filename}: {instructions}",
                    )
                    break
                recent_instructions.append(instructions)

                self.call_from_thread(
                    self._log_progress, f"step {step}: {filename} — {instructions}"
                )

                creating = not os.path.isfile(os.path.join(cwd, filename))
                if creating:
                    proposal = propose_new_file(filename, instructions, cwd)
                else:
                    with open(os.path.join(cwd, filename), "r", errors="replace") as f:
                        content = f.read()
                    sections = split_sections(content, filename)
                    section = pick_section(
                        instructions, filename, sections, self.backend, self.subagent_roles["planning"]
                    )
                    proposal = propose_edit(
                        filename, instructions, cwd, line_range=(section["start"], section["end"])
                    )

                with open(proposal["path"], "w") as f:
                    f.write(proposal["updated"])
                self._push_undo(
                    proposal["path"], proposal["label"], proposal["original"],
                    existed_before=not creating,
                )
                if creating and filename not in candidates:
                    candidates = sorted(candidates + [filename])

                diff = diff_preview(proposal["original"], proposal["updated"])
                self.call_from_thread(self._task_step_applied, proposal, diff)
                verb = "created" if creating else "edited"
                history.append(f"{filename}: {verb} -- {proposal['summary']}")
            else:
                self.call_from_thread(
                    self._log_progress,
                    f"stopped after {self.TASK_MAX_STEPS} steps (safety limit)",
                )
        except GenerationCancelled:
            self.call_from_thread(self._log_progress, "stopped")
        except EditError as e:
            self.call_from_thread(self._log_progress, f"error: {e}")
        except Exception as e:
            self.call_from_thread(self._log_progress, f"error: task failed: {e}")
        finally:
            self._loop_running = False
            self.call_from_thread(self._enable_input)
            if time.monotonic() - start_time >= NOTIFY_MIN_SECONDS:
                notify("fm-pcc", f"/task finished: {task}")

    def _task_step_applied(self, proposal: dict, diff: str) -> None:
        self._add_message(
            Message("system", f"wrote {proposal['label']}: {proposal['summary']}\n\n{diff}")
        )

    def _log_progress(self, text: str) -> None:
        self._add_message(Message("system", text))

    @staticmethod
    def _sanitize_session_name(name: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", name)

    def _handle_save(self, arg: str) -> None:
        name = arg.strip()
        if not name:
            self._add_message(Message("system", "usage: /save <name>"))
            return

        os.makedirs(SESSIONS_DIR, exist_ok=True)
        path = os.path.join(SESSIONS_DIR, f"{self._sanitize_session_name(name)}.json")

        on_device_transcript = None
        transcript_path = self.backend._transcript_path
        if transcript_path and os.path.isfile(transcript_path):
            try:
                with open(transcript_path, "r") as f:
                    on_device_transcript = json.load(f)
            except (OSError, json.JSONDecodeError):
                on_device_transcript = None

        snapshot = {
            "model": self.model,
            "turn": self.turn,
            "messages": [{"role": m.role, "text": m.text} for m in self._message_log],
            "on_device_transcript": on_device_transcript,
            "cloud_history": self.backend._cloud_history,
            "ollama_history": self.backend._ollama_history,
        }
        try:
            with open(path, "w") as f:
                json.dump(snapshot, f, indent=2)
        except OSError as e:
            self._add_message(Message("system", f"couldn't save session '{name}': {e}"))
            return
        self._add_message(Message("system", f"saved session '{name}'"))

    def _handle_resume(self, arg: str) -> None:
        name = arg.strip()
        if not name:
            try:
                saved = sorted(
                    f[:-5] for f in os.listdir(SESSIONS_DIR) if f.endswith(".json")
                )
            except OSError:
                saved = []
            if not saved:
                self._add_message(Message("system", "no saved sessions — try /save <name> first"))
            else:
                self._add_message(
                    Message("system", "saved sessions:\n" + "\n".join(f"  {s}" for s in saved))
                )
            return

        path = os.path.join(SESSIONS_DIR, f"{self._sanitize_session_name(name)}.json")
        if not os.path.isfile(path):
            self._add_message(Message("system", f"no saved session named '{name}'"))
            return
        try:
            with open(path, "r") as f:
                snapshot = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            self._add_message(Message("system", f"couldn't read session '{name}': {e}"))
            return

        self.backend._cloud_history = snapshot.get(
            "cloud_history", {m: [] for m in CLOUD_SHORTCUTS}
        )
        self.backend._ollama_history = snapshot.get("ollama_history", {})

        transcript = snapshot.get("on_device_transcript")
        if transcript is not None:
            transcript_path = os.path.join(tempfile.gettempdir(), f"fm-pcc-{uuid.uuid4().hex}.json")
            with open(transcript_path, "w") as f:
                json.dump(transcript, f)
            self.backend._transcript_path = transcript_path
        else:
            self.backend._transcript_path = None

        self.query_one("#log", VerticalScroll).remove_children()
        self._message_log = []
        for m in snapshot.get("messages", []):
            self._add_message(Message(m["role"], m["text"]))

        self.turn = snapshot.get("turn", 0)
        self.model = snapshot.get("model", self.model)
        self._update_chrome()
        self._add_message(Message("system", f"resumed session '{name}'"))

    def _handle_undo(self) -> None:
        if not self._undo_stack:
            self._add_message(Message("system", "nothing to undo"))
            return
        entry = self._undo_stack.pop()
        try:
            if entry.get("existed_before", True):
                with open(entry["path"], "w") as f:
                    f.write(entry["original"])
                self._add_message(Message("system", f"reverted {entry['label']}"))
            else:
                os.remove(entry["path"])
                self._add_message(Message("system", f"removed {entry['label']} (undid its creation)"))
        except OSError as e:
            self._add_message(Message("system", f"couldn't undo the change to {entry['label']}: {e}"))

    def _handle_push(self) -> None:
        cwd = os.getcwd()
        try:
            status = subprocess.run(
                ["git", "-C", cwd, "status", "--porcelain"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            self._add_message(Message("system", f"couldn't check git status: {e}"))
            return
        if status.returncode != 0:
            self._add_message(Message("system", "not a git repository here (or git isn't available)"))
            return
        if not status.stdout.strip():
            self._add_message(Message("system", "nothing to push -- working tree is clean"))
            return

        self.query_one(Input).disabled = True
        self._add_message(Message("system", "pushing…"))
        self._run_push()

    @work(thread=True)
    def _run_push(self) -> None:
        cwd = os.getcwd()
        message = self._last_task_description or "automated changes"
        try:
            add = subprocess.run(
                ["git", "-C", cwd, "add", "-A"], capture_output=True, text=True, timeout=30
            )
            if add.returncode != 0:
                self.call_from_thread(self._push_finished, False, add.stderr.strip())
                return

            commit = subprocess.run(
                ["git", "-C", cwd, "commit", "-m", f"fm-pcc: {message}"],
                capture_output=True, text=True, timeout=30,
            )
            if commit.returncode != 0:
                detail = (commit.stderr or commit.stdout).strip()
                self.call_from_thread(self._push_finished, False, detail)
                return

            push = subprocess.run(
                ["git", "-C", cwd, "push"], capture_output=True, text=True, timeout=120
            )
            if push.returncode != 0:
                detail = (push.stderr or push.stdout).strip()
                self.call_from_thread(self._push_finished, False, detail)
                return
        except (OSError, subprocess.TimeoutExpired) as e:
            self.call_from_thread(self._push_finished, False, str(e))
            return

        self.call_from_thread(self._push_finished, True, "")

    def _push_finished(self, success: bool, detail: str) -> None:
        self._enable_input()
        if success:
            self._add_message(Message("system", "pushed."))
        else:
            self._add_message(Message("system", f"push failed: {detail[:300]}"))

    def _handle_subagents(self, arg: str) -> None:
        if not arg:
            lines = "\n".join(
                f"  {role} → {model_label(model)}" for role, model in self.subagent_roles.items()
            )
            self._add_message(
                Message(
                    "system",
                    f"subagent roles (used by /ask and /task):\n{lines}\n\n"
                    f"set with: /subagents [planning|building] <model>",
                )
            )
            return

        parts = arg.split(maxsplit=1)
        valid_model = len(parts) == 2 and (parts[1] in MODEL_ORDER or parts[1].startswith("ollama:"))
        if len(parts) != 2 or parts[0] not in self.subagent_roles or not valid_model:
            choices = ", ".join(MODEL_ORDER)
            self._add_message(
                Message(
                    "system",
                    f"usage: /subagents [planning|building] <model> — model must be "
                    f"one of: {choices}, or ollama:<name>",
                )
            )
            return

        role, model = parts
        self.subagent_roles[role] = model
        self._add_message(Message("system", f"{role} → {model_label(model)}"))

    @work(thread=True)
    def _run_ask(self, question: str) -> None:
        """Orchestrator/worker for Q&A: the "planning" role either answers
        directly or splits the question into sub-questions, each dispatched
        to the "building" role, then "planning" synthesizes a final answer
        from that research. Read-only -- no files are touched.
        """
        self._loop_running = True
        self._loop_cancel_requested = False
        planning_model = self.subagent_roles["planning"]
        building_model = self.subagent_roles["building"]
        start_time = time.monotonic()
        try:
            self.call_from_thread(
                self._log_progress, f"{model_label(planning_model)} is thinking this through…"
            )
            plan = decompose_question(question, self.backend, planning_model)

            if "answer" in plan:
                self.call_from_thread(self._ask_answered, plan["answer"])
                return

            subquestions = plan["subquestions"]
            self.call_from_thread(
                self._log_progress,
                f"breaking this into {len(subquestions)} sub-question(s) for "
                f"{model_label(building_model)}…",
            )

            subanswers = []
            for i, subq in enumerate(subquestions, start=1):
                if self._loop_cancel_requested:
                    self.call_from_thread(self._log_progress, "stopped")
                    return
                self.call_from_thread(self._log_progress, f"  {i}. {subq}")
                answer = self.backend.classify(subq, building_model)
                subanswers.append((subq, answer))

            self.call_from_thread(
                self._log_progress, f"{model_label(planning_model)} is synthesizing an answer…"
            )
            final = synthesize_answer(question, subanswers, self.backend, planning_model)
            self.call_from_thread(self._ask_answered, final)
        except GenerationCancelled:
            self.call_from_thread(self._log_progress, "stopped")
        except Exception as e:
            self.call_from_thread(self._log_progress, f"error: ask failed: {e}")
        finally:
            self._loop_running = False
            self.call_from_thread(self._enable_input)
            if time.monotonic() - start_time >= NOTIFY_MIN_SECONDS:
                notify("fm-pcc", f"/ask finished: {question}")

    def _ask_answered(self, answer: str) -> None:
        self.turn += 1
        self._add_message(Message("assistant", answer))
        self._update_chrome()

    @work(thread=True)
    def _run_compare(self, question: str) -> None:
        """Ask every model the same question, one at a time, showing each
        answer as it comes back. Uses classify() (history-free) for all of
        them so this never pollutes any model's real conversation.
        """
        self._loop_running = True
        self._loop_cancel_requested = False
        ollama_available = self._ollama_available()
        try:
            for m in MODEL_ORDER:
                if self._loop_cancel_requested:
                    self.call_from_thread(self._log_progress, "stopped")
                    return
                if m in self.backend._icloud_plus_unavailable:
                    self.call_from_thread(
                        self._log_progress, f"skipping {model_label(m)} (requires iCloud+)"
                    )
                    continue
                if m == "ollama" and not ollama_available:
                    self.call_from_thread(
                        self._log_progress, f"skipping {model_label(m)} (not running)"
                    )
                    continue
                self.call_from_thread(self._log_progress, f"asking {model_label(m)}…")
                try:
                    answer = self.backend.classify(question, m)
                except GenerationCancelled:
                    raise
                except Exception as e:
                    answer = f"(error: {e})"
                self.call_from_thread(self._compare_result, m, answer)
        except GenerationCancelled:
            self.call_from_thread(self._log_progress, "stopped")
        finally:
            self._loop_running = False
            self.call_from_thread(self._enable_input)

    def _compare_result(self, model: str, answer: str) -> None:
        self._add_message(
            Message("assistant", f"{model_label(model)}:\n{answer}")
        )

    def _show_edit_proposal(self, proposal: dict, diff: str) -> None:
        if self._thinking is not None:
            self._thinking.remove()
            self._thinking = None
        self._pending_edit = proposal
        self._add_message(
            Message(
                "system",
                f"{proposal['summary']}\n\n{diff}\n"
                f"/apply to write this change to {proposal['label']}, /discard to cancel",
            )
        )
        self._enable_input()

    def _apply_pending_edit(self) -> None:
        if not self._pending_edit:
            self._add_message(Message("system", "no pending edit — try /edit <path> <instructions>"))
            return
        proposal = self._pending_edit
        try:
            with open(proposal["path"], "w") as f:
                f.write(proposal["updated"])
            self._push_undo(proposal["path"], proposal["label"], proposal["original"])
            self._add_message(Message("system", f"wrote {proposal['label']}"))
        except OSError as e:
            self._add_message(Message("system", f"failed to write {proposal['label']}: {e}"))
        self._pending_edit = None

    def _history_up(self) -> None:
        if not self._history:
            return
        input_widget = self.query_one(Input)
        if self._history_index is None:
            self._history_draft = input_widget.value
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        self._recall_history(input_widget)

    def _history_down(self) -> None:
        if self._history_index is None:
            return
        input_widget = self.query_one(Input)
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
        else:
            self._history_index = None
        self._recall_history(input_widget)

    def _recall_history(self, input_widget: Input) -> None:
        value = (
            self._history[self._history_index]
            if self._history_index is not None
            else self._history_draft
        )
        self._suppress_palette_once = True
        input_widget.value = value
        input_widget.cursor_position = len(value)
        self.query_one("#palette", OptionList).display = False

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "input":
            return
        self._update_palette(event.value)

    def _update_palette(self, value: str) -> None:
        if self._model_picker_active:
            # A deferred Input.Changed (e.g. from clearing the input after
            # submit) can arrive after the picker has already opened -- don't
            # let ordinary slash-filter logic clobber it.
            return

        command_name = value[1:].split(" ", 1)[0].lower() if value.startswith("/") else ""
        is_complete = command_name in self.COMMANDS or command_name in self.COMMAND_ALIASES
        self.query_one(Input).styles.color = (
            "#ffff00" if is_complete else self.NORMAL_INPUT_COLOR
        )

        palette = self.query_one("#palette", OptionList)
        if self._suppress_palette_once:
            # Programmatic recall (history up/down) fires a deferred
            # Input.Changed just like typing does -- don't let it reopen the
            # filter dropdown on top of a history recall.
            self._suppress_palette_once = False
            palette.display = False
            return

        if value.startswith("/") and " " not in value:
            query = value[1:].lower()
            matches = [name for name in self.COMMANDS if name.startswith(query)]
            if matches:
                palette.clear_options()
                for name in matches:
                    palette.add_option(Option(f"/{name}  {self.COMMANDS[name]}", id=name))
                palette.highlighted = 0
                palette.display = True
                return
        palette.display = False

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if not self._model_picker_active:
            return
        self._select_model(event.option.id)
        self._close_model_picker()

    def on_key(self, event: events.Key) -> None:
        palette = self.query_one("#palette", OptionList)

        if self._model_picker_active:
            # Up/down/enter are OptionList's own bindings (it has real focus
            # while the picker is open) -- selection comes through
            # on_option_list_option_selected. Only handle what it doesn't.
            if event.key == "tab" and palette.highlighted is not None:
                option = palette.get_option_at_index(palette.highlighted)
                self._select_model(option.id)
                self._close_model_picker()
                event.prevent_default()
                event.stop()
            elif event.key == "escape":
                self._close_model_picker()
                event.prevent_default()
                event.stop()
            return

        if not palette.display:
            if event.key == "up":
                self._history_up()
                event.prevent_default()
                event.stop()
            elif event.key == "down":
                self._history_down()
                event.prevent_default()
                event.stop()
            elif event.key == "escape":
                self._handle_escape()
                event.prevent_default()
                event.stop()
            return
        if event.key == "down":
            palette.action_cursor_down()
            event.prevent_default()
            event.stop()
        elif event.key == "up":
            palette.action_cursor_up()
            event.prevent_default()
            event.stop()
        elif event.key == "tab":
            if palette.highlighted is not None:
                option = palette.get_option_at_index(palette.highlighted)
                input_widget = self.query_one(Input)
                input_widget.value = f"/{option.id} "
                input_widget.cursor_position = len(input_widget.value)
            palette.display = False
            event.prevent_default()
            event.stop()
        elif event.key == "escape":
            palette.display = False
            event.prevent_default()
            event.stop()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#palette", OptionList).display = False
        prompt = event.value.strip()
        if not prompt:
            return
        event.input.value = ""

        if not self._history or self._history[-1] != prompt:
            self._history.append(prompt)
        self._history_index = None
        self._history_draft = ""

        # Always echo what was actually typed, slash command or not -- a
        # command's own response (e.g. /compare's "asking on-device…" log)
        # otherwise appears with no visible record of what was asked.
        self._add_message(Message("user", prompt))

        if prompt.startswith("/"):
            self._handle_command(prompt)
            return

        event.input.disabled = True

        expanded, attachments = expand_file_references(prompt, os.getcwd())
        if attachments:
            self._add_message(Message("system", f"attached: {', '.join(attachments)}"))

        if self.turn == 0:
            cwd = os.getcwd()
            expanded = f"{build_environment_header(cwd)}\n\n{expanded}"
            self._add_message(Message("system", f"context: {cwd} and its contents included"))

        self._thinking = self._add_message(Message("thinking", ""))
        self._respond(expanded)

    @work(thread=True)
    def _respond(self, prompt: str) -> None:
        model = self.model
        try:
            text = self.backend.respond(prompt, model)
            self.call_from_thread(self._finish_turn, text, None)
        except GenerationCancelled:
            self.call_from_thread(self._finish_cancelled)
        except ICloudPlusRequired as e:
            self.call_from_thread(self._finish_icloud_plus_required, model, str(e))
        except Exception as e:
            self.call_from_thread(self._finish_turn, None, str(e))

    def _finish_turn(self, text: str | None, error: str | None) -> None:
        if self._thinking is not None:
            self._thinking.remove()
            self._thinking = None
        if error is not None:
            self._add_message(Message("system", f"error: {error}"))
        else:
            self.turn += 1
            self._add_message(Message("assistant", text))
            self._update_chrome()
        self._enable_input()

    def _finish_icloud_plus_required(self, failed_model: str, error: str) -> None:
        if self._thinking is not None:
            self._thinking.remove()
            self._thinking = None
        fallback = self._previous_model if self._previous_model != failed_model else None
        fallback = fallback or "on-device"
        self._add_message(
            Message(
                "system",
                f"error: {model_label(failed_model)} requires iCloud+ on this "
                f"account ({error}) — switching back to {model_label(fallback)}",
            )
        )
        self.model = fallback
        self._enable_input()

    def _enable_input(self) -> None:
        input_widget = self.query_one(Input)
        input_widget.disabled = False
        input_widget.focus()

    def _finish_cancelled(self) -> None:
        if self._thinking is not None:
            self._thinking.remove()
            self._thinking = None
        self._add_message(Message("system", "stopped"))
        self._enable_input()

    def _handle_escape(self) -> None:
        input_widget = self.query_one(Input)
        if not input_widget.disabled:
            return  # nothing running -- escape has nothing to do here

        now = time.monotonic()
        if self._last_escape_time and now - self._last_escape_time < 1.5:
            self._last_escape_time = 0.0
            if self._loop_running:
                self._loop_cancel_requested = True
            cancel_active_process()
        else:
            self._last_escape_time = now
            self.query_one("#status", Static).update("press esc again to stop…")
            self.set_timer(1.5, self._update_chrome)


def _add_shortcut_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--shortcut-cloud", metavar="NAME",
        help=f"Override the Shortcuts name for cloud (default: {CLOUD_SHORTCUTS['cloud']['name']})",
    )
    parser.add_argument(
        "--shortcut-cloud-pro", metavar="NAME",
        help=f"Override the Shortcuts name for cloud pro (default: {CLOUD_SHORTCUTS['cloud-pro']['name']})",
    )
    parser.add_argument(
        "--ollama-model", metavar="NAME",
        help="Ollama model to use (default: the first one 'ollama list' returns)",
    )
    parser.add_argument(
        "--ollama-host", metavar="URL", default=OLLAMA_HOST_DEFAULT,
        help=f"Ollama server URL (default: {OLLAMA_HOST_DEFAULT})",
    )


def _shortcut_overrides(args: argparse.Namespace) -> dict[str, str]:
    overrides = {}
    if args.shortcut_cloud:
        overrides["cloud"] = args.shortcut_cloud
    if args.shortcut_cloud_pro:
        overrides["cloud-pro"] = args.shortcut_cloud_pro
    return overrides


def _model_arg(value: str) -> str:
    if value in MODEL_ORDER or value.startswith("ollama:"):
        return value
    choices = ", ".join(MODEL_ORDER)
    raise argparse.ArgumentTypeError(
        f"invalid choice: {value!r} (choose from {choices}, or ollama:<name>)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fm-pcc",
        description="Chat with Apple's on-device, cloud, cloud pro, or local Ollama models.",
    )
    sub = parser.add_subparsers(dest="command")

    respond_p = sub.add_parser("respond", help="Non-interactive one-shot response")
    respond_p.add_argument("prompt", nargs="?", help="Prompt (reads stdin if omitted)")
    respond_p.add_argument("-m", "--model", default="cloud-pro", type=_model_arg)
    _add_shortcut_args(respond_p)

    parser.add_argument(
        "-m", "--model", default="on-device", type=_model_arg,
        help="Starting model for the chat TUI (default: on-device)",
    )
    _add_shortcut_args(parser)

    args = parser.parse_args()

    if args.command == "respond":
        prompt = args.prompt or sys.stdin.read().strip()
        if not prompt:
            parser.error("no prompt given (pass as argument or pipe via stdin)")
        expanded, attachments = expand_file_references(prompt, os.getcwd())
        if attachments:
            print(f"[attached: {', '.join(attachments)}]", file=sys.stderr)
        backend = Backend(
            _shortcut_overrides(args), ollama_model=args.ollama_model, ollama_host=args.ollama_host
        )
        print(backend.respond(expanded, args.model))
        return

    ChatApp(
        initial_model=args.model,
        shortcut_overrides=_shortcut_overrides(args),
        ollama_model=args.ollama_model,
        ollama_host=args.ollama_host,
    ).run()


if __name__ == "__main__":
    main()
