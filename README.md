# fm-pcc

A terminal chat UI for Apple's Foundation Models — on-device, cloud, or
cloud pro — plus local [Ollama](https://ollama.com) models, in the spirit
of `fm chat` or Claude Code's CLI.

Apple's `fm` CLI (macOS 27) only exposes the on-device model — there's no
public CLI or API yet for Apple's cloud-backed tiers. `fm-pcc` fills that gap
by driving a "Use Model" Shortcut for each cloud tier instead.

## How it works

- **On-device** — calls `fm respond` directly, using its own
  `--resume`/`--save-transcript` flags to keep real multi-turn context
  across a conversation.
- **Cloud / Cloud Pro** — have no CLI/API, so this shells out to a saved
  Shortcut per tier via `shortcuts run` and strips the RTF it returns.
  Shortcuts has no scriptable session concept, so multi-turn context here is
  approximated by resending prior turns as plain text with each new prompt.
- **Ollama** — talks to a local `ollama serve` over HTTP (`/api/chat`,
  stdlib `http.client`, no extra dependency), passing real multi-turn
  message history natively instead of the resend-as-text approximation the
  Shortcuts-backed tiers need. Every locally installed model shows up as
  its own entry in the `/model` menu (`ollama:<name>`), each with
  independent conversation history; picking bare `ollama` auto-resolves to
  whichever one `ollama list` returns first.

## Requirements

- macOS 27 with Apple Intelligence enabled, and the `fm` CLI licensed
  (`fm license`).
- [`uv`](https://docs.astral.sh/uv/), used both to install `fm-pcc` and to
  run it.
- Two saved Shortcuts, each a "Use Model" action bound to Shortcut Input
  followed by "Stop and Output" set to the action's response — one set to
  **Cloud** named `PCC-Cloud`, one set to **Cloud Pro** named
  `PCC-CloudPro` (override either name with `--shortcut-cloud`/
  `--shortcut-cloud-pro` if you named yours differently). You don't need to
  build these yourself — see below.
- Optional: [Ollama](https://ollama.com) installed and running
  (`ollama serve`) with at least one model pulled (`ollama pull llama3.2`)
  if you want the `ollama` model — everything else works without it.

## Install

```
uv tool install git+https://github.com/justwaters/fm-pcc
```

This puts a `fm-pcc` command on your `PATH` (run `uv tool update-shell`
once if a fresh shell can't find it), so it works from any directory.

The first time you actually use Cloud or Cloud Pro, if its shortcut isn't
installed yet, `fm-pcc` opens its iCloud share link for you —
[Cloud](https://www.icloud.com/shortcuts/9f4e45968b974ef7ad8d29eb06f98a9b),
[Cloud Pro](https://www.icloud.com/shortcuts/7f7f8e41dfee459a89c28ca0b8c60984)
— tap **Add Shortcut** in the sheet that appears, and it'll pick up from
there. On-device chat works with no setup at all.

For local development, install from a checkout instead:
`uv tool install -e .`

## Usage

```
fm-pcc                                    # launch the chat TUI (starts on-device)
fm-pcc --model cloud-pro                  # start the TUI on cloud pro instead
fm-pcc respond "What is Swift?"           # one-shot, non-interactive (default: cloud-pro)
fm-pcc respond -m on-device "..."         # one-shot on-device
fm-pcc --model ollama                     # start the TUI on Ollama (first model ollama list has)
fm-pcc --model ollama --ollama-model llama3.2   # ...or a specific one
```

### In the chat

On startup, if the current directory is a git repo and/or has a README,
fm-pcc shows a one-line orientation note (repo name, file count, the
README's first line) — no model call involved, just local filesystem
and `git rev-parse`.

Replies are rendered as markdown (headings, bold, lists, code blocks) —
cloud/Ollama models routinely answer in markdown, and plain text renders
through unchanged either way.

Reference a local file by mentioning `@path/to/file` anywhere in your
message (relative to wherever you ran `fm-pcc`, or an absolute path) — its
contents get inlined into what's actually sent to the model, and fm-pcc
shows an `attached: ...` note so you can see what went out. `@readme` is a
special case that resolves to whichever README variant actually exists
(`README.md`, `README`, `README.rst`, ...) since the real filename varies
by project.

Type `/` and a live palette pops up above the input, filtering as you keep
typing — same as Claude Code or the Gemini CLI. `↑`/`↓` moves the
highlight, `Tab` completes the highlighted command into the input,
`Escape` dismisses it.

Slash commands, same spirit as `fm chat`:

| Command                     | Action                                              |
|-----------------------------|-------------------------------------------------------|
| `/model`                    | Open a menu to pick a model (`↑`/`↓`, `Enter`/`Tab`) — locally installed Ollama models are listed individually in a tree under `ollama` |
| `/model <name>`             | Switch directly — `on-device`, `cloud`, `cloud-pro`, `ollama`, or `ollama:<name>` for a specific local model |
| `/edit <path> <instructions>` | Propose an edit to a file (on-device only, see below) |
| `/task <description>`       | Multi-step edit loop that writes as it goes (see below) |
| `/ask <question>`           | Research a question via cloud/core subagents (see below) |
| `/subagents [planning\|building] <model>` | Show or set which model plays each subagent role  |
| `/compare <question>`       | Ask every model the same question, one at a time       |
| `/save <name>`               | Save the conversation under a name                     |
| `/resume [name]`             | Resume a saved conversation, or list saved ones        |
| `/undo`                      | Revert the last file write made by `/edit` or `/task`  |
| `/apply`                    | Write the pending edit proposed by `/edit`             |
| `/discard`                  | Discard the pending edit proposed by `/edit`           |
| `/clear`                    | Start a new conversation                               |
| `/help`                     | List commands and shortcuts                            |
| `/quit`                     | Exit                                                   |

`/edit` shows a diff and waits for `/apply` before touching disk — nothing
is written automatically. It only works on-device: it uses guided
generation (`fm respond --schema`) to get a structured line-anchored edit
(a line number plus freshly-written replacement/insertion text) rather than
asking the model to reproduce file content verbatim, which the on-device
model is unreliable at for anything spanning more than one line. Neither
cloud tier has equivalent schema control via Shortcuts, so editing isn't
available there yet.

`/task <description>` runs an orchestrator/worker loop instead of `/edit`'s
single reviewed change: Cloud Pro plans, on-device executes and **writes
immediately, with no per-step `/apply`** — closer to a subagent that Cloud
Pro keeps dispatching to than a one-shot assistant. Each iteration, Cloud
Pro sees the task, the full content of every file in the directory, and a
running log of what's already been done, then either says the task is
done or picks one concrete next step (a file plus specific instructions).
That file gets deterministically split into sections (real top-level
blocks for brace languages like CSS/JS, fixed-size chunks otherwise — not
model-summarized, since that's a mechanical task a parser gets right for
free), Cloud Pro picks the relevant section, and on-device drafts and
writes the edit scoped to it — the same machinery `/edit` uses. This
repeats until Cloud Pro says done or a 15-step safety cap is hit. `Esc`
`Esc` stops it between steps.

This is genuinely autonomous, so it can genuinely go wrong: on real
testing, a task needing cleanup/consolidation (not just clean additions)
made the loop spiral — each step only had room to insert or replace one
line-anchored region, so instead of removing stray content from an
earlier step it kept adding more, without ever converging. To catch this,
each step's instructions are compared against the last few for suspicious
similarity (Cloud Pro asking for essentially the same fix again, worded
slightly differently is what non-convergence actually looked like in
practice) — if it looks like a repeat, the loop stops immediately with a
"not making progress" message instead of grinding to the step cap and
compounding the damage. It's a mitigation, not a fix for the underlying
cause: `/task` is best suited to clean, additive changes, and worth
watching (or interrupting) on anything that requires cleanup.

`/subagents` controls which model plays each of two roles, **planning**
(planning/judgment calls — defaults to `cloud-pro`) and **building**
(fast/local execution — defaults to `on-device`), used by both `/task`
(its planning and section-picking) and `/ask` (below). Either role can be
set to any model, including a specific `ollama:<name>`. One thing this
*doesn't* change: `/task`'s actual file-writing step always runs on-device
regardless of the "building" setting, since it's the only backend with the
guided-generation schema support editing requires — this is a real
technical constraint, not a default that "building" can override.

`/ask <question>` is a similar orchestrator/worker pattern applied to
research instead of editing, entirely read-only. The **planning** role
either answers directly (if it's confident it can) or splits the question
into a few sub-questions; each sub-question is dispatched to the
**building** role; then **planning** synthesizes a final answer from that
research. Cheap questions just get answered directly in one round-trip —
decomposition only kicks in when the planning role itself judges it would
help.

`/compare <question>` asks every model (on-device, cloud, cloud pro,
ollama) the same question, one at a time, and shows each answer as it
comes back — useful for seeing how they actually differ on a given
prompt. Uses history-free calls for all of them, so it never affects any
model's real conversation; if one model errors (e.g. Ollama isn't
running), that shows up as its answer instead of aborting the rest.

`/save <name>` writes the current conversation to
`~/.fm-pcc/sessions/<name>.json` — every message shown on screen, which
model/turn you were on, on-device's real transcript (so `fm respond
--resume` picks back up correctly), and the resend-as-text history for
cloud/cloud-pro/Ollama. `/resume <name>` clears the screen and restores
all of that; `/resume` with no name lists what's saved instead of
resuming anything.

`/undo` reverts the most recent file write made by `/apply` or `/task`,
restoring that file's exact prior content. It's a stack — repeated
`/undo` walks back further, up to the last 20 writes across both
commands — not a single-slot toggle, so it composes with a `/task` run
that made several changes.

`/task` and `/ask` post a macOS notification when they finish, but only
if the run took 5 seconds or longer — quick ones don't bother you. This
is best-effort: it shells out to `osascript` and silently does nothing
if that fails (e.g. notifications aren't permitted), rather than
interrupting the run itself.

### Statusline

The line just above the input reads `directory (branch) | model |
Context:xx%`, in the spirit of Claude Code's own statusline — directory
and branch are just `cwd`'s basename and `git branch --show-current`
(the `(branch)` part is omitted outside a git repo). The context
percentage is exact where it can be:

- **on-device** — `fm count-tokens --transcript` against the documented
  4,096-token session limit for the on-device model.
- **ollama** — the real token counts Ollama returns with every chat
  response, against that model's own context length from `/api/show`.

Neither Apple's Shortcuts bridge nor its cloud/cloud-pro tiers expose any
token accounting, so there's no way to measure either number for those —
**cloud/cloud-pro's percentage is a rough chars÷4 estimate against a
guessed 32k context window**, not a real measurement. Treat it as a
rough indicator, not a reliable count, for those two tiers specifically.

### Keybindings (TUI)

| Key      | Action                                    |
|----------|----------------------------------------------|
| `Ctrl+T` | Cycle on-device / cloud / cloud pro / ollama   |
| `Ctrl+R` | Reset the conversation                        |
| `Enter`  | Send the message                              |
| `↑`/`↓`  | Step back/forward through what you've sent (when no dropdown is open) |
| `Esc` `Esc` | Stop the response/edit/task currently in progress          |
| `Ctrl+C` `Ctrl+C` | Quit                                              |

The first `Esc` just warns you; the second, pressed within about 1.5s,
actually kills the underlying `fm`/`shortcuts` process rather than merely
resetting the UI — so a stopped on-device turn never finishes writing its
transcript, and a stopped edit never reaches disk. `Ctrl+C` works the same
way (first press warns, second quits), and also cancels anything running
first so quitting mid-response doesn't hang waiting on it.

### Image analysis (not yet in fm-pcc's chat)

fm-pcc's chat is text-only for now, but the underlying `fm` CLI can also
analyze images directly, with two built-in tools worth knowing about:

```
fm respond --image photo.jpg --tool ocr "transcribe the text in this image"
fm respond --image photo.jpg --tool barcode "what does the barcode/QR code say?"
```

These aren't just prompt hints — they call real Vision framework requests
(`VNRecognizeTextRequest`/`VNDetectBarcodesRequest`) rather than relying on
the model's own vision understanding of the image pixels. That distinction
matters in practice: asked to decode a QR code *without* `--tool barcode`,
the model confidently returned a plausible-looking but entirely made-up
value, since it has no real way to decode barcode pixel patterns; with the
tool, it decoded correctly. Similarly, `--tool ocr` transcribed a
multi-line block of dense text (including an alphanumeric reference code)
exactly, where the model's unassisted reading of the same image dropped a
character and a space.

## Limitations

- Cloud/Cloud Pro's "multi-turn context" is just prior turns re-sent as
  text, not a real session — it'll drift on long conversations.
- No streaming; each response is shown once it's fully generated.
