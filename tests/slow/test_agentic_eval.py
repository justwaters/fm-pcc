"""Agentic-coding evaluation of /task and /ask on the real on-device model.

Every case runs in a scratch directory with planning AND building
on-device, and is judged by *running* the result -- the code has to work,
not just look right: features, bug fixes (from a description, an error
message, or failing tests), refactors across files, writing tests,
scaffolding projects, multi-file changes, code + git, and questions about
the code. Three batches: the original 24, then two batches written after
the pipeline was tuned on the first and run untuned as a held-out measure
(17/20 and 8/12 on their first runs; their failures were then fixed).

KNOWN_LIMITATIONS lists cases that document a real on-device model limit
rather than a fm-pcc bug; they're run and reported but don't fail the
suite.

Run: tests/run.sh slow   (or directly, optionally with case-name filters:
     uv run --with textual --with rich python3 tests/slow/test_agentic_eval.py js-)
Exit 0 = every non-limitation case passed (or no on-device model here).
"""
import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
import fm_pcc.app as m  # noqa: E402


def sh(*cmd, timeout=60):
    r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def py(code):
    return sh(sys.executable, "-c", code)


def node(code):
    return sh("node", "-e", code)


def read(p):
    return open(p).read() if os.path.exists(p) else ""


def ok_if(rc_out, want_rc=0, contains=None):
    rc, out = rc_out
    fails = []
    if rc != want_rc:
        fails.append(f"exit {rc}: {out[-300:]}")
    if contains is not None and contains not in out:
        fails.append(f"output lacks {contains!r}: {out[-300:]}")
    return fails


def has_docstrings(path, names):
    try:
        tree = ast.parse(read(path))
    except SyntaxError as e:
        return [f"{path} no longer parses: {e}"]
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    return [f"{n} has no docstring" for n in names if n not in fns or not ast.get_docstring(fns[n])]


BIG = "\n\n".join(f"def function_{i}(x):\n    return x + {i}" for i in range(300)) + "\n"

CASES = [
    # (name, category, files, task, verify)
    ("py-add-function", "feature",
     {"calc.py": "def add(a, b):\n    return a + b\n"},
     "add a multiply function to calc.py",
     lambda: ok_if(py("import calc; assert calc.multiply(3, 4) == 12 and calc.add(2, 2) == 4; print('ok')"), contains="ok")),
    ("py-add-param", "feature",
     {"greet.py": "def greet(name):\n    return f\"Hello, {name}\"\n"},
     "add an optional greeting parameter to greet in greet.py that defaults to Hello",
     lambda: ok_if(py("from greet import greet; assert greet('Bo') == 'Hello, Bo', greet('Bo'); assert greet('Bo', 'Hi') == 'Hi, Bo', greet('Bo','Hi'); print('ok')"), contains="ok")),
    ("py-error-handling", "feature",
     {"parse.py": "def parse_int(s):\n    return int(s)\n"},
     "make parse_int in parse.py return None instead of raising when the string isn't a number",
     lambda: ok_if(py("from parse import parse_int; assert parse_int('5') == 5; assert parse_int('x') is None; print('ok')"), contains="ok")),
    ("py-type-hints", "feature",
     {"calc.py": "def add(a, b):\n    return a + b\n"},
     "add type hints to the add function in calc.py (ints)",
     lambda: (lambda f: [] if f.args.args[0].annotation is not None and f.returns is not None else ["no annotations"])(
         next(n for n in ast.walk(ast.parse(read("calc.py"))) if isinstance(n, ast.FunctionDef)))),
    ("py-docstrings-all", "feature",
     {"shapes.py": "def area(w, h):\n    return w * h\n\n\ndef perimeter(w, h):\n    return 2 * (w + h)\n\n\ndef is_square(w, h):\n    return w == h\n"},
     "add a docstring to every function in shapes.py",
     lambda: has_docstrings("shapes.py", ["area", "perimeter", "is_square"])),
    ("py-large-file", "feature",
     {"funcs.py": BIG},
     "change function_147 in funcs.py so it returns x * 2",
     lambda: ok_if(py("import funcs; assert funcs.function_147(5) == 10; assert funcs.function_146(1) == 147; assert funcs.function_299(0) == 299; print('ok')"), contains="ok")),

    ("py-bug-empty-list", "bugfix",
     {"stats.py": "def average(xs):\n    return sum(xs) / len(xs)\n"},
     "fix average in stats.py: it crashes on an empty list, it should return 0 for an empty list",
     lambda: ok_if(py("from stats import average; assert average([]) == 0; assert average([2, 4]) == 3; print('ok')"), contains="ok")),
    ("py-bug-off-by-one", "bugfix",
     {"util.py": "def range_sum(a, b):\n    \"\"\"Sum of the integers from a to b, inclusive.\"\"\"\n    return sum(range(a, b))\n"},
     "range_sum in util.py is off by one: it should include b",
     lambda: ok_if(py("from util import range_sum; assert range_sum(1, 3) == 6, range_sum(1,3); print('ok')"), contains="ok")),
    ("py-bug-from-error", "bugfix",
     {"main.py": "def load(s):\n    return json.loads(s)\n\nprint(load('{\"a\": 1}')['a'])\n"},
     "running main.py fails with NameError: name 'json' is not defined. fix it",
     lambda: ok_if(sh(sys.executable, "main.py"), contains="1")),
    ("py-fix-failing-test", "bugfix",
     {"math_ops.py": "def square(x):\n    return x * 2\n",
      "test_math_ops.py": "import unittest\nfrom math_ops import square\n\n\nclass T(unittest.TestCase):\n    def test_square(self):\n        self.assertEqual(square(3), 9)\n\n\nif __name__ == '__main__':\n    unittest.main()\n"},
     "the test in test_math_ops.py fails. fix the code so it passes",
     lambda: ok_if(sh(sys.executable, "-m", "unittest", "-q", "test_math_ops")) + (["the test was edited instead of the code"] if "square(3), 9" not in read("test_math_ops.py") else [])),
    ("js-bug-iseven", "bugfix",
     {"utils.js": "function isEven(n) {\n  return n % 2 === 1;\n}\n\nmodule.exports = { isEven };\n"},
     "isEven in utils.js returns the wrong answer. fix it",
     lambda: ok_if(node("const {isEven}=require('./utils.js'); if(!(isEven(2)&&!isEven(3)))process.exit(1); console.log('ok')"), contains="ok")),

    ("py-rename-everywhere", "refactor",
     {"lib.py": "def compute_total(xs):\n    return sum(xs)\n",
      "main.py": "from lib import compute_total\n\nprint(compute_total([1, 2, 3]))\n"},
     "rename compute_total to sum_values everywhere",
     lambda: ok_if(sh(sys.executable, "main.py"), contains="6")
     + (["lib.py still defines compute_total"] if "def compute_total" in read("lib.py") else [])
     + (["main.py still uses compute_total"] if "compute_total" in read("main.py") else [])
     + (["sum_values not defined"] if "def sum_values" not in read("lib.py") else [])),
    ("py-use-helper", "refactor",
     {"utils.py": "def slugify(text):\n    return text.lower().replace(' ', '-')\n",
      "app.py": "def make_url(title):\n    return '/posts/' + title.lower().replace(' ', '-')\n\nprint(make_url('Hello World'))\n"},
     "make make_url in app.py use slugify from utils.py instead of doing it itself",
     lambda: ok_if(sh(sys.executable, "app.py"), contains="/posts/hello-world")
     + (["app.py doesn't use slugify"] if "slugify" not in read("app.py") else [])),
    ("py-extract-module", "refactor",
     {"main.py": "def double(x):\n    return x * 2\n\n\ndef triple(x):\n    return x * 3\n\n\nprint(double(2) + triple(2))\n"},
     "move the double and triple functions from main.py into a new file helpers.py and import them in main.py",
     lambda: ok_if(sh(sys.executable, "main.py"), contains="10")
     + (["helpers.py lacks the functions"] if "def double" not in read("helpers.py") or "def triple" not in read("helpers.py") else [])
     + (["main.py still defines them"] if "def double" in read("main.py") else [])),
    ("py-remove-unused", "refactor",
     {"util.py": "def used():\n    return 1\n\n\ndef old_helper():\n    return 2\n"},
     "remove the unused old_helper function from util.py",
     lambda: ok_if(py("import util; assert util.used() == 1; assert not hasattr(util, 'old_helper'); print('ok')"), contains="ok")),

    ("py-write-tests", "tests",
     {"calc.py": "def add(a, b):\n    return a + b\n"},
     "create test_calc.py with unittest tests for add in calc.py",
     lambda: (lambda r: ok_if(r) + ([] if "Ran" in r[1] and "Ran 0" not in r[1] else [f"no tests ran: {r[1][-200:]}"]))(
         sh(sys.executable, "-m", "unittest", "-v", "test_calc"))),

    ("scaffold-py-cli", "scaffold",
     {"sample.txt": "one two three\nfour five\n"},
     "create a python script wordcount.py that prints the number of words in the file given as its first command line argument",
     lambda: ok_if(sh(sys.executable, "wordcount.py", "sample.txt"), contains="5")),
    ("scaffold-module-and-test", "scaffold",
     {},
     "create temperature.py with a celsius_to_fahrenheit function, and test_temperature.py with unittest tests for it",
     lambda: ok_if(py("from temperature import celsius_to_fahrenheit as f; assert f(100) == 212 and f(0) == 32; print('ok')"), contains="ok")
     + ok_if(sh(sys.executable, "-m", "unittest", "-q", "test_temperature"))),
    ("scaffold-website", "scaffold",
     {},
     "create a simple website: index.html, style.css, and script.js. the page has a button that shows an alert saying hi when clicked",
     lambda: [f for f in [
         None if all(os.path.exists(p) for p in ("index.html", "style.css", "script.js")) else "missing files",
         None if "style.css" in read("index.html") and "script.js" in read("index.html") else "index.html doesn't link style.css and script.js",
         None if "<button" in read("index.html") else "no button",
         None if "alert" in read("script.js") + read("index.html") else "no alert",
     ] if f]),

    ("config-and-code", "multi-file",
     {"config.json": "{\n  \"name\": \"demo\"\n}\n",
      "app.py": "import json\n\nconfig = json.load(open('config.json'))\nprint(config['name'])\n"},
     "add a timeout setting of 30 to config.json and make app.py also print the timeout",
     lambda: ok_if(sh(sys.executable, "app.py"), contains="30") + ok_if(sh(sys.executable, "app.py"), contains="demo")),
    ("feature-and-commit", "git+code",
     {"calc.py": "def add(a, b):\n    return a + b\n", "__git__": ""},
     "add a subtract function to calc.py and commit it with the message 'add subtract'",
     lambda: ok_if(py("import calc; assert calc.subtract(5, 3) == 2; print('ok')"), contains="ok")
     + ([] if sh("git", "log", "-1", "--format=%s")[1] == "add subtract" else [f"commit: {sh('git', 'log', '-1', '--format=%s')[1]}"])),

    ("swift-add-function", "feature",
     {"Math.swift": "func add(_ a: Int, _ b: Int) -> Int {\n    return a + b\n}\n"},
     "add a function isPrime(_ n: Int) -> Bool to Math.swift",
     lambda: (lambda: (open("main.swift", "w").write("print(isPrime(7), isPrime(8), add(1, 2))\n"),
                       ok_if(sh("swiftc", "Math.swift", "main.swift", "-o", "prog", timeout=120)) or ok_if(sh("./prog"), contains="true false 3"))[1])()),
]

