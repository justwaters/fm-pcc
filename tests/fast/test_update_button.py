import sys, asyncio, unittest.mock as mock
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m
from textual.widgets import Button
from textual.containers import VerticalScroll

def last_text(app):
    log = app.query_one("#log", VerticalScroll)
    return str(log.children[-1].render())

# --- version helpers ---
assert m._version_tuple("0.34") == (0, 34)
assert m.is_newer("0.34", "0.33") is True
assert m.is_newer("0.33", "0.33") is False
assert m.is_newer("0.32", "0.33") is False
print("version helpers OK")

class FakeResp:
    def __init__(self, body, url=""):
        self.body = body.encode()
        self.url = url
    def read(self):
        return self.body
    def geturl(self):
        return self.url
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

with mock.patch("urllib.request.urlopen", side_effect=OSError("no network")):
    latest, error = m.fetch_latest_version()
    assert latest is None
    assert "no network" in error
print("fetch_latest_version graceful-failure with diagnostic OK")

with mock.patch("urllib.request.urlopen", return_value=FakeResp('{"tag_name": "v9.99"}')):
    latest, error = m.fetch_latest_version()
    assert latest == "9.99", latest
    assert error is None
print("fetch_latest_version success-path (v-prefixed tag) OK")

# the web redirect is tried first and needs no API call at all
web = FakeResp("", url="https://github.com/justwaters/fm-pcc/releases/tag/v1.23")
with mock.patch("urllib.request.urlopen", return_value=web) as urlopen:
    latest, error = m.fetch_latest_version()
    assert (latest, error) == ("1.23", None), (latest, error)
    assert urlopen.call_count == 1
print("latest version read from the web redirect OK")

# API rate-limited but the web page works -> still fine (the real bug)
def web_ok_api_limited(req, timeout):
    if "api.github.com" in req.full_url:
        raise OSError("HTTP Error 403: rate limit exceeded")
    return FakeResp("", url="https://github.com/justwaters/fm-pcc/releases/tag/v2.00")
with mock.patch("urllib.request.urlopen", side_effect=web_ok_api_limited):
    assert m.fetch_latest_version() == ("2.00", None)
print("API rate limit doesn't break the update check OK")

with mock.patch("urllib.request.urlopen", return_value=FakeResp('{"message": "Not Found"}')):
    latest, error = m.fetch_latest_version()
    assert latest is None
    assert error is not None
print("fetch_latest_version handles missing tag_name OK")

def sync_call_from_thread(fn, *a, **kw):
    return fn(*a, **kw)

async def main():
    # startup check: update available -> button shown
    app = m.ChatApp()
    async with app.run_test() as pilot:
        with mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread), \
             mock.patch.object(m, "fetch_latest_version", return_value=("9.99", None)), \
             mock.patch.object(m, "__version__", "0.33"):
            app._check_for_update.__wrapped__(app)
        button = app.query_one("#update-button", Button)
        assert button.display is True
        assert "9.99" in str(button.label) and "0.33" in str(button.label)
        print("startup check: button shown OK:", str(button.label))

    # startup check: no update -> button hidden
    app = m.ChatApp()
    async with app.run_test() as pilot:
        with mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread), \
             mock.patch.object(m, "fetch_latest_version", return_value=(None, "timed out")):
            app._check_for_update.__wrapped__(app)
        button = app.query_one("#update-button", Button)
        assert button.display is False
        print("startup check: silent on failure OK")

    # /update manual command: up to date
    app = m.ChatApp()
    async with app.run_test() as pilot:
        with mock.patch.object(m, "fetch_latest_version", return_value=("0.33", None)), \
             mock.patch.object(m, "__version__", "0.33"):
            app._handle_command("/update")
            await app.workers.wait_for_complete()
            await pilot.pause()
        assert "up to date (v0.33)" in last_text(app), last_text(app)
        print("/update up-to-date message OK")

    # /update manual command: update available
    app = m.ChatApp()
    async with app.run_test() as pilot:
        with mock.patch.object(m, "fetch_latest_version", return_value=("9.99", None)), \
             mock.patch.object(m, "__version__", "0.33"):
            app._handle_command("/update")
            await app.workers.wait_for_complete()
            await pilot.pause()
        assert "update available: v0.33 -> v9.99" in last_text(app), last_text(app)
        button = app.query_one("#update-button", Button)
        assert button.display is True
        print("/update finds-update message + shows button OK")

    # /update manual command: check failed -> real diagnostic shown
    app = m.ChatApp()
    async with app.run_test() as pilot:
        with mock.patch.object(m, "fetch_latest_version", return_value=(None, "[Errno 8] nodename nor servname provided")):
            app._handle_command("/update")
            await app.workers.wait_for_complete()
            await pilot.pause()
        assert "update check failed" in last_text(app), last_text(app)
        assert "nodename" in last_text(app)
        print("/update surfaces real error OK:", last_text(app))

    # clicking the button still runs the real update flow
    app = m.ChatApp()
    async with app.run_test() as pilot:
        with mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread), \
             mock.patch.object(m, "fetch_latest_version", return_value=("9.99", None)), \
             mock.patch.object(m, "__version__", "0.33"):
            app._check_for_update.__wrapped__(app)
        button = app.query_one("#update-button", Button)
        fake_result = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread), \
             mock.patch("subprocess.run", return_value=fake_result), \
             mock.patch.object(m, "__version__", "0.33"):
            app._run_update.__wrapped__(app)
        assert "restart fm-pcc" in str(button.label), button.label
        print("button click -> real update flow still OK")

asyncio.run(main())
print("ALL UPDATE-BUTTON TESTS PASSED")
