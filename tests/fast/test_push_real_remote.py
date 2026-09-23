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
        with open("index.html", "w") as f:
            f.write("<html>hi</html>\n")
        app._last_task_description = "create index.html"
        app._handle_push()
        await app.workers.wait_for_complete()
        await pilot.pause()
        text = last_text(app)
        print("push result:", text)
        assert text == "pushed.", text

    log_bare = subprocess.run(["git", "log", "--oneline", "-1", "main"], cwd=bare, capture_output=True, text=True)
    print("bare repo log:", log_bare.stdout.strip())
    assert "fm-pcc: create index.html" in log_bare.stdout
    os.chdir(orig_cwd)

asyncio.run(main())
print("REAL PUSH TO REMOTE TEST PASSED")
