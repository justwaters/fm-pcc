import sys, os, asyncio, shutil, tempfile, subprocess, unittest.mock as mock
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m
from textual.containers import VerticalScroll
from textual.widgets import Static

def last_text(app):
    log = app.query_one("#log", VerticalScroll)
    return str(log.children[-1].render())

async def main():
    # --- palette / model cycle / reset ---
    app = m.ChatApp()
    async with app.run_test() as pilot:
        await pilot.press("/")
        await pilot.pause()
        palette = app.query_one("#palette")
        assert palette.display
        await pilot.press("escape")
        await pilot.pause()
        assert not palette.display

        start = app.model
        app.action_toggle_model()
        assert app.model != start
        print("model cycle OK")

        app._add_message(m.Message("user", "x"))
        app.action_reset()
        assert app._message_log == []
        print("reset OK")

        app._handle_command("/subagents")
        print("subagents usage OK (no markup crash)")

    # --- save/resume ---
    m.SESSIONS_DIR = tempfile.mkdtemp(prefix="fm-pcc-test-sessions-")
    app = m.ChatApp()
    async with app.run_test() as pilot:
        app._handle_save("")
        app._add_message(m.Message("user", "hello there"))
        app._add_message(m.Message("assistant", "hi! **bold**"))
        app.turn = 3
        app.model = "cloud-pro"
        app.backend._cloud_history["cloud-pro"] = [("q1", "a1")]
        app._handle_save("mytest")
        assert os.path.isfile(os.path.join(m.SESSIONS_DIR, "mytest.json"))
        app.action_reset()
        app._handle_resume("mytest")
        assert app.turn == 3
        assert app.model == "cloud-pro"
        texts = [msg.text for msg in app._message_log]
        assert "hello there" in texts
        app._handle_resume("")
        app._handle_resume("doesnotexist")
        print("save/resume OK")

    # --- undo ---
    app = m.ChatApp()
    async with app.run_test() as pilot:
        app._handle_undo()
        assert "nothing to undo" in last_text(app)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("original content\n")
            path = f.name
        app._pending_edit = {"path": path, "label": os.path.basename(path),
                              "original": "original content\n", "updated": "updated content\n"}
        app._apply_pending_edit()
        with open(path) as f:
            assert f.read() == "updated content\n"
        app._handle_undo()
        with open(path) as f:
            assert f.read() == "original content\n"
        os.unlink(path)
        print("undo OK")

    # --- notify ---
    s = m._applescript_string('he said "hi" \\ ok')
    assert s == '"he said \\"hi\\" \\\\ ok"'
    with mock.patch("subprocess.run") as run:
        m.notify("fm-pcc", "x" * 300)
        script = run.call_args[0][0][2]
        assert "…" in script
    with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
        m.notify("t", "m")
    print("notify OK")

    # --- notify wiring ---
    app = m.ChatApp()
    async with app.run_test() as pilot:
        calls = []
        def fake_notify(title, message):
            calls.append((title, message))
        def sync_call_from_thread(fn, *a, **kw):
            return fn(*a, **kw)
        with mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread), \
             mock.patch.object(m, "notify", fake_notify), \
             mock.patch.object(m.time, "monotonic", side_effect=[1000.0, 1010.0]), \
             mock.patch.object(m, "decompose_question", return_value={"answer": "slow"}):
            app._run_ask.__wrapped__(app, "slow question")
        assert len(calls) == 1
        print("notify wiring OK")

    # --- launch context ---
    hint = m.launch_context_hint(os.getcwd())
    assert hint and "git repo" in hint and "README:" in hint
    tmp = tempfile.mkdtemp()
    assert m.launch_context_hint(tmp) is None
    shutil.rmtree(tmp)
    print("launch context OK")

    # --- help note ---
    app = m.ChatApp()
    async with app.run_test() as pilot:
        app._handle_command("/help")
        text = last_text(app)
        assert "--tool ocr" in text and "--tool barcode" in text
    print("help note OK")

    print("ALL FULL REGRESSION TESTS PASSED")

asyncio.run(main())