ASK_CASES = [
    ("ask-where-defined", "understanding",
     {"a.py": "def helper():\n    return 1\n", "b.py": "def parse_config(path):\n    return {}\n", "c.py": "import b\n"},
     "which file defines parse_config?",
     lambda answer: [] if "b.py" in answer else [f"answer: {answer[:200]}"]),
    ("ask-what-does", "understanding",
     {"util.py": "def f(xs):\n    return [x for x in xs if x % 2 == 0]\n"},
     "what does the function f in util.py do?",
     lambda answer: [] if "even" in answer.lower() else [f"answer: {answer[:200]}"]),
]


FIN = "def helper_0(x):\n    \"\"\"Helper 0.\"\"\"\n    return x * 0 + 1\n\ndef helper_1(x):\n    \"\"\"Helper 1.\"\"\"\n    return x * 1 + 1\n\ndef helper_2(x):\n    \"\"\"Helper 2.\"\"\"\n    return x * 2 + 1\n\ndef helper_3(x):\n    \"\"\"Helper 3.\"\"\"\n    return x * 3 + 1\n\ndef helper_4(x):\n    \"\"\"Helper 4.\"\"\"\n    return x * 4 + 1\n\ndef helper_5(x):\n    \"\"\"Helper 5.\"\"\"\n    return x * 5 + 1\n\ndef helper_6(x):\n    \"\"\"Helper 6.\"\"\"\n    return x * 6 + 1\n\ndef helper_7(x):\n    \"\"\"Helper 7.\"\"\"\n    return x * 7 + 1\n\ndef helper_8(x):\n    \"\"\"Helper 8.\"\"\"\n    return x * 8 + 1\n\ndef helper_9(x):\n    \"\"\"Helper 9.\"\"\"\n    return x * 9 + 1\n\ndef helper_10(x):\n    \"\"\"Helper 10.\"\"\"\n    return x * 10 + 1\n\ndef helper_11(x):\n    \"\"\"Helper 11.\"\"\"\n    return x * 11 + 1\n\ndef helper_12(x):\n    \"\"\"Helper 12.\"\"\"\n    return x * 12 + 1\n\ndef helper_13(x):\n    \"\"\"Helper 13.\"\"\"\n    return x * 13 + 1\n\ndef helper_14(x):\n    \"\"\"Helper 14.\"\"\"\n    return x * 14 + 1\n\ndef helper_15(x):\n    \"\"\"Helper 15.\"\"\"\n    return x * 15 + 1\n\ndef helper_16(x):\n    \"\"\"Helper 16.\"\"\"\n    return x * 16 + 1\n\ndef helper_17(x):\n    \"\"\"Helper 17.\"\"\"\n    return x * 17 + 1\n\ndef helper_18(x):\n    \"\"\"Helper 18.\"\"\"\n    return x * 18 + 1\n\ndef helper_19(x):\n    \"\"\"Helper 19.\"\"\"\n    return x * 19 + 1\n\ndef helper_20(x):\n    \"\"\"Helper 20.\"\"\"\n    return x * 20 + 1\n\ndef helper_21(x):\n    \"\"\"Helper 21.\"\"\"\n    return x * 21 + 1\n\ndef helper_22(x):\n    \"\"\"Helper 22.\"\"\"\n    return x * 22 + 1\n\ndef helper_23(x):\n    \"\"\"Helper 23.\"\"\"\n    return x * 23 + 1\n\ndef helper_24(x):\n    \"\"\"Helper 24.\"\"\"\n    return x * 24 + 1\n\ndef helper_25(x):\n    \"\"\"Helper 25.\"\"\"\n    return x * 25 + 1\n\ndef helper_26(x):\n    \"\"\"Helper 26.\"\"\"\n    return x * 26 + 1\n\ndef helper_27(x):\n    \"\"\"Helper 27.\"\"\"\n    return x * 27 + 1\n\ndef helper_28(x):\n    \"\"\"Helper 28.\"\"\"\n    return x * 28 + 1\n\ndef helper_29(x):\n    \"\"\"Helper 29.\"\"\"\n    return x * 29 + 1\n\ndef helper_30(x):\n    \"\"\"Helper 30.\"\"\"\n    return x * 30 + 1\n\ndef helper_31(x):\n    \"\"\"Helper 31.\"\"\"\n    return x * 31 + 1\n\ndef helper_32(x):\n    \"\"\"Helper 32.\"\"\"\n    return x * 32 + 1\n\ndef helper_33(x):\n    \"\"\"Helper 33.\"\"\"\n    return x * 33 + 1\n\ndef helper_34(x):\n    \"\"\"Helper 34.\"\"\"\n    return x * 34 + 1\n\ndef helper_35(x):\n    \"\"\"Helper 35.\"\"\"\n    return x * 35 + 1\n\ndef helper_36(x):\n    \"\"\"Helper 36.\"\"\"\n    return x * 36 + 1\n\ndef helper_37(x):\n    \"\"\"Helper 37.\"\"\"\n    return x * 37 + 1\n\ndef helper_38(x):\n    \"\"\"Helper 38.\"\"\"\n    return x * 38 + 1\n\ndef helper_39(x):\n    \"\"\"Helper 39.\"\"\"\n    return x * 39 + 1\n\ndef helper_40(x):\n    \"\"\"Helper 40.\"\"\"\n    return x * 40 + 1\n\ndef helper_41(x):\n    \"\"\"Helper 41.\"\"\"\n    return x * 41 + 1\n\ndef helper_42(x):\n    \"\"\"Helper 42.\"\"\"\n    return x * 42 + 1\n\ndef helper_43(x):\n    \"\"\"Helper 43.\"\"\"\n    return x * 43 + 1\n\ndef helper_44(x):\n    \"\"\"Helper 44.\"\"\"\n    return x * 44 + 1\n\ndef helper_45(x):\n    \"\"\"Helper 45.\"\"\"\n    return x * 45 + 1\n\ndef helper_46(x):\n    \"\"\"Helper 46.\"\"\"\n    return x * 46 + 1\n\ndef helper_47(x):\n    \"\"\"Helper 47.\"\"\"\n    return x * 47 + 1\n\ndef helper_48(x):\n    \"\"\"Helper 48.\"\"\"\n    return x * 48 + 1\n\ndef helper_49(x):\n    \"\"\"Helper 49.\"\"\"\n    return x * 49 + 1\n\ndef helper_50(x):\n    \"\"\"Helper 50.\"\"\"\n    return x * 50 + 1\n\ndef helper_51(x):\n    \"\"\"Helper 51.\"\"\"\n    return x * 51 + 1\n\ndef helper_52(x):\n    \"\"\"Helper 52.\"\"\"\n    return x * 52 + 1\n\ndef helper_53(x):\n    \"\"\"Helper 53.\"\"\"\n    return x * 53 + 1\n\ndef helper_54(x):\n    \"\"\"Helper 54.\"\"\"\n    return x * 54 + 1\n\ndef helper_55(x):\n    \"\"\"Helper 55.\"\"\"\n    return x * 55 + 1\n\ndef helper_56(x):\n    \"\"\"Helper 56.\"\"\"\n    return x * 56 + 1\n\ndef helper_57(x):\n    \"\"\"Helper 57.\"\"\"\n    return x * 57 + 1\n\ndef helper_58(x):\n    \"\"\"Helper 58.\"\"\"\n    return x * 58 + 1\n\ndef helper_59(x):\n    \"\"\"Helper 59.\"\"\"\n    return x * 59 + 1" + "\n\n\ndef compute_tax(amount, rate):\n    \"\"\"Tax owed; rate is a percentage like 20 for 20%.\"\"\"\n    return amount * rate\n"

