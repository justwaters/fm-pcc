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

**Everything runs on the on-device model out of the box** — chat,
`/task`, `/ask`, and `fm-pcc respond` — with no Shortcuts to install, no
network, and no cloud quota. On launch fm-pcc checks that the model is
ready and, if it isn't, says exactly what to do: the one piece of setup
Apple requires is agreeing to its Foundation Models terms once, which
`/license` opens for you to read and answer (fm-pcc never agrees on your
behalf), or that Apple Intelligence is turned on and downloaded. Cloud
tiers are opt-in, below.

The first time you actually use Cloud or Cloud Pro, if its shortcut isn't
installed yet, `fm-pcc` opens its iCloud share link for you —
[Cloud](https://www.icloud.com/shortcuts/9f4e45968b974ef7ad8d29eb06f98a9b),
[Cloud Pro](https://www.icloud.com/shortcuts/7f7f8e41dfee459a89c28ca0b8c60984)
— tap **Add Shortcut** in the sheet that appears, and it'll pick up from
there.

**Cloud tiers fail sometimes** — a usage limit, a network blip, Cloud
Pro specifically also requires the signed-in account to have iCloud+
(this doesn't seem to apply to plain Cloud) — and fm-pcc handles this by
falling back rather than just showing an error: **cloud-pro → cloud →
on-device**, in that order, stopping at the first tier that actually
works. This applies to normal chat and to `/task`'s and `/ask`'s
planning role (if you've set it to a cloud tier) — `/compare` is the one
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

Tests live in two suites, run with `tests/run.sh [fast|slow|all]`:

- **`tests/fast/`** — mocked, offline tests of the TUI and backends
  (Textual's headless `run_test()` harness, fake shortcut/`fm`/git
  results, throwaway temp git repos). About 15 seconds total; no model
  calls.
- **`tests/slow/`** — real `/task` runs against the actual on-device
  model in scratch temp directories. `test_task_reliability.py` is a
  corpus of 94 requests — edits, renames, moves, commits, theming, and
  combinations, in varied phrasings — each checked against the resulting
  files and git history; most batches of it were written and run *before*
  tuning anything for them, as an honest measure of how new phrasings
  fare. `test_agentic_eval.py` is 71 agentic-coding tasks — features,
  bug fixes (from a description, a pasted traceback, or failing tests),
  refactors across files, writing tests, scaffolding projects, a
  multi-module app, Go and Node projects, code questions via `/ask` —
  each judged by *running* the result (the code has to work, not just
  look right). Its first 24 cases went from 7/24 to 24/24; three later
  batches, each written after the previous round of tuning, scored
  17/20, 8/12, and 9/15 on their first, untuned runs before their
  failures were fixed. Two cases are kept as documented model
  limitations (see Limitations).
  `test_docs_real.py` downloads the doc sets fresh (the eight languages
  and four smaller Apple frameworks) and asks 20 questions with known
  answers: the passages the search picks must contain the answer every
  time (that's fm-pcc's code), and at least 17 answers must be right
  (that's the model, measured at 19). `test_swift_docs_real.py` has
  `/task` write Swift against eight Apple frameworks with their docs
  installed, 18 tasks judged by `swiftc`: the API each task needs must be
  among the ones offered to the model every time (fm-pcc's code), and at
  least 6 must pass (the model; measured 7–12, and 7 without docs); its
  Apple downloads are cached for a week. `test_mapreduce_real.py` runs
  map-reduce (below) on material that can't fit the model's window: a ~100 KB project, a ~40 KB attached
  file, a long chat, a big file needing edits in several places, and a
  large diff to describe. `test_crossfile_real.py` is 12 changes that
  have to be threaded through several files (a field from the model to
  the output, a parameter passed down three layers, a return type and
  its callers, a feature spanning three modules) in Python, JavaScript
  and Swift, each judged by running the program; written before any of
  the cross-file work, they went from 0/12 (both baseline runs) to
  9/12 (both final runs);
  at least 7 must pass.
  `test_behavior_real.py` is 13 requests that say what the code must do
  — by example, by rule, or by expected output — in projects with no
  tests, each judged by hidden checks: 7/13 in both runs before the
  request-example checks, 9/13 in all three runs after; at least 8 must
  pass.
  `test_rules_real.py` is 10 requests stated only as rules ("at least 8
  characters, a digit, and an uppercase letter"), with hidden checks on
  the edge cases the rules decide: 9/10 in both runs; at least 8 must
  pass.
  `test_web_real.py` asks `/web` 20 questions with known answers against
  the real web (20/20 measured, the model alone 12/20; at least 17 must
  be right), and skips if the search service can't be reached.
  `test_autoweb_real.py` has `/task` research on its own (with Allow
  always): four requests needing current facts must come out current (at
  least 3; measured 4 of 4 in two runs), and plain coding requests must
  not search.
  `test_build_real.py` builds 8 small projects from one sentence each
  (a to-do CLI, a word-frequency tool with tests, a Python package, a
  three-page site, a Node CSV summary, a Swift program, a bank account
  with tests, a Node converter with a CLI and tests), judged by running
  them: 2/8 before the build work, 4/8 after (at least 3 must pass).
  `test_on_device_capabilities.py` covers
  create/rename/move and the push/branch gating. About 25 minutes in
  total, plus about 6 minutes the first time to download Apple's docs.
  These guard against cases where the model's real behavior didn't match
  what the code assumed (a small on-device model asked to "create a
  folder" once created a *file* named `test` instead, because `/task` had
  no folder-creation action at all). They pass as "skipped" on a machine
  without an on-device model set up.

The slow suite forces *both* of `/task`'s subagent roles to on-device,
including planning — a real `/task` run defaults to Cloud Pro for
planning. Testing the harder, fully-offline configuration on purpose is
what showed that asking the on-device model to plan step by step wasn't
fixable with prompting, and led to the plan-up-front design described
under `/task` below: its real failure modes (swapped source and
destination, commentary pasted into paths and commit messages, never
noticing a task was done, turning a rename request into a rewrite of the
file) are now caught or avoided in code rather than hoped away.

Every test runs with `FM_PCC_HOME` pointed at its own temp directory, so
tests never read or write your real `~/.fm-pcc` (saved sessions, and
remembered unavailable tiers that would otherwise change outcomes).

The fast suite runs before every commit, and `scripts/release.sh` runs
both suites before tagging — a failure blocks either one. To enable the
commit hook, point git at this repo's tracked hooks directory (once per
clone):

```
git config core.hooksPath scripts/git-hooks
```

Not on by default — git only trusts a hooks path you've explicitly set.

## Usage

```
fm-pcc                                    # launch the chat TUI (starts on-device)
fm-pcc --model cloud-pro                  # start the TUI on cloud pro instead
fm-pcc respond "What is Swift?"           # one-shot, non-interactive (default: on-device)
fm-pcc respond -m cloud-pro "..."         # one-shot on cloud pro
fm-pcc --model ollama                     # start the TUI on Ollama (first model ollama list has)
fm-pcc --model ollama --ollama-model llama3.2   # ...or a specific one
```

### In the chat

Messages you've sent sit on a gray band, like in Claude Code's CLI, so
your side of the conversation is easy to spot when scrolling back.

Chat itself can't change files or run git — only `/task` can — so a chat
message that `/task` fully understands on its own ("can you please commit
and push", "rename app.js to main.js", "add a comment to app.js…") is run
as `/task` directly. A message that reads like a change request but isn't
fully understood ("i want the ui to have a green and yellow theme") gets a
normal chat reply plus a tip; replying "do it", "you do it", or "yes, do that" then runs
that request as `/task`. Questions ("what happens if I rename…?") always
stay chat, so talking *about* a change never makes one.

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
| `/task <description>`       | Plan and carry out a multi-step change, writing as it goes (see below) |
| `/ask <question>`           | Answer a question about the project's code (see below) |
| `/subagents [planning\|building] <model>` | Show or set which model plays each subagent role  |
| `/compare <question>`       | Ask every model the same question, one at a time       |
| `/save <name>`               | Save the conversation under a name                     |
| `/resume [name]`             | Resume a saved conversation, or list saved ones        |
| `/export [file\|copy]`       | Export the transcript as Markdown (or `.txt`/`.json`), or copy it |
| `/run <command>`             | Run a shell command here and show its output           |
| `/license`                   | Read and agree to Apple's on-device model terms (one-time setup) |
| `/docs [question]`           | Download language docs (picker), or answer a question from them |
| `/web <question>`            | Search the web and answer from the pages found, with sources |
| `/web ask\|always\|never`     | Whether fm-pcc asks before searching the web on its own (default: ask) |
| `/verify [on\|off]`          | Turn `/task`'s automatic checks (tests, syntax) on or off |
| `/undo`                      | Revert the last file write, folder creation, or move made by `/edit` or `/task` |
| `/push`                      | Commit and push the current changes to git (publishing a new branch if needed) |
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
is written automatically. It uses the same editing machinery as `/task`
(below): unambiguous literal changes are made directly in code, anything
else is rewritten by the on-device model with guided generation (`fm
respond --schema`) and checked before it's shown. Editing only works
on-device — neither cloud tier has equivalent schema control via
Shortcuts.

`/task <description>` plans the whole task up front, shows the plan, then
carries it out step by step and **writes immediately, with no per-step
`/apply`** (`/undo` reverts any step). A plan is made of these steps:

- **edit** an existing file
- **create** a new file (optionally inside a folder), or a new empty folder
- **rename** or **move** a file or folder
- **stage** or **commit** changes — and push, pull, or branch changes,
  which are gated (see below)

Every path is restricted to somewhere inside the current directory (no
absolute paths, no `..`). `Esc` `Esc` stops it between steps.

**Why it works this way.** The on-device model is good at *language* —
rewriting a file to add a line — and bad at *bookkeeping*: telling a
rename's source from its destination, noticing a task is already done,
keeping commentary out of a filename. An earlier design had a model pick
one step at a time and decide when it was finished; measured on the real
on-device model, that failed every case of the reliability suite below.
So `/task` now does the bookkeeping in code (`src/fm_pcc/taskplan.py`):

- **Planning is deterministic where the request has a recognizable
  shape.** The request is split into clauses ("rename a.txt to b.txt, move
  it into docs, and commit as 'tidy'"), and the common English forms of
  rename, move, commit, push/pull, branch, and create requests are read
  directly — including declaratives like "config.toml should be called
  settings.toml", "utils.py should go in lib", "get data.csv out of tmp",
  and "record these changes in git as 'snapshot'". Every path is resolved
  against the real directory tree (and against earlier steps of the same
  plan, so "rename a.txt to b.txt and move it into docs" works). A clause
  phrased as an instruction to change contents ("add…", "fix…", "in x.txt,
  replace…") that names one file becomes an edit of that file.
- **Anything else goes to the planning model**, asked for the whole plan
  at once as structured output (a fixed action vocabulary it can't step
  outside). Its plan is then grounded the same way: steps the request
  never asked for are dropped (an unrequested commit, an edit when the
  request never asked to change contents), swapped or missing source and
  destination paths are repaired against what actually exists (including
  a misspelled filename), and a missing destination folder is added. When
  the request names no file ("make the ui…"), the planner picks the files
  to edit; any of those that turns out to need no change — say, an HTML
  page with no colors in a theme change — is skipped with a note instead
  of stopping the task, and a change to one must actually involve the
  request to be written. A request it still can't turn
  into valid steps is reported, not guessed at.
- **Edits are checked before they're written.** Unambiguous literal
  changes don't need a model at all and are made exactly in code:
  removing a named line or word, "replace A with B" / "change A to B",
  setting `key = value` in config files, deleting a named function or
  class, whole-file case changes, and whole-theme re-colors ("make the ui
  a green and yellow theme": every color in a stylesheet shifts to the
  requested hues keeping its lightness, so dark stays dark and contrast
  survives; grays, and colors like an error red, are left alone). A
  targeted color change to a stylesheet with color variables ("make the
  accent teal") has the model pick new *values* for existing variable
  names, which code substitutes. Everything else is rewritten by the
  on-device model (guided generation; large files one section at a time),
  then checked against expectations derived from the request itself —
  "remove X" means X is gone, "add…" means no existing line was lost,
  "…saying Y" means Y is present, and the file must actually change. If
  the model collapses the file's formatting, the original layout is
  restored around its changes. Rewrites of code, stylesheets, and HTML
  are also checked for structure: comments and braces still balance, HTML
  tags still pair up, CSS variables the rest of the file uses still exist,
  and the original indentation style is kept; a change that only moves
  whitespace around is rejected. A failed check is retried with the problem
  spelled out; if no attempt passes, the step fails with an error and
  nothing is written.

`/task` treats git operations in two tiers. Staging and committing locally
happen automatically, same as any other step — they're local and
reversible (a commit with no message gets a descriptive one, and
committing an already-clean tree is a no-op, not an error). Pushing,
pulling, and creating or switching branches never happen automatically:
if the plan reaches one, `/task` stops and tells you to run the matching
command yourself (`/push`, `/pull`, `/branch create <name>`, `/branch
switch <name>`), then re-run `/task` for anything after it. Deleting files
isn't something `/task` does; it says so instead of trying.

**Coding.** `/task` is built to do real coding work on-device, measured
against the agentic-coding suite in `tests/slow/`:

- **It sees the code.** Planning, every edit, and `/ask` get the most
  relevant files in full plus an outline of the rest (function and class
  signatures), fitted to the on-device model's 4096-token context — and
  when a project is bigger than that, map-reduce (below) reads the rest.
- **Code is written as plain text,** not squeezed through a JSON string:
  on the same prompts the model got 8/8 right in plain text and 5/8
  through guided generation. Code edits ask for a correct implementation
  of your request, in your own words (not a planner's paraphrase).
- **Refactors are exact.** "Rename compute_total to sum_values
  everywhere" renames the symbol in every file that uses it; "move
  double and triple from main.py into helpers.py" moves the functions,
  carries their imports along, and imports them back; "add a timeout of
  30 to config.json" edits the JSON as data; "add a docstring to every
  function" works through them one at a time; a request naming one
  function in a bigger file edits just that function.
- **Changes across files.** A request that has to be carried through
  several files — "add a category: Item gets a field, Store.add takes
  it, the report shows it, and main.py passes it" — gets a step for every
  file it involves, not just the ones the planner thought of: files it
  names, files defining the code it names (`Item`, `Store.add`,
  `createTodo`), and files it refers to by name ("the report"). Each of
  those edits is told its own part of the request ("main.py applies 10%
  before printing"), and they run definitions first — models, then the
  store, then the report, then the script — so every edit sees the ones
  before it. Renames like "rename `sum` in math.js to `total`, and update
  everything that uses it" are done in code across every file.
- **Building from one sentence.** "Build a command-line to-do app in
  Python…", "make convert.js, a Node module…; cli.js that…; and test.js
  that…" — every code file the request names gets made, even ones the
  planner left out (test files especially), each told its own part of the
  request, where the request puts it (a bare `main.py` at the top, not
  inside the package beside it; `style.css` where the pages look for it).
  A file that comes back with another file written into it (`//
  main.swift` and its code below a struct) is cut back to its own part,
  and a planner that runs out of room still builds the files the request
  names.
- **It checks its work.** After changing code, `/task` runs the
  project's own checks: syntax checks of what changed (Python, `node
  --check`, a Swift type check, valid JSON), the test suite if there is
  one (pytest or unittest, `npm test`, `swift build`, `go test`, `cargo
  test`), and anything your request said to run ("running main.py fails
  with…" re-runs main.py). With no test suite, it has the model write a
  short script that calls the changed code and runs it on a copy of the
  project; only a crash inside a function this task changed counts.
  Programs that ran before the task (`main.py`, `app.py`, `index.js`,
  `main.swift`) are run again on a copy of the project, and have to
  still run — a change threaded through three of four files used to pass
  every syntax check while `main.py` crashed. A crash from a missing
  import (`NameError: name 'User' is not defined`, with `User` in
  models.py) is fixed in code, without the model.
  Your request is checked too, when it says what the code should do:
  examples it spells out ("`apply_discount(50, 150)` gives 0", "'1h30m'
  gives 90", "1994 is 'MCMXCIV'") are run against the changed code,
  inputs it mentions ("strings like '1h30m', '45m' or '2h'") have to at
  least not crash, and "it should print 'Total: $10.80'" is checked
  against what the program prints. Commands it spells out ("`python
  todo.py add <text>` adds an item, `python todo.py list` prints them")
  are run in order on a copy of the project, and have to work and print
  what it says. A test file written for the request has to contain
  tests: one the runner finds nothing in no longer counts as passing.
  A wrong result goes to the fix loop as
  "returned X, but the request says it should give Y". So does a changed
  Python function using a name nothing defines (it would crash with a
  NameError the moment it ran). And a function that's only a placeholder
  (`raise NotImplementedError`, `pass`, `throw new Error('not
  implemented')`) is written from its signature — shown the stub, the
  model handed it back unchanged every time.
  Two checks need no model at all: documented examples of the functions
  it changed (doctests, or `'1h30m' -> 90` in a docstring) are run and
  compared, and a new standalone script is run as-is (catching, say, an
  import of a package that isn't installed). Tests that already failed
  before the task started don't count against a new feature (they do
  for a fix). On a failure it shows
  the model the output and has it fix the file the failure points at, up
  to 3 rounds, trying a test written in the same run if the code under
  test comes back unchanged (a new test can be wrong too). The model sees
  the errors, not the warnings around them, and a fix may not take away
  what you asked for: anything the task added that your request names (a
  requested `Button`, `store.increment()`) has to still be there, or the
  fix is rejected and retried — seen for real, a fix made SwiftUI code
  compile by swapping the requested button for a tap gesture. Nothing is
  committed until the checks pass; if they never do, `/task` stops and
  shows the output. `/verify off` turns all of this off.
- **Edits keep your imports.** A code edit that drops an existing import
  the request didn't touch is rejected and retried — seen for real,
  adding `@Observable` to a class also deleted `import SwiftUI`. Requests
  about imports, cleanup, or switching libraries can still remove them.
- **"Fix the failing test"** edits the code under test, not the test,
  unless you ask about the test itself; "the tests fail, fix it" runs the
  tests and fixes whatever file the failures point at. A traceback pasted
  into the request is used as evidence for the fix.
- **Rewrites are checked for damage:** an "add" request must keep every
  existing function and class, and a definition copied in from another
  file (the model copied a Go test function into the package it tests)
  is removed along with any import it leaves unused.

`/run <command>` runs a shell command in the current directory and shows
its output — the explicit way to run something `/task` won't run on its
own (a plan step like "run the server" stops and suggests `/run`).

`fm serve`'s OpenAI-style tool-calling was tried as an alternative to this
fixed vocabulary and is broken on this OS build (it leaks raw, unparsed
generation text instead of returning structured tool calls) — and a
model-invoked tool is a weaker guarantee anyway, since a model can simply
choose not to call it.

`/subagents` controls which model plays each of two roles, **planning**
(planning/judgment calls — defaults to `on-device`) and **building**
(fast/local execution — defaults to `on-device`), used by both `/task`
(planning whatever the parser can't read, and picking which section of a
large file to edit) and `/ask` (below). Either role can be
set to any model, including a specific `ollama:<name>`. One thing this
*doesn't* change: `/task`'s actual file-writing step always runs on-device
regardless of the "building" setting.

### Documentation library: `/docs`

`/docs` opens a picker of official documentation to download, for the
latest version of each:

| Set | Source |
|---|---|
| Swift | *The Swift Programming Language* (swift.org), plus Xcode's AI-ready guides and Swift compiler diagnostics when Xcode is installed |
| Python | The official docs for the newest release (docs.python.org's text archive) |
| HTML, JavaScript, CSS | MDN's reference and guides for each |
| Go | The spec, the memory model, Effective Go, the FAQ, and the standard library (from `go doc`, when Go is installed) |
| Rust | *The Rust Book* and *The Rust Reference* |
| React | react.dev's Learn and Reference sections |

Plus Apple's framework references from developer.apple.com, grouped under
**Apple frameworks** in the picker: Swift Standard Library, SwiftUI,
Foundation, UIKit, AppKit, SwiftData, Foundation Models, Observation,
Combine, App Intents, WidgetKit, Swift Charts, MapKit, and Core Data.
These download each framework's own pages — every type, method, and
article, rendered from Apple's documentation data with Swift declarations,
availability, and code samples — starting at the framework's top page and
following its child pages (deprecated symbols skipped). Small frameworks
take seconds (Foundation Models: 757 pages, ~20 s); the biggest take a
few minutes (SwiftUI: 7,117 pages, ~2.5 min), and Foundation, UIKit,
AppKit, and the Swift Standard Library stop at 12,000 pages each (~4.5
min), reaching their types and main members first.

Choosing a set downloads it in the background — just the docs folders of
each source (a sparse, shallow git clone; Python's docs archive), a few
seconds each — then cleans it into text, splits it at headings, and
indexes it locally (SQLite full-text search) under `~/.fm-pcc/docs`. All
eight together are about 4,100 pages. Choosing an installed set offers
**Update** (to the newest docs) or **Remove**; `/docs update <set>` and
`/docs remove <set>` do the same.

`/docs <question>` searches the downloaded docs — just the languages or
frameworks the question names, if it names any ("in CSS…", "React's
useEffect", "SwiftUI's NavigationStack", "LanguageModelSession") — and
answers on-device from the best-matching passages that fit one window,
listing the official pages it used. Search is local full-text ranking,
tuned on questions with known answers: an API the question names as code
(`enumerate()`, `Array.at`, `@Model`, `LanguageModelSession`) ranks its
own entry first, operators are searched by name (`?` as "question mark"),
and Python's reference is split per function. On 20 test questions across
the languages and Apple frameworks, the passages handed to the model
contained the answer for all 20, and the model answered 19 correctly.
Searching needs no network once a set is downloaded.

Installed docs are used automatically, too: `/ask` adds the passages
relevant to a question; `/task` adds the docs for the language of each
file it edits, and when a check fails it looks the error up before asking
the model to fix it.

For Swift, `/task` goes further with the Apple frameworks you've
downloaded, because the on-device model's knowledge of Apple's newer APIs
is thin — on its own it invents them (`LanguageModel.response(to:)`,
`Observation<Counter>`):

- **Only the frameworks a file imports.** Editing a file that says
  `import SwiftUI` and `import SwiftData` uses those two sets (plus the
  Swift Standard Library); a new Swift file uses what the project's other
  Swift files import.
- **The APIs it needs, with examples.** fm-pcc lists the framework's
  types, macros and modifiers whose names or summaries match the request
  (`LanguageModelSession` from "language model … session", `BarMark` from
  "a bar chart"), the model picks the one or two it will use — from that
  list only, so it can't name one that doesn't exist — and the edit is
  written with their declarations and a code example from Apple's page in
  view. Macros are shown as they're written (`@Model`), not as their
  declarations.
- **Compiler errors answered from the docs.** When the Swift type check
  fails, the fix round is told what the docs say about each error:
  "`respond` isn't on `SystemLanguageModel`; it exists on
  `LanguageModelSession`", the type's real members, "use
  `SystemLanguageModel.default.isAvailable`", the documented forms of a
  call that didn't match, or which framework to import for a missing name.
  The error excerpt it sees also leaves out warnings, which used to crowd
  the actual error out.

On 18 Swift tasks against Apple's frameworks (Foundation Models, SwiftData,
SwiftUI, Swift Charts, Observation, Foundation, Combine, MapKit), judged by
whether `swiftc` accepts the result and it does what was asked, `/task`
got 7/18 in both runs without docs, and 7 to 12/18 with them (seven
runs: 10, 12, 10 and 10 with all 14 Apple sets installed; 8, 7 and 8 with
just those eight) — about 9 on average. The model's results swing from run
to run, so treat this as "noticeably better", not a fixed number. Some
tasks it now gets right that it never did without docs (checking
`SystemLanguageModel` availability, a Foundation Models summarizer, a
SwiftData `ModelContainer`, an `@Observable` class). Some still fail
either way (adding a SwiftUI search field, a MapKit map, a `@Generable`
type), and two that passed without docs got worse with them — the model
followed the old `URLSession.dataTask` API it was shown instead of
`data(from:)`, and fumbled Swift Charts' `.value(_:_:)` labels.

### Web research: `/web`

`/web <question>` searches the web and answers from what it finds:
fm-pcc searches DuckDuckGo, fetches the top few pages (text and HTML
only, each capped in size and time), splits them into passages and ranks
them locally the same way `/docs` does, and the on-device model answers
from the best ones that fit its window — listing the pages it used.

It's for what the model doesn't know and the downloaded docs don't
cover: default settings and limits, error messages, library changes,
command-line tools. On 20 such questions (default ports and settings,
error messages, API changes, git and npm commands), the model alone got
12 right; `/web` got all 20, in about 5–15 seconds each. Asked on its
own how to undo a commit but keep its changes, the model said
`git reset --hard` — which throws the changes away; `/web` answered
`git reset --soft HEAD~1`, with sources.

**Searching on its own, with your permission.** `/task` and chat also
notice when a request needs facts newer than the model knows — "Research
the latest ai models and build me an html page with a list of them",
"add the current Node.js LTS version to the README" — and ask first:

```
This needs facts the on-device model may not have. Allow agent to search
the web? It would look up "latest AI models 2026" on DuckDuckGo.
  › Allow once
    Allow always
    Deny
```

Allowed, it searches, reads the top pages, and works from notes of what
they say: the HTML page lists GPT-5, Claude Opus 4.6 and GPT-5.2 Pro
(or, another run, GPT-6 Astra and Claude Fable 5.1 from September 2026)
instead of the GPT-4, Claude 3 and BERT the model lists on its own. On
four such requests (that page, the newest Python, the current Node.js LTS,
the newest iPhones), it came out current every time in two full runs; on
its own the model wrote Python 3.11, Node 16 and the iPhone 15.
Searches include today's date — left alone, the model searched for
"latest AI models 2024", the year its knowledge ends. Denied, or with
Esc, it goes on with what the model knows.

Deciding a request needs the web is the model's judgment, kept only when
the request names something specific (a product, company, person or
version) or a time ("latest", "last month's", "upcoming"). On 40 requests
written to test it after it was built, it caught all 20 that needed the
web, and asked about 4 of 20 that didn't — coding requests naming a tool
("a GitHub Actions workflow that runs pytest", "a Dockerfile for this
Python app"). Deny those; with **Allow always**, they quietly spend
10–20 seconds searching. `/web ask` (the default), `/web always` and
`/web never` set this, and are remembered in `~/.fm-pcc/state.json`;
`/web never` keeps fm-pcc fully offline except when you type `/web`.

**What leaves the device.** Your search terms go to DuckDuckGo and the
pages it finds are fetched — only after you type `/web` or allow it; the
answers and the work are still done on-device. `/ask` and `/docs` never
search. DuckDuckGo needs no account or key; fm-pcc reads its plain HTML
results page, which isn't an official API — if it refuses a search (too
many in a row, or a captcha), fm-pcc says so instead of guessing.

### Map-reduce: past the 4096-token window

The on-device model sees about 4,096 tokens at a time (roughly 13 KB of
text, including its own reply). Every `fm respond` call is a fresh
session with its own full window, so when material is bigger than that,
fm-pcc splits it, handles each piece in its own session — three at a time,
which measured about twice as fast as one after another — and combines
the results. If the combined results still don't fit one window, they're
grouped and combined again: as many tiers as it takes, until one session
can see everything that's left.

Pieces are *extracted from*, not summarized, where that's possible: each
session copies the lines that answer the question word for word, and
fm-pcc keeps only quotes that really appear in the source — a summary of
a summary drifts, a checked quote can't. Each quote is cited by the file
and line it came from. A session that fails (a model reply that runs on
too long, say) is skipped and reported instead of sinking the rest.

Where it's used, on the on-device model:

- **`/ask` on a project too big to show at once:** every file is read
  (the ~120 KB most relevant to the question, in a very large project),
  and the answer cites `[file:line]` for each fact.
- **Chat with a large `@file` or a long paste:** answered from all of it,
  instead of cutting it off at 8,000 characters.
- **`/task` planning in a large project:** a session per group of files
  decides which files and functions actually matter, so the plan is made
  with those in view rather than whatever matched keywords.
- **`/task` edits that touch many places in a big file:** each part that
  needs the change is found (by name — "every admin handler" — or by a
  session per function), rewritten in its own session with the top of the
  file as context, and spliced back in one write.
- **Long chats:** if an on-device conversation outgrows its session, the
  earlier turns are condensed and the chat carries on from the summary.
- **Commit messages** for a large set of changes `/task` didn't make
  itself are written from the diff, however big.

`/ask <question>` answers questions about the project in the current
directory, entirely read-only. It gives the model the relevant files, plus
any definitions matching the question (ask about "the tax rate" and it
finds `TAX_RATE = 0.2`), and with on-device planning answers in one call
with that code in view. With a cloud planning role it's an
orchestrator/worker pattern: the **planning** role
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

`/export` writes everything shown on screen — your messages, replies,
and `/task`'s plans and diffs — to `fm-pcc-transcript-<timestamp>.md` in
the current directory, readable as-is or on GitHub (diffs in `diff`
blocks). `/export <file>` picks the name, with the format from its
extension: `.md`, `.txt` (plain text, like the screen), or `.json`.
`/export copy` puts the Markdown on the clipboard instead. It never
overwrites an existing file. Unlike `/save`, an export is for reading or
sharing, not for `/resume`.

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
release](https://github.com/justwaters/fm-pcc/releases/latest), read from
where that page redirects (which, unlike GitHub's API, has no hourly
rate limit), with the API as a fallback. It's invisible the rest of the
time, when you're already up to date. Clicking
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

**On-device coding**, measured with `tests/slow/test_agentic_eval.py`:

- **4096-token context.** Map-reduce lets fm-pcc read past it, and
  `/task` now plans a change across every file it involves — but each
  edit is still written one file at a time, so the files can disagree in
  ways no check catches. On 12 cross-file changes it gets 9
  right (0 before); the misses are the model's own code (an email check that
  rejects `a@b.co`, nested JavaScript template strings that don't parse,
  a Swift protocol requirement its conforming types can't meet).
- **Logic stated only as a rule.** `/task` checks the examples your
  request spells out, but a rule with no example ("a dot somewhere after
  the @", "trim hyphens from both ends") isn't turned into checks, so
  code that runs and breaks the rule gets through: a slugify that leaves
  a leading hyphen, `parse_duration('2h')` returning 0 when the request
  only listed '2h' as an input. Give an example of the tricky case ("'2h'
  gives 120") and it's checked. (Having the model turn rules into
  examples itself was measured and left out: on 10 rule-only requests
  `/task` already got 9 right, and about 1 in 30 of the examples the
  model proposed — even after a second, independent answer had to agree
  — was wrong, which would have "fixed" correct code.) And when a check
  does catch the model's mistake, the fix rounds repair it about 7 times
  in 10 (measured on 10 bugs from real runs, 5 runs each), nearly always
  in the first round; the rest are the same few every time ("each extra
  kilogram or part of one" was never fixed). Rewriting the failing
  function from scratch instead was measured too (38 of 50 against 36 of
  50, within the run-to-run noise) and left out, and so was trying three
  fixes in parallel and keeping one that passes: sampled six times, the
  bugs the model can fix it fixes in most samples (so the sequential
  rounds already get them), and the ones it can't it got wrong in all
  six — with up to six different wrong answers — so the parallel version
  fixed the same number at three times the wait. When it can't repair
  the code, `/task` stops without committing.
- **Some logic it can't write at all.** A quoted-field CSV parser came
  out wrong in every attempt measured. fm-pcc catches the wrong result
  and stops without committing it, but can't make the model get it
  right. (A duration parser used to be on this list; written from its
  signature and checked against the request's examples, it now comes
  out right in about three runs of four.)
- **Wording sensitivity.** Small prompt changes flip right answers to
  wrong ones (one added sentence cost one of six single-function fixes),
  which is why fm-pcc's prompts are measured rather than guessed.
- **Self-review doesn't work.** Asked whether its own change was correct,
  the model flagged every wrong change but also 3 of 4 correct ones, so
  fm-pcc doesn't use it as a check.
- **Docs answers are only as good as the model's reading.** `/docs`
  finds the right pages reliably (and lists them), but the on-device
  model can still blur what it read — asked how Rust's `String` differs
  from `&str`, it cited the right pages yet mixed the two up, and asked
  what `context.WithTimeout` returns "besides the new context", it
  described only the context. Check the linked pages for anything
  important.
- **Apple's newer APIs.** The model barely knows them; with the
  frameworks downloaded in `/docs`, `/task` gets about half of the Swift
  tasks measured against them right (compiled and doing what was asked),
  versus about 4 in 10 without. Download the frameworks your project imports, and expect
  to finish some Swift changes yourself.
- **Whole projects from one sentence.** `/task` gets about half of a set
  of small multi-file builds working end to end (4 of 8). The rest are
  the model's own code — a circle's area without π, results written to a
  file instead of printed — or bugs the checks catch (a to-do list
  command printing nothing) that three fix rounds can't repair; `/task`
  then stops and says so rather than reporting a broken app as done.
- **Speed.** Each model call takes a few seconds; a task with a failing
  check and several repair rounds can take a minute or two, and reading
  a ~100 KB project through map-reduce takes one to two minutes.

Other limitations:

- Cloud/Cloud Pro's "multi-turn context" is just prior turns re-sent as
  text, not a real session — it'll drift on long conversations.
- No streaming; each response is shown once it's fully generated.
