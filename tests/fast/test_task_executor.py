"""Offline tests for /task's plan-then-execute loop and the edit retry
loop. The model is mocked or not needed at all: rename/move/commit/create
folder are planned deterministically and executed without a model call."""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m  # noqa: E402


def sync_call_from_thread(fn, *args, **kwargs):
    return fn(*args, **kwargs)


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


async def run_task(task: str) -> list[str]:
    log: list[str] = []
    app = m.ChatApp()
    async with app.run_test():
        with mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread), \
             mock.patch.object(app, "_log_progress", side_effect=log.append), \
             mock.patch.object(m, "notify"), \
             mock.patch.object(m, "fm_structured", side_effect=AssertionError("no model call expected")), \
             mock.patch.object(app.backend, "classify_with_fallback",
                               side_effect=AssertionError("no model call expected")):
            app._run_task.__wrapped__(app, task)
    return log


async def main():
    orig = os.getcwd()
    cwd = tempfile.mkdtemp(prefix="fm-pcc-executor-")
    os.chdir(cwd)
    try:
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "T")
        with open("old.txt", "w") as f:
            f.write("hi\n")
        os.makedirs("archive")
        with open("archive/.keep", "w") as f:
            f.write("")
        git("add", "-A")
        git("commit", "-q", "-m", "init")

        log = await run_task("rename old.txt to new.txt, move it into archive, and commit with message 'tidy'")
        assert os.path.isfile("archive/new.txt") and not os.path.exists("old.txt"), log
        assert git("log", "-1", "--format=%s").strip() == "tidy", log
        assert git("status", "--porcelain").strip() == "", log
        assert log[-1] == "done after 3 step(s).", log
        print("rename + move + commit, no model OK")

        log = await run_task("commit my changes")
        assert "nothing to commit" in "\n".join(log) and not any(l.startswith("error") for l in log), log
        print("commit on a clean tree is a no-op, not an error OK")

        log = await run_task("commit everything and push")
        assert any("/push" in l and "explicit" in l for l in log), log
        print("push stays gated OK")

        log = await run_task("delete archive/new.txt")
        assert os.path.isfile("archive/new.txt"), log
        assert any("can't" in l for l in log), log
        print("unsupported request reported, nothing touched OK")

        with open("story.txt", "w") as f:
            f.write("The cat sat.\n")
        log = await run_task("in story.txt replace cat with dog")
        with open("story.txt") as f:
            assert f.read() == "The dog sat.\n", log
        print("literal edit with no model call OK")
    finally:
        os.chdir(orig)
        shutil.rmtree(cwd, ignore_errors=True)

    # ---- the edit retry loop ----
    replies = iter([
        m.EditError("Error: The session's transcript exceeded the model's context size."),
        {"content": "hello\n"},            # unchanged -> rejected
        {"content": "hello\nworld\n"},     # right
    ])

    def fake_fm_structured(schema, prompt, greedy=True):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    tmp = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmp, "notes.txt"), "w") as f:
            f.write("hello\n")
        with mock.patch.object(m, "fm_structured", side_effect=fake_fm_structured):
            proposal = m.propose_edit("notes.txt", "add a line that says world", tmp)
        assert proposal["updated"] == "hello\nworld\n", proposal
        print("edit retries past a failed and a rejected attempt OK")

        with mock.patch.object(m, "fm_structured", return_value={"content": "hello\n"}):
            try:
                m.propose_edit("notes.txt", "add a line that says world", tmp)
            except m.EditError as e:
                assert "couldn't produce a correct change" in str(e), e
            else:
                raise AssertionError("an edit that never passes its checks must not be returned")
        print("edit that never passes checks raises instead of writing OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


asyncio.run(main())
print("ALL TASK EXECUTOR TESTS PASSED")
