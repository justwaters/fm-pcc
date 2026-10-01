"""Changes that have to be threaded through several files, on the real
on-device model: a new field carried from the model to the storage to the
output, a parameter passed down through layers, a return type changed and
every caller updated, a feature spanning three modules. Each only works if
the files agree with each other, and each is judged by running the result.

These were written before any work on cross-file changes, as the honest
measure for it: 0/12 in both baseline runs, 9/12 in both final runs (7-9
while tuning). The three misses are the model's own code -- an email check
rejecting a@b.co, nested JS template strings that don't parse, a Swift
protocol requirement its types can't meet -- so the bar is MIN_PASS, a
floor that catches a collapse. Run like the agentic eval (both /task
roles on-device).

Run: tests/run.sh slow   (or directly, optionally with case-name filters:
     uv run --with textual --with rich python3 tests/slow/test_crossfile_real.py swift-)
"""
import asyncio
import importlib.util
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("agentic_eval", os.path.join(HERE, "test_agentic_eval.py"))
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)
sh, py, node, read, ok_if, run_one = ev.sh, ev.py, ev.node, ev.read, ev.ok_if, ev.run_one

MIN_PASS = 7

INVENTORY = dict(ev.APP_FILES)

USERS = {
    "models.py": "from dataclasses import dataclass\n\n\n@dataclass\nclass User:\n    id: int\n    name: str\n    email: str\n",
    "users.py": "USERS = [\n    {'id': 1, 'name': 'Ada', 'email': 'ada@example.com'},\n    {'id': 2, 'name': 'Bo', 'email': 'bo@example.com'},\n]\n\n\n"
                "def find_user(user_id):\n    for u in USERS:\n        if u['id'] == user_id:\n            return u\n    return None\n",
    "app.py": "from users import find_user\n\n\ndef greeting(user_id):\n    user = find_user(user_id)\n    if user is None:\n        return 'Hello, stranger'\n"
              "    return f\"Hello, {user['name']} <{user['email']}>\"\n\n\nif __name__ == '__main__':\n    print(greeting(1))\n    print(greeting(3))\n",
}

LAYERS = {
    "transport.py": "CALLS = []\n\n\ndef http_get(url):\n    CALLS.append({'url': url})\n    return '{\"name\": \"Ada\"}'\n",
    "client.py": "import json\nfrom transport import http_get\n\n\ndef fetch_json(url):\n    return json.loads(http_get(url))\n",
    "service.py": "from client import fetch_json\n\nBASE = 'https://api.example.com'\n\n\ndef get_user(user_id):\n    return fetch_json(f'{BASE}/users/{user_id}')\n",
}

SIGNUP = {
    "db.py": "SAVED = []\n\n\ndef save_user(name, email):\n    SAVED.append((name, email))\n    return len(SAVED)\n",
    "signup.py": "from db import save_user\n\n\ndef signup(name, email):\n    return save_user(name, email)\n",
}

TODO_JS = {
    "todo.js": "function createTodo(title) {\n  return { title, done: false };\n}\n\nmodule.exports = { createTodo };\n",
    "format.js": "function formatTodo(todo) {\n  return `${todo.done ? '[x]' : '[ ]'} ${todo.title}`;\n}\n\nmodule.exports = { formatTodo };\n",
    "index.js": "const { createTodo } = require('./todo');\nconst { formatTodo } = require('./format');\n\n"
                "const todos = [createTodo('Buy milk'), createTodo('Call Bo')];\nfor (const t of todos) console.log(formatTodo(t));\n",
}

MATH_JS = {
    "math.js": "function sum(xs) {\n  return xs.reduce((a, b) => a + b, 0);\n}\n\nmodule.exports = { sum };\n",
    "stats.js": "const { sum } = require('./math');\n\nfunction mean(xs) {\n  return xs.length ? sum(xs) / xs.length : 0;\n}\n\nmodule.exports = { mean };\n",
    "index.js": "const { sum } = require('./math');\nconst { mean } = require('./stats');\n\nconsole.log(sum([1, 2, 3]), mean([2, 4]));\n",
}

