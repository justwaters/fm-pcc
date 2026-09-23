import sys, asyncio, unittest.mock as mock
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m
import tempfile
m.STATE_PATH = tempfile.mktemp(suffix='.json')

class FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

async def main():
    # app launched directly on cloud-pro (no real "previous" model distinct from it)
    app = m.ChatApp(initial_model="cloud-pro")
    async with app.run_test() as pilot:
        app.backend._shortcut_ready["cloud-pro"] = True
        assert app._previous_model is None

        def fake_run(cmd):
            if cmd[:2] == ["shortcuts", "run"]:
                return FakeCompleted(1, stderr="you must be signed in to an iCloud+ account to use the Cloud Pro model.")
            return FakeCompleted(0)

        def sync_call_from_thread(fn, *a, **kw):
            return fn(*a, **kw)

        with mock.patch.object(m, "_run", side_effect=fake_run), \
             mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread):
            app._respond.__wrapped__(app, "howdy")

        assert app.model == "on-device", app.model
        print("fallback-to-on-device OK when no real previous model:", app.model)

asyncio.run(main())
print("FALLBACK TEST PASSED")
