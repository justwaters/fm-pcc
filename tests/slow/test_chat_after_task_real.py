"""Chat after /task, on the real on-device model, through the same input
path as typing: chat learns what a file says (an @file question), /task
changes that file, and then chat is asked about it. Seen for real: after
/task fixed findings.md, chat went on answering from its own earlier
table -- the change looked as if it hadn't happened.

Each case checks the file really changed first (a /task that didn't make
its change says nothing about chat, and is reported, not counted).

Run: tests/run.sh slow   (or directly, optionally with case-name filters:
     uv run --with textual --with rich python3 tests/slow/test_chat_after_task_real.py todo)
"""
import asyncio
import os
import re
import shutil
import sys
import tempfile
import time
import types
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
import fm_pcc.app as m  # noqa: E402

MIN_PASS = 6

IPHONES = "# iPhone 18 lineup\n\n- iPhone 18: $999\n- iPhone 18 Pro: $1,199\n- iPhone 18 Pro Max: $1,299\n"
CASES = [
    # (name, files, first chat message, /task request, file check, question, reply check)
    ("todo", {"todo.md": "# To do\n\n- buy milk\n- call Bo\n- pay rent\n"},
     "@todo.md what's on my todo list?", "remove the pay rent line from todo.md",
     lambda f: "rent" not in f["todo.md"] and "milk" in f["todo.md"],
     "what's on my todo list now?", lambda r: "milk" in r.lower() and "rent" not in r.lower()),
    ("port", {"server.md": "# Server\n\nPort: 8080\nHost: localhost\n"},
     "@server.md which port does the server use?", "change the port in server.md to 3000",
     lambda f: "3000" in f["server.md"] and "8080" not in f["server.md"],
     "which port does the server use now?", lambda r: "3000" in r and "8080" not in r),
    ("team", {"team.md": "# Team\n\n- Alice (lead)\n- Bob\n- Carol\n"},
     "@team.md who is on the team?", "add Dave to the list in team.md",
     lambda f: "Dave" in f["team.md"],
     "who is on the team now?", lambda r: "Dave" in r),
    ("menu", {"menu.md": "# Menu\n\n- Coffee: $3\n- Tea: $2\n"},
     "@menu.md how much is tea?", "change the tea price in menu.md to $2.50",
     lambda f: "2.50" in f["menu.md"],
     "how much is tea now?", lambda r: "2.50" in r),
    ("iphones", {"findings.md": IPHONES},
     "@findings.md list the models and prices in this file", "remove the iPhone 18 line (the $999 one) from findings.md",
     lambda f: "$999" not in f["findings.md"] and "Pro Max" in f["findings.md"],
     "list the models and prices in findings.md", lambda r: "999" not in r and "1,199" in r.replace("1199", "1,199")),
    ("written", {},
     "give me a packing list for a beach trip with three items", "create packing.md listing sunscreen, a towel and a kite",
     lambda f: "kite" in f.get("packing.md", "").lower(),
     "what's in packing.md?", lambda r: "kite" in r.lower()),
    # written after the change, as a held-out check
    ("notes", {"notes.txt": "Team meeting on Tuesday at 10am in room 4.\n"},
     "@notes.txt when is the team meeting?", "change Tuesday to Thursday in notes.txt",
     lambda f: "Thursday" in f["notes.txt"],
     "when is the team meeting now?", lambda r: "thursday" in r.lower() and "tuesday" not in r.lower()),
    ("json", {"config.json": '{\n  "name": "demo",\n  "debug": true\n}\n'},
     "@config.json is debug turned on?", "set debug to false in config.json",
     lambda f: '"debug": false' in f["config.json"],
     "is debug turned on in config.json now?", lambda r: re.search(r"\b(?:false|off|no|not|disabled)\b", r, re.I)),
    ("later", {"server.md": "# Server\n\nPort: 8080\nHost: localhost\n"},
     "@server.md which port does the server use?", "change the port in server.md to 3000",
     lambda f: "3000" in f["server.md"],
     ["thanks!", "and which port does it use now?"], lambda r: "3000" in r and "8080" not in r),
]


async def run(files, messages):
    cwd = tempfile.mkdtemp(prefix="fm-pcc-chat-after-task-")
    orig = os.getcwd()
    os.chdir(cwd)
    for p, t in files.items():
        open(p, "w").write(t)
    log, replies, after_task = [], [], {}
    try:
        app = m.ChatApp()
        app.subagent_roles["planning"] = "on-device"
        async with app.run_test():
            box = app.query_one(m.Input)

            def run_task(r):
                m.ChatApp._run_task.__wrapped__(app, r)
                after_task.update({f: open(f, errors="replace").read() for f in os.listdir(".") if os.path.isfile(f)})

            with mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
                 mock.patch.object(app, "_log_progress", side_effect=log.append), mock.patch.object(m, "notify"), \
                 mock.patch.object(app, "_run_task", side_effect=run_task), \
                 mock.patch.object(app, "_respond", side_effect=lambda p, *a, **k: m.ChatApp._respond.__wrapped__(app, p, *a, **k)):
                for msg in messages:
                    box.disabled = False
                    before = len([x for x in app._message_log if x.role == "assistant"])
                    app.on_input_submitted(types.SimpleNamespace(value=msg, input=box))
                    said = [x.text for x in app._message_log if x.role == "assistant"]
                    replies.append(said[-1] if len(said) > before else "")
    finally:
        os.chdir(orig)
        shutil.rmtree(cwd, ignore_errors=True)
    return log, replies, after_task


async def main() -> int:
    if not shutil.which("fm"):
        print("SKIPPED: no on-device model")
        return 0
    os.environ["FM_PCC_AUTO_WEB"] = "0"
    filters = sys.argv[1:]
    cases = [c for c in CASES if not filters or any(f in c[0] for f in filters)]
    passed = counted = 0
    for name, files, first, task, file_ok, question, reply_ok in cases:
        start = time.monotonic()
        asked = question if isinstance(question, list) else [question]
        log, replies, after = await run(files, [first, f"/task {task}", *asked])
        took = f"({time.monotonic() - start:.0f}s)"
        if not after or not file_ok(after):
            print(f"n/a  {name} {took}: /task didn't make its change -- {list(after.items())[:2]}")
            continue
        counted += 1
        good = bool(reply_ok(replies[-1]))
        passed += good
        print(f"{'ok  ' if good else 'FAIL'} {name} {took}", flush=True)
        if not good:
            print(f"       first reply: {replies[0][:200]!r}")
            print(f"       last reply:  {replies[-1][:300]!r}")
    need = min(MIN_PASS, counted) if not filters else counted
    print(f"\n{passed}/{counted} right after /task changed the file ({len(cases) - counted} n/a; need {need})")
    return 1 if passed < need else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
