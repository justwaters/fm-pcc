import sys, os, asyncio, tempfile, subprocess
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m
from textual.containers import VerticalScroll

def last_text(app):
    log = app.query_one("#log", VerticalScroll)
    return str(log.children[-1].render())

async def main():
    tmp_nogit = tempfile.mkdtemp()
    orig_cwd = os.getcwd()
    os.chdir(tmp_nogit)
    app = m.ChatApp()
    async with app.run_test() as pilot:
        app._handle_push()
        assert "not a git repository" in last_text(app), last_text(app)
        print("not-a-git-repo OK")
    os.chdir(orig_cwd)

    tmp = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp, check=True)
    with open(os.path.join(tmp, "a.txt"), "w") as f:
        f.write("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp, check=True)

    os.chdir(tmp)
    app = m.ChatApp()
    async with app.run_test() as pilot:
        # No remote configured at all -- can't tell "nothing to push" from
        # "nothing to push TO", so this now attempts it and surfaces git's
        # own actionable error instead of a misleading "nothing to push".
        app._handle_push()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "No configured push destination" in last_text(app), last_text(app)
        print("clean-tree no-remote-configured surfaces real git error OK")

        with open("b.txt", "w") as f:
            f.write("new file\n")
        app._last_task_description = "add b.txt"
        app._handle_push()
        await app.workers.wait_for_complete()
        await pilot.pause()
        text = last_text(app)
        print("push result (no remote):", text)
        assert "push failed" in text or "pushed." in text
        log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=tmp, capture_output=True, text=True)
        assert "fm-pcc: add b.txt" in log.stdout, log.stdout
        print("commit happened with task-derived message OK:", log.stdout.strip())
    os.chdir(orig_cwd)

asyncio.run(main())
print("ALL PUSH TESTS PASSED")
