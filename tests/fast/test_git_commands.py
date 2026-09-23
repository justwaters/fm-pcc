import sys, os, asyncio, tempfile, subprocess
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m
from textual.containers import VerticalScroll

def last_text(app):
    log = app.query_one("#log", VerticalScroll)
    return str(log.children[-1].render())

async def main():
    bare = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", "--bare"], cwd=bare, check=True)

    repo = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", bare], cwd=repo, check=True)
    with open(os.path.join(repo, "a.txt"), "w") as f:
        f.write("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=repo, check=True)

    orig_cwd = os.getcwd()
    os.chdir(repo)
    app = m.ChatApp()
    async with app.run_test() as pilot:
        # /branch create
        app._handle_command("/branch create feature-x")
        await app.workers.wait_for_complete()
        await pilot.pause()
        print("branch create:", last_text(app))
        assert "now on branch feature-x" in last_text(app)
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True).stdout.strip()
        assert branch == "feature-x", branch

        # /branch switch back
        app._handle_command("/branch switch main")
        await app.workers.wait_for_complete()
        await pilot.pause()
        print("branch switch:", last_text(app))
        assert "now on branch main" in last_text(app)
        branch2 = subprocess.run(["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True).stdout.strip()
        assert branch2 == "main", branch2

        # /branch bad usage
        app._handle_command("/branch bogus")
        assert "usage:" in last_text(app), last_text(app)
        print("branch usage message OK")

        # /pull (nothing new upstream, should still succeed)
        app._handle_command("/pull")
        await app.workers.wait_for_complete()
        await pilot.pause()
        print("pull:", last_text(app))
        assert "pulled" in last_text(app).lower()

    os.chdir(orig_cwd)

asyncio.run(main())
print("ALL GIT COMMAND TESTS PASSED")
