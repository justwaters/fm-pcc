import sys, os, asyncio, tempfile, unittest.mock as mock
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m
from textual.containers import VerticalScroll

def last_text(app):
    log = app.query_one("#log", VerticalScroll)
    return str(log.children[-1].render())

class FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

tmp_state = tempfile.mktemp(suffix=".json")
m.STATE_PATH = tmp_state

# --- load with no file yet -> empty set ---
assert m.load_icloud_plus_unavailable() == set()
print("load-no-file OK")

# --- save + reload round-trip ---
m.save_icloud_plus_unavailable({"cloud-pro"})
assert m.load_icloud_plus_unavailable() == {"cloud-pro"}
print("save/load round-trip OK")

# --- corrupted file -> graceful empty set, no crash ---
with open(tmp_state, "w") as f:
    f.write("not json{{{")
assert m.load_icloud_plus_unavailable() == set()
print("corrupted-file graceful OK")

os.remove(tmp_state)

async def main():
    # --- a fresh Backend picks up persisted state from a prior "session" ---
    m.save_icloud_plus_unavailable({"cloud-pro"})
    backend = m.Backend()
    assert backend._icloud_plus_unavailable == {"cloud-pro"}
    print("Backend.__init__ loads persisted state OK")

    # --- full app: simulate a fresh launch (as if after an update) still
    # knows cloud-pro requires iCloud+, without hitting the shortcut again ---
    app = m.ChatApp()
    async with app.run_test() as pilot:
        assert "cloud-pro" in app.backend._icloud_plus_unavailable
        before = app.model
        app._handle_command("/model cloud-pro")
        assert app.model == before, "should still be blocked after 'restart'"
        assert "requires iCloud+" in last_text(app)
        print("persisted block survives a fresh app instance OK")

        # --- /model reset clears it, in memory and on disk ---
        app._handle_command("/model reset")
        assert "cleared the unavailable status" in last_text(app), last_text(app)
        assert app.backend._icloud_plus_unavailable == set()
        assert m.load_icloud_plus_unavailable() == set()
        print("/model reset clears memory + disk OK")

        # --- reset with nothing to clear ---
        app._handle_command("/model reset")
        assert "no models are currently marked" in last_text(app), last_text(app)
        print("/model reset with nothing to clear OK")

    # --- end to end: real shortcut failure -> persists -> new app instance blocked ---
    app2 = m.ChatApp()
    async with app2.run_test() as pilot:
        app2.backend._shortcut_ready["cloud-pro"] = True
        def fake_run(cmd):
            if cmd[:2] == ["shortcuts", "run"] and "PCC-CloudPro" in cmd:
                return FakeCompleted(1, stderr="you must be signed in to an iCloud+ account to use the Cloud Pro model.")
            return FakeCompleted(0)
        def sync_call_from_thread(fn, *a, **kw):
            return fn(*a, **kw)
        with mock.patch.object(m, "_run", side_effect=fake_run), \
             mock.patch.object(app2, "call_from_thread", side_effect=sync_call_from_thread):
            app2._select_model("cloud-pro")
            app2._respond.__wrapped__(app2, "howdy")
        assert m.load_icloud_plus_unavailable() == {"cloud-pro"}
        print("real failure persists to disk OK")

    # a brand new app instance (simulating relaunch after update) should
    # already know, with zero network calls needed
    app3 = m.ChatApp()
    async with app3.run_test() as pilot:
        assert "cloud-pro" in app3.backend._icloud_plus_unavailable
        print("new instance after 'restart' already knows OK")

asyncio.run(main())
print("ALL ICLOUD+ PERSISTENCE TESTS PASSED")
