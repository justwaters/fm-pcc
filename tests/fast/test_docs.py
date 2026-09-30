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
eq(docs.sets_named_in("How do I stream a LanguageModelSession response?"), ["apple-foundationmodels"])
q = "In Apple's Foundation Models framework, which class do you create to get responses?"
terms = docs.query_terms(q, docs.naming_words(q))
assert '"Foundation"' not in terms and '"Models"' not in terms and '"class"' in terms and '"responses"' in terms, terms
eq(docs._symbol_title({"title": "Model()", "fragments": [{"text": "macro"}, {"text": " "}, {"text": "Model"}]}), "@Model")
eq(docs.sets_named_in("SwiftUI NavigationStack with a path"), ["apple-swiftui"])
eq(docs.sets_named_in("URLSession in Foundation"), ["apple-foundation"])
assert "apple-foundation" not in docs.sets_named_in("the Foundation Models framework")
# Swift files bring in the Apple frameworks they import, not all of them
eq(docs.sets_imported("import SwiftUI\n@preconcurrency import Combine\nimport struct Foundation.URL\nimport Alamofire\n"),
   ["apple-swiftui", "apple-combine", "apple-foundation"])
eq(docs.sets_for_files(["App.swift"]), ["swift", "apple-swift"])
code_root = tempfile.mkdtemp(prefix="fm-pcc-swift-")
with open(os.path.join(code_root, "ContentView.swift"), "w") as f:
    f.write("import SwiftUI\nimport SwiftData\n\nstruct ContentView: View {}\n")
with open(os.path.join(code_root, "Chat.swift"), "w") as f:
    f.write("import FoundationModels\n")
eq(docs.sets_for_code(["ContentView.swift"], code_root), ["swift", "apple-swift", "apple-swiftui", "apple-swiftdata"])
# a new file takes the imports of the project's other Swift files
eq(docs.sets_for_code(["NewView.swift"], code_root, ["ContentView.swift", "Chat.swift", "notes.md"]),
   ["swift", "apple-swift", "apple-swiftui", "apple-swiftdata", "apple-foundationmodels"])