CASES2 = [
    ("b2-dataclass-method", "feature",
     {"point.py": "from dataclasses import dataclass\n\n\n@dataclass\nclass Point:\n    x: int\n    y: int\n"},
     "add a to_dict method to the Point class in point.py",
     lambda: ok_if(py("from point import Point; assert Point(1, 2).to_dict() == {'x': 1, 'y': 2}; print('ok')"), contains="ok")),
    ("b2-js-add-export", "feature",
     {"strings.js": "function lower(s) {\n  return s.toLowerCase();\n}\n\nmodule.exports = { lower };\n"},
     "add a capitalize function to strings.js and export it",
     lambda: ok_if(node("const s=require('./strings.js'); if(s.capitalize('hello')!=='Hello'||s.lower('A')!=='a')process.exit(1); console.log('ok')"), contains="ok")),
    ("b2-cli-flag", "feature",
     {"hello.py": "print('Hello')\n"},
     "add a --name argument to hello.py so it prints Hello followed by that name",
     lambda: ok_if(sh(sys.executable, "hello.py", "--name", "Bo"), contains="Bo")),
    ("b2-divide-zero", "feature",
     {"mathx.py": "def divide(a, b):\n    return a / b\n"},
     "make divide in mathx.py return None when b is zero instead of crashing",
     lambda: ok_if(py("from mathx import divide; assert divide(6, 3) == 2 and divide(1, 0) is None; print('ok')"), contains="ok")),
    ("b2-sort-by-age", "feature",
     {"people.py": "def sort_people(people):\n    return sorted(people, key=lambda p: p['name'])\n"},
     "make sort_people in people.py sort by age instead of name",
     lambda: ok_if(py("from people import sort_people as s; r=s([{'name':'a','age':3},{'name':'b','age':1}]); assert [p['age'] for p in r]==[1,3]; print('ok')"), contains="ok")),
    ("b2-nameerror", "bugfix",
     {"report.py": "def total(xs):\n    totl = 0\n    for x in xs:\n        total_ = totl + x\n        totl = total_\n    return totl\n\n\nprint(totl)\n"},
     "running report.py fails with NameError: name 'totl' is not defined. fix it so it prints total([1, 2, 3])",
     lambda: ok_if(sh(sys.executable, "report.py"), contains="6")),
    ("b2-js-string-sum", "bugfix",
     {"sum.js": "function sum(a, b) {\n  return a + b;\n}\n\nmodule.exports = { sum };\n"},
     "sum in sum.js returns '12' for sum('1', '2') instead of 3. fix it so it adds them as numbers",
     lambda: ok_if(node("const {sum}=require('./sum.js'); if(sum('1','2')!==3||sum(2,2)!==4)process.exit(1); console.log('ok')"), contains="ok")),
    ("b2-failing-tests", "bugfix",
     {"text.py": "def is_palindrome(s):\n    return s == s[::-1]\n",
      "test_text.py": "import unittest\nfrom text import is_palindrome\n\n\nclass T(unittest.TestCase):\n    def test_simple(self):\n        self.assertTrue(is_palindrome('abba'))\n\n    def test_case(self):\n        self.assertTrue(is_palindrome('Abba'))\n\n\nif __name__ == '__main__':\n    unittest.main()\n"},
     "the tests in test_text.py fail. fix text.py",
     lambda: ok_if(sh(sys.executable, "-m", "unittest", "-q", "test_text")) + (["test edited"] if "is_palindrome('Abba')" not in read("test_text.py") else [])),
    ("b2-keyerror", "bugfix",
     {"store.py": "ITEMS = {'apple': 1}\n\n\ndef get_item(name):\n    return ITEMS[name]\n"},
     "get_item in store.py raises KeyError for missing items; make it return None instead",
     lambda: ok_if(py("from store import get_item; assert get_item('apple') == 1 and get_item('x') is None; print('ok')"), contains="ok")),
    ("b2-rename-class", "refactor",
     {"animals.py": "class Animal:\n    def speak(self):\n        return '...'\n",
      "dogs.py": "from animals import Animal\n\n\nclass Dog(Animal):\n    def speak(self):\n        return 'woof'\n\n\nprint(Dog().speak(), isinstance(Dog(), Animal))\n"},
     "rename the class Animal to Creature everywhere",
     lambda: ok_if(sh(sys.executable, "dogs.py"), contains="woof True") + (["Animal remains"] if "Animal" in read("animals.py") + read("dogs.py") else [])),
    ("b2-extract-validator", "refactor",
     {"app.py": "import re\n\n\ndef validate_email(s):\n    return re.match(r'[^@]+@[^@]+\\.[^@]+', s) is not None\n\n\nprint(validate_email('a@b.co'), validate_email('nope'))\n"},
     "move the validate_email function from app.py into validators.py",
     lambda: ok_if(sh(sys.executable, "app.py"), contains="True False") + (["not moved"] if "def validate_email" not in read("validators.py") or "def validate_email" in read("app.py") else [])),
    ("b2-js-rename", "refactor",
     {"users.js": "function getUser(id) {\n  return { id };\n}\n\nmodule.exports = { getUser };\n",
      "main.js": "const { getUser } = require('./users.js');\nconsole.log(getUser(7).id);\n"},
     "rename getUser to fetchUser in all files",
     lambda: ok_if(node("require('./main.js')"), contains="7") + (["getUser remains"] if "getUser" in read("users.js") + read("main.js") else [])),
    ("b2-write-stack-tests", "tests",
     {"stack.py": "class Stack:\n    def __init__(self):\n        self.items = []\n\n    def push(self, x):\n        self.items.append(x)\n\n    def pop(self):\n        return self.items.pop()\n\n    def is_empty(self):\n        return not self.items\n"},
     "write unittest tests for the Stack class in stack.py in a new file test_stack.py",
     lambda: (lambda r: ok_if(r) + ([] if re.search(r"Ran [2-9]\d* tests|Ran \d{2,} tests", r[1]) else [f"fewer than 2 tests: {r[1][-150:]}"]))(
         sh(sys.executable, "-m", "unittest", "-v", "test_stack"))),
    ("b2-node-hello", "scaffold",
     {},
     "create a node script hello.js that prints Hello, World!",
     lambda: ok_if(sh("node", "hello.js"), contains="Hello, World!")),
    ("b2-circle-class", "scaffold",
     {},
     "create a python module geometry.py with a Circle class that takes a radius and has an area method",
     lambda: ok_if(py("from geometry import Circle; import math; assert abs(Circle(2).area() - math.pi * 4) < 0.01; print('ok')"), contains="ok")),
    ("b2-version-const", "multi-file",
     {"version.py": "", "main.py": "print('app')\n"},
     "add a VERSION = '1.2.0' constant to version.py and make main.py print it",
     lambda: ok_if(sh(sys.executable, "main.py"), contains="1.2.0")),
    ("b2-large-file-bug", "bugfix",
     {"finance.py": FIN},
     "fix compute_tax in finance.py: rate is a percentage, so it should divide by 100",
     lambda: ok_if(py("from finance import compute_tax, helper_7; assert compute_tax(200, 20) == 40 and helper_7(1) == 8; print('ok')"), contains="ok")),
    ("b2-fix-and-commit", "git+code",
     {"messages.py": "WELCOME = 'You will recieve an email'\n", "__git__": ""},
     "fix the typo recieve in messages.py and commit with message 'fix typo'",
     lambda: (["typo remains"] if "recieve" in read("messages.py") or "receive" not in read("messages.py") else [])
     + ([] if sh("git", "log", "-1", "--format=%s")[1] == "fix typo" else ["not committed"])),
]

