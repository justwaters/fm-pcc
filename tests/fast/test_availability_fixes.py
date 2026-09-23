import sys, os, asyncio, tempfile, unittest.mock as mock
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m
from textual.containers import VerticalScroll
from textual.widgets import OptionList, Input

m.STATE_PATH = tempfile.mktemp(suffix=".json")

def last_texts(app, n=1):
    log = app.query_one("#log", VerticalScroll)
    return [str(w.render()) for w in log.children[-n:]]

def sync_call_from_thread(fn, *a, **kw):
    return fn(*a, **kw)

async def main():
    # ===== Issue #2: user message always echoed, including slash commands =====
    app = m.ChatApp()
    async with app.run_test() as pilot:
        input_widget = app.query_one(Input)
        input_widget.value = "/help"
        await pilot.press("enter")
        await pilot.pause()
        texts = [str(w.render()) for w in app.query_one("#log", VerticalScroll).children]
        assert any("/help" in t and "you" in t.lower() for t in texts), texts
        print("issue #2: /help echoed as user message OK")

        input_widget.value = "/compare what is 5+3"
        with mock.patch.object(app.backend, "classify", return_value="8"), \
             mock.patch.object(app, "_ollama_available", return_value=False):
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
        texts = [str(w.render()) for w in app.query_one("#log", VerticalScroll).children]
        assert any("/compare what is 5+3" in t for t in texts), texts
        print("issue #2: /compare echoed as user message OK")

    # ===== Issue #1 & #4: /compare skips icloud+-blocked and ollama-unavailable =====
    app = m.ChatApp()
    async with app.run_test() as pilot:
        app.backend._icloud_plus_unavailable.add("cloud-pro")
        asked = []
        def fake_classify(question, model):
            asked.append(model)
            return f"answer from {model}"
        with mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread), \
             mock.patch.object(app.backend, "classify", side_effect=fake_classify), \
             mock.patch.object(app, "_ollama_available", return_value=False):
            app._run_compare.__wrapped__(app, "hi")
        assert "cloud-pro" not in asked, asked
        assert "ollama" not in asked, asked
        assert "on-device" in asked and "cloud" in asked
        print("issue #1/#4: /compare skips blocked cloud-pro and unavailable ollama OK:", asked)

        # progress log should mention skipping, not silently omit
        texts = "\n".join(last_texts(app, 10))
        assert "skipping cloud pro" in texts.lower(), texts
        assert "skipping ollama" in texts.lower(), texts
        print("issue #1/#4: skip reasons logged OK")

    # ===== Issue #4: /model picker greys out ollama when not running =====
    app = m.ChatApp()
    async with app.run_test() as pilot:
        with mock.patch.object(m, "_ollama_list_models", side_effect=RuntimeError("not running")):
            app._open_model_picker()
        palette = app.query_one("#palette", OptionList)
        ollama_option = next(o for o in palette._options if o.id == "ollama")
        assert ollama_option.disabled, "ollama should be disabled in picker when not running"
        assert "Not running" in str(ollama_option.prompt), ollama_option.prompt
        print("issue #4: picker greys out ollama OK:", ollama_option.prompt)
        app._close_model_picker()

    # ===== Issue #4: direct /model ollama blocked when not running =====
    app = m.ChatApp()
    async with app.run_test() as pilot:
        with mock.patch.object(m, "_ollama_list_models", side_effect=RuntimeError("not running")):
            before = app.model
            app._handle_command("/model ollama")
        assert app.model == before, "should not have switched"
        assert "isn't reachable" in last_texts(app)[0], last_texts(app)
        print("issue #4: direct /model ollama rejection OK")

    # ===== sanity: ollama picker still works fine when it IS running =====
    app = m.ChatApp()
    async with app.run_test() as pilot:
        with mock.patch.object(m, "_ollama_list_models", return_value=["llama3.2"]):
            app._open_model_picker()
        palette = app.query_one("#palette", OptionList)
        tag_option = next(o for o in palette._options if o.id == "ollama:llama3.2")
        assert not tag_option.disabled
        print("sanity: ollama enabled + tree shown when running OK")
        app._close_model_picker()

asyncio.run(main())
print("ALL AVAILABILITY-FIX TESTS PASSED")