LOGGER_JS = {
    "config.js": "module.exports = { appName: 'demo' };\n",
    "logger.js": "function info(msg) {\n  console.log(`INFO ${msg}`);\n}\n\nmodule.exports = { info };\n",
    "app.js": "const { info } = require('./logger');\n\ninfo('starting');\n",
}

TASKS_SWIFT = {
    "Models.swift": "struct TodoItem {\n    let title: String\n}\n",
    "Store.swift": "final class TodoStore {\n    private(set) var items: [TodoItem] = []\n\n"
                   "    func add(title: String) {\n        items.append(TodoItem(title: title))\n    }\n}\n",
    "main.swift": "let store = TodoStore()\nstore.add(title: \"Write\")\nstore.add(title: \"Read\")\nfor item in store.items {\n    print(item.title)\n}\n",
}

SHAPES_SWIFT = {
    "Circle.swift": "struct Circle {\n    let radius: Double\n}\n",
    "Square.swift": "struct Square {\n    let side: Double\n}\n",
    "main.swift": "let circle = Circle(radius: 1)\nlet square = Square(side: 2)\nprint(\"shapes ready\")\n",
}


def swift_run(expect: str):
    def check():
        rc, out = sh("swiftc", *sorted(f for f in os.listdir(".") if f.endswith(".swift")), "-o", "prog", timeout=240)
        if rc != 0:
            return [f"doesn't compile: {out[-400:]}"]
        return ok_if(sh("./prog"), contains=expect)
    return check