ASK_CASES2 = [
    ("b2-ask-login", "understanding",
     {"app.py": "def render_home():\n    return 'home'\n\n\ndef handle_signin(user, password):\n    return user == 'admin' and password == 'x'\n"},
     "which function in app.py handles logging in?",
     lambda a: [] if "handle_signin" in a else [f"answer: {a[:200]}"]),
    ("b2-ask-exports", "understanding",
     {"calc.js": "function add(a, b) { return a + b; }\nfunction secret() {}\nmodule.exports = { add };\n"},
     "what does calc.js export?",
     lambda a: [] if "add" in a and ("secret" not in a or "not" in a.lower()) else [f"answer: {a[:200]}"]),
]

CASES3 = [
    ("b3-validation", "feature",
     {"users.py": "def create_user(email):\n    return {'email': email}\n"},
     "add input validation to create_user in users.py: raise ValueError if the email has no @",
     lambda: ok_if(py("from users import create_user as c\nassert c('a@b')['email']=='a@b'\ntry:\n    c('nope'); raise SystemExit(1)\nexcept ValueError: print('ok')"), contains="ok")),
    ("b3-repr", "feature",
     {"books.py": "class Book:\n    def __init__(self, title, author):\n        self.title = title\n        self.author = author\n"},
     "add a __repr__ to the Book class in books.py that shows the title and author",
     lambda: ok_if(py("from books import Book; r=repr(Book('Dune','Herbert')); assert 'Dune' in r and 'Herbert' in r, r; print('ok')"), contains="ok")),
    ("b3-comprehension", "refactor",
     {"totals.py": "def squares(xs):\n    out = []\n    for x in xs:\n        out.append(x * x)\n    return out\n"},
     "convert the loop in squares in totals.py to a list comprehension",
     lambda: ok_if(py("from totals import squares; assert squares([1,2,3])==[1,4,9]; print('ok')"), contains="ok")
     + (["still a for-loop statement"] if "out.append" in read("totals.py") else [])),
    ("b3-cache", "feature",
     {"db.py": "CALLS = 0\n\n\ndef load():\n    global CALLS\n    CALLS += 1\n    return [1, 2, 3]\n\n\ndef fetch_all():\n    return load()\n"},
     "make fetch_all in db.py cache its result so calling it again doesn't call load() again",
     lambda: ok_if(py("import db; a=db.fetch_all(); b=db.fetch_all(); assert a==b==[1,2,3] and db.CALLS==1, db.CALLS; print('ok')"), contains="ok")),
    ("b3-median", "bugfix",
     {"stats.py": "def median(xs):\n    xs = sorted(xs)\n    return xs[len(xs) // 2]\n"},
     "median in stats.py gives the wrong result for even-length lists. fix it",
     lambda: ok_if(py("from stats import median; assert median([1,2,3,4])==2.5 and median([3,1,2])==2; print('ok')"), contains="ok")),
    ("b3-js-throw", "feature",
     {"rect.js": "function area(w, h) {\n  return w * h;\n}\n\nmodule.exports = { area };\n"},
     "make area in rect.js throw an error if the width or height is negative",
     lambda: ok_if(node("const {area}=require('./rect.js'); if(area(2,3)!==6)process.exit(1); try{area(-1,2);process.exit(1)}catch(e){console.log('ok')}"), contains="ok")),
    ("b3-rename-file-fix-import", "refactor",
     {"helpers.py": "def twice(x):\n    return 2 * x\n", "main.py": "from helpers import twice\n\nprint(twice(21))\n"},
     "rename helpers.py to utils.py and update the import in main.py",
     lambda: ok_if(sh(sys.executable, "main.py"), contains="42") + (["helpers.py still exists"] if os.path.exists("helpers.py") else [])),
    ("b3-package", "scaffold",
     {},
     "create a python package calc with an __init__.py and an ops.py module containing add and sub functions",
     lambda: ok_if(py("from calc.ops import add, sub; assert add(2,3)==5 and sub(5,3)==2; print('ok')"), contains="ok")),
    ("b3-readme", "docs",
     {"main.py": "print(sum([1, 2, 3]))\n"},
     "write a README.md for this project that explains what main.py does",
     lambda: [] if "main.py" in read("README.md") and len(read("README.md")) > 40 else [f"README: {read('README.md')[:200]!r}"]),
    ("b3-add-to-tests", "tests",
     {"calc.py": "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n",
      "test_calc.py": "import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n\n\nif __name__ == '__main__':\n    unittest.main()\n"},
     "add a test for multiply to test_calc.py",
     lambda: (lambda r: ok_if(r) + ([] if "Ran 2 tests" in r[1] else [f"expected 2 tests: {r[1][-120:]}"]))(sh(sys.executable, "-m", "unittest", "-v", "test_calc"))),
    ("b3-two-bugs", "bugfix",
     {"shop.py": "def subtotal(prices):\n    return sum(prices) - 1\n\n\ndef with_tax(amount):\n    return amount * 0.2\n",
      "test_shop.py": "import unittest\nfrom shop import subtotal, with_tax\n\n\nclass T(unittest.TestCase):\n    def test_subtotal(self):\n        self.assertEqual(subtotal([1, 2]), 3)\n\n    def test_tax(self):\n        self.assertAlmostEqual(with_tax(10), 12)\n\n\nif __name__ == '__main__':\n    unittest.main()\n"},
     "fix the failing tests in test_shop.py",
     lambda: ok_if(sh(sys.executable, "-m", "unittest", "-q", "test_shop")) + (["tests edited"] if "with_tax(10), 12" not in read("test_shop.py") else [])),
]

