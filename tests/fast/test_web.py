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
print("ALL WEB TESTS PASSED")
