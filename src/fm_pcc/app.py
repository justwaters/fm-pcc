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
import uuid
from dataclasses import dataclass
from typing import Callable

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Input, OptionList, Static
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


def expand_file_references(prompt: str, cwd: str) -> tuple[str, list[str]]:
    """Inline the contents of any @path/to/file mentions found in `prompt`.

    Paths are resolved relative to `cwd` unless already absolute (~ is
    expanded too). Mentions that don't resolve to a readable file are left
    alone -- so stray "@" text (an email, a handle) is harmless. Returns the
    prompt with file contents appended, and the list of resolved labels for
    display.
    """
    attachments: list[str] = []
    seen: set[str] = set()
    extra = ""

    for ref in _FILE_REF_RE.findall(prompt):
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
    """Ask a cloud model to pick the next single edit step, or declare done.

    This is the orchestrator half of the /task loop: it sees the whole task,
    the full content of every candidate file, and a running log of what's
    already been changed, and decides either that nothing more is needed or
    exactly one concrete next step (a file plus specific instructions for
    just that change). The on-device model never sees this -- it only ever
    executes one bounded, already-decided edit at a time.
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
        + "\n\n".join(listing)
        + f"\n\nSteps already taken:\n{progress}\n\n"
        "If the task is now fully done, reply with exactly: DONE\n"
        "Otherwise reply with exactly two lines:\n"
        "FILE: <exact filename to edit next>\n"
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

    matched = next(
        (c for c in candidates if c.lower() == filename.lower() or c.lower() in filename.lower()),
        None,
    )
    if not matched:
        raise EditError(f"model picked an unknown file '{filename}'")

    return {"file": matched, "instructions": instructions}


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


def _ollama_chat(model: str, messages: list[dict], host: str) -> str:
    """POST to Ollama's /api/chat, cancellable the same way as _run().

    Uses http.client directly (stdlib, no new dependency) rather than
    `ollama run <model>` so multi-turn history can be passed natively as
    a messages list instead of re-stuffing prior turns into a text prompt
    the way the Shortcuts-backed cloud tiers have to.
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
    return payload["message"]["content"]


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
        self._ollama_history: list[dict[str, str]] = []

    def shortcut_name(self, model: str) -> str:
        return self._shortcut_overrides.get(model, CLOUD_SHORTCUTS[model]["name"])

    def reset(self) -> None:
        self._transcript_path = None
        for history in self._cloud_history.values():
            history.clear()
        self._ollama_history.clear()

    def respond(self, prompt: str, model: str) -> str:
        if model == "on-device":
            return self._respond_on_device(prompt)
        if model == "ollama":
            return self._respond_ollama(prompt)
        return self._respond_cloud(prompt, model)

    def _resolve_ollama_model(self) -> str:
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

    def _respond_ollama(self, prompt: str) -> str:
        model = self._resolve_ollama_model()
        self._ollama_history.append({"role": "user", "content": prompt})
        try:
            text = _ollama_chat(model, self._ollama_history, self.ollama_host)
        except Exception:
            self._ollama_history.pop()  # don't keep a failed turn in context
            raise
        self._ollama_history.append({"role": "assistant", "content": text})
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
        if model == "ollama":
            return _ollama_chat(
                self._resolve_ollama_model(), [{"role": "user", "content": prompt}], self.ollama_host
            )
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


@dataclass
class Message:
    role: str  # "user" | "assistant" | "system" | "thinking"
    text: str


