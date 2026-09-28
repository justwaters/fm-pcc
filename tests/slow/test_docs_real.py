"""/docs against the real downloads and the real on-device model.

Every doc set used here is downloaded fresh from its official source, then
asked questions with known answers. Two separate bars, because they measure
two different things:

- Retrieval is fm-pcc's own code (the local search): for EVERY question,
  the passages handed to the model must contain the answer.
- Answering is the on-device model reading those passages: measured on
  these 20 questions at 19/20 (a benchmark of search strategies x prompts
  picked the current ones), so at least 17/20 must come out right, with
  every miss listed.

Needs network (skipped without it).
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

# (set, question, answer present in the evidence, answer correct)
QUESTIONS = [
    ("swift", "In Swift, what keyword declares a constant?", r"\blet\b", r"\blet\b"),
    ("swift", "In Swift, what keyword marks a function that can throw an error?", r"\bthrows\b", r"\bthrows\b"),
    ("python", "In Python, what does str.removeprefix return when the string doesn't start with the prefix?",
     r"removeprefix", r"original|unchanged|same string|copy of"),
    ("python", "In Python, what does enumerate() give you for each item?", r"enumerate", r"index|count|tuple|pair"),
    ("javascript", "In JavaScript, what does Array.prototype.at(-1) return?", r"\blast\b|negative", r"\blast\b"),
    ("javascript", "In JavaScript, what does Array.prototype.includes return?", r"true|false", r"\btrue\b|\bfalse\b|boolean"),
    ("css", "In CSS, what does the gap property set?", r"gap", r"gutter|gaps?\b|space between"),
    ("css", "In CSS, what does position: sticky do?", r"sticky", r"scroll|stick|threshold"),
    ("html", "Which HTML element represents a dialog box or modal?", r"dialog", r"dialog"),
    ("html", "Which attribute makes an HTML input field mandatory?", r"\brequired\b", r"\brequired\b"),
    ("go", "In Go, besides the new context, what does context.WithTimeout return?", r"CancelFunc|cancel", r"cancel"),
    ("go", "In Go, how do you start a goroutine?", r"\bgo\s+\w+\(|go statement|goroutine",
     r"`go`|\bgo keyword|\bgo f|go statement|\bgo\s+\w+\("),
    ("rust", "In Rust, how many mutable references to a value can you have at the same time?",
     r"mutable reference", r"\b(?:one|only one|a single|1)\b"),
    ("rust", "In Rust, what does the ? operator do?", r"\?|question mark|propagat", r"propagat|early return|returns? (?:the )?err"),
    ("react", "In React, when does the cleanup function returned from useEffect run?", r"cleanup",
     r"before|unmount|remov|re-?run|next"),
    ("react", "In React, which hook adds state to a function component?", r"useState", r"useState"),
    ("apple-foundationmodels", "In Apple's Foundation Models framework, which class do you create to get responses from the model?",
     r"LanguageModelSession", r"LanguageModelSession"),
    ("apple-foundationmodels", "In Foundation Models, which macro lets the model generate an instance of your Swift type?",
     r"Generable", r"Generable"),
    ("apple-swiftdata", "In SwiftData, which macro turns a Swift class into a stored model?", r"@Model|Model\(\)", r"@?Model\b"),
    ("apple-widgetkit", "In WidgetKit, which protocol provides a widget's timeline of entries?",
     r"TimelineProvider", r"TimelineProvider"),
]
MIN_CORRECT = 17


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
    questions = [q for q in QUESTIONS if not filters or q[0] in filters]
    home = tempfile.mkdtemp(prefix="fm-pcc-docs-real-")
    retrieval_misses, wrong = [], []
    try:
        with mock.patch.object(m, "DOCS_HOME", home):
            app = m.ChatApp()
            async with app.run_test():
                for set_id in dict.fromkeys(q[0] for q in questions):
                    start = time.monotonic()
                    info = app._docs.install(set_id)
                    print(f"downloaded {set_id}: {info['pages']} pages in {time.monotonic() - start:.0f}s", flush=True)
                for set_id, question, evidence_rx, answer_rx in questions:
                    named = [s for s in docs.sets_named_in(question) if s in app._docs.installed()]
                    chosen = app._docs_passages(question, app._docs_search(question, named or None))
                    retrieved = any(re.search(evidence_rx, body, re.I) for _, body in chosen)
                    answers = []
                    with mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
                         mock.patch.object(app, "_ask_answered", side_effect=answers.append), \
                         mock.patch.object(app, "_log_progress"), mock.patch.object(m, "notify"):
                        app._run_docs_question.__wrapped__(app, question)
                    answer = answers[-1] if answers else ""
                    correct = bool(re.search(answer_rx, answer, re.I)) and "Sources:" in answer
                    status = "ok  " if retrieved and correct else ("RETR" if not retrieved else "MISS")
                    print(f"{status} {set_id}: {question}", flush=True)
                    if not retrieved:
                        retrieval_misses.append(question)
                        print(f"       passages: {[src[:70] for src, _ in chosen]}")
                    if not correct:
                        wrong.append(question)
                        print(f"       answer: {answer[:300]!r}")
    finally:
        shutil.rmtree(home, ignore_errors=True)
    need = MIN_CORRECT if not filters else len(questions) - len(questions) // 7
    right = len(questions) - len(wrong)
    print(f"\nretrieval: {len(questions) - len(retrieval_misses)}/{len(questions)} (must be all); "
          f"answers: {right}/{len(questions)} (need {need})")
    return 1 if retrieval_misses or right < need else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
