"""Offline tests for map-reduce (fm_pcc.mapreduce) and where the app uses
it: splitting, parallel sessions, as many reduce tiers as needed, quote
verification, cancellation, the chat's overflow recovery, large pastes,
and multi-place edits to big files. The model is always faked."""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m  # noqa: E402
from fm_pcc import mapreduce as mr  # noqa: E402


def eq(got, want):
    assert got == want, f"\n got: {got!r}\nwant: {want!r}"


# ---- splitting ----
text = "\n".join(f"line {i} " + "x" * 40 for i in range(400))
pieces = mr.split_text(text, "big.txt", 2000, overlap_lines=3)
assert all(len(p.text) <= 2000 for p in pieces), max(len(p.text) for p in pieces)
assert pieces[0].text.startswith("line 0 ") and pieces[-1].text.endswith("line 399 " + "x" * 40)
assert pieces[1].start_line <= pieces[0].start_line + len(pieces[0].text.splitlines()) - 1  # overlap
assert all(set(p.text.splitlines()) <= set(text.splitlines()) for p in pieces)  # never mid-line
edit_pieces = mr.split_text(text, "big.txt", 2000, overlap_lines=0)
eq(sum(len(p.text.splitlines()) for p in edit_pieces), 400)  # no overlap when editing
docs = [("a.txt", "a" * 300), ("b.txt", "b" * 300), ("huge.txt", "h\n" * 3000)]
packed = mr.split_documents(docs, 1000)
eq(packed[0].source, "a.txt, b.txt")
assert all(len(p.text) <= 1000 for p in packed) and any("huge.txt lines" in p.source for p in packed)
print("splitting OK")

# ---- quotes: only what's really in the source survives ----
eq(mr.verify_quotes(["the rate is 0.2", "the rate is 0.5", "tiny"], "Note:  the rate\nis 0.2 today"), ["the rate is 0.2"])
eq(mr.parse_quote_lines("> one line\n> another line"), ["one line", "another line"])
eq(mr.parse_quote_lines("NONE"), [])
print("quotes OK")

# ---- sessions run in parallel ----
active, peak = [0], [0]
lock = threading.Lock()


def slow(item):
    with lock:
        active[0] += 1
        peak[0] = max(peak[0], active[0])
    time.sleep(0.05)
    with lock:
        active[0] -= 1
    return item * 2


engine = mr.MapReduce(parallel=3)
eq(engine.map(list(range(9)), slow), [i * 2 for i in range(9)])
eq(peak[0], 3)
eq(engine.sessions, 9)
print("parallel map OK")

# ---- as many tiers as it takes ----
engine = mr.MapReduce(budget_chars=200)
calls = []


def combine(text, final):
    calls.append(final)
    return "F:" + str(len(text)) if final else "c" * 30  # each group shrinks to 30 chars


result = engine.reduce(["r" * 90 for _ in range(40)], combine)
assert result.startswith("F:") and calls[-1] is True
assert engine.tiers >= 3, engine.tiers  # 40x90 -> 20x30 -> 4x30 -> final
print(f"multi-tier reduce OK ({engine.tiers} tiers, {engine.sessions} sessions)")

# ---- answer_over: invented quotes are dropped, sources kept ----
docs = [(f"f{i}.py", "\n".join(f"# filler {j} for file {i}" for j in range(80))
         + (f"\nRATE_{i} = {i * 7}" if i % 4 == 0 else "")) for i in range(24)]


def fake_ask(prompt):
    if "Copy, word for word" in prompt:
        real = [l for l in prompt.splitlines() if l.startswith("RATE_")]
        return "\n".join(f"> {l}" for l in real) + "\n> RATE_99 = 1000  (made up)" if real else "NONE"
    if "Keep only the excerpts" in prompt:
        return "\n".join(l for l in prompt.splitlines() if l.startswith(("[", ">")))
    return "answer:" + ",".join(sorted(set(l.split(" =")[0] for l in prompt.splitlines() if "RATE_" in l)))


engine = mr.MapReduce(budget_chars=1500)
answer, sources = mr.answer_over("what are the RATE values?", docs, fake_ask, engine)
assert "RATE_99" not in answer, answer
eq(sources, sorted(f"f{i}.py" for i in range(0, 24, 4)))
eq(mr.locate("RATE_8 = 56", docs), "f8.py:81")  # cited by exact file and line
assert engine.sessions > len(docs) // 2
answer, _ = mr.answer_over("what is RATE_4?", [("small.py", "RATE_4 = 28\n")], lambda p: "RATE_4 is 28", mr.MapReduce())
eq(answer, "RATE_4 is 28")  # fits: one plain call, no map-reduce
print("answer_over OK")

# ---- relevant_files, condense, find_edit_sites ----
found = mr.relevant_files(
    "fix totals", [(f"m{i}.py", "code " * 50) for i in range(10)],
    lambda prompt, paths: [{"path": p, "relevant": p in ("m3.py", "m7.py"), "names": ["total"]} for p in paths],
    mr.MapReduce(budget_chars=900),
)
eq(sorted(found), ["m3.py", "m7.py"])
summary = mr.condense("turn " * 3000, "a conversation", lambda p: "short summary", mr.MapReduce(budget_chars=1200))
eq(summary, "short summary")
blocks = [(f"f{i}", i * 10, i * 10 + 10, f"def f{i}(): pass  # {'LOG' if i % 3 == 0 else ''}") for i in range(7)]
eq(mr.find_edit_sites("add logging", blocks, lambda p: "LOG" in p.split("```")[1], mr.MapReduce()),
   [(0, 10), (30, 40), (60, 70)])
