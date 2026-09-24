"""Offline tests for the docs library (fm_pcc.docs) and /docs: cleaning,
splitting at headings, indexing and search, install/update/remove, the
picker, /docs questions, and docs used as /task context. A fake doc set
stands in for the real downloads (covered in tests/slow/test_docs_real.py)."""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m  # noqa: E402
from fm_pcc import docs  # noqa: E402


def eq(got, want):
    assert got == want, f"\n got: {got!r}\nwant: {want!r}"


# ---- cleaning ----
meta, body = docs.front_matter('---\ntitle: "`gap` CSS property"\nslug: Web/CSS/gap\n---\nThe **gap** property.\n')
eq(meta["slug"], "Web/CSS/gap")
eq(docs.clean_markdown("See {{cssxref(\"grid\")}} and {{Compat}}. [MDN](/x) {/*a*/}\n\n\n\nEnd"), "See `grid` and . MDN\n\nEnd")
text = docs.html_to_text("<h2>Maps</h2><p>A map is\n  unordered.</p><pre>m := map[string]int{}\n</pre><script>x()</script>")
assert text.startswith("## Maps") and "A map is unordered." in text and "m := map[string]int{}" in text and "x()" not in text, text
assert "r[type.str]" not in docs.re.sub(r"^r\[[\w.\-]+\]\s*$", "", "r[type.str]\nText", flags=docs.re.MULTILINE)
print("cleaning OK")

# ---- passages split at headings, never inside code ----
page = docs.Page("Arrays", "https://x/arrays", "Intro text.\n\n## flatMap\n\nMaps then flattens.\n\n```js\n# not a heading\n```\n\n## at\n\nIndexing.")
parts = docs.passages(page)
eq([h for h, _ in parts], ["Arrays", "Arrays — flatMap", "Arrays — at"])
assert "# not a heading" in parts[1][1]
long_page = docs.Page("Long", "u", "\n\n".join("para " + "x" * 300 for _ in range(20)))
assert all(len(b) <= docs.PASSAGE_CHARS for _, b in docs.passages(long_page))
print("passages OK")

# ---- query terms and language detection ----
terms = docs.query_terms("what does str.removeprefix do in python?")
assert '"str removeprefix"' in terms and '"removeprefix"' in terms and '"what"' not in terms, terms
eq(docs.sets_named_in("how do I go about this"), [])
eq(docs.sets_named_in("context.WithTimeout in Go"), ["go"])
eq(docs.sets_named_in("React useEffect cleanup"), ["react"])
eq(docs.sets_for_files(["a.py", "b.tsx", "c.css"]), ["python", "javascript", "css", "react"])
print("terms / detection OK")


# ---- a fake doc set: install, search, update, remove ----
def fake_fetch(work, progress):
    progress("downloading fake docs…")
    yield docs.Page("Optional Binding", "https://docs.swift.org/optional-binding",
                    "Use `if let` or `guard let` to unwrap an optional.\n\n## guard let\n\nguard let exits the scope early if nil.")
    yield docs.Page("Closures", "https://docs.swift.org/closures", "Closures capture values from their context.")


fake = docs.DocSet("swift", "Swift", "fake Swift docs", fake_fetch, (".swift",))
root = tempfile.mkdtemp(prefix="fm-pcc-docs-")
with mock.patch.dict(docs.SETS, {"swift": fake}):
    lib = docs.Library(root)
    info = lib.install("swift")
    eq((info["pages"], info["passages"]), (2, 3))
    hits = lib.search("how does guard let work in swift")
    eq(hits[0]["heading"], "Optional Binding — guard let")
    eq(hits[0]["url"], "https://docs.swift.org/optional-binding")
    eq(lib.search("nothing matches this zzzqqq"), [])
    lib.install("swift")                      # update: replaced, not duplicated
    eq(len(lib.search("closures capture", limit=50)), 1)
    lib.remove("swift")
    eq(lib.installed(), {})
    eq(lib.search("guard let"), [])
print("library install/search/update/remove OK")


# ---- the app: /docs picker, download, question, /task context ----
async def app_checks():
    home = tempfile.mkdtemp(prefix="fm-pcc-docs-app-")
    with mock.patch.object(m, "DOCS_HOME", home), mock.patch.dict(docs.SETS, {"swift": fake}):
        app = m.ChatApp()
        async with app.run_test() as pilot:
            app._handle_command("/docs")
            await pilot.pause()
            palette = app.query_one("#palette", m.OptionList)
            assert palette.display and app._picker_handler == app._docs_picked
            labels = [str(palette.get_option_at_index(i).prompt) for i in range(palette.option_count)]
            assert any("Swift" in l and "not downloaded" in l for l in labels), labels
            eq(palette.option_count, len(docs.SETS))

            # choosing a set downloads it (the worker run inline)
            download = m.ChatApp._run_docs_download.__wrapped__
            with mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
                 mock.patch.object(app, "_run_docs_download", side_effect=lambda sid: download(app, sid)), \
                 mock.patch.object(m.shutil, "which", return_value="/usr/bin/git"):
                app._picker_choose("swift")
            assert "swift" in app._docs.installed()
            print("/docs picker downloads a set OK")

            # choosing an installed set offers update/remove
            app._handle_command("/docs")
            app._picker_choose("swift")
            ids = [palette.get_option_at_index(i).id for i in range(palette.option_count)]
            eq(ids, ["update:swift", "remove:swift", "cancel"])
            app._picker_choose("cancel")
            assert "swift" in app._docs.installed()
            print("/docs installed-set actions OK")

            # /docs <question>: answered from the passages, with sources
            answers = []
            with mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
                 mock.patch.object(app, "_ask_answered", side_effect=answers.append), \
                 mock.patch.object(m.mapreduce, "answer_over", return_value=("guard let exits early.", [])) as ao:
                app._run_docs_question.__wrapped__(app, "how does guard let work in Swift?")
            docs_given = ao.call_args.args[1]
            assert "guard let exits the scope early" in docs_given[0][1], docs_given
            assert "Sources:\n- https://docs.swift.org/optional-binding" in answers[-1], answers
            print("/docs question answered with sources OK")

            # docs flow into /task edit context for matching files
            ctx = m.docs_context(app._docs, "use guard let to unwrap the optional", ["main.swift"])
            assert ctx.startswith("Relevant documentation:") and "guard let" in ctx, ctx
            eq(m.docs_context(app._docs, "use guard let", ["main.py"]), "")  # no Python docs installed
            print("docs as /task context OK")

            app._handle_command("/docs remove swift")
            eq(app._docs.installed(), {})
            app._handle_command("/docs what is a closure")
            print("/docs remove + no-docs message OK")
    shutil.rmtree(home, ignore_errors=True)


asyncio.run(app_checks())
shutil.rmtree(root, ignore_errors=True)
print("ALL DOCS TESTS PASSED")
