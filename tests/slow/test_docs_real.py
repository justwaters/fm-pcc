"""/docs against the real downloads and the real on-device model: every
doc set is downloaded fresh from its official source, then asked one
question with a known answer, answered on-device from the passages the
local search finds. Needs network (skipped without it).

Run: tests/run.sh slow   (or directly, optionally with set-name filters:
     uv run --with textual --with rich python3 tests/slow/test_docs_real.py rust go)
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest.mock as mock
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
import fm_pcc.app as m  # noqa: E402
from fm_pcc import docs  # noqa: E402

# (set, question, check on the answer)
QUESTIONS = [
    ("swift", "In Swift, what keyword declares a constant?", lambda a: re.search(r"`?\blet\b`?", a)),
    ("python", "In Python, what does str.removeprefix return when the string doesn't start with the prefix?",
     lambda a: re.search(r"original|unchanged|same string|copy of the (?:original )?string", a, re.I)),
    ("html", "Which HTML element represents a dialog box or modal?", lambda a: "dialog" in a.lower()),
    ("javascript", "In JavaScript, what does Array.prototype.at(-1) return?", lambda a: re.search(r"\blast\b", a, re.I)),
    ("css", "In CSS, what does the gap property set?", lambda a: re.search(r"gutter|gaps?\b|space between", a, re.I)),
    ("go", "In Go, besides the new context, what does context.WithTimeout return?",
     lambda a: re.search(r"cancel", a, re.I)),
    ("rust", "In Rust, how many mutable references to a value can you have at the same time?",
     lambda a: re.search(r"\b(?:one|only one|a single|1)\b", a, re.I)),
    ("react", "In React, when does the cleanup function returned from useEffect run?",
     lambda a: re.search(r"before|unmount|remov|re-?run|next", a, re.I)),
]


def online() -> bool:
    try:
        urllib.request.urlopen("https://github.com", timeout=10)
        return True
    except Exception:
        return False


def on_device_available() -> bool:
    try:
        return subprocess.run(["fm", "available", "--model", "system"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


async def main() -> int:
    if not on_device_available() or not online() or not shutil.which("git"):
        print("SKIPPED: needs the on-device model, network access, and git")
        return 0
    filters = sys.argv[1:]
    home = tempfile.mkdtemp(prefix="fm-pcc-docs-real-")
    failed = []
    try:
        with mock.patch.object(m, "DOCS_HOME", home):
            app = m.ChatApp()
            async with app.run_test():
                for set_id, question, check in QUESTIONS:
                    if filters and set_id not in filters:
                        continue
                    start = time.monotonic()
                    try:
                        info = app._docs.install(set_id)
                    except Exception as e:
                        print(f"FAIL {set_id}: download failed: {e}")
                        failed.append(set_id)
                        continue
                    answers, log = [], []
                    with mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
                         mock.patch.object(app, "_ask_answered", side_effect=answers.append), \
                         mock.patch.object(app, "_log_progress", side_effect=log.append), \
                         mock.patch.object(m, "notify"):
                        app._run_docs_question.__wrapped__(app, question)
                    answer = answers[-1] if answers else ""
                    ok = bool(answer) and bool(check(answer)) and "Sources:" in answer
                    print(f"{'ok  ' if ok else 'FAIL'} {set_id} ({info['pages']} pages, {time.monotonic() - start:.0f}s): {question}", flush=True)
                    if not ok:
                        failed.append(set_id)
                        print(f"       answer: {answer[:400]!r}")
                        for line in log[-4:]:
                            print(f"       | {line[:200]}")
    finally:
        shutil.rmtree(home, ignore_errors=True)
    print(f"\n{len(failed)} failed" if failed else "\nall passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