ASK_CASES3 = [
    ("b3-ask-where-setting", "understanding",
     {"settings.py": "TAX_RATE = 0.2\nCURRENCY = 'USD'\n", "shop.py": "from settings import TAX_RATE\n\n\ndef tax(x):\n    return x * TAX_RATE\n"},
     "where is the tax rate defined, and what is it?",
     lambda a: [] if "settings.py" in a and "0.2" in a else [f"answer: {a[:200]}"]),
]


def setup(files):
    for path, content in files.items():
        if path == "__git__":
            continue
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
    if "__git__" in files:
        for cmd in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@e.com"],
                    ["git", "config", "user.name", "T"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "init"]):
            subprocess.run(cmd, check=True, capture_output=True)


async def run_one(name, files, task, verify, ask=False):
    cwd = tempfile.mkdtemp(prefix=f"fmpcc-eval-{name}-")
    orig = os.getcwd()
    os.chdir(cwd)
    log, answer = [], ""
    start = time.monotonic()
    try:
        setup(files)
        app = m.ChatApp()
        app.subagent_roles["planning"] = "on-device"
        app.subagent_roles["building"] = "on-device"
        async with app.run_test():
            with mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
                 mock.patch.object(app, "_log_progress", side_effect=log.append), \
                 mock.patch.object(m, "notify"):
                if ask:
                    captured = []
                    with mock.patch.object(app, "_ask_answered", side_effect=captured.append), \
                         mock.patch.object(app, "_finish_turn", side_effect=lambda t, e, *a: captured.append(t or e or "")):
                        app._run_ask.__wrapped__(app, task)
                    answer = captured[-1] if captured else ""
                else:
                    app._run_task.__wrapped__(app, task)
        elapsed = time.monotonic() - start
        try:
            failures = verify(answer) if ask else verify()
        except Exception as e:
            failures = [f"verify raised {type(e).__name__}: {e}"]
    except Exception as e:
        elapsed = time.monotonic() - start
        failures = [f"harness raised {type(e).__name__}: {e}"]
    finally:
        os.chdir(orig)
        snapshot = {}
        for root, _d, fs in os.walk(cwd):
            if ".git" in root:
                continue
            for f in fs:
                p = os.path.join(root, f)
                if f in ("prog",) or f.endswith(".pyc"):
                    continue
                try:
                    snapshot[os.path.relpath(p, cwd)] = open(p).read()[:1500]
                except Exception:
                    pass
        shutil.rmtree(cwd, ignore_errors=True)
    return failures, log, answer, elapsed, snapshot


