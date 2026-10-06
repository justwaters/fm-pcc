"""Plain chat hands change requests to /task (chat itself can't touch
files or git), "do it" runs the change just discussed, questions stay chat,
and /push publishes a branch that has no upstream yet."""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m  # noqa: E402
from textual.containers import VerticalScroll  # noqa: E402
from textual.widgets import Input  # noqa: E402


def eq(got, want):
    assert got == want, f"\n got: {got!r}\nwant: {want!r}"


def texts(app):
    return [str(w.render()) for w in app.query_one("#log", VerticalScroll).children]


async def say(app, pilot, text):
    app.query_one(Input).value = text
    await pilot.press("enter")
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def main():
    orig = os.getcwd()
    cwd = tempfile.mkdtemp(prefix="fm-pcc-routing-")
    os.chdir(cwd)
    try:
        for name in ("app.js", "index.html"):
            with open(name, "w") as f:
                f.write("x\n")

        app = m.ChatApp()
        async with app.run_test() as pilot:
            tasks, chats = [], []
            def fake_run_task(request):
                tasks.append(request)
                app._enable_input()  # what the real /task does when it finishes

            with mock.patch.object(app, "_run_task", side_effect=fake_run_task), \
                 mock.patch.object(app.backend, "respond",
                                   side_effect=lambda p, model: (chats.append(p) or "sure", model)):
                await say(app, pilot, "can you please commit and push.")
                assert tasks == ["can you please commit and push."], tasks
                assert any("running as /task" in t for t in texts(app))
                print("direct action request runs as /task OK")

                await say(app, pilot, "what happens if I rename app.js to main.js?")
                assert len(tasks) == 1 and len(chats) == 1, (tasks, chats)
                print("question about an action stays chat OK")

                await say(app, pilot, "i want the ui to have a green and yellow theme")
                assert len(tasks) == 1 and len(chats) == 2
                assert any('reply "do it"' in t for t in texts(app)), texts(app)
                print("vague change request goes to chat, with a do-it tip OK")

                await say(app, pilot, "you do it")
                assert tasks[-1] == "i want the ui to have a green and yellow theme", tasks
                print('"you do it" runs the discussed change as /task OK')

                await say(app, pilot, "yes")
                assert len(tasks) == 2 and len(chats) == 3, (tasks, chats)
                print("a second 'yes' with nothing pending is just chat OK")

                # the answer wanted in a file: only /task can write it
                await say(app, pilot, "give me a list of the iphone 18 family lineup with prices, and write it to findings.md")
                assert tasks[-1].endswith("write it to findings.md"), tasks
                await say(app, pilot, "how do I write a list to out.txt in Python?")
                assert len(tasks) == 3, tasks
                print("asking for the answer in a file runs as /task OK")

                # a vague follow-up runs with what was said before it
                await say(app, pilot, "the base iphone 18 doesnt exist")
                await say(app, pilot, "so change the file")
                await say(app, pilot, "do it")
                eq(tasks[-1], 'The user said: "the base iphone 18 doesnt exist". so change the file')
                print("a vague follow-up keeps the conversation OK")

                # research looks at what was typed, not the folder listing the first turn carries
            researched = []
            with mock.patch.object(app, "_research_if_needed", side_effect=lambda r: researched.append(r)), \
                 mock.patch.object(app.backend, "respond", side_effect=lambda p, model: ("ok", model)):
                app.turn = 0
                await say(app, pilot, "what is a closure")
            eq(researched, ["what is a closure"])
            print("research gets the typed message OK")
    finally:
        os.chdir(orig)
        shutil.rmtree(cwd, ignore_errors=True)

    # ---- the planner gives up on a follow-up: the one file it can be about ----
    cwd = tempfile.mkdtemp(prefix="fm-pcc-followup-")
    os.chdir(cwd)
    try:
        with open("findings.md", "w") as f:
            f.write("- iPhone 18: $999\n- iPhone 18 Pro: $1,199\n")
        app = m.ChatApp()
        async with app.run_test():
            app._message_log += [m.Message("user", "the base iphone 18 doesnt exist"),
                                 m.Message("user", "so change the file")]
            request = app._with_conversation("so change the file")
            eq(app._followup, (request, "findings.md"))
            log = []
            gave_up = [m.taskplan.step("UNSUPPORTED", details="work out how to do it")]
            with mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
                 mock.patch.object(app, "_log_progress", side_effect=log.append), mock.patch.object(m, "notify"), \
                 mock.patch.object(m, "plan_task", return_value=(gave_up, None)), \
                 mock.patch.object(m, "propose_edit", return_value={
                     "path": os.path.join(cwd, "findings.md"), "label": "findings.md",
                     "original": "- iPhone 18: $999\n- iPhone 18 Pro: $1,199\n", "updated": "- iPhone 18 Pro: $1,199\n",
                     "summary": "edited findings.md"}):
                app._run_task.__wrapped__(app, request)
            assert any(str(l).startswith("plan:\n1. edit findings.md") for l in log), log
            eq(open("findings.md").read(), "- iPhone 18 Pro: $1,199\n")
            print("a follow-up the planner can't place edits the one file OK")
    finally:
        os.chdir(orig)
        shutil.rmtree(cwd, ignore_errors=True)

    # ---- /push on a branch with no upstream ----
    cwd = tempfile.mkdtemp(prefix="fm-pcc-upstream-")
    bare = tempfile.mkdtemp(prefix="fm-pcc-upstream-bare-")
    try:
        def git(*args):
            return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        git("init", "-q", "-b", "master")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "T")
        with open(os.path.join(cwd, "a.txt"), "w") as f:
            f.write("a\n")
        git("add", "-A")
        git("commit", "-q", "-m", "init")
        git("remote", "add", "origin", bare)
        m.git_push(cwd)
        assert git("rev-parse", "--abbrev-ref", "master@{u}").strip() == "origin/master"
        print("/push publishes a branch with no upstream OK")
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(bare, ignore_errors=True)


asyncio.run(main())
print("ALL CHAT ROUTING TESTS PASSED")
