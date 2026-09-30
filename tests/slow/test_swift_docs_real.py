"""/task writing Swift against Apple's frameworks, with the real on-device
model and Apple's real documentation, judged by the compiler.

The on-device model's knowledge of Apple's newer APIs is thin: without
docs it invents them (`LanguageModel.response(to:)`,
`Observation<Counter>`). With the Apple frameworks downloaded in /docs,
/task looks up the APIs a Swift file's imports provide -- the model picks
the one or two it needs from real pages, and sees their declarations and
examples -- and answers the compiler's "no member" / "cannot find" /
"no exact matches" errors from the docs during its fix rounds.

Two bars, as in test_docs_real.py:

- Finding the API is fm-pcc's own code: for EVERY task, the API it needs
  must be among the candidates the model is offered (WANT).
- Writing the code is the model: a task passes when `swiftc -typecheck`
  accepts the result and it does what was asked. Measured on these 18:
  7/18 in both runs without docs; with them 10, 12, 10 and 10 (all 14
  Apple sets installed) and 8, 7 and 8 (just these 8). The first 8 tasks were
  used while building this, the last 10 written afterwards and never
  tuned on. The model's score swings run to run, so MIN_PASS is a floor
  that catches a collapse, with every failure listed.

Needs swiftc, the on-device model, and network for the first download.
The Apple sets are cached for a week in the system temp folder
(fm-pcc-swift-docs-cache): SwiftUI and Foundation take minutes each.
Run: tests/run.sh slow   (or directly, optionally with task-name filters:
     uv run --with textual --with rich python3 tests/slow/test_swift_docs_real.py fm-reply)
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest.mock as mock
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
import fm_pcc.app as m  # noqa: E402

SETS = ["apple-foundationmodels", "apple-swiftdata", "apple-swiftui", "apple-charts", "apple-observation",
        "apple-foundation", "apple-combine", "apple-mapkit"]
CACHE = os.path.join(tempfile.gettempdir(), "fm-pcc-swift-docs-cache")
CACHE_DAYS = 7
MIN_PASS = 6

# The API each task needs, which must be among the candidates offered.
WANT = {
    "fm-reply": "LanguageModelSession", "fm-available": "SystemLanguageModel", "fm-generable": "Generable",
    "swiftdata-model": "Model", "swiftui-nav": "NavigationStack", "swiftui-search": "searchable",
    "charts-bar": "BarMark", "observable": "Observable", "fm-summarize": "LanguageModelSession",
    "swiftdata-container": "ModelContainer", "swiftui-toggle": "Toggle", "swiftui-alert": "alert",
    "urlsession": "URLSession", "observable-view": "Observable", "charts-line": "LineMark",
    "combine-subject": "Subject", "mapkit-map": "Marker", "json-decode": "JSONDecoder",
}


def offered(name: str, request: str, files: list[str], cwd: str, library) -> bool:
    """Whether the API `name` needs is among the candidates /task offers."""
    installed = library.installed()
    sets = [s for s in dict.fromkeys(m.docs.sets_for_code(files, cwd) + m.docs.sets_named_in(request))
            if s.startswith("apple-") and s in installed]
    want = WANT[name]
    for c in library.api_candidates(request, sets):
        base = c["title"].lstrip("@").split("(")[0]
        if base == want or (want == "Subject" and base.endswith("Subject")):
            return True
    return False

# (name, starting files, request, what the result must contain)
TASKS = [
 ("fm-reply", {"Assistant.swift": 'import FoundationModels\n\nfunc reply(to prompt: String) async throws -> String {\n    fatalError("TODO")\n}\n'},
  "in Assistant.swift, implement reply(to:) so it sends the prompt to Apple's on-device language model and returns the response text",
  lambda t: "fatalError" not in t and "LanguageModelSession" in t),
 ("fm-available", {"Assistant.swift": 'import FoundationModels\n\nfunc modelIsReady() -> Bool {\n    fatalError("TODO")\n}\n'},
  "in Assistant.swift, implement modelIsReady() so it returns whether the on-device system language model is available to use",
  lambda t: "fatalError" not in t and "SystemLanguageModel" in t),
 ("fm-generable", {"Recipe.swift": 'import FoundationModels\n\n'},
  "in Recipe.swift, add a @Generable struct Recipe with a name string and an ingredients array of strings, and an async throwing function makeRecipe(for dish: String) -> Recipe that asks a LanguageModelSession to generate a Recipe",
  lambda t: "@Generable" in t and "generating" in t),
 ("swiftdata-model", {"Models.swift": 'import SwiftData\nimport Foundation\n\n'},
  "in Models.swift, add a SwiftData model class Note with a title string and a created date",
  lambda t: "@Model" in t and "class Note" in t),
 ("swiftui-nav", {"ContentView.swift": 'import SwiftUI\n\nstruct ContentView: View {\n    let fruits = ["Apple", "Banana", "Cherry"]\n\n    var body: some View {\n        List(fruits, id: \\.self) { fruit in\n            Text(fruit)\n        }\n    }\n}\n'},
  "in ContentView.swift, wrap the list in a NavigationStack and make each fruit a NavigationLink that shows a detail Text with the fruit's name",
  lambda t: "NavigationStack" in t and "NavigationLink" in t),
 ("swiftui-search", {"ContentView.swift": 'import SwiftUI\n\nstruct ContentView: View {\n    let fruits = ["Apple", "Banana", "Cherry"]\n\n    var body: some View {\n        NavigationStack {\n            List(fruits, id: \\.self) { fruit in\n                Text(fruit)\n            }\n        }\n    }\n}\n'},
  "in ContentView.swift, add a search field that filters the fruits list by the text typed",
  lambda t: "searchable" in t),
 ("charts-bar", {"SalesChart.swift": 'import SwiftUI\nimport Charts\n\nstruct Sale: Identifiable {\n    let id = UUID()\n    let month: String\n    let amount: Double\n}\n\nstruct SalesChart: View {\n    let sales: [Sale]\n\n    var body: some View {\n        Text("TODO")\n    }\n}\n'},
  "in SalesChart.swift, replace the TODO text with a bar chart of the sales, month on the x axis and amount on the y axis",
  lambda t: "Chart" in t and "BarMark" in t and "TODO" not in t),
 ("observable", {"Counter.swift": 'import Foundation\nimport Observation\n\nclass Counter {\n    var count = 0\n    func increment() { count += 1 }\n}\n'},
  "in Counter.swift, make Counter observable using the Observation framework",
  lambda t: "@Observable" in t),
]

# Written afterwards and never tuned on (measured separately below).
HELD_OUT = [
 ("fm-summarize", {"Summarizer.swift": 'import FoundationModels\n\nfunc summarize(_ text: String) async throws -> String {\n    fatalError("TODO")\n}\n'},
  "in Summarizer.swift, implement summarize(_:) using a language model session whose instructions tell it to summarize text in one sentence, and return the summary text",
  lambda t: "fatalError" not in t and "LanguageModelSession" in t and "instructions" in t.lower()),
 ("swiftdata-container", {"Store.swift": 'import SwiftData\nimport Foundation\n\n@Model\nclass Note {\n    var title: String\n    init(title: String) { self.title = title }\n}\n'},
  "in Store.swift, add a function makeContainer() throws -> ModelContainer that creates a model container for the Note model",
  lambda t: "func makeContainer" in t and "ModelContainer(" in t),
 ("swiftui-toggle", {"SettingsView.swift": 'import SwiftUI\n\nstruct SettingsView: View {\n    var body: some View {\n        Form {\n            Text("Settings")\n        }\n    }\n}\n'},
  "in SettingsView.swift, add a toggle labeled Dark mode to the form, bound to a new state property",
  lambda t: "Toggle(" in t and "@State" in t),
 ("swiftui-alert", {"ListView.swift": 'import SwiftUI\n\nstruct ListView: View {\n    @State private var showAlert = false\n\n    var body: some View {\n        Button("Delete") {\n            showAlert = true\n        }\n    }\n}\n'},
  "in ListView.swift, show an alert titled Deleted with an OK button when showAlert is true",
  lambda t: ".alert(" in t),
 ("urlsession", {"Fetcher.swift": 'import Foundation\n\nfunc fetchText(from url: URL) async throws -> String {\n    fatalError("TODO")\n}\n'},
  "in Fetcher.swift, implement fetchText(from:) so it downloads the URL's data with URLSession and returns it decoded as a UTF-8 string",
  lambda t: "fatalError" not in t and "URLSession" in t),
 ("observable-view", {"CounterView.swift": 'import SwiftUI\n\nclass Store {\n    var count = 0\n    func increment() { count += 1 }\n}\n\nstruct CounterView: View {\n    let store = Store()\n\n    var body: some View {\n        Text("TODO")\n    }\n}\n'},
  "in CounterView.swift, make Store observable with the Observation framework and make CounterView show store.count in a Text with a button that calls store.increment()",
  lambda t: "@Observable" in t and "Button" in t and "TODO" not in t),
 ("charts-line", {"TempChart.swift": 'import SwiftUI\nimport Charts\n\nstruct Reading: Identifiable {\n    let id = UUID()\n    let day: Int\n    let celsius: Double\n}\n\nstruct TempChart: View {\n    let readings: [Reading]\n\n    var body: some View {\n        Text("TODO")\n    }\n}\n'},
  "in TempChart.swift, replace the TODO text with a line chart of the readings, day on the x axis and celsius on the y axis",
  lambda t: "LineMark" in t and "TODO" not in t),
 ("combine-subject", {"EventBus.swift": 'import Combine\n\nfinal class EventBus {\n}\n'},
  "in EventBus.swift, give EventBus a publisher property named events that you can send strings through, and a send(_ name: String) method that sends a value to it",
  lambda t: "func send" in t and ("Subject" in t)),
 ("mapkit-map", {"PlaceView.swift": 'import SwiftUI\nimport MapKit\n\nstruct PlaceView: View {\n    var body: some View {\n        Text("TODO")\n    }\n}\n'},
  "in PlaceView.swift, replace the TODO text with a map showing a marker named Apple Park at latitude 37.3349 and longitude -122.0090",
  lambda t: "Map" in t and "Marker" in t and "TODO" not in t),
 ("json-decode", {"Users.swift": 'import Foundation\n\nstruct User: Codable {\n    let name: String\n    let age: Int\n}\n\nfunc parseUsers(_ data: Data) throws -> [User] {\n    fatalError("TODO")\n}\n'},
  "in Users.swift, implement parseUsers(_:) by decoding the JSON data into an array of User",
  lambda t: "fatalError" not in t and "JSONDecoder" in t),
]

ALL = TASKS + HELD_OUT


def ready() -> bool:
    try:
        if subprocess.run(["fm", "available", "--model", "system"], capture_output=True, timeout=15).returncode != 0:
            return False
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(shutil.which("swiftc"))


def online() -> bool:
    try:
        urllib.request.urlopen("https://developer.apple.com/tutorials/data/documentation/charts.json", timeout=10)
        return True
    except OSError:
        return False


def typecheck(cwd: str) -> tuple[bool, str]:
    files = sorted(f for f in os.listdir(cwd) if f.endswith(".swift"))
    r = subprocess.run(["swiftc", "-typecheck", *files], cwd=cwd, capture_output=True, text=True, timeout=180)
    return r.returncode == 0, r.stderr


def library() -> str:
    """The Apple sets, from the cache when it's fresh enough."""
    lib = m.docs.Library(CACHE)
    info = lib.installed()
    stale = [s for s in SETS if s not in info or
             time.time() - time.mktime(time.strptime(info[s]["installed"], "%Y-%m-%d")) > CACHE_DAYS * 86400]
    for set_id in stale:
        start = time.monotonic()
        got = lib.install(set_id)
        print(f"downloaded {set_id}: {got['pages']} pages in {time.monotonic() - start:.0f}s", flush=True)
    return CACHE


