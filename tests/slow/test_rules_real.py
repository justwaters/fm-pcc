"""Requests stated as rules, not examples -- "a password is strong when it
has at least 8 characters, a digit, and an uppercase letter" -- on the
real on-device model, each judged by hidden checks that include the edge
cases the rule decides (exactly 8 characters; no digit). None of these
projects has a test suite, and none of the requests gives an example to
check: the rule is the only spec.

These were written as the measure for turning rules into checks (the
model proposing examples, kept only when a second, independent answer
agreed). The measurement said not to: /task already got 9/10 in both runs
without it, and the one miss ("each extra kilogram or part of one") was
exactly where the model couldn't produce a right example either. Of the
examples it proposed across these and the behavior tasks, 22% were wrong;
after the agreement filter 1 in 32 still was -- a cost with no gain to
show here. So this stays as a regression check for rule-stated requests,
with a floor of 8. Run like the agentic eval (both /task roles on-device).

Run: tests/run.sh slow   (or directly, optionally with case-name filters:
     uv run --with textual --with rich python3 tests/slow/test_rules_real.py password)
"""
import asyncio
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("behavior_real", os.path.join(HERE, "test_behavior_real.py"))
bh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bh)
ev, stub, checks, js_checks, run_one = bh.ev, bh.stub, bh.checks, bh.js_checks, bh.run_one

MIN_PASS = 8

CASES = [
    # (name, files, task, verify)
    ("py-password", {"auth.py": stub("is_strong", "password")},
     "implement is_strong in auth.py: a password is strong when it has at least 8 characters, at least one digit, "
     "and at least one uppercase letter",
     checks("auth", ("is_strong('Abcdefg1')", True), ("is_strong('Abcdef1')", False), ("is_strong('abcdefg1')", False),
            ("is_strong('Abcdefgh')", False), ("is_strong('ZZZZZZZZZ9')", True))),
    ("py-grade", {"grades.py": stub("letter_grade", "score")},
     "implement letter_grade in grades.py: 90 and above is 'A', 80 up to 89 is 'B', 70 up to 79 is 'C', 60 up to 69 "
     "is 'D', and anything below 60 is 'F'",
     checks("grades", ("letter_grade(90)", "A"), ("letter_grade(89)", "B"), ("letter_grade(80)", "B"),
            ("letter_grade(60)", "D"), ("letter_grade(59)", "F"), ("letter_grade(100)", "A"))),
    ("py-shipping", {"shipping.py": stub("shipping_cost", "weight_kg, express")},
     "implement shipping_cost in shipping.py: it's 5 for the first kilogram plus 2 for each extra kilogram or part "
     "of one, and express doubles the total",
     checks("shipping", ("shipping_cost(1, False)", 5), ("shipping_cost(0.5, False)", 5), ("shipping_cost(2, False)", 7),
            ("shipping_cost(2.5, False)", 9), ("shipping_cost(1, True)", 10))),
    ("py-truncate", {"text.py": stub("truncate", "text, limit")},
     "implement truncate in text.py: if the text is longer than limit characters, cut it so that the result including "
     "a trailing '...' is exactly limit characters long; text that already fits is returned unchanged",
     checks("text", ("truncate('hello world', 8)", "hello..."), ("truncate('short', 10)", "short"),
            ("truncate('exactly10!', 10)", "exactly10!"))),
    ("py-palindrome", {"words.py": stub("is_palindrome", "text")},
     "implement is_palindrome in words.py: it ignores case, spaces and punctuation, and returns whether what's left "
     "reads the same forwards and backwards",
     checks("words", ("is_palindrome('A man, a plan, a canal: Panama')", True), ("is_palindrome('race car')", True),
            ("is_palindrome('hello')", False), ("is_palindrome('')", True))),
    ("py-bmi", {"health.py": stub("bmi_category", "weight_kg, height_m")},
     "implement bmi_category in health.py: compute BMI as weight divided by height squared; under 18.5 is "
     "'underweight', 18.5 up to but not including 25 is 'normal', 25 up to but not including 30 is 'overweight', "
     "and 30 or more is 'obese'",
     checks("health", ("bmi_category(50, 1.8)", "underweight"), ("bmi_category(70, 1.75)", "normal"),
            ("bmi_category(81, 1.8)", "overweight"), ("bmi_category(100, 1.8)", "obese"))),
    ("py-initials", {"names.py": stub("initials", "full_name")},
     "implement initials in names.py: return the first letter of each word in the name, uppercased, joined with no "
     "spaces or dots, ignoring extra spaces between words",
     checks("names", ("initials('ada lovelace')", "AL"), ("initials('  grace   brewster hopper ')", "GBH"),
            ("initials('Bo')", "B"))),
    ("js-age", {"age.js": "function canVote(age, isCitizen) {\n  throw new Error('not implemented');\n}\n\n"
                          "module.exports = { canVote };\n"},
     "implement canVote in age.js: someone can vote when they are 18 or older and a citizen",
     js_checks("age.js", ("canVote(18, true)", "true"), ("canVote(17, true)", "false"), ("canVote(30, false)", "false"))),
    ("js-range", {"range.js": "function range(start, end, step) {\n  throw new Error('not implemented');\n}\n\n"
                              "module.exports = { range };\n"},
     "implement range in range.js: return an array of numbers from start up to but not including end, counting by "
     "step, which defaults to 1",
     js_checks("range.js", ("range(0, 3)", "[0,1,2]"), ("range(1, 10, 3)", "[1,4,7]"), ("range(5, 5)", "[]"))),
    ("js-pluralize", {"plural.js": "function pluralize(word, count) {\n  throw new Error('not implemented');\n}\n\n"
                                  "module.exports = { pluralize };\n"},
     "implement pluralize in plural.js: return the count, a space, and the word, adding an 's' to the word unless "
     "the count is exactly 1",
     js_checks("plural.js", ("pluralize('cat', 1)", "'1 cat'"), ("pluralize('cat', 0)", "'0 cats'"),
               ("pluralize('dog', 3)", "'3 dogs'"))),
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
