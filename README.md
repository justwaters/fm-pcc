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

**Cloud tiers fail sometimes** — a usage limit, a network blip, Cloud
Pro specifically also requires the signed-in account to have iCloud+
(this doesn't seem to apply to plain Cloud) — and fm-pcc handles this by
falling back rather than just showing an error: **cloud-pro → cloud →
on-device**, in that order, stopping at the first tier that actually
works. This applies to normal chat and to `/task`'s and `/ask`'s
planning role (which defaults to Cloud Pro) — `/compare` is the one
exception, since it deliberately wants each tier's own real answer (or a
clear skip) rather than a substituted one. Whichever tier actually
answered becomes the active model (and, for `/task`/`/ask`, the new
planning role) going forward, with a message explaining why.

A tier that's failed gets greyed out in `/model` so it isn't retried on
every single message. iCloud+ specifically is remembered in
`~/.fm-pcc/state.json` — not just for the session, but across quitting,
updating, and relaunching fm-pcc, since it's a real, persistent account
fact. Everything else (usage limits, network trouble, Ollama not
running) is only tracked for the current session, since those are
transient and a permanent memory of them would eventually be wrong —
restarting fm-pcc, or running `/model reset`, clears all of it and lets
those tiers be tried again.

For local development, install from a checkout instead:
`uv tool install -e .`

### Development

`tests/test_on_device_capabilities.py` runs real `/task` runs against the
actual on-device model in scratch temp directories — creating/renaming/
moving files and folders, and git add/commit/push/branch behavior — since
the bugs it guards against were all cases where the model's real behavior
didn't match what the code assumed (a small on-device model asked to
"create a folder" once created a *file* named `test` instead, because
`/task` had no folder-creation action at all). Run it directly with
`uv run tests/test_on_device_capabilities.py`; it exits 0 (including
"skipped, no on-device model available" on a machine without one set up)
or 1 on a real behavioral failure.

It forces *both* of `/task`'s subagent roles to on-device, including
planning — a real `/task` run defaults to Cloud Pro for planning and only
executes on-device, which is meaningfully more reliable (a bigger model
planning for a small one to execute). Testing the harder, fully-offline
configuration on purpose surfaced several real on-device planner
confusions this way (e.g. writing a whole sentence where a destination
path belonged, or not recognizing a one-step task as complete and
proposing a redundant follow-up) that got fixed with clearer prompting
and, where prompting alone wasn't reliable enough, deterministic
recovery/rejection in `plan_next_step` itself — catching a known bad
pattern in code rather than continuing to hope the model avoids it.

To have it block commits automatically, point git at this repo's tracked
hooks directory (once per clone):

```
git config core.hooksPath scripts/git-hooks
```

Not on by default — git only trusts a hooks path you've explicitly set.

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

The model has no real awareness of your filesystem on its own —
verified directly against `fm respond`, not just fm-pcc's own prompting:
asked "what directory are we in?" with no context at all, it confidently
answers with a plausible-sounding but entirely made-up path. So the
first message of every conversation has the real working directory and
its top-level contents (names only, not file contents — that's what
`@file` is for) silently included alongside what you typed, shown as a
`context: ...` note. Only the first message pays this cost; each
backend's own multi-turn memory (on-device's `--resume` transcript, the
cloud tiers' resend-as-text history, Ollama's native message array)
keeps it in view for the rest of that conversation.

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
| `/model reset`              | Clear any "requires iCloud+" restrictions and retry those tiers |
| `/edit <path> <instructions>` | Propose an edit to a file (on-device only, see below) |
| `/task <description>`       | Multi-step edit loop that writes as it goes (see below) |
| `/ask <question>`           | Research a question via cloud/core subagents (see below) |
| `/subagents [planning\|building] <model>` | Show or set which model plays each subagent role  |
| `/compare <question>`       | Ask every model the same question, one at a time       |
| `/save <name>`               | Save the conversation under a name                     |
| `/resume [name]`             | Resume a saved conversation, or list saved ones        |
| `/undo`                      | Revert the last file write, folder creation, or move made by `/edit` or `/task` |
| `/push`                      | Commit and push the current changes to git             |
| `/pull`                      | Pull the latest changes from git                        |
| `/branch create <name>`      | Create a new git branch and switch to it                |
| `/branch switch <name>`      | Switch to an existing git branch                        |
| `/update`                    | Check for a newer version now, and say why if it can't tell |
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
Pro sees the task, the full content of every file in the directory, which
folders already exist, and a running log of what's already been done,
then either says the task is done or picks exactly one of:

- **edit** an existing file (the same section-scoped, line-anchored
  machinery `/edit` uses)
- **create** a new file, optionally inside a folder that already exists
  (or was created moments earlier in the same run) — this is what makes
  `/task make me a website about <product>` work starting from an empty
  directory: the first step creates `index.html`, later steps can create
  `style.css` or edit either file further
- **create** a new, empty folder
- **rename** or **move** an existing file or folder
- a **git** operation — see below