CASES = [
    # (name, files, task, verify)
    ("py-thread-field", INVENTORY,
     "add a category to inventory items: Item gets a category field, Store.add takes it, the report shows it after "
     "the name like 'apple (fruit): 10 x $0.50', and main.py adds both items as fruit",
     lambda: ok_if(sh(sys.executable, "main.py"), contains="apple (fruit): 10 x $0.50")
     + ok_if(sh(sys.executable, "main.py"), contains="pear (fruit)")),
    ("py-thread-param", LAYERS,
     "let callers of get_user in service.py pass a timeout in seconds, and pass it all the way down through "
     "fetch_json in client.py to http_get in transport.py, which should record it in CALLS",
     lambda: ok_if(py("import service, transport; service.get_user(7, timeout=3); c = transport.CALLS[-1]; "
                      "assert c.get('timeout') == 3, c; assert c['url'].endswith('/users/7'); print('ok')"), contains="ok")),
    ("py-rename-method", INVENTORY,
     "rename Store.total_value to total_cost everywhere, including the report and the tests",
     lambda: ok_if(sh(sys.executable, "main.py"), contains="Total: $")
     # (the inventory's own test fails before any change: total_value has a
     # deliberate bug the agentic eval asks to fix)
     + ok_if(py("from inventory.store import Store; s = Store(); s.add('a', 2.0, 1); "
                "assert s.total_cost() == 2.0; assert not hasattr(s, 'total_value'); print('ok')"), contains="ok")
     + (["tests/test_store.py doesn't use total_cost"] if "total_cost" not in read("tests/test_store.py") else [])
     + (["total_value still used"] if any(re.search(r"\btotal_value\b", read(p)) for p in
                                          ("inventory/store.py", "inventory/report.py", "tests/test_store.py")) else [])),
    ("py-extract-module", INVENTORY,
     "move the formatting of a single report line into a new function format_line(item) in a new module "
     "inventory/formatting.py, and have format_report use it",
     lambda: ok_if(py("from inventory.formatting import format_line\nfrom inventory.models import Item\n"
                      "assert format_line(Item('fig', 2.0, 3)) == 'fig: 3 x $2.00', format_line(Item('fig', 2.0, 3)); print('ok')"),
                   contains="ok")
     + ok_if(sh(sys.executable, "main.py"), contains="apple: 10 x $0.50")
     + (["report.py doesn't use format_line"] if "format_line" not in read("inventory/report.py") else [])),
    ("py-feature-3files", INVENTORY,
     "add a discount: Store gets an apply_discount(percent) method that lowers every item's price by that percent, "
     "the report adds a line 'Discount: 10%' when one was applied, and main.py applies 10% before printing",
     lambda: ok_if(sh(sys.executable, "main.py"), contains="Discount: 10%")
     + ok_if(sh(sys.executable, "main.py"), contains="apple: 10 x $0.45")),
    ("py-return-type", USERS,
     "change find_user in users.py to return a User from models.py instead of a dict, and update app.py to match",
     lambda: ok_if(sh(sys.executable, "app.py"), contains="Hello, Ada <ada@example.com>")
     + ok_if(py("from users import find_user; from models import User; u = find_user(2); "
                "assert isinstance(u, User) and u.name == 'Bo', u; assert find_user(9) is None; print('ok')"), contains="ok")),
    ("py-validation-layer", SIGNUP,
     "add a validators.py with validate_email(email) that raises ValueError unless the email has an @ and a dot "
     "after it, and make signup call it before saving",
     lambda: ok_if(py("import signup, db\ntry:\n    signup.signup('x', 'nope')\n    print('no error')\nexcept ValueError:\n"
                      "    pass\nassert db.SAVED == [], db.SAVED\nassert signup.signup('a', 'a@b.co') == 1\nprint('ok')"),
                   contains="ok")),
    ("js-thread-field", TODO_JS,
     "give todos a priority: createTodo takes an optional priority that defaults to 'normal', formatTodo shows it "
     "as '[high] ' before the title when it isn't normal, and index.js makes 'Call Bo' high priority",
     lambda: ok_if(node("require('./index.js')"), contains="[high] Call Bo")
     + ok_if(node("require('./index.js')"), contains="[ ] Buy milk")),
    ("js-rename-export", MATH_JS,
     "rename sum in math.js to total, and update everything that uses it",
     lambda: ok_if(node("const m=require('./math.js'), s=require('./stats.js'); require('./index.js'); "
                        "if(m.total([1,2,3])!==6||m.sum||s.mean([2,4])!==3)process.exit(1); console.log('ok')"),
                   contains="ok")),
    ("js-config-flag", LOGGER_JS,
     "add a verbose setting to config.js that defaults to false, add a debug(msg) function to logger.js that only "
     "prints 'DEBUG msg' when config.verbose is true, and call debug('loaded config') in app.js",
     lambda: ok_if(node("require('./app.js')"), contains="INFO starting")
     + (["debug printed while verbose is false"] if "DEBUG" in node("require('./app.js')")[1] else [])
     + ok_if(node("const c=require('./config.js'); c.verbose=true; require('./app.js')"), contains="DEBUG loaded config")),
    ("swift-thread-field", TASKS_SWIFT,
     "add a done flag to TodoItem that starts false, a markDone(title:) method on TodoStore, and make main.swift "
     "mark Write as done and print each item with a checkmark ✓ before done ones",
     swift_run("✓ Write")),
    ("swift-protocol", SHAPES_SWIFT,
     "add a Shape protocol with an area() method returning Double, make Circle and Square conform to it, and make "
     "main.swift print the total area of the circle and the square formatted to 2 decimal places",
     swift_run("7.14")),
]


async def main() -> int:
    if not ev.on_device_available():
        print("SKIPPED: on-device model isn't available here")
        return 0
    filters = sys.argv[1:]
    cases = [c for c in CASES if not filters or any(f in c[0] for f in filters)]
    failed = []
    for name, files, task, verify in cases:
        if name.startswith("swift-") and not shutil.which("swiftc"):
            print(f"skip {name}: no swiftc")
            continue
        start = time.monotonic()
        failures, log, _answer, _elapsed, snapshot = await run_one(name, dict(files), task, verify)
        print(f"{'ok  ' if not failures else 'FAIL'} {name} ({time.monotonic() - start:.0f}s)", flush=True)
        if failures:
            failed.append(name)
            for f in failures[:3]:
                print(f"       - {f[:300]}")
            for line in log[-4:]:
                print(f"       | {str(line)[:200]}")
    passed = len(cases) - len(failed)
    need = MIN_PASS if not filters else 0
    print(f"\n{passed}/{len(cases)} passed (need {need})")
    return 1 if passed < need else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
