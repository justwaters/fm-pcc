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
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from . import __version__

MODEL_LABELS = {
    "on-device": "on-device",
    "cloud-pro": "cloud pro",
}
MODEL_COLORS = {
    "on-device": "#f0b429",
    "cloud-pro": "#38d9c9",
}
CLOUD_PRO_SHORTCUT = "AppleAI"
CLOUD_PRO_SHORTCUT_URL = "https://www.icloud.com/shortcuts/13ea99c480a245d5a8a8de6cca0fb397"

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


def propose_edit(path: str, instructions: str, cwd: str) -> dict:
    """Ask the on-device model for a line-anchored edit to `path`.

    Guided generation on a small on-device model is unreliable at copying
    multi-line source text verbatim into a JSON string -- it reliably mangles
    escaping and truncates. So instead of asking for an old/new text snippet,
    this shows the file as numbered lines and asks for a line number plus
    freshly-*written* replacement text, which the model is much better at.
    We do the actual line lookup ourselves, so there's no verbatim-matching
    step to fail. On-device only for now -- Cloud Pro (via Shortcuts) has no
    schema control to constrain output the same way.
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

    if len(original) > MAX_FILE_CHARS:
        raise EditError(
            f"{label} is too large to edit on-device "
            f"(over {MAX_FILE_CHARS} characters)"
        )

    lines = original.splitlines()
    numbered = "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines))

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
        result = subprocess.run(
            ["fm", "respond", "--model", "system", "--no-stream", "--greedy",
             "--schema", schema_path, prompt],
            capture_output=True, text=True,
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

    if not isinstance(anchor, int) or not (1 <= anchor <= max(len(lines), 1)):
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

    on_status(f"'{name}' shortcut not found — it's needed for Cloud Pro.")
    on_status("Opening the install prompt in Shortcuts (tap “Add Shortcut”)…")
    subprocess.run(["open", url])

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if name in _installed_shortcuts():
            on_status(f"'{name}' installed.")
            return True
        time.sleep(1.5)

    on_status(
        f"Still waiting on '{name}'. On-device chat works without it — "
        f"try Cloud Pro again once you've added it."
    )
    return False


class Backend:
    """Talks to fm (on-device) and Shortcuts (Cloud Pro)."""

    def __init__(
        self,
        shortcut: str = CLOUD_PRO_SHORTCUT,
        shortcut_url: str = CLOUD_PRO_SHORTCUT_URL,
        on_status: Callable[[str], None] = print,
    ):
        self.shortcut = shortcut
        self.shortcut_url = shortcut_url
        self.on_status = on_status
        self._shortcut_ready = False
        self._transcript_path: str | None = None
        self._cloud_history: list[tuple[str, str]] = []

    def reset(self) -> None:
        self._transcript_path = None
        self._cloud_history.clear()

    def respond(self, prompt: str, model: str) -> str:
        if model == "on-device":
            return self._respond_on_device(prompt)
        return self._respond_cloud_pro(prompt)

    def _respond_on_device(self, prompt: str) -> str:
        if self._transcript_path is None:
            self._transcript_path = os.path.join(
                tempfile.gettempdir(), f"fm-pcc-{uuid.uuid4().hex}.json"
            )

        args = ["fm", "respond", "--no-stream"]
        if os.path.exists(self._transcript_path):
            args += ["--resume", self._transcript_path]
        args += ["--save-transcript", self._transcript_path, prompt]

        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "fm respond failed")
        return result.stdout.strip()

    def _respond_cloud_pro(self, prompt: str) -> str:
        if not self._shortcut_ready:
            self._shortcut_ready = ensure_shortcut_installed(
                self.shortcut, self.shortcut_url, self.on_status
            )

        full_prompt = prompt
        if self._cloud_history:
            context = "\n\n".join(
                f"User: {u}\nAssistant: {a}" for u, a in self._cloud_history
            )
            full_prompt = f"{context}\n\nUser: {prompt}"

        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, "input.txt")
            out_path = os.path.join(tmp, "output.rtf")
            with open(in_path, "w") as f:
                f.write(full_prompt)

            result = subprocess.run(
                ["shortcuts", "run", self.shortcut, "-i", in_path, "-o", out_path],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(
                    f"Shortcut '{self.shortcut}' failed: {detail}\n"
                    f"Check it exists ('shortcuts list') and its 'Use Model' "
                    f"action is bound to Shortcut Input."
                )
            if not os.path.exists(out_path):
                raise RuntimeError(f"Shortcut '{self.shortcut}' produced no output.")

            text = subprocess.run(
                ["textutil", "-convert", "txt", "-stdout", out_path],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

        self._cloud_history.append((prompt, text))
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
    ]

    model: reactive[str] = reactive("on-device")

    def __init__(self, initial_model: str = "on-device", shortcut: str = CLOUD_PRO_SHORTCUT):
        super().__init__()
        self.backend = Backend(
            shortcut,
            on_status=lambda msg: self.call_from_thread(
                self._add_message, Message("system", msg)
            ),
        )
        self.set_reactive(ChatApp.model, initial_model)
        self.turn = 0
        self._thinking: MessageWidget | None = None
        self._pending_edit: dict | None = None

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
        self.model = "cloud-pro" if self.model == "on-device" else "on-device"
        self._add_message(Message("system", f"switched to {MODEL_LABELS[self.model]}"))

    def action_reset(self) -> None:
        self.backend.reset()
        self.turn = 0
        self.query_one("#log", VerticalScroll).remove_children()
        self._update_chrome()
        self._add_message(Message("system", "conversation reset"))

    def _add_message(self, message: Message) -> MessageWidget:
        log = self.query_one("#log", VerticalScroll)
        widget = MessageWidget(message, MODEL_COLORS[self.model])
        log.mount(widget)
        log.scroll_end(animate=False)
        return widget

    COMMANDS = {
        "help": "show this help",
        "model": "show, or switch, the active model (on-device, cloud-pro)",
        "edit": "propose an edit: /edit <path> <instructions> (on-device only)",
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
                "  ctrl+t  toggle on-device / cloud pro\n"
                "  ctrl+r  start a new conversation\n"
                "  enter   send your message"
            )
            self._add_message(
                Message("system", f"commands:\n{commands}\n\nshortcuts:\n{shortcuts}")
            )
        elif name == "model":
            if not arg:
                self._add_message(Message("system", f"model: {MODEL_LABELS[self.model]}"))
            elif arg in MODEL_LABELS:
                self.model = arg
                self._add_message(Message("system", f"switched to {MODEL_LABELS[self.model]}"))
            else:
                self._add_message(
                    Message("system", f"unknown model '{arg}' — try on-device or cloud-pro")
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
        except EditError as e:
            self.call_from_thread(self._finish_turn, None, str(e))
            return
        except Exception as e:
            self.call_from_thread(self._finish_turn, None, f"edit failed: {e}")
            return
        diff = diff_preview(proposal["original"], proposal["updated"])
        self.call_from_thread(self._show_edit_proposal, proposal, diff)

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

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "input":
            return
        self._update_palette(event.value)

    def _update_palette(self, value: str) -> None:
        command_name = value[1:].split(" ", 1)[0].lower() if value.startswith("/") else ""
        is_complete = command_name in self.COMMANDS or command_name in self.COMMAND_ALIASES
        self.query_one(Input).styles.color = (
            "#ffff00" if is_complete else self.NORMAL_INPUT_COLOR
        )

        palette = self.query_one("#palette", OptionList)
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

    def on_key(self, event: events.Key) -> None:
        palette = self.query_one("#palette", OptionList)
        if not palette.display:
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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fm-pcc", description="Chat with Apple's on-device or Cloud Pro models."
    )
    sub = parser.add_subparsers(dest="command")

    respond_p = sub.add_parser("respond", help="Non-interactive one-shot response")
    respond_p.add_argument("prompt", nargs="?", help="Prompt (reads stdin if omitted)")
    respond_p.add_argument(
        "-m", "--model", default="cloud-pro", choices=["on-device", "cloud-pro"]
    )
    respond_p.add_argument("--shortcut", default=CLOUD_PRO_SHORTCUT)

    parser.add_argument(
        "-m", "--model", default="on-device", choices=["on-device", "cloud-pro"],
        help="Starting model for the chat TUI (default: on-device)",
    )
    parser.add_argument("--shortcut", default=CLOUD_PRO_SHORTCUT)

    args = parser.parse_args()

    if args.command == "respond":
        prompt = args.prompt or sys.stdin.read().strip()
        if not prompt:
            parser.error("no prompt given (pass as argument or pipe via stdin)")
        expanded, attachments = expand_file_references(prompt, os.getcwd())
        if attachments:
            print(f"[attached: {', '.join(attachments)}]", file=sys.stderr)
        backend = Backend(args.shortcut)
        print(backend.respond(expanded, args.model))
        return

    ChatApp(initial_model=args.model, shortcut=args.shortcut).run()


if __name__ == "__main__":
    main()