Every path is restricted to somewhere inside the current directory (no
absolute paths, no `..`) for all of these. This repeats until Cloud Pro
says done or a 15-step safety cap is hit; a step that looks like a
near-repeat of a recent one (compared only against other steps of the
*same* kind, so e.g. creating a folder and then a file inside it never
look like a false repeat of each other) stops the loop early rather than
spiraling. `Esc` `Esc` stops it between steps.

`/task` can plan git operations too, but treats them in two tiers. Staging
and committing locally (`git add`, `git commit`) happen automatically, same
as any other step — they're local and reversible. Pushing, pulling, and
creating or switching branches never happen automatically: if the task
calls for one, `/task` stops and tells you to run the matching command
yourself (`/push`, `/pull`, `/branch create <name>`, `/branch switch
<name>`) to confirm it, then re-run `/task` to continue. This mirrors how
`/push` already worked before `/task` could touch git at all: file and
local-commit changes happen automatically as `/task` runs, but nothing
leaves your machine, and nothing switches you to a different branch,
until you explicitly ask it to.

All of `/task`'s actions (edit, create file, create folder, rename, move,
each git operation) are implemented as a fixed, named vocabulary the
planning model picks exactly one from per step, each backed by its own
plain Python function that actually performs it — a deterministic
"skills" dispatch, in effect. That's deliberate, not a placeholder for
something fancier: `fm serve`'s OpenAI-style tool-calling was tried first
and is broken on this OS build (it leaks raw, unparsed generation text
instead of returning structured tool calls), and a model-invoked tool is
a weaker guarantee anyway, since a model can simply choose not to call it
and hallucinate regardless — the same failure mode a fixed-vocabulary
planner avoids by construction.

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
model's real conversation. It skips whichever models it already knows
aren't reachable (a cloud tier flagged as requiring iCloud+ or otherwise
unavailable this session, or Ollama when it isn't running/has no
models) rather than pointlessly calling them to watch them fail —
deliberately *not* falling back the way chat/`/task`/`/ask` do, since
the whole point here is seeing each tier's own real answer, not a
substituted one wearing the wrong label. Anything else that errors
mid-run still shows up as that model's answer instead of aborting the
rest.

`/save <name>` writes the current conversation to
`~/.fm-pcc/sessions/<name>.json` — every message shown on screen, which
model/turn you were on, on-device's real transcript (so `fm respond
--resume` picks back up correctly), and the resend-as-text history for
cloud/cloud-pro/Ollama. `/resume <name>` clears the screen and restores
all of that; `/resume` with no name lists what's saved instead of
resuming anything.

`/undo` reverts the most recent change made by `/apply` or `/task` —
restoring a file's exact prior content, removing a file or folder that
step created rather than leaving an empty one behind, or moving a
renamed/moved file or folder back where it came from. It's a stack —
repeated `/undo` walks back further, up to the last 20 changes across
both commands — not a single-slot toggle, so it composes with a `/task`
run that made several changes. Undoing a folder's creation only works if
it's still empty — if a later step put something inside it, that's left
alone rather than silently deleting a whole tree of other changes.

`/task` and `/ask` post a macOS notification when they finish, but only
if the run took 5 seconds or longer — quick ones don't bother you. This
is best-effort: it shells out to `osascript` and silently does nothing
if that fails (e.g. notifications aren't permitted), rather than
interrupting the run itself.

### Statusline

The line below the input reads `directory (branch) | model |
Context:xx%`, in the spirit of Claude Code's own statusline — directory
and branch are just `cwd`'s basename and `git branch --show-current`
(the `(branch)` part is omitted outside a git repo). Its text color
reflects which of three families is active — on-device, any PCC tier
(cloud or cloud-pro, sharing one color), or Ollama — a coarser grouping
than the four individual accent colors used for the input border and
prompt glyph elsewhere. The context percentage is exact where it can be:

- **on-device** — `fm count-tokens --transcript` against the documented
  4,096-token session limit for the on-device model.
- **ollama** — the real token counts Ollama returns with every chat
  response, against that model's own context length from `/api/show`.

Neither Apple's Shortcuts bridge nor its cloud/cloud-pro tiers expose any
token accounting, so there's no way to measure either number for those —
**cloud/cloud-pro's percentage is a rough chars÷4 estimate against a
guessed 32k context window**, not a real measurement. Treat it as a
rough indicator, not a reliable count, for those two tiers specifically.

Next to the statusline, on the right, an **Update (vX.XX -> vY.YY)**
button appears whenever a newer version is available — checked once at
startup against this repo's [latest GitHub
release](https://github.com/justwaters/fm-pcc/releases/latest). It's
invisible the rest of the time, when you're already up to date. Clicking
it runs `uv tool upgrade fm-pcc` in the background and reports success or
failure as a message; since the running process already has its own code
loaded in memory, you'll need to restart fm-pcc afterward to actually use
the new version. `/update` re-runs that same check on demand and always
reports something — up to date, an update found (which also shows the
button), or the actual error if the check itself failed (offline, GitHub
unreachable, etc.) — since the startup check stays silent on failure by
design and gives you no way to tell why nothing showed up.

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
