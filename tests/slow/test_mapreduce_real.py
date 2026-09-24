"""Map-reduce against the real on-device model, on material that can't fit
its 4096-token window: a ~100 KB project, a ~40 KB attached file, a chat
that outgrows its session, a big file needing edits in several places, and
a large diff to describe. Each case checks the answer or the resulting
files.

Run: tests/run.sh slow   (or directly, optionally with case-name filters:
     uv run --with textual --with rich python3 tests/slow/test_mapreduce_real.py ask)
Exit 0 = all passed (or no on-device model here), 1 = any failed.
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
import fm_pcc.app as m  # noqa: E402
from fm_pcc import mapreduce  # noqa: E402


def on_device_available() -> bool:
    try:
        return subprocess.run(["fm", "available", "--model", "system"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def big_project():
    """~30 modules of plausible filler (~100 KB), with a few real facts
    hidden in specific files."""
    for i in range(28):
        area = ["billing", "reports", "search", "email", "export", "cache", "media"][i % 7]
        body = "\n\n".join(
            f"def {area}_step_{i}_{j}(record):\n"
            f"    \"\"\"Normalize {area} field {j} of a record for module {i}.\"\"\"\n"
            f"    value = record.get('{area}_{j}', 0)\n"
            f"    if value is None:\n        return {{}}\n"
            f"    return {{'{area}_{j}': value * {j + 1} + {i}}}"
            for j in range(12)
        )
        write(f"app/{area}/module_{i}.py", f'"""{area.title()} helpers, part {i}."""\n\n{body}\n')
    write("app/auth/settings.py",
          '"""Authentication settings."""\n\n# How long a login stays valid, in seconds.\nSESSION_TTL_SECONDS = 1800\n'
          'PASSWORD_MIN_LENGTH = 12\n')
    write("app/net/client.py",
          '"""HTTP client."""\n\nMAX_RETRIES = 5\n\n\ndef fetch_with_retry(url):\n'
          '    """Fetch url, retrying up to MAX_RETRIES times."""\n    for attempt in range(MAX_RETRIES):\n'
          '        pass\n')
    write("app/net/backoff.py",
          '"""Backoff policy."""\n\nBACKOFF_BASE_SECONDS = 2\n\n\ndef delay(attempt):\n'
          '    return BACKOFF_BASE_SECONDS ** attempt\n')


def sync_call(fn, *a, **k):
    return fn(*a, **k)


async def run_ask(question):
    app = m.ChatApp()
    app.subagent_roles["planning"] = "on-device"
    answers, log = [], []
    async with app.run_test():
        with mock.patch.object(app, "call_from_thread", side_effect=sync_call), \
             mock.patch.object(app, "_log_progress", side_effect=log.append), \
             mock.patch.object(app, "_ask_answered", side_effect=answers.append), \
             mock.patch.object(m, "notify"):
            app._run_ask.__wrapped__(app, question)
    return (answers[-1] if answers else ""), log


async def run_task(task):
    app = m.ChatApp()
    app.subagent_roles["planning"] = "on-device"
    log = []
    async with app.run_test():
        with mock.patch.object(app, "call_from_thread", side_effect=sync_call), \
             mock.patch.object(app, "_log_progress", side_effect=log.append), \
             mock.patch.object(m, "notify"):
            app._run_task.__wrapped__(app, task)
    return log


# ---- cases: each returns a list of failure strings ----

async def ask_paraphrase():
    big_project()
    answer, log = await run_ask("how long does a login session last before it expires?")
    ok = "1800" in answer or "30 minutes" in answer or "half an hour" in answer
    return [] if ok else [f"answer: {answer[:300]}", *log[-3:]]


async def ask_two_files():
    big_project()
    answer, log = await run_ask("how many times does the HTTP client retry, and what's the base backoff delay in seconds?")
    missing = [v for v in ("5", "2") if v not in answer]
    return [] if not missing and "MAX_RETRIES" in answer or not missing else [f"missing {missing}: {answer[:300]}"]


async def chat_big_file():
    filler = "".join(f"Section {i}. The quarterly planning notes discuss routine matters, staffing, and budget "
                     f"allocations for team {i}, none of which change the schedule.\n" for i in range(260))
    write("notes.txt", filler + "Final decision: the product launch date is October 14.\n")
    app = m.ChatApp()
    async with app.run_test() as pilot:
        from textual.widgets import Input
        from textual.containers import VerticalScroll
        app.query_one(Input).value = "what is the product launch date? @notes.txt"
        await pilot.press("enter")
        for i in range(1800):
            await pilot.pause(0.1)
            if i > 5 and not app.query_one(Input).disabled:
                break
        replies = [msg.text for msg in app._message_log if msg.role == "assistant"]
    answer = replies[-1] if replies else ""
    return [] if "October 14" in answer else [f"answer: {answer[:300]}"]


async def chat_overflow():
    backend = m.Backend()
    backend._respond_on_device("Please remember this: my project's codename is BLUEHERON. Just say OK.")
    long = "Here is some background you can ignore. " + ("Lorem ipsum dolor sit amet, consectetur adipiscing. " * 60)
    for i in range(6):
        backend._respond_on_device(f"{long} (message {i}) Reply with just the word noted.")
    reply = backend._respond_on_device("What is my project's codename?")
    # (fm keeps a long transcript within the window itself in this case;
    # fm-pcc's condensing is the fallback for when it can't, covered
    # offline in tests/fast/test_mapreduce.py. Either way the chat has to
    # keep working and remember.)
    return [] if "BLUEHERON" in reply.upper() else [f"reply: {reply[:200]}"]


async def task_big_project():
    big_project()
    log = await run_task("the login session expires too soon, make it last one hour")
    text = open("app/auth/settings.py").read()
    return [] if "3600" in text and "PASSWORD_MIN_LENGTH = 12" in text else [f"settings.py: {text!r}", *log[-6:]]


async def task_every_site():
    handlers = []
    for i in range(60):
        name = f"admin_handler_{i}" if i in (7, 31, 52) else f"user_handler_{i}"
        handlers.append(f"def {name}(request):\n    \"\"\"Handle request {i}.\"\"\"\n    data = request.get('payload', {{}})\n"
                        f"    result = {{'id': {i}, 'size': len(data)}}\n    return result")
    write("handlers.py", "import logging\n\nlog = logging.getLogger(__name__)\n\n\n" + "\n\n\n".join(handlers) + "\n")
    original = open("handlers.py").read()
    log = await run_task("in handlers.py, add a log.info call at the start of every admin handler")
    text = open("handlers.py").read()
    problems = []
    for i in (7, 31, 52):
        block = text.split(f"def admin_handler_{i}(")[1].split("\ndef ")[0]
        if "log.info" not in block:
            problems.append(f"admin_handler_{i} has no log.info")
    others = [b for b in text.split("\ndef ")[1:] if b.startswith("user_handler") and "log.info" in b]
    if others:
        problems.append(f"{len(others)} user handlers were changed too")
    ok = subprocess.run([sys.executable, "-c", "import handlers"], capture_output=True).returncode == 0
    if not ok:
        problems.append("handlers.py no longer imports")
    if len(text.splitlines()) < len(original.splitlines()):
        problems.append("lines were lost")
    return problems + ([f"| {l[:200]}" for l in log[-6:]] if problems else [])


async def commit_big_diff():
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@e.com"], ["git", "config", "user.name", "T"]):
        subprocess.run(cmd, check=True)
    big_project()
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], check=True)
    # a big change /task didn't make: rename a concept across many files
    for root, _d, files in os.walk("app"):
        for f in files:
            p = os.path.join(root, f)
            write(p, open(p).read().replace("Normalize", "Sanitize"))
    await run_task("commit everything")
    message = subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip()
    if message == "init":
        return ["nothing was committed"]
    if message.startswith("Update ") or not (3 <= len(message) <= 72):
        return [f"commit message: {message!r}"]
    return []


CASES = [
    ("ask-paraphrase", ask_paraphrase),
    ("ask-two-files", ask_two_files),
    ("chat-big-file", chat_big_file),
    ("chat-overflow", chat_overflow),
    ("task-big-project", task_big_project),
    ("task-every-site", task_every_site),
    ("commit-big-diff", commit_big_diff),
]


async def main() -> int:
    if not on_device_available():
        print("SKIPPED: on-device model isn't available here")
        return 0
    filters = sys.argv[1:]
    failed = []
    for name, case in CASES:
        if filters and not any(f in name for f in filters):
            continue
        cwd = tempfile.mkdtemp(prefix=f"fm-pcc-mr-{name}-")
        orig = os.getcwd()
        os.chdir(cwd)
        start = time.monotonic()
        try:
            problems = await case()
        except Exception as e:
            problems = [f"raised {type(e).__name__}: {e}"]
        finally:
            os.chdir(orig)
            shutil.rmtree(cwd, ignore_errors=True)
        print(f"{'ok  ' if not problems else 'FAIL'} {name} ({time.monotonic() - start:.0f}s)", flush=True)
        for p in problems:
            print(f"       - {p}")
        if problems:
            failed.append(name)
    print(f"\n{len(CASES) - len(failed) if not filters else '?'} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
