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
    tmp = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp, check=True)
    subprocess.run(["git", "remote", "add", "origin", bare], cwd=tmp, check=True)
    with open(os.path.join(tmp, "a.txt"), "w") as f:
        f.write("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=tmp, check=True)

    orig_cwd = os.getcwd()
    os.chdir(tmp)
    app = m.ChatApp()
    async with app.run_test() as pilot:
        app._handle_push()
        assert "nothing to push" in last_text(app), last_text(app)
        print("clean tree, real upstream, correctly says nothing to push: OK")
    os.chdir(orig_cwd)

asyncio.run(main())