class MessageWidget(Static):
    """A single turn, rendered as flowing text (no bubble/border chrome)."""

    def __init__(self, message: Message, accent: str):
        if message.role == "user":
            body = Text.from_markup(f"[#7b838a]you ›[/] {message.text}")
        elif message.role == "assistant":
            prefix = Text("fm-pcc ›", style=f"bold {accent}")
            body = Text.assemble(prefix, " ", message.text)
        elif message.role == "thinking":
            body = Text.from_markup(f"[{accent}]· thinking…[/]")
        else:
            body = Text.from_markup(f"[#7b838a italic]{message.text}[/]")
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
    #status { padding: 0 3; color: #7b838a; }
    #palette {
        display: none;
        height: auto;
        max-height: 8;
        margin: 0 2 0 2;
        border: round #7b838a;
        background: #1b2126;
    }
    #palette > .option-list--option-highlighted { background: #2a3138; }
    #inputbar { height: 3; border: round #7b838a; margin: 0 2 1 2; padding: 0 1; }
    #prompt-glyph { width: 2; content-align: center middle; }
    #input { border: none; background: transparent; }
    #input:focus { border: none; }
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
        self._task_running = False
        self._task_cancel_requested = False

    def compose(self) -> ComposeResult:
        yield Static(id="banner")
        yield Static(id="subtitle")
        yield VerticalScroll(id="log")
        yield Static(id="status")
        yield OptionList(id="palette")
        with Horizontal(id="inputbar"):
            yield Static("❯", id="prompt-glyph")
            yield Input(placeholder="Message fm-pcc… (/ for commands)", id="input")

    def on_mount(self) -> None:
        self.query_one("#banner", Static).update(
            _gradient(
                f" fm-pcc v{__version__} — Apple Foundation Models Chat",
                "#f0b429", "#38d9c9",
            )
        )
        self.query_one(Input).focus()
        self._update_chrome()

    def watch_model(self, _value: str) -> None:
        self._update_chrome()

    NORMAL_INPUT_COLOR = "#e7e5dd"

    def _update_chrome(self) -> None:
        accent = MODEL_COLORS[self.model]
        self.query_one("#subtitle", Static).update(
            f" model: {MODEL_LABELS[self.model]} · /help for help"
        )
        self.query_one("#status", Static).update(
            f"{MODEL_LABELS[self.model]} · turn {self.turn}"
        )
        self.query_one("#inputbar").styles.border = ("round", accent)
        self.query_one("#prompt-glyph", Static).update(Text("❯", style=f"bold {accent}"))
        self.query_one(Input).styles.color = self.NORMAL_INPUT_COLOR

    def action_toggle_model(self) -> None:
        next_index = (MODEL_ORDER.index(self.model) + 1) % len(MODEL_ORDER)
        self._select_model(MODEL_ORDER[next_index])

    def _select_model(self, model: str) -> None:
        if model == self.model:
            return
        self.model = model
        self._add_message(Message("system", f"switched to {MODEL_LABELS[self.model]}"))

    def _open_model_picker(self) -> None:
        palette = self.query_one("#palette", OptionList)
        palette.clear_options()
        for key in MODEL_ORDER:
            marker = "● " if key == self.model else "○ "
            palette.add_option(Option(f"{marker}{MODEL_LABELS[key]}", id=key))
        palette.highlighted = MODEL_ORDER.index(self.model)
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

    def _add_message(self, message: Message) -> MessageWidget:
        log = self.query_one("#log", VerticalScroll)
        widget = MessageWidget(message, MODEL_COLORS[self.model])
        log.mount(widget)
        log.scroll_end(animate=False)
        return widget

    COMMANDS = {
        "help": "show this help",
        "model": "open a menu to switch models, or /model <name> directly",
        "edit": "propose an edit: /edit <path> <instructions> (on-device only)",
        "task": "run a multi-step edit loop, writing as it goes: /task <description>",
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
                "  ctrl+t          cycle on-device / cloud / cloud pro\n"
                "  ctrl+r          start a new conversation\n"
                "  enter           send your message\n"
                "  ↑ / ↓           step through what you've sent\n"
                "  esc esc         stop the current response/edit/task\n"
                "  ctrl+c ctrl+c   quit"
            )
            self._add_message(
                Message("system", f"commands:\n{commands}\n\nshortcuts:\n{shortcuts}")
            )
        elif name == "model":
            if not arg:
                self._open_model_picker()
            elif arg in MODEL_LABELS:
                self._select_model(arg)
            else:
                choices = ", ".join(MODEL_ORDER)
                self._add_message(
                    Message("system", f"unknown model '{arg}' — try one of: {choices}")
                )
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
            self.query_one(Input).disabled = True
            self._run_task(arg)
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
        self._task_running = True
        self._task_cancel_requested = False
        history: list[str] = []
        recent_instructions: list[str] = []
        try:
            candidates = sorted(
                f for f in os.listdir(cwd)
                if os.path.isfile(os.path.join(cwd, f)) and not f.startswith(".")
            )
            if not candidates:
                raise EditError("no files in the current directory")

            for step in range(1, self.TASK_MAX_STEPS + 1):
                if self._task_cancel_requested:
                    self.call_from_thread(self._task_progress, "stopped")
                    break

                self.call_from_thread(
                    self._task_progress, f"step {step}: deciding what to do next…"
                )
                plan = plan_next_step(task, candidates, cwd, history, self.backend, "cloud-pro")
                if plan is None:
                    self.call_from_thread(
                        self._task_progress,
                        f"done after {step - 1} step(s)."
                        if step > 1 else "nothing to do -- task already satisfied.",
                    )
                    break

                filename, instructions = plan["file"], plan["instructions"]

                if is_stalling(instructions, recent_instructions):
                    self.call_from_thread(
                        self._task_progress,
                        f"stopped: step {step} looks like a repeat of a recent "
                        f"step, not real progress -- {filename}: {instructions}",
                    )
                    break
                recent_instructions.append(instructions)

                self.call_from_thread(
                    self._task_progress, f"step {step}: {filename} — {instructions}"
                )

                with open(os.path.join(cwd, filename), "r", errors="replace") as f:
                    content = f.read()
                sections = split_sections(content, filename)
                section = pick_section(instructions, filename, sections, self.backend, "cloud-pro")

                proposal = propose_edit(
                    filename, instructions, cwd, line_range=(section["start"], section["end"])
                )
                with open(proposal["path"], "w") as f:
                    f.write(proposal["updated"])

                diff = diff_preview(proposal["original"], proposal["updated"])
                self.call_from_thread(self._task_step_applied, proposal, diff)
                history.append(f"{filename}: {proposal['summary']}")
            else:
                self.call_from_thread(
                    self._task_progress,
                    f"stopped after {self.TASK_MAX_STEPS} steps (safety limit)",
                )
        except GenerationCancelled:
            self.call_from_thread(self._task_progress, "stopped")
        except EditError as e:
            self.call_from_thread(self._task_progress, f"error: {e}")
        except Exception as e:
            self.call_from_thread(self._task_progress, f"error: task failed: {e}")
        finally:
            self._task_running = False
            self.call_from_thread(self._enable_input)

    def _task_step_applied(self, proposal: dict, diff: str) -> None:
        self._add_message(
            Message("system", f"wrote {proposal['label']}: {proposal['summary']}\n\n{diff}")
        )

    def _task_progress(self, text: str) -> None:
        self._add_message(Message("system", text))

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

        if prompt.startswith("/"):
            self._handle_command(prompt)
            return

        event.input.disabled = True
        self._add_message(Message("user", prompt))

        expanded, attachments = expand_file_references(prompt, os.getcwd())
        if attachments:
            self._add_message(Message("system", f"attached: {', '.join(attachments)}"))

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
            if self._task_running:
                self._task_cancel_requested = True
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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fm-pcc",
        description="Chat with Apple's on-device, cloud, cloud pro, or local Ollama models.",
    )
    sub = parser.add_subparsers(dest="command")

    respond_p = sub.add_parser("respond", help="Non-interactive one-shot response")
    respond_p.add_argument("prompt", nargs="?", help="Prompt (reads stdin if omitted)")
    respond_p.add_argument("-m", "--model", default="cloud-pro", choices=MODEL_ORDER)
    _add_shortcut_args(respond_p)

    parser.add_argument(
        "-m", "--model", default="on-device", choices=MODEL_ORDER,
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
