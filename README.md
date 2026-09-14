# fm-pcc

A terminal chat UI for Apple's Foundation Models, on-device or Cloud Pro,
in the spirit of `fm chat` or Claude Code's CLI.

Apple's `fm` CLI (macOS 27) only exposes the on-device model — there's no
public CLI or API yet for Apple's cloud-backed tiers. `fm-pcc` fills that gap
for Cloud Pro by driving a Shortcuts "Use Model" action instead.

## How it works

- **On-device** — calls `fm respond` directly, using its own
  `--resume`/`--save-transcript` flags to keep real multi-turn context
  across a conversation.
- **Cloud Pro** — has no CLI/API, so this shells out to a saved Shortcut via
  `shortcuts run` and strips the RTF it returns. Shortcuts has no scriptable
  session concept, so multi-turn context here is approximated by resending
  prior turns as plain text with each new prompt.

## Requirements

- macOS 27 with Apple Intelligence enabled, and the `fm` CLI licensed
  (`fm license`).
- [`uv`](https://docs.astral.sh/uv/), used both to install `fm-pcc` and to
  run it.
- A saved Shortcut named `AppleAI` (or pass `--shortcut <name>`), shaped
  like a "Use Model" action set to **Cloud Pro**, bound to Shortcut Input,
  followed by "Stop and Output" set to the action's response. You don't
  need to build this yourself — see below.

## Install

```
uv tool install git+https://github.com/justwaters/fm-pcc
```

This puts a `fm-pcc` command on your `PATH` (run `uv tool update-shell`
once if a fresh shell can't find it), so it works from any directory.

The first time you actually use Cloud Pro, if the `AppleAI` shortcut isn't
installed yet, `fm-pcc` opens its
[iCloud share link](https://www.icloud.com/shortcuts/13ea99c480a245d5a8a8de6cca0fb397)
for you — tap **Add Shortcut** in the sheet that appears, and it'll pick up
from there. On-device chat works with no setup at all.

For local development, install from a checkout instead:
`uv tool install -e .`

## Usage

```
fm-pcc                              # launch the chat TUI (starts on-device)
fm-pcc --model cloud-pro            # start the TUI on Cloud Pro instead
fm-pcc respond "What is Swift?"     # one-shot, non-interactive (default: cloud-pro)
fm-pcc respond -m on-device "..."   # one-shot on-device
```

### Keybindings (TUI)

| Key      | Action                          |
|----------|----------------------------------|
| `Ctrl+T` | Toggle On-Device / Cloud Pro      |
| `Ctrl+R` | Reset the conversation            |
| `Enter`  | Send the message                  |

## Limitations

- Cloud Pro's "multi-turn context" is just prior turns re-sent as text, not
  a real session — it'll drift on long conversations.
- No streaming; each response is shown once it's fully generated.
