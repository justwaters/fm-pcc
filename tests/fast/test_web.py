"""Offline tests for /web (fm_pcc.web and the command): parsing a saved
DuckDuckGo results page, unwrapping redirect links, skipping pages that
aren't text, ranking passages, and the /web flow -- with the network
faked throughout."""
import asyncio
import os
import sys
import unittest.mock as mock
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m  # noqa: E402
from fm_pcc import docs, web  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def eq(got, want):
    assert got == want, f"\n got: {got!r}\nwant: {want!r}"


# ---- search results ----
page = open(os.path.join(HERE, "fixtures", "ddg_results.html"), "rb").read()
with mock.patch.object(web, "_get", return_value=("text/html", page)):
    results = web.search("vite default dev server port")
eq(len(results), 3)
eq(results[0]["url"], "https://vite.dev/config/server-options")
assert all(r["title"] and r["url"].startswith("https://") for r in results), results
assert any("5173" in r["snippet"] for r in results), results
eq(web._result_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1&rut=x"), "https://example.com/a?b=1")
eq(web._result_url("https://duckduckgo.com/y.js?ad=1"), None)
with mock.patch.object(web, "_get", side_effect=urllib.error.URLError("offline")):
    try:
        web.search("x")
        raise AssertionError("expected WebError")
    except web.WebError as e:
        assert "are you online" in str(e), e
with mock.patch.object(web, "_get", return_value=("text/html", b"<html>unusual traffic detected</html>")):
    try:
        web.search("x")
        raise AssertionError("expected WebError")
    except web.WebError as e:
        assert "captcha" in str(e), e
print("search results OK")

# ---- pages: text and HTML only ----
article = b"<html><head><title>Server Options</title></head><body><nav>menu</nav><h2>server.port</h2><p>" + \
          b"Default: 5173. Specify server port. " * 20 + b"</p></body></html>"
with mock.patch.object(web, "_get", return_value=("text/html; charset=utf-8", article)):
    p = web.fetch("https://vite.dev/config")
assert p.title == "Server Options" and "Default: 5173" in p.text and "menu" not in p.text, p
with mock.patch.object(web, "_get", return_value=("application/pdf", b"%PDF")):
    eq(web.fetch("https://x/doc.pdf"), None)
with mock.patch.object(web, "_get", return_value=("text/html", b"<p>tiny</p>")):
    eq(web.fetch("https://x/tiny"), None)
print("page fetching OK")

# ---- ranking passages ----
pages = [docs.Page("Vite server options", "https://vite.dev/config",
                   "## server.port\n\nDefault: 5173. Specify server port.\n\n## server.host\n\nWhich IP addresses to listen on."),
         docs.Page("Cooking", "https://food.example", "How to bake bread with yeast and flour.")]
hits = web.rank("what is the default port of the vite dev server", pages, [{"title": "t", "url": "https://s", "snippet": "x"}])
assert hits and hits[0]["url"] == "https://vite.dev/config" and "5173" in hits[0]["body"], hits
assert all(h["url"] != "https://food.example" for h in hits), hits
print("ranking OK")


# ---- /web in the app ----
async def app_checks():
    app = m.ChatApp()
    async with app.run_test():
        answers, logs = [], []
        with mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
             mock.patch.object(app, "_ask_answered", side_effect=answers.append), \
             mock.patch.object(app, "_log_progress", side_effect=logs.append), \
             mock.patch.object(m.web, "search", return_value=[{"title": "Vite", "url": "https://vite.dev/config", "snippet": ""}]), \
             mock.patch.object(m.web, "fetch_all", return_value=pages[:1]), \
             mock.patch.object(m.mapreduce, "answer_over", return_value=("The default port is 5173.", [])) as ao:
            app._run_web_question.__wrapped__(app, "what is the default port of the vite dev server")
            assert "5173" in ao.call_args.args[1][0][1], ao.call_args.args[1]
            eq(answers[-1], "The default port is 5173.\n\nSources:\n- https://vite.dev/config")
            assert any("DuckDuckGo" in l for l in logs), logs           # says where the question goes, once
            app._run_web_question.__wrapped__(app, "again")
            assert sum("DuckDuckGo" in l for l in logs) == 1, logs
        # a search that fails says why, in plain words
        with mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
             mock.patch.object(app, "_ask_answered", side_effect=answers.append), \
             mock.patch.object(app, "_log_progress"), \
             mock.patch.object(m.web, "search", side_effect=m.web.WebError("couldn't reach the search service (offline) -- are you online?")):
            app._run_web_question.__wrapped__(app, "x")
        assert answers[-1].startswith("Couldn't search the web: couldn't reach"), answers[-1]
        # /web with no question explains itself; nothing is searched
        with mock.patch.object(m.web, "search") as s:
            app._handle_command("/web")
            s.assert_not_called()
    print("/web command OK")


asyncio.run(app_checks())

# ---- deciding a request needs the web ----
assert web.names_something("Research the latest ai models and build me an html page")
assert web.names_something("who is the CEO of OpenAI?") and web.names_something("is Python 3.14 out yet?")
assert not web.names_something("build me an html page with a contact form")
assert not web.names_something("make a landing page for my bakery with a menu section")
assert not web.names_something("add a multiply function to calc.py")
judge = mock.Mock(return_value={"needs_web": True})
assert not web.needs_web("make the button blue in style.css", judge) and not judge.called   # no model call
assert web.needs_web("what's the newest version of Python?", judge)
assert not web.needs_web("what's the newest version of Python?", lambda s, p: {"needs_web": False})
def refuse(schema, prompt):
    raise RuntimeError("Error: The model refused to answer.")
assert web.needs_web("who is on the current US Supreme Court?", refuse)   # refused: the request names something
print("needs_web OK")


# ---- asking permission, from a worker thread like /task ----
async def permission_checks():
    import threading
    os.environ.pop("FM_PCC_AUTO_WEB", None)
    app = m.ChatApp()
    async with app.run_test() as pilot:
        palette = app.query_one("#palette", m.OptionList)
        found = ("- GPT-5\n- Claude Opus 5", ["https://example.com/models"])

        def ask_in_background():
            out = []
            t = threading.Thread(target=lambda: out.append(app._research_if_needed("research the latest ai models")),
                                 daemon=True)
            t.start()
            return t, out

        async def finished(t):
            # (never t.join(): the worker logs through the app's own loop)
            for _ in range(200):
                await pilot.pause(0.05)
                if not t.is_alive():
                    return
            raise AssertionError("the worker thread never finished")

        with mock.patch.object(m.web, "needs_web", return_value=True), \
             mock.patch.object(app, "_search_queries", return_value=["latest AI models 2026"]), \
             mock.patch.object(app, "_research", return_value=found) as research:
            # Allow once: searches, nothing remembered
            t, out = ask_in_background()
            for _ in range(50):
                await pilot.pause(0.05)
                if palette.display:
                    break
            eq([palette.get_option_at_index(i).id for i in range(palette.option_count)], ["once", "always", "deny"])
            assert any("Allow agent to search the web?" in msg.text and "latest AI models 2026" in msg.text
                       for msg in app._transcript), [msg.text for msg in app._transcript]
            app._picker_choose("once")
            await finished(t)
            eq(out, [found])
            assert m.load_state().get("web_permission") is None
            # Esc closes the prompt: Deny
            t, out = ask_in_background()
            for _ in range(50):
                await pilot.pause(0.05)
                if palette.display:
                    break
            app._close_model_picker()
            await finished(t)
            eq(out, [None])
            eq(research.call_count, 1)
            # Allow always: remembered, and not asked again
            t, out = ask_in_background()
            for _ in range(50):
                await pilot.pause(0.05)
                if palette.display:
                    break
            app._picker_choose("always")
            await finished(t)
            eq(m.load_state().get("web_permission"), "always")
            t, out = ask_in_background()
            await finished(t)
            eq(out, [found])
            assert not palette.display
            # /web never: no searching on its own; /web ask: asked again
            app._handle_command("/web never")
            eq(m.load_state().get("web_permission"), "never")
            eq(app._research_if_needed("research the latest ai models"), None)
            app._handle_command("/web ask")
            eq(m.load_state().get("web_permission"), None)
        # the permission setting and iCloud+ memory share state.json
        m.save_icloud_plus_unavailable({"cloud-pro"})
        m.update_state(web_permission="always")
        eq(m.load_icloud_plus_unavailable(), {"cloud-pro"})
        m.save_icloud_plus_unavailable(set())
        eq(m.load_state().get("web_permission"), "always")
        m.update_state(web_permission=None)

        # what was found reaches the planner, every edit, and chat replies
        with mock.patch.object(app, "_research_if_needed", return_value=found), \
             mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
             mock.patch.object(app, "_log_progress"), mock.patch.object(m, "notify"), \
             mock.patch.object(m, "plan_task", return_value=([], None)) as plan:
            app._run_task.__wrapped__(app, "research the latest ai models and build me models.html")
        eq(plan.call_args.kwargs.get("research"), found[0])
        prompts = []
        with mock.patch.object(app, "_research_if_needed", return_value=found), \
             mock.patch.object(app.backend, "respond", side_effect=lambda p, mdl: (prompts.append(p) or "GPT-5 and Claude Opus 5.", mdl)), \
             mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)):
            app._respond.__wrapped__(app, "what are the latest ai models?")
        assert "Facts found on the web" in prompts[0] and "Claude Opus 5" in prompts[0], prompts
        reply = [msg.text for msg in app._message_log if msg.role == "assistant"][-1]
        assert reply.endswith("Sources:\n- https://example.com/models"), reply
    os.environ["FM_PCC_AUTO_WEB"] = "0"
    print("permission prompt, settings, and research plumbing OK")


asyncio.run(permission_checks())
print("ALL WEB TESTS PASSED")