eq(docs.sets_for_code(["main.py"], code_root, ["ContentView.swift"]), ["python"])
# rendering Apple's DocC JSON
title, text = docs.render_apple_page({
    "metadata": {"title": "respond(to:)", "roleHeading": "Instance Method", "platforms": [{"name": "macOS", "introducedAt": "26.0"}]},
    "abstract": [{"type": "text", "text": "Produces a response to a "}, {"type": "codeVoice", "code": "Prompt"}, {"type": "text", "text": "."}],
    "primaryContentSections": [
        {"kind": "declarations", "declarations": [{"languages": ["swift"], "tokens": [{"text": "func "}, {"text": "respond"}, {"text": "(to prompt: String) async throws -> Response"}]}]},
        {"kind": "content", "content": [{"type": "heading", "level": 2, "text": "Discussion"},
                                         {"type": "codeListing", "syntax": "swift", "code": ["let r = try await session.respond(to: \"Hi\")"]}]},
    ],
})
eq(title, "respond(to:)")
assert "Produces a response to a `Prompt`." in text and "func respond(to prompt: String) async throws -> Response" in text
assert "## Discussion" in text and "session.respond(to:" in text and "macOS 26.0" in text, text
# an API page for a code prompt: a macro as it's written, and the page's
# example that best matches the request
ref = docs.api_reference([
    ("Model()", "Macro: Model()\n\nConverts a Swift class into a stored model.\n\nAvailability: macOS 14.0\n\n"
                "```swift\n@attached(member, names: arbitrary) @attached(memberAttribute) macro Model()\n```"),
    ("Model() — Overview", "Unrelated:\n\n```swift\nlet container = try ModelContainer(for: Trip.self)\n```\n\n"
                           "Annotate your model classes:\n\n```swift\n@Model\nclass Note {\n    var title: String\n}\n```"),
], 600, "add a model class Note with a title")
assert "@attached" not in ref and "Write it as `@Model`" in ref and "class Note" in ref, ref
assert "ModelContainer" not in ref and "Availability" not in ref, ref
print("API reference OK")
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
            eq(palette.option_count, len(docs.SETS) + 1)  # + the Apple frameworks heading
            assert any("Apple frameworks" in l for l in labels) and any("SwiftUI" in l for l in labels), labels

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
                 mock.patch.object(m, "ask_on_device", return_value="guard let optional binding"), \
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
            # an edit to a SwiftUI file gets SwiftUI's docs; one that
            # doesn't import SwiftUI doesn't
            ui = docs.DocSet("apple-swiftui", "Apple · SwiftUI", "fake SwiftUI", lambda w, p: iter([
                docs.Page("Toggle", "https://developer.apple.com/documentation/swiftui/toggle",
                          "A control that toggles between on and off states.\n\n    Toggle(\"Dark mode\", isOn: $dark)")]), ())
            with mock.patch.dict(docs.SETS, {"apple-swiftui": ui}):
                app._docs.install("apple-swiftui")
            ctx = m.docs_context(app._docs, "add a switch to turn dark mode on and off", ["ContentView.swift"], root=code_root)
            assert "developer.apple.com/documentation/swiftui/toggle" in ctx, ctx
            ctx = m.docs_context(app._docs, "add a switch to turn dark mode on and off", ["Chat.swift"], root=code_root)
            assert "swiftui/toggle" not in ctx, ctx
            # the model picks the APIs it needs from real pages, and sees
            # their declarations and examples first
            fm_pages = [
                docs.Page("LanguageModelSession", "https://developer.apple.com/documentation/foundationmodels/languagemodelsession",
                          "Class: LanguageModelSession\n\nAn object that represents a session that interacts with a language model.\n\n"
                          "```swift\nfinal class LanguageModelSession\n```\n\n## Overview\n\nCreate a session and prompt it:\n\n"
                          "```swift\nlet session = LanguageModelSession()\nlet response = try await session.respond(to: prompt)\n```"),
                docs.Page("respond(to:options:)", "https://developer.apple.com/documentation/foundationmodels/languagemodelsession/respond(to:options:)",
                          "Instance Method: respond(to:options:)\n\nProduces a response to a prompt."),
                docs.Page("Prompting an on-device foundation model", "https://developer.apple.com/documentation/foundationmodels/prompting",
                          "Article: Prompting an on-device foundation model\n\nTailor your prompts to the language model."),
            ]
            fmset = docs.DocSet("apple-foundationmodels", "Apple · Foundation Models", "fake", lambda w, p: iter(fm_pages), ())
            with mock.patch.dict(docs.SETS, {"apple-foundationmodels": fmset}):
                app._docs.install("apple-foundationmodels")
            request = "send the prompt to the language model session and return the response text"
            cands = app._docs.api_candidates(request, ["apple-foundationmodels"])
            eq([c["title"] for c in cands], ["LanguageModelSession"])  # members count toward their type; no articles
            eq(cands[0]["summary"], "An object that represents a session that interacts with a language model.")
            seen = []
            ctx = m.docs_context(app._docs, request, ["Chat.swift"], 1500, root=code_root,
                                 pick_apis=lambda req, cs: seen.append(cs) or ["LanguageModelSession"])
            assert seen and ctx.index("[LanguageModelSession <") < ctx.index("session.respond(to: prompt)"), ctx
            assert ctx.startswith("Relevant documentation:\n[LanguageModelSession"), ctx
            # compiler errors answered from the docs: real members, where a
            # name lives, the shared instance, the documented signatures
            fm_pages.append(docs.Page("SystemLanguageModel", "https://developer.apple.com/documentation/foundationmodels/systemlanguagemodel",
                                      "Class: SystemLanguageModel\n\nAn on-device model.\n\n## Getting the default model\n\n"
                                      "- `default`: The base version of the model.\n\n- `isAvailable`: Whether it's ready."))
            fm_pages.append(docs.Page("respond(to:options:)", "https://developer.apple.com/documentation/foundationmodels/languagemodelsession/respond(to:options:)",
                                      "Instance Method: respond(to:options:)\n\nProduces a response.\n\n```swift\nfunc respond(to prompt: String) async throws -> Response<String>\n```"))
            with mock.patch.dict(docs.SETS, {"apple-foundationmodels": fmset}):
                app._docs.install("apple-foundationmodels")
            facts = app._docs.explain_errors(
                "A.swift:5:36: error: value of type 'SystemLanguageModel' has no member 'respond'\n"
                "A.swift:6:1: error: instance member 'isAvailable' cannot be used on type 'SystemLanguageModel'\n"
                "A.swift:7:1: error: no exact matches in call to instance method 'respond'\n"
                "A.swift:8:1: warning: unrelated", ["apple-foundationmodels"])
            assert "`respond` isn't on `SystemLanguageModel`; it exists on `LanguageModelSession`" in facts, facts
            assert "its members are `default`, `isAvailable`" in facts, facts
            assert "use `SystemLanguageModel.default.isAvailable`" in facts, facts
            assert "`func respond(to prompt: String) async throws -> Response<String>`" in facts, facts
            eq(app._docs.explain_errors("A.swift:1:1: error: something else entirely", ["apple-foundationmodels"]), "")
            print("compiler errors explained from the docs OK")
            app._docs.remove("apple-foundationmodels")
            app._docs.remove("apple-swiftui")
            print("docs as /task context OK")

            app._handle_command("/docs remove swift")
            eq(app._docs.installed(), {})
            app._handle_command("/docs what is a closure")
            print("/docs remove + no-docs message OK")
    shutil.rmtree(home, ignore_errors=True)


asyncio.run(app_checks())
shutil.rmtree(root, ignore_errors=True)
shutil.rmtree(code_root, ignore_errors=True)
print("ALL DOCS TESTS PASSED")
