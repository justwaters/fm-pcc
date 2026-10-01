"""Building small projects from one sentence, on the real on-device model:
an empty folder (or one with just a data file), a request like "build a
command-line to-do app in Python ... saved in todos.json", and a judge
that runs the result -- the files have to work together, not just exist.

Written before any work on building whole projects, as the honest measure
for it: 2/8 in both baseline runs (named files never created, two files
written into one, main.py put inside the package, a test file with no
tests counted as passing, planning overflowing the window), 4/8 in the
three runs after. The rest are the model's own code -- a circle's area
without pi, results written to a file instead of printed -- or bugs the
checks catch (`python todo.py list` printing nothing) that the fix rounds
can't repair, where /task stops instead of reporting success. MIN_PASS is
a floor. Run like the agentic eval (both /task roles on-device).

Run: tests/run.sh slow   (or directly, optionally with case-name filters:
     uv run --with textual --with rich python3 tests/slow/test_build_real.py todo)
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

MIN_PASS = 3


def has(text, *words):
    return all(re.search(w, text, re.I) for w in words)


def check_todo():
    out = []
    for args in (["add", "buy milk"], ["add", "call Bo"]):
        rc, o = sh(sys.executable, "todo.py", *args)
        if rc:
            return [f"todo.py {' '.join(args)} failed: {o[-200:]}"]
    rc, listed = sh(sys.executable, "todo.py", "list")
    if rc or not (has(listed, "buy milk", "call Bo") and listed.find("buy milk") < listed.find("call Bo")):
        out.append(f"list after two adds: {listed[-200:]!r}")
    rc, o = sh(sys.executable, "todo.py", "done", "1")
    rc2, listed = sh(sys.executable, "todo.py", "list")
    if rc or "buy milk" in listed or "call Bo" not in listed:
        out.append(f"list after done 1: {listed[-200:]!r}")
    if not os.path.exists("todos.json"):
        out.append("no todos.json")
    return out


def check_wordfreq():
    open("sample.txt", "w").write("The cat. the dog! THE bird, a cat; a cat?\n")
    rc, o = sh(sys.executable, "wordfreq.py", "sample.txt")
    rows = [l.replace(":", " ").split() for l in o.strip().splitlines() if l.strip()]
    top = [(r[0].lower(), r[-1]) for r in rows[:3] if len(r) >= 2]
    out = []
    # "cat" and "the" tie at 3 (either order), then "a" at 2
    if rc or len(top) < 3 or set(top[:2]) != {("cat", "3"), ("the", "3")} or top[2] != ("a", "2"):
        out.append(f"output: {o[-200:]!r}")
    if not os.path.exists("test_wordfreq.py"):
        out.append("no test_wordfreq.py")
    else:
        out += ok_if(sh(sys.executable, "-m", "pytest", "-q", "test_wordfreq.py")
                     if shutil.which("pytest") else sh(sys.executable, "-m", "unittest", "-q", "test_wordfreq"))
    return out


def check_shapes():
    rc, o = sh(sys.executable, "main.py")
    out = [] if not rc and re.search(r"12\.5[67]", o) and re.search(r"\b9(?:\.0+)?\b", o) else [f"main.py: {o[-200:]!r}"]
    for f in ("shapes/circle.py", "shapes/square.py"):
        if not os.path.exists(f):
            out.append(f"no {f}")
    return out


def check_site():
    pages = ["index.html", "about.html", "contact.html"]
    out = [f"no {p}" for p in pages + ["style.css"] if not os.path.exists(p)]
    for p in pages:
        if os.path.exists(p):
            text = read(p)
            missing = [q for q in pages if q != p and f'href="{q}"' not in text and f"href='{q}'" not in text]
            if missing:
                out.append(f"{p} doesn't link to {missing}")
            if "style.css" not in text:
                out.append(f"{p} doesn't use style.css")
    return out


def plain(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)    # node colors the numbers it prints


def check_sales():
    rc, o = sh("node", "summary.js")
    o = plain(o)
    return [] if not rc and re.search(r"\b1,?150(?:\.0+)?\b", o) and re.search(r"lamp", o, re.I) else [f"summary.js: {o[-200:]!r}"]


def check_vowels():
    files = sorted(f for f in os.listdir(".") if f.endswith(".swift"))
    rc, o = sh("swiftc", *files, "-o", "prog", timeout=240)
    if rc:
        return [f"doesn't compile: {o[-300:]}"]
    rc, o = sh("./prog")
    out = [] if re.search(r"\b3\b", o) else [f"output: {o[-200:]!r}"]
    if not os.path.exists("Counter.swift"):
        out.append("no Counter.swift")
    return out


def check_bank():
    out = ok_if(py("from bank import Account\na = Account()\na.deposit(50)\na.withdraw(20)\n"
                   "assert a.balance == 30, a.balance\ntry:\n    a.withdraw(100)\n    print('no error')\n"
                   "except ValueError:\n    pass\nassert a.balance == 30\nprint('ok')"), contains="ok")
    if not os.path.exists("test_bank.py"):
        out.append("no test_bank.py")
    else:
        out += ok_if(sh(sys.executable, "-m", "pytest", "-q", "test_bank.py")
                     if shutil.which("pytest") else sh(sys.executable, "-m", "unittest", "-q", "test_bank"))
    return out


def check_convert():
    out = ok_if(node("const c = require('./convert.js'); if (Math.abs(c.cToF(100) - 212) > 1e-9 || "
                     "Math.abs(c.fToC(32)) > 1e-9) process.exit(1); console.log('ok')"), contains="ok")
    rc, o = sh("node", "cli.js", "100", "C")
    o = plain(o)
    if rc or not re.search(r"\b212\b", o):
        out.append(f"node cli.js 100 C: {o[-150:]!r}")
    out += ok_if(sh("node", "test.js")) if os.path.exists("test.js") else ["no test.js"]
    return out


CASES = [
    # (name, starting files, request, check)
    ("py-todo-cli", {},
     "build a command-line to-do app in Python: `python todo.py add <text>` adds an item, `python todo.py list` prints "
     "them numbered, and `python todo.py done <n>` removes item n; the items are saved in todos.json",
     check_todo),
    ("py-wordfreq", {},
     "write wordfreq.py, a Python tool that prints the 3 most common words in the text file given as its first argument, "
     "one per line as 'word count', ignoring case and punctuation, and tests for it in test_wordfreq.py",
     check_wordfreq),
    ("py-shapes-package", {},
     "create a Python package shapes with modules circle.py and square.py, each with an area function, and a main.py "
     "that prints the area of a circle of radius 2 and of a square of side 3",
     check_shapes),
    ("static-site", {},
     "make a small website: index.html, about.html and contact.html, each with the same nav bar linking to all three "
     "pages, all styled by one style.css",
     check_site),
    ("node-sales", {"sales.csv": "product,amount\nlamp,500\nchair,300\ndesk,350\n"},
     "write summary.js, a Node.js script that reads sales.csv (columns product,amount) and prints the total amount and "
     "the product with the highest amount",
     check_sales),
    ("swift-vowels", {},
     "write a Swift command-line program: a Counter struct in Counter.swift with a method that counts the vowels in a "
     "string, and a main.swift that prints the vowel count of \"hello world\"",
     check_vowels),
    ("py-bank", {},
     "build bank.py with an Account class that has deposit, withdraw (raising ValueError when the balance is too low) "
     "and a balance attribute starting at 0, and test_bank.py with tests for all three",
     check_bank),
    ("js-convert", {},
     "make convert.js, a Node module exporting cToF and fToC; cli.js that converts a number given on the command line, "
     "so `node cli.js 100 C` prints 212 F; and test.js that checks both functions with assert",
     check_convert),
]


async def main() -> int:
    if not ev.on_device_available():
        print("SKIPPED: on-device model isn't available here")
        return 0
    filters = sys.argv[1:]
    cases = [c for c in CASES if not filters or any(f in c[0] for f in filters)]
    failed = []
    for name, files, task, check in cases:
        if name.startswith("swift-") and not shutil.which("swiftc"):
            print(f"skip {name}: no swiftc")
            continue
        start = time.monotonic()
        failures, log, _answer, _elapsed, snapshot = await run_one(name, dict(files), task, check)
        print(f"{'ok  ' if not failures else 'FAIL'} {name} ({time.monotonic() - start:.0f}s) files: {sorted(snapshot)}",
              flush=True)
        if failures:
            failed.append(name)
            for f in failures[:3]:
                print(f"       - {f[:300]}")
            for line in log[-4:]:
                print(f"       | {str(line)[:220]}")
    passed = len(cases) - len(failed)
    need = MIN_PASS if not filters else 0
    print(f"\n{passed}/{len(cases)} passed (need {need})")
    return 1 if passed < need else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
