"""Requests that say what the code must do -- by example ("'1h30m' gives
90"), by rule ("an @ with something before it and a dot after it"), or by
expected output ("it should print 'Total: $10.80'") -- on the real
on-device model, each judged by hidden checks /task never sees. None of
these projects has a test suite: the request is the only spec.

These were written before any work on behavior checks, as the honest
measure for it: 7/13 in both baseline runs, 9/13 in all three runs after
(which tasks pass still varies: the model's code changes run to run).
MIN_PASS is a floor above the baseline. Run like the agentic eval (both
/task roles on-device).

Run: tests/run.sh slow   (or directly, optionally with case-name filters:
     uv run --with textual --with rich python3 tests/slow/test_behavior_real.py email)
"""
import asyncio
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("agentic_eval", os.path.join(HERE, "test_agentic_eval.py"))
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)
sh, py, node, ok_if, run_one = ev.sh, ev.py, ev.node, ev.ok_if, ev.run_one

MIN_PASS = 8


def stub(name, args, doc=""):
    return f"def {name}({args}):\n    {doc and repr(doc) + chr(10) + '    '}raise NotImplementedError\n"


def checks(module, *cases):
    """Hidden checks: (expression, expected) pairs evaluated after import."""
    lines = [f"from {module} import *", "fails = []"]
    for expr, want in cases:
        lines.append(f"try:\n    got = {expr}\nexcept Exception as e:\n    got = f'raised {{type(e).__name__}}: {{e}}'")
        lines.append(f"if got != {want!r}: fails.append({expr!r} + ' -> ' + repr(got) + ', want ' + {repr(want)!r})")
    lines.append("print('ok' if not fails else 'FAILED ' + '; '.join(fails))")
    return lambda: ok_if(py("\n".join(lines)), contains="ok")


def js_checks(module, *cases):
    lines = [f"const m = require('./{module}');", "const fails = [];"]
    for expr, want in cases:
        lines.append(f"try {{ const got = JSON.stringify(m.{expr}); if (got !== JSON.stringify({want})) "
                     f"fails.push(`{expr} -> ${{got}}, want {want}`); }} catch (e) {{ fails.push(`{expr} raised ${{e}}`); }}")
    lines.append("console.log(fails.length ? 'FAILED ' + fails.join('; ') : 'ok');")
    return lambda: ok_if(node("\n".join(lines)), contains="ok")