print("relevant_files / condense / find_edit_sites OK")

# ---- cancellation ----
stop = mr.MapReduce(cancelled=lambda: True)
try:
    stop.map([1, 2], lambda x: x)
except InterruptedError:
    print("cancellation OK")
else:
    raise AssertionError("a cancelled engine must not run sessions")

# ---- a big file's parts never overlap and cover everything ----
big = "import os\n\n" + "\n\n".join(f"def handler_{i}(req):\n    return {i}" for i in range(400)) + "\n"
parts = m._file_blocks("big.py", big)
covered = sorted((s, e) for _, s, e, _ in parts)
assert all(a[1] <= b[0] for a, b in zip(covered, covered[1:])), "parts overlap"
assert all(len(t) <= m.REWRITE_MAX_CHARS for *_, t in parts)
print("file blocks OK")


# ---- the app: overflowing chat, big pastes, multi-place edits ----
async def app_checks():
    # (1) on-device chat that outgrows its window continues from a summary
    backend = m.Backend()
    backend._on_device_turns = [("q" * 100, "a" * 100)] * 5
    calls = []

    def fake_run(cmd, timeout=None):
        calls.append(cmd)
        if "--resume" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "Error: The session's transcript exceeded the model's context size.")
        return subprocess.CompletedProcess(cmd, 0, "fresh reply", "")

    backend._transcript_path = tempfile.mktemp(suffix=".json")
    open(backend._transcript_path, "w").write("{}")
    with mock.patch.object(m, "_run", side_effect=fake_run), \
         mock.patch.object(m.Backend, "classify", return_value="they talked about q and a"):
        eq(backend._respond_on_device("next question"), "fresh reply")
    assert "Summary of our conversation so far:\nthey talked about q and a" in calls[-1][-1], calls[-1]
    eq(backend._on_device_turns[0][0], "(summary of the conversation so far)")
    print("chat overflow continues from a summary OK")

    cwd = tempfile.mkdtemp(prefix="fm-pcc-mr-")
    orig = os.getcwd()
    os.chdir(cwd)
    try:
        # (2) a big @file goes through map-reduce instead of being truncated
        with open("notes.txt", "w") as f:
            f.write(("filler text " * 20 + "\n") * 200 + "the launch date is March 3\n")
        app = m.ChatApp()
        async with app.run_test() as pilot:
            with mock.patch.object(m.mapreduce, "answer_over", return_value=("It's March 3 [notes.txt]", ["notes.txt"])) as ao:
                from textual.widgets import Input
                app.query_one(Input).value = "when is the launch? @notes.txt"
                await pilot.press("enter")
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
            question, docs = ao.call_args.args[0], ao.call_args.args[1]
            assert "notes.txt" in question and docs[0][0] == "notes.txt" and "March 3" in docs[0][1], (question, docs[0][0])
        print("big @file answered over the whole file OK")

        # (3) /task: an edit touching several parts of a big file
        big = "\n\n".join(f"def handler_{i}(req):\n    return {i}" for i in range(400)) + "\n"
        with open("handlers.py", "w") as f:
            f.write(big)
        app = m.ChatApp()
        async with app.run_test():
            wanted = {"handler_5", "handler_200", "handler_399"}
            with mock.patch.object(m, "judge_yes_on_device", side_effect=lambda p: any(f"def {w}(" in p for w in wanted)), \
                 mock.patch.object(m, "fm_code", side_effect=lambda p, g=True: m.taskplan.extract_code_block(
                     p.split("Current handlers.py:")[1]).replace("return", "return -1 *")), \
                 mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
                 mock.patch.object(m, "notify"):
                out = app._edit_every_site("handlers.py", big, "negate the handlers that need it", cwd, "", "t", {})
        changed = [l for l in out.splitlines() if "-1 *" in l]
        eq(len(changed), 3)
        eq(len(out.splitlines()), len(big.splitlines()))
        assert "return -1 * 200" in out and "return 201" in out
        print("multi-place edit to a big file OK")

        # a part edit that re-imports and adds module-level setup: the
        # import is hoisted once to the top, the setup dropped
        def noisy(p, g=True):
            body = m.taskplan.extract_code_block(p.split("Current handlers.py:")[1])
            return "import json\n\nprint('setup')\n\n" + body.replace("return", "return json.loads('1') *")
        with open("handlers.py", "w") as f:
            f.write(big)
        app = m.ChatApp()
        async with app.run_test():
            with mock.patch.object(m, "judge_yes_on_device", side_effect=lambda p: any(f"def {w}(" in p for w in wanted)), \
                 mock.patch.object(m, "fm_code", side_effect=noisy), \
                 mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
                 mock.patch.object(m, "notify"):
                out = app._edit_every_site("handlers.py", big, "use json in the handlers that need it", cwd, "", "t", {})
        eq(out.count("import json"), 1)
        assert out.startswith("import json\n") and "print('setup')" not in out, out[:200]
        eq(out.count("json.loads('1') *"), 3)
        subprocess.run([sys.executable, "-c", "compile(open('handlers.py').read(), 'h', 'exec')"], check=True)
        print("part edits: imports hoisted once, stray setup dropped OK")
    finally:
        os.chdir(orig)
        shutil.rmtree(cwd, ignore_errors=True)


asyncio.run(app_checks())
print("ALL MAPREDUCE TESTS PASSED")