BIG10 = "def transform_0(record):\n    \"\"\"Normalize field 0.\"\"\"\n    value = record.get('f0', 0)\n    return {'f0': value * 0 + 1}\n\n\ndef transform_1(record):\n    \"\"\"Normalize field 1.\"\"\"\n    value = record.get('f1', 0)\n    return {'f1': value * 1 + 1}\n\n\ndef transform_2(record):\n    \"\"\"Normalize field 2.\"\"\"\n    value = record.get('f2', 0)\n    return {'f2': value * 2 + 1}\n\n\ndef transform_3(record):\n    \"\"\"Normalize field 3.\"\"\"\n    value = record.get('f3', 0)\n    return {'f3': value * 3 + 1}\n\n\ndef transform_4(record):\n    \"\"\"Normalize field 4.\"\"\"\n    value = record.get('f4', 0)\n    return {'f4': value * 4 + 1}\n\n\ndef transform_5(record):\n    \"\"\"Normalize field 5.\"\"\"\n    value = record.get('f5', 0)\n    return {'f5': value * 5 + 1}\n\n\ndef transform_6(record):\n    \"\"\"Normalize field 6.\"\"\"\n    value = record.get('f6', 0)\n    return {'f6': value * 6 + 1}\n\n\ndef transform_7(record):\n    \"\"\"Normalize field 7.\"\"\"\n    value = record.get('f7', 0)\n    return {'f7': value * 7 + 1}\n\n\ndef transform_8(record):\n    \"\"\"Normalize field 8.\"\"\"\n    value = record.get('f8', 0)\n    return {'f8': value * 8 + 1}\n\n\ndef transform_9(record):\n    \"\"\"Normalize field 9.\"\"\"\n    value = record.get('f9', 0)\n    return {'f9': value * 9 + 1}\n\n\ndef transform_10(record):\n    \"\"\"Normalize field 10.\"\"\"\n    value = record.get('f10', 0)\n    return {'f10': value * 10 + 1}\n\n\ndef transform_11(record):\n    \"\"\"Normalize field 11.\"\"\"\n    value = record.get('f11', 0)\n    return {'f11': value * 11 + 1}\n\n\ndef transform_12(record):\n    \"\"\"Normalize field 12.\"\"\"\n    value = record.get('f12', 0)\n    return {'f12': value * 12 + 1}\n\n\ndef transform_13(record):\n    \"\"\"Normalize field 13.\"\"\"\n    value = record.get('f13', 0)\n    return {'f13': value * 13 + 1}\n\n\ndef transform_14(record):\n    \"\"\"Normalize field 14.\"\"\"\n    value = record.get('f14', 0)\n    return {'f14': value * 14 + 1}\n\n\ndef transform_15(record):\n    \"\"\"Normalize field 15.\"\"\"\n    value = record.get('f15', 0)\n    return {'f15': value * 15 + 1}\n\n\ndef transform_16(record):\n    \"\"\"Normalize field 16.\"\"\"\n    value = record.get('f16', 0)\n    return {'f16': value * 16 + 1}\n\n\ndef transform_17(record):\n    \"\"\"Normalize field 17.\"\"\"\n    value = record.get('f17', 0)\n    return {'f17': value * 17 + 1}\n\n\ndef transform_18(record):\n    \"\"\"Normalize field 18.\"\"\"\n    value = record.get('f18', 0)\n    return {'f18': value * 18 + 1}\n\n\ndef transform_19(record):\n    \"\"\"Normalize field 19.\"\"\"\n    value = record.get('f19', 0)\n    return {'f19': value * 19 + 1}\n\n\ndef transform_20(record):\n    \"\"\"Normalize field 20.\"\"\"\n    value = record.get('f20', 0)\n    return {'f20': value * 20 + 1}\n\n\ndef transform_21(record):\n    \"\"\"Normalize field 21.\"\"\"\n    value = record.get('f21', 0)\n    return {'f21': value * 21 + 1}\n\n\ndef transform_22(record):\n    \"\"\"Normalize field 22.\"\"\"\n    value = record.get('f22', 0)\n    return {'f22': value * 22 + 1}\n\n\ndef transform_23(record):\n    \"\"\"Normalize field 23.\"\"\"\n    value = record.get('f23', 0)\n    return {'f23': value * 23 + 1}\n\n\ndef transform_24(record):\n    \"\"\"Normalize field 24.\"\"\"\n    value = record.get('f24', 0)\n    return {'f24': value * 24 + 1}\n\n\ndef transform_25(record):\n    \"\"\"Normalize field 25.\"\"\"\n    value = record.get('f25', 0)\n    return {'f25': value * 25 + 1}\n\n\ndef transform_26(record):\n    \"\"\"Normalize field 26.\"\"\"\n    value = record.get('f26', 0)\n    return {'f26': value * 26 + 1}\n\n\ndef transform_27(record):\n    \"\"\"Normalize field 27.\"\"\"\n    value = record.get('f27', 0)\n    return {'f27': value * 27 + 1}\n\n\ndef transform_28(record):\n    \"\"\"Normalize field 28.\"\"\"\n    value = record.get('f28', 0)\n    return {'f28': value * 28 + 1}\n\n\ndef transform_29(record):\n    \"\"\"Normalize field 29.\"\"\"\n    value = record.get('f29', 0)\n    return {'f29': value * 29 + 1}\n\n\ndef transform_30(record):\n    \"\"\"Normalize field 30.\"\"\"\n    value = record.get('f30', 0)\n    return {'f30': value * 30 + 1}\n\n\ndef transform_31(record):\n    \"\"\"Normalize field 31.\"\"\"\n    value = record.get('f31', 0)\n    return {'f31': value * 31 + 1}\n\n\ndef transform_32(record):\n    \"\"\"Normalize field 32.\"\"\"\n    value = record.get('f32', 0)\n    return {'f32': value * 32 + 1}\n\n\ndef transform_33(record):\n    \"\"\"Normalize field 33.\"\"\"\n    value = record.get('f33', 0)\n    return {'f33': value * 33 + 1}\n\n\ndef transform_34(record):\n    \"\"\"Normalize field 34.\"\"\"\n    value = record.get('f34', 0)\n    return {'f34': value * 34 + 1}\n\n\ndef transform_35(record):\n    \"\"\"Normalize field 35.\"\"\"\n    value = record.get('f35', 0)\n    return {'f35': value * 35 + 1}\n\n\ndef transform_36(record):\n    \"\"\"Normalize field 36.\"\"\"\n    value = record.get('f36', 0)\n    return {'f36': value * 36 + 1}\n\n\ndef transform_37(record):\n    \"\"\"Normalize field 37.\"\"\"\n    value = record.get('f37', 0)\n    return {'f37': value * 37 + 1}\n\n\ndef transform_38(record):\n    \"\"\"Normalize field 38.\"\"\"\n    value = record.get('f38', 0)\n    return {'f38': value * 38 + 1}\n\n\ndef transform_39(record):\n    \"\"\"Normalize field 39.\"\"\"\n    value = record.get('f39', 0)\n    return {'f39': value * 39 + 1}\n\n\ndef transform_40(record):\n    \"\"\"Normalize field 40.\"\"\"\n    value = record.get('f40', 0)\n    return {'f40': value * 40 + 1}\n\n\ndef transform_41(record):\n    \"\"\"Normalize field 41.\"\"\"\n    value = record.get('f41', 0)\n    return {'f41': value * 41 + 1}\n\n\ndef transform_42(record):\n    \"\"\"Normalize field 42.\"\"\"\n    value = record.get('f42', 0)\n    return {'f42': value * 42 + 1}\n\n\ndef transform_43(record):\n    \"\"\"Normalize field 43.\"\"\"\n    value = record.get('f43', 0)\n    return {'f43': value * 43 + 1}\n\n\ndef transform_44(record):\n    \"\"\"Normalize field 44.\"\"\"\n    value = record.get('f44', 0)\n    return {'f44': value * 44 + 1}\n\n\ndef transform_45(record):\n    \"\"\"Normalize field 45.\"\"\"\n    value = record.get('f45', 0)\n    return {'f45': value * 45 + 1}\n\n\ndef transform_46(record):\n    \"\"\"Normalize field 46.\"\"\"\n    value = record.get('f46', 0)\n    return {'f46': value * 46 + 1}\n\n\ndef transform_47(record):\n    \"\"\"Normalize field 47.\"\"\"\n    value = record.get('f47', 0)\n    return {'f47': value * 47 + 1}\n\n\ndef transform_48(record):\n    \"\"\"Normalize field 48.\"\"\"\n    value = record.get('f48', 0)\n    return {'f48': value * 48 + 1}\n\n\ndef transform_49(record):\n    \"\"\"Normalize field 49.\"\"\"\n    value = record.get('f49', 0)\n    return {'f49': value * 49 + 1}\n\n\ndef transform_50(record):\n    \"\"\"Normalize field 50.\"\"\"\n    value = record.get('f50', 0)\n    return {'f50': value * 50 + 1}\n\n\ndef transform_51(record):\n    \"\"\"Normalize field 51.\"\"\"\n    value = record.get('f51', 0)\n    return {'f51': value * 51 + 1}\n\n\ndef transform_52(record):\n    \"\"\"Normalize field 52.\"\"\"\n    value = record.get('f52', 0)\n    return {'f52': value * 52 + 1}\n\n\ndef transform_53(record):\n    \"\"\"Normalize field 53.\"\"\"\n    value = record.get('f53', 0)\n    return {'f53': value * 53 + 1}\n\n\ndef transform_54(record):\n    \"\"\"Normalize field 54.\"\"\"\n    value = record.get('f54', 0)\n    return {'f54': value * 54 + 1}\n\n\ndef transform_55(record):\n    \"\"\"Normalize field 55.\"\"\"\n    value = record.get('f55', 0)\n    return {'f55': value * 55 + 1}\n\n\ndef transform_56(record):\n    \"\"\"Normalize field 56.\"\"\"\n    value = record.get('f56', 0)\n    return {'f56': value * 56 + 1}\n\n\ndef transform_57(record):\n    \"\"\"Normalize field 57.\"\"\"\n    value = record.get('f57', 0)\n    return {'f57': value * 57 + 1}\n\n\ndef transform_58(record):\n    \"\"\"Normalize field 58.\"\"\"\n    value = record.get('f58', 0)\n    return {'f58': value * 58 + 1}\n\n\ndef transform_59(record):\n    \"\"\"Normalize field 59.\"\"\"\n    value = record.get('f59', 0)\n    return {'f59': value * 59 + 1}\n\n\ndef transform_60(record):\n    \"\"\"Normalize field 60.\"\"\"\n    value = record.get('f60', 0)\n    return {'f60': value * 60 + 1}\n\n\ndef transform_61(record):\n    \"\"\"Normalize field 61.\"\"\"\n    value = record.get('f61', 0)\n    return {'f61': value * 61 + 1}\n\n\ndef transform_62(record):\n    \"\"\"Normalize field 62.\"\"\"\n    value = record.get('f62', 0)\n    return {'f62': value * 62 + 1}\n\n\ndef transform_63(record):\n    \"\"\"Normalize field 63.\"\"\"\n    value = record.get('f63', 0)\n    return {'f63': value * 63 + 1}\n\n\ndef transform_64(record):\n    \"\"\"Normalize field 64.\"\"\"\n    value = record.get('f64', 0)\n    return {'f64': value * 64 + 1}\n\n\ndef transform_65(record):\n    \"\"\"Normalize field 65.\"\"\"\n    value = record.get('f65', 0)\n    return {'f65': value * 65 + 1}\n\n\ndef transform_66(record):\n    \"\"\"Normalize field 66.\"\"\"\n    value = record.get('f66', 0)\n    return {'f66': value * 66 + 1}\n\n\ndef transform_67(record):\n    \"\"\"Normalize field 67.\"\"\"\n    value = record.get('f67', 0)\n    return {'f67': value * 67 + 1}\n\n\ndef transform_68(record):\n    \"\"\"Normalize field 68.\"\"\"\n    value = record.get('f68', 0)\n    return {'f68': value * 68 + 1}\n\n\ndef transform_69(record):\n    \"\"\"Normalize field 69.\"\"\"\n    value = record.get('f69', 0)\n    return {'f69': value * 69 + 1}\n\n\ndef transform_70(record):\n    \"\"\"Normalize field 70.\"\"\"\n    value = record.get('f70', 0)\n    return {'f70': value * 70 + 1}\n\n\ndef transform_71(record):\n    \"\"\"Normalize field 71.\"\"\"\n    value = record.get('f71', 0)\n    return {'f71': value * 71 + 1}\n\n\ndef transform_72(record):\n    \"\"\"Normalize field 72.\"\"\"\n    value = record.get('f72', 0)\n    return {'f72': value * 72 + 1}\n\n\ndef transform_73(record):\n    \"\"\"Normalize field 73.\"\"\"\n    value = record.get('f73', 0)\n    return {'f73': value * 73 + 1}\n\n\ndef transform_74(record):\n    \"\"\"Normalize field 74.\"\"\"\n    value = record.get('f74', 0)\n    return {'f74': value * 74 + 1}\n\n\ndef transform_75(record):\n    \"\"\"Normalize field 75.\"\"\"\n    value = record.get('f75', 0)\n    return {'f75': value * 75 + 1}\n\n\ndef transform_76(record):\n    \"\"\"Normalize field 76.\"\"\"\n    value = record.get('f76', 0)\n    return {'f76': value * 76 + 1}\n\n\ndef transform_77(record):\n    \"\"\"Normalize field 77.\"\"\"\n    value = record.get('f77', 0)\n    return {'f77': value * 77 + 1}\n\n\ndef transform_78(record):\n    \"\"\"Normalize field 78.\"\"\"\n    value = record.get('f78', 0)\n    return {'f78': value * 78 + 1}\n\n\ndef transform_79(record):\n    \"\"\"Normalize field 79.\"\"\"\n    value = record.get('f79', 0)\n    return {'f79': value * 79 + 1}" + "\n\n\ndef merge_records(a, b):\n    \"\"\"Combine two records; values in b win.\"\"\"\n    out = dict(a)\n    out.update(b)\n    return out\n"

