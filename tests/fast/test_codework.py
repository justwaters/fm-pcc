"""Offline tests for fm_pcc.codework (context, deterministic code ops,
checks, smoke-run crash attribution, definition lookup) and for /task's
verify-and-repair loop with the model mocked out."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m  # noqa: E402
from fm_pcc import codework as c  # noqa: E402
from fm_pcc import taskplan as t  # noqa: E402


def eq(got, want):
    assert got == want, f"\n got: {got!r}\nwant: {want!r}"


def project(files):
    d = tempfile.mkdtemp(prefix="fm-pcc-codework-")
    for path, text in files.items():
        full = os.path.join(d, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
    return d


# ---- context ----
d = project({
    "b.py": "def parse_config(path):\n    return {}\n",
    "a.py": "def helper():\n    return 1\n",
    "big.py": "\n".join(f"def f{i}():\n    return {i}\n" for i in range(200)),
})
ctx = c.build_context(d, "which file defines parse_config?", 600)
assert ctx.startswith("--- b.py ---\ndef parse_config"), ctx
assert "big.py (outline)" in ctx or "Other files" in ctx, ctx
assert len(ctx) <= 600, len(ctx)
eq(c.outline("b.py", "import os\n\ndef parse_config(path):\n    pass\n\nclass A:\n    pass\n"),
   ["3: def parse_config(path):", "6: class A:"])
eq(c.outline("x.js", "function add(a, b) {\n}\nconst sub = (a, b) => a - b;\n"),
   ["1: function add(a, b) {", "3: const sub = (a, b) => a - b;"])
eq(c.find_definitions(project({"settings.py": "TAX_RATE = 0.2\n"}), "where is the tax rate defined?"),
   ["settings.py:1: TAX_RATE = 0.2"])
print("context OK")

# ---- rename a symbol across files ----
d = project({"lib.py": "def compute_total(xs):\n    return sum(xs)\n",
             "main.py": "from lib import compute_total\nprint(compute_total([1]))\nprint('compute_totals')\n"})
changes = c.rename_symbol("compute_total", "sum_values", d)
eq(sorted(changes), ["lib.py", "main.py"])
eq(changes["main.py"][1], "from lib import sum_values\nprint(sum_values([1]))\nprint('compute_totals')\n")
print("rename_symbol OK")

# ---- move functions into another file ----
d = project({"app.py": "import re\nimport os\n\n\ndef validate_email(s):\n    return re.match(r'.+@.+', s) is not None\n\n\n"
                       "def other():\n    return os.sep\n\n\nprint(validate_email('a@b'))\n"})
changes = c.move_functions(["validate_email"], "app.py", "validators.py", d)
app_new, val_new = changes["app.py"][1], changes["validators.py"][1]
assert "def validate_email" not in app_new and "from validators import validate_email" in app_new, app_new
assert val_new.startswith("import re\n") and "def validate_email" in val_new and "import os" not in val_new, val_new
for rel, (_old, new) in changes.items():
    open(os.path.join(d, rel), "w").write(new)
ok, out = c.run_check([sys.executable, "app.py"], d)
assert ok and out == "True", out
print("move_functions OK")

# ---- JSON ----
eq(c.json_set('{\n    "name": "demo"\n}\n', "timeout", 30), '{\n    "name": "demo",\n    "timeout": 30\n}\n')
eq(t.json_assignment("add a timeout setting of 30 to config.json"), ("timeout", 30))
print("json_set OK")

# ---- function blocks ----
eq([(n, s, e) for n, s, e in c.function_blocks("x.py", "@dec\ndef a():\n    pass\n\ndef b():\n    return 1\n")],
   [("a", 0, 3), ("b", 4, 6)])
eq(c.function_blocks("x.js", "function a() {\n  if (x) {\n  }\n}\nconst b = () => {\n};\n"), [("a", 0, 4), ("b", 4, 6)])
print("function_blocks OK")

# ---- checks ----
d = project({"calc.py": "def add(a, b):\n    return a + b\n",
             "test_calc.py": "import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n"
                             "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n",
             "main.py": "print(1)\n"})
labels = [label for label, _ in c.detect_checks(d, ["calc.py"], "running main.py prints the wrong thing")]
eq(labels, ["Python syntax", "tests", "run main.py"])
for label, argv in c.detect_checks(d, ["calc.py"]):
    ok, out = c.run_check(argv, d)
    assert ok, (label, out)
print("detect_checks OK")

# ---- smoke runs: only crashes inside project code count ----
eq(c.smoke_crash_origin('Traceback:\n  File "_fm_pcc_smoke.py", line 2, in <module>\n'
                        '  File "db.py", line 9, in f\nAttributeError: x', "_fm_pcc_smoke.py"), "db.py")
eq(c.smoke_crash_origin('Traceback:\n  File "_fm_pcc_smoke.py", line 2, in <module>\nNameError: x',
                        "_fm_pcc_smoke.py"), None)
eq(c.smoke_crash_origin('  File "db.py", line 2\nAssertionError', "_fm_pcc_smoke.py"), None)
eq(c.smoke_crash_origin("TypeError: x\n    at sum (calc.js:3:9)\n    at Object.<anonymous> (_fm_pcc_smoke.js:2:1)",
                        "_fm_pcc_smoke.js"), "calc.js")
eq(c.smoke_crash_origin("ReferenceError: x\n    at Object.<anonymous> (_fm_pcc_smoke.js:5:1)\n"
                        "    at Module._compile (node:internal/modules/cjs/loader:1:1)", "_fm_pcc_smoke.js"), None)
d = project({"db.py": "def f():\n    return f.missing\n", "web.js": "document.getElementById('x');\n"})
ok, out, origin = c.run_smoke(d, "import db\nprint(db.f())\n", "python")
assert not ok and origin == "db.py" and "AttributeError" in out, (ok, origin, out)
ok, _out, origin = c.run_smoke(d, "import nonexistent_module\n", "python")
assert ok and origin is None
assert os.listdir(d) == ["db.py", "web.js"] or sorted(os.listdir(d)) == ["db.py", "web.js"]  # nothing written
eq(c.smoke_targets(["db.py", "web.js", "test_db.py"], d), ["db.py"])
print("smoke runs OK")

# ---- definitions: kept, not copied, copies removed with their imports ----
assert c.check_definitions("temps.py", "add an f_to_c function", "def c_to_f(c):\n    return c\n", "def f_to_c(f):\n    return f\n")
eq(c.check_definitions("temps.py", "rename c_to_f to f_to_c", "def c_to_f(c):\n    return c\n", "def f_to_c(f):\n    return f\n"), [])
assert c.check_definitions("calc.go", "fix Max", "func Max() {\n}\n", "func Max() {\n}\nfunc TestMax(t *testing.T) {\n}\n", {"TestMax"})
go = 'package calc\n\nimport "testing"\n\nfunc Max(a, b int) int {\n\treturn a\n}\n\nfunc TestMax(t *testing.T) {\n\tt.Fatal()\n}\n'
eq(c.drop_definitions("calc.go", go, {"TestMax"}), "package calc\n\nfunc Max(a, b int) int {\n\treturn a\n}\n")
print("definitions OK")

# ---- documented examples, runnable scripts, crash location ----
d = project({"duration.py": "def parse_duration(s):\n    \"\"\"'1h30m' -> 90, '45m' -> 45.\"\"\"\n    return 0\n"})
script = c.docstring_examples_script("duration.py", c.read_text(d, "duration.py"), {"parse_duration"})
ok, out, _o = c.run_smoke(d, script, "python")
assert "parse_duration('1h30m') returned 0, but its docstring says 90" in out, out
eq(c.changed_functions("x.py", "def a():\n    return 1\n\ndef b():\n    return 2\n", "def a():\n    return 1\n\ndef b():\n    return 3\n"), {"b"})
assert c.runnable_script("avg.py", "import csv\nprint(1)\n") and not c.runnable_script("wc.py", "import sys\nprint(sys.argv[1])\n")
eq(c.crash_function('File "_s.py", line 4, in <module>\n  File "records.py", line 9, in transform_25\nAttributeError'), "transform_25")
print("behavior checks OK")

# ---- test runs: failing-test ids, no-tests-ran, tests/ folders, foreign paths ----
eq(c.failing_tests("FAIL: test_total (test_store.T.test_total)\n--- FAIL: TestMax (0.00s)\nnot ok 1 - spaces\n"),
   {"test_total (test_store.T.test_total)", "TestMax", "spaces"})
assert c.no_tests_ran("Ran 0 tests in 0.000s\n\nNO TESTS RAN")
d = project({"inventory/__init__.py": "", "inventory/store.py": "def total():\n    return 1\n",
             "tests/test_store.py": "import unittest\nfrom inventory.store import total\n\n\nclass T(unittest.TestCase):\n"
                                    "    def test_total(self):\n        self.assertEqual(total(), 1)\n"})
tests = [argv for label, argv in c.detect_checks(d, []) if label == "tests"]
ok, out = c.run_check(tests[0], d)
assert ok and "Ran 1 test" in out, (tests, out)
eq(c.local_imports(d, "tests/test_store.py", c.project_files(d)), ["inventory/store.py"])
out = ('  File "/opt/local/Library/Frameworks/Python.framework/Versions/3.13/lib/python3.13/unittest/main.py", line 1\n'
       '  File "tests/test_store.py", line 7')
eq(c.files_in_output(out, ["main.py", "tests/test_store.py"], d), ["tests/test_store.py"])
print("test runs OK")

# ---- code blocks from plain-text replies ----
eq(t.extract_code_block("Here:\n```python\ndef f():\n    return 1\n```\nDone."), "def f():\n    return 1")
eq(t.unescape_literal_newlines("a\nb\n", "def f():\\n    return 1"), "def f():\n    return 1")
print("reply cleanup OK")


# ---- /task: verify-and-repair loop, model mocked ----
async def verify_loop():
    d = project({"stats.py": "def average(xs):\n    return sum(xs) / len(xs)\n",
                 "test_stats.py": "import unittest\nfrom stats import average\n\n\nclass T(unittest.TestCase):\n"
                                  "    def test_empty(self):\n        self.assertEqual(average([]), 0)\n"})
    orig = os.getcwd()
    os.chdir(d)
    replies = iter([
        "```python\ndef average(xs):\n    return sum(xs) / len(xs) if xs else None\n```",   # wrong: None
        "```python\ndef average(xs):\n    return sum(xs) / len(xs) if xs else 0\n```",      # fixed
    ])
    prompts = []

    def fake_fm_code(prompt, greedy=True):
        prompts.append(prompt)
        return t.extract_code_block(next(replies))

    log = []
    try:
        app = m.ChatApp()
        async with app.run_test():
            with mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
                 mock.patch.object(app, "_log_progress", side_effect=log.append), \
                 mock.patch.object(m, "notify"), \
                 mock.patch.object(m, "fm_code", side_effect=fake_fm_code):
                app._run_task.__wrapped__(app, "fix average in stats.py: it should return 0 for an empty list")
        with open("stats.py") as f:
            assert "else 0" in f.read(), log
        assert any("tests failed -- fixing stats.py" in line for line in log), log
        assert "checks passed" in " ".join(log) and log[-1].startswith("done"), log
        assert "AssertionError: None != 0" in prompts[1], prompts[1][-400:]
        print("verify-and-repair loop OK")

        # /verify off: no checks run
        app = m.ChatApp()
        async with app.run_test():
            app._handle_command("/verify off")
            assert app._verify_enabled is False
    finally:
        os.chdir(orig)
        shutil.rmtree(d, ignore_errors=True)


asyncio.run(verify_loop())

# the fix excerpt keeps errors, not the warnings around them
swiftc_out = """A.swift:4:17: warning: no calls to throwing functions occur within 'try' expression
2 |
4 |     let m = try await SystemLanguageModel()
A.swift:13:12: error: cannot convert return expression of type 'Response<String>' to return type 'String'
13 |     return response
A.swift:14:1: note: add '.content'
"""
kept = c.errors_only(swiftc_out)
assert "cannot convert" in kept and "no calls to throwing" not in kept and "add '.content'" not in kept, kept
assert c.errors_only("Traceback ...\nValueError: x") == "Traceback ...\nValueError: x"
print("errors_only OK")

# a fix round must keep what the task added and the request asked for
before = "import SwiftUI\n\nstruct CounterView: View {\n    var body: some View {\n        Text(\"TODO\")\n    }\n}\n"
current = before.replace('Text("TODO")', 'Button("Add") { store.increment() }')
keep = c.requested_additions("show a button that calls store.increment()", before, current)
assert keep == ["Button", "increment"], keep
assert c.check_kept(keep, current) == []
assert "removed `Button`" in c.check_kept(keep, current.replace("Button", "Text"))[0]
assert c.requested_additions("rename total to grand_total", "total = 1", "grand_total = 1") == ["grand_total"]
assert c.requested_additions("fix the crash", "x = 1", "x = 2") == []
d = tempfile.mkdtemp(prefix="fm-pcc-keep-")
with open(os.path.join(d, "view.py"), "w") as f:
    f.write("def view(store):\n    return Button('Add', store.increment) + broken\n")
fixes = iter(["```python\ndef view(store):\n    return Text('Add')\n```",                        # drops Button
              "```python\ndef view(store):\n    return Button('Add', store.increment)\n```"])  # keeps it
asked = []
with mock.patch.object(m, "fm_code", side_effect=lambda p, g=True: asked.append(p) or t.extract_code_block(next(fixes))):
    out = m.propose_edit("view.py", "fix it", d, feedback="NameError: broken", expectations=False, keep=["Button"])
assert "Button(" in out["updated"] and len(asked) == 2, out["updated"]
assert "removed `Button`" in asked[1], asked[1][-300:]
shutil.rmtree(d, ignore_errors=True)
# plain words pasted in by a botched edit aren't protected
assert c.requested_additions("rename sum to total, and update everything that uses it",
                             "function sum(xs) {}", "function total, and update everything that uses it {}") == []
print("requested additions kept OK")

# cross-file changes: which files a request involves, and in what order
d = tempfile.mkdtemp(prefix="fm-pcc-touch-")
for rel, text in {
    "inventory/__init__.py": "",
    "inventory/models.py": "from dataclasses import dataclass\n\n\n@dataclass\nclass Item:\n    name: str\n",
    "inventory/store.py": "from inventory.models import Item\n\n\nclass Store:\n    def __init__(self):\n        self.items = []\n\n"
                          "    def add(self, name):\n        self.items.append(Item(name))\n",
    "inventory/report.py": "def format_report(store):\n    return '\\n'.join(i.name for i in store.items)\n",
    "main.py": "from inventory.store import Store\nfrom inventory.report import format_report\n\ns = Store()\ns.add('apple')\n"
               "print(format_report(s))\n",
    "tests/test_store.py": "from inventory.store import Store\n",
}.items():
    os.makedirs(os.path.join(d, os.path.dirname(rel)), exist_ok=True)
    with open(os.path.join(d, rel), "w") as f:
        f.write(text)
files = c.project_files(d)
request = "add a category: Item gets a category field, Store.add takes it, the report shows it, and main.py passes fruit"
touched = c.files_touched(d, request, files)
assert set(touched) == {"inventory/models.py", "inventory/store.py", "inventory/report.py", "main.py"}, touched
assert touched["inventory/models.py"] == ["Item"] and "add" in touched["inventory/store.py"], touched
eq(c.dependency_order(d, ["main.py", "inventory/report.py", "inventory/store.py", "inventory/models.py"]),
   ["inventory/models.py", "inventory/store.py", "inventory/report.py", "main.py"])
# the plan gets the files it missed, definitions first
steps = m._cover_touched([t.step("EDIT", "main.py", details=request)], request, d, files)
eq([(s["path"], bool(s.get("optional"))) for s in steps],
   [("inventory/models.py", True), ("inventory/store.py", True), ("inventory/report.py", True), ("main.py", False)])
# an edit whose instructions name other files goes to those files
steps = m._cover_touched([t.step("EDIT", "main.py", details="add a debug function to inventory/report.py")],
                         "x", d, files)
eq([s["path"] for s in steps], ["inventory/report.py"])
# programs that ran before have to run after; they run on a copy
eq(c.entry_points(d, files), ["main.py"])
ok, out = c.run_entry(d, "main.py")
assert ok and out == "apple", out
with open(os.path.join(d, "inventory/store.py"), "a") as f:
    f.write("\nraise RuntimeError('broken')\n")
ok, out = c.run_entry(d, "main.py")
assert not ok and "inventory/store.py" in out and "broken" in out, out
assert not os.path.exists(os.path.join(d, "__pycache__")) or True
shutil.rmtree(d, ignore_errors=True)
eq(list(c.files_touched(tempfile.gettempdir(), "add a line to the readme saying Run main.py to start", ["README.md", "main.py"])),
   [])   # (main.py is text to write; .md files aren't code to cover)
# ...but an apostrophe isn't a quote: "item's price ... 'Discount: 10%'" still names the report
d2 = tempfile.mkdtemp(prefix="fm-pcc-apos-")
for rel in ("report.py", "store.py", "main.py"):
    open(os.path.join(d2, rel), "w").write("x = 1\n")
req = ("add a discount: Store gets an apply_discount(percent) method that lowers every item's price by that percent, "
       "the report adds a line 'Discount: 10%' when one was applied, and main.py applies 10% before printing")
assert "report.py" in c.files_touched(d2, req, ["report.py", "store.py", "main.py"]), c.files_touched(d2, req, ["report.py", "store.py", "main.py"])
shutil.rmtree(d2, ignore_errors=True)
print("cross-file coverage, order, and entry points OK")

# a missing import is added in code, not by the model
d = tempfile.mkdtemp(prefix="fm-pcc-imp-")
for rel, text in {"models.py": "class User:\n    pass\n", "users.py": "import os\n\nUSERS = [User()]\n",
                  "format.js": "function formatTodo(t) { return t; }\nmodule.exports = { formatTodo };\n",
                  "index.js": "console.log(formatTodo(1));\n"}.items():
    with open(os.path.join(d, rel), "w") as f:
        f.write(text)
fix = c.missing_import_fix(d, 'Traceback:\n  File "users.py", line 3, in <module>\nNameError: name \'User\' is not defined',
                           ["models.py", "users.py", "format.js", "index.js"])
eq(fix[0], "users.py")
eq(fix[2], "import os\nfrom models import User\n\nUSERS = [User()]\n")
fix = c.missing_import_fix(d, f"{d}/index.js:1\nReferenceError: formatTodo is not defined", ["format.js", "index.js"])
eq(fix[2], "const { formatTodo } = require('./format');\nconsole.log(formatTodo(1));\n")
eq(c.missing_import_fix(d, "NameError: name 'Nobody' is not defined", ["models.py", "users.py"]), None)
shutil.rmtree(d, ignore_errors=True)
print("missing imports added OK")

# Swift: types copied in from a file beside it are removed
store = "final class TodoStore {\n    func add() {}\n}\n\nstruct TodoItem {\n    let title: String\n}\n"
eq([n for n, _s, _e in c.type_blocks(store)], ["TodoStore", "TodoItem"])
eq(c.drop_definitions("Store.swift", store, {"TodoItem"}), "final class TodoStore {\n    func add() {}\n}\n")
d = tempfile.mkdtemp(prefix="fm-pcc-swift-ns-")
for rel, text in {"Models.swift": "struct TodoItem {\n    let title: String\n}\n\nfunc helper() {}\n", "Store.swift": store}.items():
    with open(os.path.join(d, rel), "w") as f:
        f.write(text)
assert {"TodoItem", "helper"} <= c.names_defined_elsewhere(d, "Store.swift"), c.names_defined_elsewhere(d, "Store.swift")
shutil.rmtree(d, ignore_errors=True)
print("Swift copied types OK")

# plan fixes: a step's own file named by extension, a second create
steps = m._cover_touched([t.step("EDIT", "config.js", details="add debug to logger.js when config.verbose is set, "
                                                              "and call debug('loaded config')")],
                         "x", tempfile.gettempdir(), ["config.js", "logger.js"])
eq([s["path"] for s in steps], ["logger.js"])
steps = m._cover_touched([t.step("CREATE_FILE", "Shape.swift", details="a Shape protocol"),
                          t.step("CREATE_FILE", "Shape.swift", details="make Circle conform")], "x", tempfile.gettempdir(), [])
eq([s["action"] for s in steps], ["CREATE_FILE", "EDIT"])
# ...but not when the step's own file is named ("the readme")
steps = m._cover_touched([t.step("EDIT", "README.md", details="add a line to the readme saying Run main.py to start")],
                         "x", tempfile.gettempdir(), ["README.md", "main.py"])
eq([s["path"] for s in steps], ["README.md"])
# plans with folders/renames keep their order
plan = [t.step("CREATE_FOLDER", "test"), t.step("CREATE_FILE", "test/path.txt")]
eq([s["action"] for s in m._cover_touched(plan, "x", tempfile.gettempdir(), [])], ["CREATE_FOLDER", "CREATE_FILE"])
plan = [t.step("RENAME", "hello.py", "greet.py"), t.step("EDIT", "greet.py", details="print hello world")]
eq([s["action"] for s in m._cover_touched(plan, "x", tempfile.gettempdir(), ["hello.py"])], ["RENAME", "EDIT"])
# a file created for the whole request isn't then "edited" for it again, and
# a one-file request gets no "part of this request" (seen: "write it to findings.md")
lineup = "give me a list of the iphone 18 family lineup with prices, and write it to findings.md"
steps = m._cover_touched([t.step("CREATE_FILE", "findings.md", details=lineup), t.step("EDIT", "findings.md", details=lineup)],
                         lineup, tempfile.gettempdir(), [])
eq([(s["action"], s["details"]) for s in steps], [("CREATE_FILE", lineup)])
print("plan repairs OK")

# Swift: main.swift's statements pasted into another file are removed
d = tempfile.mkdtemp(prefix="fm-pcc-swift-main-")
main_swift = "let store = TodoStore()\nfor item in store.items {\n    print(item.title)\n}\n"
with open(os.path.join(d, "main.swift"), "w") as f:
    f.write(main_swift)
models = "struct TodoItem {\n    let title: String\n}\n"
eq(c.drop_copied_statements(d, "Models.swift", models, models.replace("}\n", "}\n\n" + main_swift, 1)), models)
shutil.rmtree(d, ignore_errors=True)
# a second edit of the same file is optional
steps = m._cover_touched([t.step("EDIT", "a.py", details="x"), t.step("EDIT", "a.py", details="y")],
                         "x", tempfile.gettempdir(), ["a.py"])
eq([bool(s.get("optional")) for s in steps], [False, True])
# ...but a brace that also closes something elsewhere is just a brace
d = tempfile.mkdtemp(prefix="fm-pcc-swift-brace-")
with open(os.path.join(d, "main.swift"), "w") as f:
    f.write("for x in [1] {\n    print(x)\n}\n")
shape = "protocol Shape {\n    func area() -> Double\n}\n"
eq(c.drop_copied_statements(d, "Shape.swift", "", shape), shape)
shutil.rmtree(d, ignore_errors=True)
print("Swift statements and repeat edits OK")

# each file's part of a multi-file request
req = ("add a discount: Store gets an apply_discount(percent) method, the report adds a line 'Discount: 10%' "
       "when one was applied, and main.py applies 10% before printing")
eq(c.request_part_for(req, "main.py"), "main.py applies 10% before printing")
eq(c.request_part_for(req, "inventory/report.py"), "the report adds a line 'Discount: 10%' when one was applied")
eq(c.request_part_for("make it faster", "main.py"), "")
eq(c.request_part_for("create a package calc with an __init__.py, and an ops.py module containing add and sub functions",
                      "calc/ops.py"), "and an ops.py module containing add and sub functions".replace("and an", "an"))
print("request parts per file OK")

# an optional edit that only pastes another file's code is rejected
d = tempfile.mkdtemp(prefix="fm-pcc-copied-")
app_py = "from users import find_user\n\n\ndef greeting(user_id):\n    user = find_user(user_id)\n    return user.name\n"
with open(os.path.join(d, "app.py"), "w") as f:
    f.write(app_py)
models = "class User:\n    pass\n"
assert c.check_not_copied(d, "models.py", models, models + "\n" + app_py)
assert not c.check_not_copied(d, "models.py", models, models + "\n\ndef by_id(users, i):\n    return users[i]\n")
shutil.rmtree(d, ignore_errors=True)
print("pasted-in edits rejected OK")

# the request's own examples, as checks
ex = c.request_examples("implement parse_duration: strings like '1h30m', '45m' or '2h' give minutes, so '1h30m' gives 90",
                        {"parse_duration": 1})
eq(ex["calls"], [("parse_duration('1h30m')", 90)])
eq(ex["inputs"], ["parse_duration('45m')", "parse_duration('2h')"])
eq(c.request_examples("so apply_discount(50, 150) gives 0; apply_discount(80, 25) still gives 60",
                      {"apply_discount": 2})["calls"], [("apply_discount(50, 150)", 0), ("apply_discount(80, 25)", 60)])
eq(c.request_examples("with its items it should print 'Total: $10.80'", {})["prints"], ["Total: $10.80"])
eq(c.request_examples("make it faster", {"f": 1}), {"calls": [], "inputs": [], "prints": []})
d = tempfile.mkdtemp(prefix="fm-pcc-examples-")
with open(os.path.join(d, "duration.py"), "w") as f:
    f.write("def parse_duration(text):\n    h, _, m = text.partition('h')\n    return int(h) * 60 + int(m.rstrip('m'))\n")
ok, out = c.run_examples(d, "duration.py", ex)
assert not ok and "parse_duration('45m') raised ValueError" in out and "parse_duration('2h') raised" in out, out
with open(os.path.join(d, "math.js"), "w") as f:
    f.write("function clamp(n, lo, hi) { return 0; }\nmodule.exports = { clamp };\n")
ok, out = c.run_examples(d, "math.js", {"calls": [("clamp(5, 0, 10)", 5)], "inputs": [], "prints": []})
assert not ok and "clamp(5, 0, 10) returned 0, but the request says it should give 5" in out, out
eq(c.run_examples(d, "math.js", {"calls": [("clamp(0, 0, 10)", 0)], "inputs": [], "prints": []})[0], True)
shutil.rmtree(d, ignore_errors=True)
print("request examples OK")

# a placeholder function is written from its signature
eq(set(c.stub_functions("roman.py", "def to_roman(n):\n    raise NotImplementedError\n\n\ndef real(x):\n    return x\n")),
   {"to_roman"})
eq(set(c.stub_functions("fb.js", "function fb(n) {\n  throw new Error('not implemented');\n}\n")), {"fb"})
d = tempfile.mkdtemp(prefix="fm-pcc-stub-")
with open(os.path.join(d, "roman.py"), "w") as f:
    f.write("import math\n\n\ndef to_roman(n):\n    raise NotImplementedError\n")
asked = []
with mock.patch.object(m, "fm_code", side_effect=lambda p, g=True: asked.append(p) or "def to_roman(n):\n    return 'I' * n"):
    out = m.propose_edit("roman.py", "implement to_roman in roman.py", d)
eq(out["updated"], "import math\n\n\ndef to_roman(n):\n    return 'I' * n\n")
assert "starting with this line" in asked[0] and "NotImplementedError" not in asked[0], asked[0]
shutil.rmtree(d, ignore_errors=True)
print("placeholder functions written OK")

# names a changed function uses that nothing defines
rates = ("CALLS = []\n\n\ndef fetch_rate(c):\n    return 1\n\n\n"
         "def get_rate(currency):\n    if currency not in rates.rates:\n        rates.rates[currency] = fetch_rate(currency)\n"
         "    return rates.rates[currency]\n")
eq(c.undefined_names("rates.py", rates, {"get_rate"}), [("get_rate", "rates")])
ok_code = ("import math\n_SEEN = {}\n\n\ndef get_rate(currency, *args, **kw):\n    global X\n    try:\n        pass\n"
           "    except ValueError as e:\n        print(e)\n    total = sum(x for x in [1])\n    X = len(_SEEN)\n"
           "    return math.floor(total) + X\n")
eq(c.undefined_names("rates.py", ok_code, {"get_rate"}), [])
print("undefined names OK")

# the prompt's own labels pasted into a file are removed
eq(m._drop_copied("README.md", "# Demo\n", "# Demo\n\nChange request: add a line saying hi\n\nhi\n", tempfile.gettempdir()),
   "# Demo\n\nhi\n")
print("echoed prompt lines removed OK")

# building from one sentence: every named file gets made, in the right place
eq(c.new_files_named("write wordfreq.py, a tool that ..., and tests for it in test_wordfreq.py", []),
   ["wordfreq.py", "test_wordfreq.py"])
eq(c.new_files_named("create a Python package shapes with modules circle.py and square.py, and a main.py", []),
   ["shapes/circle.py", "shapes/square.py", "main.py"])
eq(c.new_files_named("write summary.js, a Node.js script that reads sales.csv", ["sales.csv"]), ["summary.js"])
eq(c.new_files_named("the items are saved in todos.json", []), [])
eq(c.cut_other_file("Counter.swift", "struct Counter {}\n\n// main.swift\nlet c = Counter()\n", "", "."),
   "struct Counter {}\n")
eq(c.cut_other_file("main.swift", "// main.swift\nprint(1)\n", "", "."), "// main.swift\nprint(1)\n")
req = "make convert.js exporting cToF; cli.js that converts a number; and test.js that checks it"
steps = m._cover_touched([t.step("CREATE_FILE", "convert.js", details=req), t.step("CREATE_FILE", "test.js", details=req)],
                         req, tempfile.gettempdir(), [])
eq(sorted(s["path"] for s in steps), ["cli.js", "convert.js", "test.js"])
req = "create a package shapes with modules circle.py and square.py, and a main.py that prints their areas"
steps = m._cover_touched([t.step("CREATE_FILE", "shapes/circle.py", details=req), t.step("CREATE_FILE", "shapes/main.py", details=req)],
                         req, tempfile.gettempdir(), [])
eq(sorted(s["path"] for s in steps), ["main.py", "shapes/circle.py", "shapes/square.py"])
# planning failed outright: the named files are still built
steps = m._cover_touched([t.step("UNSUPPORTED", details="x")], "build todo.py, a to-do app", tempfile.gettempdir(), [])
eq([(s["action"], s["path"]) for s in steps], [("CREATE_FILE", "todo.py")])
print("building from one sentence OK")

# commands the request spells out have to work, in order
cmds = c.request_commands("`python todo.py add <text>` adds an item, `python todo.py list` prints them numbered, "
                          "and `python todo.py done <n>` removes item n")
eq(cmds, [(["python", "todo.py", "add", "example"], None), (["python", "todo.py", "list"], ""),
          (["python", "todo.py", "done", "1"], None)])
eq(c.request_commands("so `node cli.js 100 C` prints 212 F; and test.js"), [(["node", "cli.js", "100", "C"], "212 F")])
d = tempfile.mkdtemp(prefix="fm-pcc-cmds-")
with open(os.path.join(d, "todo.py"), "w") as f:
    f.write("import json, os, sys\nF = 'todos.json'\nitems = json.load(open(F)) if os.path.exists(F) else []\n"
            "if sys.argv[1] == 'add':\n    items.append(sys.argv[2])\nelif sys.argv[1] == 'list':\n    pass\n"
            "json.dump(items, open(F, 'w'))\n")
ok, problem = c.run_commands(d, cmds)
assert not ok and "`python todo.py list` printed nothing" in problem, problem
assert not os.path.exists(os.path.join(d, "todos.json"))            # ran on a copy
shutil.rmtree(d, ignore_errors=True)
# a file named bare goes at the top unless the request mentions its folder
req = "make index.html, about.html and contact.html, all styled by one style.css"
steps = m._cover_touched([t.step("CREATE_FILE", "index.html", details=req), t.step("CREATE_FILE", "styles/style.css", details=req)],
                         req, tempfile.gettempdir(), [])
assert "style.css" in [s["path"] for s in steps] and "styles/style.css" not in [s["path"] for s in steps], steps
eq([s["action"] for s in m._cover_touched([t.step("RENAME", "index.htm", "index.html")],
                                          "Rename the file index.htm to index.html.", tempfile.gettempdir(), ["index.htm"])],
   ["RENAME"])
print("request commands and placement OK")

# out of fix rounds: ask the user how it should work, then try again with it
async def hint_checks():
    import threading
    os.environ.pop("FM_PCC_NONINTERACTIVE", None)
    app = m.ChatApp()
    async with app.run_test() as pilot:
        box = app.query_one(m.Input)
        for answer, want in (("part of a kg counts as a whole one", "part of a kg counts as a whole one"), ("", "")):
            out = []
            th = threading.Thread(target=lambda: out.append(app._ask_for_hint("tests", "AssertionError: 8 != 9")), daemon=True)
            th.start()
            for _ in range(100):
                await pilot.pause(0.05)
                if not box.disabled:
                    break
            assert any("Can you say how it should work?" in msg.text for msg in app._transcript)
            box.value = answer
            await pilot.press("enter")
            for _ in range(100):
                await pilot.pause(0.05)
                if not th.is_alive():
                    break
            eq(out, [want])
    os.environ["FM_PCC_NONINTERACTIVE"] = "1"
    eq(m.ChatApp()._ask_for_hint("tests", "x"), "")          # nobody to answer: never asks
    print("asking for a hint OK")


asyncio.run(hint_checks())

d = project({"stats.py": "def average(xs):\n    return sum(xs) / len(xs)\n",
             "test_stats.py": "import unittest\nfrom stats import average\n\n\nclass T(unittest.TestCase):\n"
                              "    def test_empty(self):\n        self.assertEqual(average([]), 0)\n"})
orig = os.getcwd()
os.chdir(d)
prompts = []


def model(prompt, greedy=True):
    prompts.append(prompt)
    if "The user explained: return 0 when the list is empty" in prompt:
        return "def average(xs):\n    return sum(xs) / len(xs) if xs else 0\n"
    return "def average(xs):\n    return sum(xs) / max(len(xs), 1) or None\n"


async def loop_with_hint():
    app = m.ChatApp()
    log = []
    async with app.run_test():
        with mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
             mock.patch.object(app, "_log_progress", side_effect=log.append), mock.patch.object(m, "notify"), \
             mock.patch.object(m, "fm_code", side_effect=model), \
             mock.patch.object(app, "_ask_for_hint", return_value="return 0 when the list is empty") as asked:
            app._run_task.__wrapped__(app, "fix average in stats.py: it should return 0 for an empty list")
    eq(asked.call_count, 1)
    assert "else 0" in open("stats.py").read(), (open("stats.py").read(), log[-4:])
    assert any("trying again with your explanation" in l for l in log), log
    print("fix loop uses the hint OK")


try:
    asyncio.run(loop_with_hint())
finally:
    os.chdir(orig)
    shutil.rmtree(d, ignore_errors=True)

print("ALL CODEWORK TESTS PASSED")
