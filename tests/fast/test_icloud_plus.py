import sys, asyncio, unittest.mock as mock
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m
import tempfile
m.STATE_PATH = tempfile.mktemp(suffix='.json')
from textual.widgets import OptionList

def last_texts(app, n=3):
    from textual.containers import VerticalScroll
    log = app.query_one("#log", VerticalScroll)
    return [str(w.render()) for w in log.children[-n:]]

class FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

async def main():
    app = m.ChatApp()
    async with app.run_test() as pilot:
        # pretend both shortcuts are already installed so _ensure_ready
        # doesn't try to open an install link
        app.backend._shortcut_ready["cloud"] = True
        app.backend._shortcut_ready["cloud-pro"] = True

        app._select_model("cloud")
        assert app._previous_model == "on-device"

        def fake_run(cmd):
            if cmd[:2] == ["shortcuts", "run"] and "PCC-CloudPro" in cmd:
                return FakeCompleted(
                    1, stderr=(
                        "Shortcut 'PCC-CloudPro' failed: Error: The action "
                        "could not run because you must be signed in to an "
                        "iCloud+ account to use the Cloud Pro model."
                    ),
                )
            if cmd[:2] == ["shortcuts", "run"]:
                # simulate real success: the -o path must actually exist
                out_path = cmd[cmd.index("-o") + 1]
                with open(out_path, "w") as f:
                    f.write("cloud tier reply")
            return FakeCompleted(0, stdout="")

        with mock.patch.object(m, "_run", side_effect=fake_run):
            app._select_model("cloud-pro")
            assert app._previous_model == "cloud"

            def sync_call_from_thread(fn, *a, **kw):
                return fn(*a, **kw)
            with mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread), \
                 mock.patch("subprocess.run", return_value=FakeCompleted(0, stdout="cloud tier reply")):
                app._respond.__wrapped__(app, "howdy")

        assert app.model == "cloud", app.model
        assert "cloud-pro" in app.backend._icloud_plus_unavailable
        texts = last_texts(app)
        assert any("requires iCloud+" in t for t in texts), texts
        print("auto-revert + error message OK, model now:", app.model)

        app._open_model_picker()
        palette = app.query_one("#palette", OptionList)
        cp_option = next(o for o in palette._options if o.id == "cloud-pro")
        assert cp_option.disabled, "cloud-pro should be disabled in the picker"
        assert "Requires iCloud+" in str(cp_option.prompt), cp_option.prompt
        print("picker shows disabled + note OK:", cp_option.prompt)
        app._close_model_picker()

        before = app.model
        app._handle_command("/model cloud-pro")
        assert app.model == before, "should not have switched"
        assert "requires iCloud+" in last_texts(app, 1)[0]
        print("direct /model rejection OK")

asyncio.run(main())
print("ALL ICLOUD+ TESTS PASSED")
