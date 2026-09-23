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

                await say(app, pilot, "yes, do that")
                assert tasks[-1] == "i want the ui to have a green and yellow theme", tasks
                print('"yes, do that" runs the discussed change as /task OK')

                await say(app, pilot, "yes")
                assert len(tasks) == 2 and len(chats) == 3, (tasks, chats)
                print("a second 'yes' with nothing pending is just chat OK")
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
