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
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Input, Static

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

    def compose(self) -> ComposeResult:
        yield Static(id="banner")
        yield Static(id="subtitle")
        yield VerticalScroll(id="log")
        yield Static(id="status")
        with Horizontal(id="inputbar"):
            yield Static("❯", id="prompt-glyph")
            yield Input(placeholder="Message fm-pcc…", id="input")

    def on_mount(self) -> None:
        self.query_one("#banner", Static).update(
            _gradient(" fm-pcc — Apple Foundation Models Chat", "#f0b429", "#38d9c9")
        )
        self.query_one(Input).focus()
        self._update_chrome()

    def watch_model(self, _value: str) -> None:
        self._update_chrome()

    def _update_chrome(self) -> None:
        accent = MODEL_COLORS[self.model]
        self.query_one("#subtitle", Static).update(
            f" model: {MODEL_LABELS[self.model]} · ctrl+t switch · ctrl+r reset"
        )
        self.query_one("#status", Static).update(
            f"{MODEL_LABELS[self.model]} · turn {self.turn}"
        )
        self.query_one("#inputbar").styles.border = ("round", accent)
        self.query_one("#prompt-glyph", Static).update(Text("❯", style=f"bold {accent}"))

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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        if not prompt:
            return
        event.input.value = ""
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