CASES = [
    # (name, files, task, verify)
    ("py-duration", {"duration.py": stub("parse_duration", "text")},
     "implement parse_duration in duration.py: it takes strings like '1h30m', '45m' or '2h' and returns the total "
     "number of minutes, so '1h30m' gives 90",
     checks("duration", ("parse_duration('1h30m')", 90), ("parse_duration('45m')", 45), ("parse_duration('2h')", 120),
            ("parse_duration('1h5m')", 65))),
    ("py-email", {"validators.py": stub("validate_email", "email")},
     "implement validate_email in validators.py: it returns True when the email has an @ with at least one character "
     "before it and a dot somewhere after it, and False otherwise",
     checks("validators", ("validate_email('a@b.co')", True), ("validate_email('nope')", False),
            ("validate_email('@b.co')", False), ("validate_email('a@bco')", False),
            ("validate_email('first.last@mail.example.org')", True))),
    ("py-slug", {"text.py": stub("slugify", "title")},
     "implement slugify in text.py: lowercase the title, turn every run of spaces and punctuation into a single "
     "hyphen, and trim hyphens from both ends, so 'Hello, World!' becomes 'hello-world'",
     checks("text", ("slugify('Hello, World!')", "hello-world"), ("slugify('  A  B ')", "a-b"),
            ("slugify('C++ & Go')", "c-go"))),
    ("py-roman", {"roman.py": stub("to_roman", "n")},
     "implement to_roman in roman.py: it converts a number from 1 to 3999 to Roman numerals, for example 1994 is "
     "'MCMXCIV' and 4 is 'IV'",
     checks("roman", ("to_roman(1994)", "MCMXCIV"), ("to_roman(4)", "IV"), ("to_roman(9)", "IX"),
            ("to_roman(3999)", "MMMCMXCIX"), ("to_roman(58)", "LVIII"))),
    ("py-cache", {"rates.py": "CALLS = []\n\n\ndef fetch_rate(currency):\n    CALLS.append(currency)\n"
                               "    return {'EUR': 0.9, 'GBP': 0.8}[currency]\n\n\n"
                               "def get_rate(currency):\n    return fetch_rate(currency)\n"},
     "make get_rate in rates.py remember the rate for each currency, so calling it again with the same currency "
     "returns the remembered rate without calling fetch_rate again",
     lambda: ok_if(py("import rates\nassert rates.get_rate('EUR') == 0.9\nassert rates.get_rate('EUR') == 0.9\n"
                      "assert rates.get_rate('GBP') == 0.8\nassert rates.CALLS == ['EUR', 'GBP'], rates.CALLS\nprint('ok')"),
                   contains="ok")),
    ("py-csv", {"csvsplit.py": stub("split_row", "line")},
     "implement split_row in csvsplit.py: split a CSV line on commas, except commas inside double quotes, and drop "
     "the quotes, so 'a,\"b,c\",d' gives ['a', 'b,c', 'd']",
     checks("csvsplit", ("split_row('a,\"b,c\",d')", ["a", "b,c", "d"]), ("split_row('x,y')", ["x", "y"]),
            ("split_row('\"p,q\"')", ["p,q"]))),
    ("py-tax-output", {"main.py": "ITEMS = [('pen', 2.50), ('book', 7.50)]\n\n"
                                  "total = sum(price for _name, price in ITEMS)\nprint(f'Total: ${total:.2f}')\n"},
     "add 8% tax to the total that main.py prints: with its items it should print 'Total: $10.80'",
     lambda: ok_if(sh(sys.executable, "main.py"), contains="Total: $10.80")),
    ("py-wordcount", {"words.py": stub("word_counts", "text")},
     "implement word_counts in words.py: it returns a dict mapping each lowercase word to how many times it appears, "
     "ignoring punctuation, so 'The cat, the hat.' gives {'the': 2, 'cat': 1, 'hat': 1}",
     checks("words", ("word_counts('The cat, the hat.')", {"the": 2, "cat": 1, "hat": 1}),
            ("word_counts('Go! go? GO.')", {"go": 3}))),
    ("py-leap", {"dates.py": stub("is_leap", "year")},
     "implement is_leap in dates.py: a year is a leap year when it's divisible by 4, except century years, which "
     "are leap years only when divisible by 400 -- so 1900 is not a leap year and 2000 is",
     checks("dates", ("is_leap(1900)", False), ("is_leap(2000)", True), ("is_leap(2024)", True),
            ("is_leap(2023)", False))),
    ("py-discount-bug", {"pricing.py": "def apply_discount(price, percent):\n    return price - price * percent / 100\n"},
     "fix apply_discount in pricing.py: it should never return less than 0, and a percent over 100 counts as 100, "
     "so apply_discount(50, 150) gives 0; apply_discount(80, 25) still gives 60",
     checks("pricing", ("apply_discount(50, 150)", 0), ("apply_discount(80, 25)", 60), ("apply_discount(10, 0)", 10))),
    ("js-clamp", {"math.js": "function clamp(n, lo, hi) {\n  throw new Error('not implemented');\n}\n\n"
                             "module.exports = { clamp };\n"},
     "implement clamp in math.js: it returns lo when n is below lo, hi when n is above hi, and n otherwise",
     js_checks("math.js", ("clamp(5, 0, 10)", "5"), ("clamp(-3, 0, 10)", "0"), ("clamp(42, 0, 10)", "10"))),
    ("js-titlecase", {"text.js": "function titleCase(s) {\n  throw new Error('not implemented');\n}\n\n"
                                 "module.exports = { titleCase };\n"},
     "implement titleCase in text.js: capitalize the first letter of every word and lowercase the rest, so "
     "'hELLO wORLD' becomes 'Hello World'",
     js_checks("text.js", ("titleCase('hELLO wORLD')", "'Hello World'"), ("titleCase('a')", "'A'"))),
    ("js-fizzbuzz", {"fb.js": "function fizzbuzz(n) {\n  throw new Error('not implemented');\n}\n\n"
                              "module.exports = { fizzbuzz };\n"},
     "implement fizzbuzz in fb.js: it returns an array of strings for 1 to n, with 'Fizz' for multiples of 3, "
     "'Buzz' for multiples of 5, 'FizzBuzz' for multiples of both, and the number otherwise -- fizzbuzz(5) gives "
     "['1', '2', 'Fizz', '4', 'Buzz']",
     js_checks("fb.js", ("fizzbuzz(5)", "['1','2','Fizz','4','Buzz']"), ("fizzbuzz(15)[14]", "'FizzBuzz'"))),
]


async def main() -> int:
    if not ev.on_device_available():
        print("SKIPPED: on-device model isn't available here")
        return 0
    filters = sys.argv[1:]
    cases = [c for c in CASES if not filters or any(f in c[0] for f in filters)]
    failed = []
    for name, files, task, verify in cases:
        start = time.monotonic()
        failures, log, _answer, _elapsed, _snapshot = await run_one(name, dict(files), task, verify)
        print(f"{'ok  ' if not failures else 'FAIL'} {name} ({time.monotonic() - start:.0f}s)", flush=True)
        if failures:
            failed.append(name)
            for f in failures[:2]:
                print(f"       - {f[:300]}")
            for line in log[-3:]:
                print(f"       | {str(line)[:200]}")
    passed = len(cases) - len(failed)
    need = MIN_PASS if not filters else 0
    print(f"\n{passed}/{len(cases)} passed (need {need})")
    return 1 if passed < need else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
