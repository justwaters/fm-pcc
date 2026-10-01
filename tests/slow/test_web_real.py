"""/web against the real web and the real on-device model: 20 questions
with known answers a small model often gets wrong on its own -- default
ports and settings, limits, error messages, API changes, git and npm
commands. Measured before any tuning: the model alone 12/20, /web 20/20
(the first 12 and the last 8 as separate batches: 7/12 vs 12/12, 5/8 vs
8/8). Asked alone, the model said to undo a commit with `git reset
--hard`, which throws the changes away.

Needs network; skipped if the search service can't be reached or asks for
a captcha, so a bad minute at DuckDuckGo can't block a release.

Run: tests/run.sh slow   (or directly, optionally with question filters:
     uv run --with textual --with rich python3 tests/slow/test_web_real.py redis)
"""
import asyncio
import os
import re
import subprocess
import sys
import time
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
import fm_pcc.app as m  # noqa: E402
from fm_pcc import web  # noqa: E402

MIN_CORRECT = 17

QUESTIONS = [
    ("What is the default port for the Vite dev server?", r"\b5173\b"),
    ("What is the default max_retries value of requests' HTTPAdapter in Python?", r"\b0\b|\bzero\b"),
    ("What is Redis's default maxmemory-policy?", r"noeviction"),
    ("What is the default worker timeout in seconds for Gunicorn?", r"\b30\b"),
    ("What is the maximum length of a Discord message for non-Nitro users?", r"\b2,?000\b"),
    ("What port does a Jupyter notebook server use by default?", r"\b8888\b"),
    ("What is the default port for MongoDB?", r"\b27017\b"),
    ("What is nginx's default client_max_body_size?", r"\b1\s?m\b|\b1\s?MB\b|1 megabyte"),
    ("What is the default interval of a Docker HEALTHCHECK?", r"\b30\s?s\b|30 seconds"),
    ("What is the default value of max_connections in PostgreSQL?", r"\b100\b"),
    ("In what year was the Go programming language publicly announced?", r"\b2009\b"),
    ("What is the default port of the Next.js dev server?", r"\b3000\b"),
    ("What does the Python error 'RuntimeError: dictionary changed size during iteration' mean?",
     r"(modif|chang|add|remov|delet|insert).{0,60}(while|during|as).{0,30}(iterat|loop)"),
    ("In pandas 2.0 and later, DataFrame.append was removed -- what should you use instead?", r"\bconcat\b"),
    ("Which npm command installs exactly what package-lock.json lists, after deleting node_modules?", r"npm ci\b"),
    ("Which git command undoes the last commit but keeps its changes staged?", r"reset --soft"),
    ("What is the default value returned by Python's sys.getrecursionlimit()?", r"\b1,?000\b"),
    ("Which HTTP status code means Too Many Requests?", r"\b429\b"),
    ("What is SQLite's default maximum number of columns in a table?", r"\b2,?000\b"),
    ("What does the Node.js error EADDRINUSE mean?", r"(address|port).{0,40}(already|in use|owns|taken)|in use"),
]


def ready() -> bool:
    try:
        if subprocess.run(["fm", "available", "--model", "system"], capture_output=True, timeout=15).returncode != 0:
            return False
    except (OSError, subprocess.TimeoutExpired):
        return False
    try:
        return bool(web.search("python"))
    except web.WebError:
        return False


async def answer(question: str) -> str:
    app = m.ChatApp()
    out = []
    async with app.run_test():
        with mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
             mock.patch.object(app, "_ask_answered", side_effect=out.append), \
             mock.patch.object(app, "_log_progress"), mock.patch.object(m, "notify"):
            app._run_web_question.__wrapped__(app, question)
    return out[-1] if out else ""


async def main() -> int:
    if not ready():
        print("SKIPPED: needs the on-device model and a reachable search service")
        return 0
    filters = [f.lower() for f in sys.argv[1:]]
    questions = [q for q in QUESTIONS if not filters or any(f in q[0].lower() for f in filters)]
    wrong, unreachable = [], 0
    for question, rx in questions:
        start = time.monotonic()
        reply = await answer(question)
        if reply.startswith("Couldn't search the web"):
            unreachable += 1
            print(f"skip ({reply})")
            continue
        body = reply.split("\n\nSources:")[0]
        ok = bool(re.search(rx, body, re.I)) and "Sources:" in reply
        print(f"{'ok  ' if ok else 'MISS'} ({time.monotonic() - start:.0f}s) {question}", flush=True)
        if not ok:
            wrong.append(question)
            print(f"       answer: {reply[:300]!r}")
    asked = len(questions) - unreachable
    if unreachable > len(questions) // 2:
        print(f"SKIPPED: the search service stopped answering ({unreachable} of {len(questions)})")
        return 0
    need = MIN_CORRECT * asked // len(QUESTIONS) if not filters else asked - asked // 5
    right = asked - len(wrong)
    print(f"\n{right}/{asked} right (need {need})")
    return 1 if right < need else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
