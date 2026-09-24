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
print("ALL CODEWORK TESTS PASSED")