APP_FILES = {
    "inventory/__init__.py": "",
    "inventory/models.py": "from dataclasses import dataclass\n\n\n@dataclass\nclass Item:\n    name: str\n    price: float\n    quantity: int\n",
    "inventory/store.py": "from inventory.models import Item\n\n\nclass Store:\n    def __init__(self):\n        self.items = []\n\n    def add(self, name, price, quantity):\n        self.items.append(Item(name, price, quantity))\n\n    def total_value(self):\n        return sum(i.price for i in self.items)\n",
    "inventory/report.py": "def format_report(store):\n    lines = [f\"{i.name}: {i.quantity} x ${i.price:.2f}\" for i in store.items]\n    lines.append(f\"Total: ${store.total_value():.2f}\")\n    return \"\\n\".join(lines)\n",
    "main.py": "from inventory.store import Store\nfrom inventory.report import format_report\n\nstore = Store()\nstore.add('apple', 0.5, 10)\nstore.add('pear', 1.25, 4)\nprint(format_report(store))\n",
    "tests/test_store.py": "import unittest\nfrom inventory.store import Store\n\n\nclass T(unittest.TestCase):\n    def test_total_value(self):\n        s = Store()\n        s.add('apple', 0.5, 10)\n        s.add('pear', 1.25, 4)\n        self.assertEqual(s.total_value(), 10.0)\n",
}