async def run_task(task: str) -> list[str]:
    app = m.ChatApp()
    app.subagent_roles["planning"] = "on-device"
    log: list[str] = []
    async with app.run_test():
        with mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
             mock.patch.object(app, "_log_progress", side_effect=log.append), mock.patch.object(m, "notify"):
            app._run_task.__wrapped__(app, task)
    return log


async def main() -> int:
    if not ready():
        print("SKIPPED: needs the on-device model and swiftc")
        return 0
    if not all(s in m.docs.Library(CACHE).installed() for s in SETS) and not online():
        print("SKIPPED: needs network to download Apple's docs")
        return 0
    filters = sys.argv[1:]
    tasks = [t for t in ALL if not filters or t[0] in filters]
    home = library()
    failed, not_offered = [], []
    for name, files, request, check in tasks:
        cwd = tempfile.mkdtemp(prefix=f"fm-pcc-swift-{name}-")
        for path, text in files.items():
            with open(os.path.join(cwd, path), "w") as f:
                f.write(text)
        if not offered(name, request, list(files), cwd, m.docs.Library(home)):
            not_offered.append(name)
            print(f"NOT OFFERED {name}: {WANT[name]} isn't among the API candidates", flush=True)
        orig = os.getcwd()
        os.chdir(cwd)
        start = time.monotonic()
        try:
            with mock.patch.object(m, "DOCS_HOME", home):
                log = await run_task(request)
        except Exception as e:
            log = [f"raised {type(e).__name__}: {e}"]
        finally:
            os.chdir(orig)
        text = "".join(open(os.path.join(cwd, f)).read() for f in sorted(os.listdir(cwd)) if f.endswith(".swift"))
        compiles, errors = typecheck(cwd)
        ok = compiles and check(text)
        print(f"{'ok  ' if ok else 'FAIL'} {name} ({time.monotonic() - start:.0f}s)", flush=True)
        if not ok:
            failed.append(name)
            print(f"       {'compiles' if compiles else (errors.strip().splitlines() or [''])[0][:200]}")
            print(f"       log: {[str(l)[:120] for l in log[-3:]]}")
        shutil.rmtree(cwd, ignore_errors=True)
    need = MIN_PASS if not filters else 0
    print(f"\nAPI offered: {len(tasks) - len(not_offered)}/{len(tasks)} (must be all); "
          f"code: {len(tasks) - len(failed)}/{len(tasks)} passed (need {need})")
    return 1 if not_offered or len(tasks) - len(failed) < need else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
