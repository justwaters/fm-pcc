import sys, asyncio, tempfile, unittest.mock as mock
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m
from textual.containers import VerticalScroll
from textual.widgets import Input, OptionList

m.STATE_PATH = tempfile.mktemp(suffix=".json")

def last_text(app):
    log = app.query_one("#log", VerticalScroll)
    return str(log.children[-1].render())

def make_cloud_pro_usage_limited(backend):
    """Simulate _run_shortcut's real side effect for a usage-limit failure
    on cloud-pro, without faking the whole subprocess/file pipeline."""
    def fake_run_shortcut(model, prompt):
        if model == "cloud-pro":
            backend._session_unavailable["cloud-pro"] = "usage limit reached"
            raise m.CloudTierUnavailable("usage limit reached")
        return f"{model} answer"
    return fake_run_shortcut

async def main():
    # ===== respond(): cloud-pro usage limit -> falls back to cloud =====
    app = m.ChatApp()
    async with app.run_test() as pilot:
        with mock.patch.object(app.backend, "_run_shortcut", side_effect=make_cloud_pro_usage_limited(app.backend)):
            text, model_used = app.backend.respond("hi", "cloud-pro")
        assert model_used == "cloud", model_used
        assert text == "cloud answer", text
        assert app.backend._session_unavailable.get("cloud-pro") == "usage limit reached"
        print("respond(): cloud-pro usage limit falls back to cloud OK")

    # ===== respond(): cloud ALSO fails -> falls back to on-device =====
    app = m.ChatApp()
    async with app.run_test() as pilot:
        def fake_run_shortcut_both_fail(model, prompt):
            app.backend._session_unavailable[model] = "usage limit reached"
            raise m.CloudTierUnavailable("usage limit reached")

        with mock.patch.object(app.backend, "_run_shortcut", side_effect=fake_run_shortcut_both_fail), \
             mock.patch.object(app.backend, "_respond_on_device", return_value="on-device answer"):
            text, model_used = app.backend.respond("hi", "cloud-pro")
        assert model_used == "on-device", model_used
        assert text == "on-device answer"
        assert app.backend._session_unavailable.get("cloud-pro") == "usage limit reached"
        assert app.backend._session_unavailable.get("cloud") == "usage limit reached"
        print("respond(): cloud-pro AND cloud both fail -> falls back to on-device OK")

    # ===== already-known-unavailable tier is skipped without even trying =====
    app = m.ChatApp()
    async with app.run_test() as pilot:
        app.backend._session_unavailable["cloud-pro"] = "usage limit reached"
        attempted = []
        def fake_run_shortcut_track(model, prompt):
            attempted.append(model)
            return f"{model} answer"
        with mock.patch.object(app.backend, "_run_shortcut", side_effect=fake_run_shortcut_track):
            text, model_used = app.backend.respond("hi", "cloud-pro")
        assert model_used == "cloud"
        assert attempted == ["cloud"], attempted  # cloud-pro never even attempted
        print("respond(): known-unavailable tier skipped without attempting OK")

    # ===== ChatApp._respond: model switches + message shown =====
    app = m.ChatApp()
    async with app.run_test() as pilot:
        app.model = "cloud-pro"

        def sync_call_from_thread(fn, *a, **kw):
            return fn(*a, **kw)

        with mock.patch.object(app.backend, "_run_shortcut", side_effect=make_cloud_pro_usage_limited(app.backend)), \
             mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread):
            app._respond.__wrapped__(app, "hello")

        assert app.model == "cloud", app.model
        log = app.query_one("#log", VerticalScroll)
        all_text = "\n".join(str(w.render()) for w in log.children)
        print("all messages:", all_text)
        assert "usage limit reached" in all_text
        assert "switched to cloud" in all_text
        print("ChatApp._respond: model auto-switches + message shown OK")

    # ===== picker greys out usage-limited model =====
    app = m.ChatApp()
    async with app.run_test() as pilot:
        app.backend._session_unavailable["cloud-pro"] = "usage limit reached"
        app._open_model_picker()
        palette = app.query_one("#palette", OptionList)
        opt = next(o for o in palette._options if o.id == "cloud-pro")
        assert opt.disabled
        assert "Usage limit reached" in str(opt.prompt), opt.prompt
        print("picker greys out usage-limited model OK:", opt.prompt)
        app._close_model_picker()

    # ===== direct /model cloud-pro is refused while usage-limited =====
    app = m.ChatApp()
    async with app.run_test() as pilot:
        app.backend._session_unavailable["cloud-pro"] = "usage limit reached"
        before = app.model
        app._handle_command("/model cloud-pro")
        assert app.model == before
        assert "usage limit reached" in last_text(app)
        print("direct /model refused while usage-limited OK")

    # ===== /model reset clears session_unavailable too =====
    app = m.ChatApp()
    async with app.run_test() as pilot:
        app.backend._session_unavailable["cloud-pro"] = "usage limit reached"
        app._handle_command("/model reset")
        assert app.backend._session_unavailable == {}
        assert "cleared the unavailable status" in last_text(app)
        print("/model reset clears session_unavailable OK")

    # ===== /compare skips a session-unavailable model (no fallback there) =====
    app = m.ChatApp()
    async with app.run_test() as pilot:
        app.backend._session_unavailable["cloud-pro"] = "usage limit reached"
        asked = []
        def fake_classify(question, model):
            asked.append(model)
            return f"answer from {model}"
        def sync_call_from_thread(fn, *a, **kw):
            return fn(*a, **kw)
        with mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread), \
             mock.patch.object(app.backend, "classify", side_effect=fake_classify), \
             mock.patch.object(app, "_ollama_available", return_value=False):
            app._run_compare.__wrapped__(app, "hi")
        assert "cloud-pro" not in asked, asked
        assert "on-device" in asked and "cloud" in asked
        print("/compare skips session-unavailable model OK:", asked)

    # ===== /task's planning role falls back and keeps working =====
    app = m.ChatApp()
    async with app.run_test() as pilot:
        def sync_call_from_thread(fn, *a, **kw):
            return fn(*a, **kw)

        calls = {"n": 0}
        def fake_classify_with_fallback(prompt, model):
            calls["n"] += 1
            if calls["n"] == 1:
                # simulate: asked for cloud-pro, backend fell back to cloud
                return "ACTION: GIT_ADD\nTARGET: \nINSTRUCTIONS: ", "cloud"
            return "DONE", "cloud"

        with mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread), \
             mock.patch.object(m, "notify"), \
             mock.patch.object(app.backend, "classify_with_fallback", side_effect=fake_classify_with_fallback), \
             mock.patch.object(m, "git_add_all"):
            app._run_task.__wrapped__(app, "do something")
        assert app.subagent_roles["planning"] == "cloud", app.subagent_roles
        print("/task planning role updates after a fallback OK")

asyncio.run(main())
print("ALL FALLBACK CHAIN TESTS PASSED")