CASES4 = [
    ("b4-app-bug-from-tests", "bugfix", dict(APP_FILES),
     "the total in the inventory report is wrong and tests/test_store.py fails. fix it",
     lambda: ok_if(sh(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"))
     + ok_if(sh(sys.executable, "main.py"), contains="Total: $10.00")),
    ("b4-app-feature-across-files", "feature", dict(APP_FILES),
     "add a remove(name) method to Store in inventory/store.py and use it in main.py to remove the pear before printing the report",
     lambda: ok_if(sh(sys.executable, "main.py"), contains="apple") + (["pear still in report"] if "pear" in sh(sys.executable, "main.py")[1] else [])),
    ("b4-app-new-field", "feature", dict(APP_FILES),
     "add a low_stock(threshold) method to Store that returns the names of items with quantity below threshold",
     lambda: ok_if(py("from inventory.store import Store\ns=Store(); s.add('a',1,2); s.add('b',1,9)\nassert s.low_stock(5)==['a'], s.low_stock(5); print('ok')"), contains="ok")),
    ("b4-go-fix", "bugfix",
     {"go.mod": "module example.com/calc\n\ngo 1.21\n",
      "calc.go": "package calc\n\n// Max returns the larger of a and b.\nfunc Max(a, b int) int {\n\tif a < b {\n\t\treturn a\n\t}\n\treturn b\n}\n",
      "calc_test.go": "package calc\n\nimport \"testing\"\n\nfunc TestMax(t *testing.T) {\n\tif Max(2, 5) != 5 || Max(7, 1) != 7 {\n\t\tt.Fatal(\"wrong max\")\n\t}\n}\n"},
     "Max in calc.go returns the wrong value, go test fails. fix it",
     lambda: ok_if(sh("go", "test", "./...", timeout=120))),
    ("b4-node-npm-test", "bugfix",
     {"package.json": "{\n  \"name\": \"t\",\n  \"version\": \"1.0.0\",\n  \"scripts\": {\"test\": \"node --test\"}\n}\n",
      "slug.js": "function slug(s) {\n  return s.toLowerCase().replace(' ', '-');\n}\nmodule.exports = { slug };\n",
      "slug.test.js": "const test = require('node:test');\nconst assert = require('node:assert');\nconst { slug } = require('./slug.js');\ntest('multiple spaces', () => {\n  assert.strictEqual(slug('Hello Big World'), 'hello-big-world');\n});\n"},
     "npm test fails for slug.js. fix slug so every space becomes a dash",
     lambda: ok_if(sh("npm", "test", "--silent", timeout=120))),
    ("b4-performance", "feature",
     {"fib.py": "def fib(n):\n    if n < 2:\n        return n\n    return fib(n - 1) + fib(n - 2)\n"},
     "fib in fib.py is far too slow for fib(80). make it fast",
     lambda: ok_if(sh(sys.executable, "-c", "from fib import fib; assert fib(80) == 23416728348467685 and fib(10) == 55; print('ok')", timeout=10), contains="ok")),
    ("b4-stack-trace", "bugfix",
     {"orders.py": "def order_total(order):\n    return sum(line['price'] * line['qty'] for line in order['lines'])\n\n\nif __name__ == '__main__':\n    print(order_total({'lines': [{'price': 2, 'quantity': 3}]}))\n"},
     "running orders.py gives:\nTraceback (most recent call last):\n  File \"orders.py\", line 6, in <module>\n    print(order_total({'lines': [{'price': 2, 'quantity': 3}]}))\n  File \"orders.py\", line 2, in order_total\nKeyError: 'qty'\nthe data uses 'quantity'. fix orders.py",
     lambda: ok_if(sh(sys.executable, "orders.py"), contains="6")),
    ("b4-csv-script", "scaffold",
     {"data.csv": "name,price\napple,1.50\npear,2.50\nplum,2.00\n"},
     "create average_price.py that reads data.csv and prints the average of the price column",
     lambda: ok_if(sh(sys.executable, "average_price.py"), contains="2.0")),
    ("b4-feature-then-tests", "tests",
     {"temps.py": "def c_to_f(c):\n    return c * 9 / 5 + 32\n"},
     "add an f_to_c function to temps.py, then write test_temps.py with unittest tests for both functions",
     lambda: ok_if(py("from temps import f_to_c, c_to_f; assert f_to_c(212)==100 and c_to_f(0)==32; print('ok')"), contains="ok")
     + (lambda r: ok_if(r) + ([] if re.search(r"Ran [2-9]", r[1]) else [f"<2 tests: {r[1][-100:]}"]))(sh(sys.executable, "-m", "unittest", "-v", "test_temps"))),
    ("b4-big-file-new-function", "feature",
     {"records.py": BIG10},
     "change merge_records in records.py so values in a win over values in b",
     lambda: ok_if(py("from records import merge_records, transform_3; assert merge_records({'x':1},{'x':2})=={'x':1}; assert transform_3({'f3':2})=={'f3':7}; print('ok')"), contains="ok")),
    ("b4-class-refactor", "refactor",
     {"shapes.py": "def area(kind, a, b=0):\n    if kind == 'square':\n        return a * a\n    if kind == 'rect':\n        return a * b\n    raise ValueError(kind)\n"},
     "refactor shapes.py into a Square class and a Rect class, each with an area() method",
     lambda: ok_if(py("from shapes import Square, Rect; assert Square(3).area()==9 and Rect(2,5).area()==10; print('ok')"), contains="ok")),
    ("b4-parse-bug", "bugfix",
     {"duration.py": "def parse_duration(s):\n    \"\"\"'1h30m' -> 90, '45m' -> 45, '2h' -> 120 (minutes).\"\"\"\n    hours, minutes = s.split('h')\n    return int(hours) * 60 + int(minutes.rstrip('m'))\n"},
     "parse_duration in duration.py crashes on '45m' and '2h'. fix it so all three documented formats work",
     lambda: ok_if(py("from duration import parse_duration as p; assert (p('1h30m'), p('45m'), p('2h')) == (90, 45, 120); print('ok')"), contains="ok")),
    ("b4-feature-commit-git", "git+code",
     {"calc.py": "def add(a, b):\n    return a + b\n",
      "test_calc.py": "import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n", "__git__": ""},
     "add a divide function to calc.py that raises ValueError on division by zero, add a test for it to test_calc.py, and commit with the message 'add divide'",
     lambda: ok_if(py("from calc import divide\nassert divide(6,3)==2\ntry:\n    divide(1,0); raise SystemExit(1)\nexcept ValueError: print('ok')"), contains="ok")
     + ok_if(sh(sys.executable, "-m", "unittest", "-q", "test_calc"))
     + ([] if sh("git", "log", "-1", "--format=%s")[1] == "add divide" else ["not committed"])),
]

ASK_CASES4 = [
    ("b4-ask-flow", "understanding", dict(APP_FILES),
     "how does main.py produce the report? which functions does it go through?",
     lambda a: [] if "format_report" in a and ("add" in a or "Store" in a) else [f"answer: {a[:200]}"]),
    ("b4-ask-bug", "understanding", dict(APP_FILES),
     "why is the total in the report wrong?",
     lambda a: [] if "quantity" in a.lower() else [f"answer: {a[:250]}"]),
]


KNOWN_LIMITATIONS = {
    # Syntactically valid code that runs without crashing but is logically
    # wrong (never fills the cache). The smoke run catches the first
    # attempt's crash and the repair fixes it, but with no tests and no
    # known expected values nothing can check the behavior itself.
    "b3-cache",
    # A duration parser ("1h30m" -> 90) the model can't write: 0/4 fixing
    # its own attempt with the failing example shown, 0/4 explaining the
    # bug first, 0/4 writing it fresh from the three examples. fm-pcc's
    # part works: the docstring examples catch the wrong result and /task
    # stops without committing it.
    "b4-parse-bug",
}


def on_device_available() -> bool:
    try:
        r = subprocess.run(["fm", "available", "--model", "system"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


async def main() -> int:
    if not on_device_available():
        print("SKIPPED: on-device model isn't available here")
        return 0
    filters = sys.argv[1:]
    all_cases = CASES + CASES2 + CASES3 + CASES4
    all_asks = ASK_CASES + ASK_CASES2 + ASK_CASES3 + ASK_CASES4
    results = []
    for kind, cases in (("task", all_cases), ("ask", all_asks)):
        for name, cat, files, task, verify in cases:
            if filters and not any(f in name or f == cat for f in filters):
                continue
            failures, log, answer, elapsed, _snap = await run_one(name, files, task, verify, ask=kind == "ask")
            known = name in KNOWN_LIMITATIONS
            status = "ok  " if not failures else ("KNOWN" if known else "FAIL")
            print(f"{status} {name} ({elapsed:.0f}s): {task}", flush=True)
            if failures:
                for f in failures:
                    print(f"       - {f[-400:]}")
                if not known:
                    for line in log:
                        print(f"       | {line[:400]}")
                    if answer:
                        print(f"       answer: {answer[:300]}")
            results.append((name, not failures, known))
    passed = sum(ok for _n, ok, _k in results)
    hard_fails = [n for n, ok, known in results if not ok and not known]
    print(f"\n{passed}/{len(results)} cases passed"
          + (f" ({len(results) - passed - len(hard_fails)} known limitation(s))" if len(results) - passed - len(hard_fails) else ""))
    return 1 if hard_fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
