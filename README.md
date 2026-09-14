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
- [`uv`](https://docs.astral.sh/uv/) — the script declares its own
  dependencies and `uv run --script` installs them on first use.
- A saved Shortcut named `AppleAI` (or pass `--shortcut <name>`) shaped like:

  ```
  Receive [Apps and 18 more] from Nowhere  (if no input: Continue)
  Use Cloud Pro model                       (Request: Shortcut Input)
  Stop and Output Response
  ```

  Build this once in the Shortcuts app: add a "Use Model" action, pick
  **Cloud Pro** from its model dropdown, drag **Shortcut Input** into the
  Request field, then add "Stop and Output" set to the action's response.

## Usage

```
./fm-pcc                              # launch the chat TUI (starts on-device)
./fm-pcc --model cloud-pro            # start the TUI on Cloud Pro instead
./fm-pcc respond "What is Swift?"     # one-shot, non-interactive (default: cloud-pro)
./fm-pcc respond -m on-device "..."   # one-shot on-device
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
