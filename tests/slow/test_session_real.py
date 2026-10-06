"""A real chat session (October 2026, v0.59.1) replayed on the real
on-device model and the real web, through the same input path as typing
in fm-pcc: research requests that ask for a file, a follow-up correction,
and "do it". The session went wrong at every step:

- "research ... Put your findings in a new findings.md file" was answered
  in chat (no file), searching "ios 18.0 ... vs ios 17.0" and a folder
  path, and concluded the camera app "has no reported differences";
- "give me a list of the iphone 18 family lineup with prices, and write it
  to findings.md" never searched ("iphone 18" didn't count as a name) and
  invented a base iPhone 18 at $999; the /task it offered created the file
  and then appended "Add iPhone 18 family lineup with prices" to it;
- "the base iphone 18 doesnt exist", "so change the file", "do it" ran
  /task with just "so change the file", which changed nothing.

Facts as of October 2026 (MacRumors, BGR, AppleInsider): the newest iOS is
27, whose Camera app adds a Siri mode, customizable controls and Pro
controls; the iPhone 18 lineup is the 18 Pro ($1,199), 18 Pro Max ($1,299)
and the foldable iPhone Duo ($1,999) -- no base iPhone 18 until 2027.

Needs network; skipped if the search service can't be reached.
Run: tests/run.sh slow   (or directly, optionally with case-name filters:
     uv run --with textual --with rich python3 tests/slow/test_session_real.py iphone)
"""
import asyncio
import os
import re
import shutil
import sys
import tempfile
import time
import types
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
import fm_pcc.app as m  # noqa: E402
from fm_pcc import web  # noqa: E402

MIN_PASS = 2

BASE_IPHONE_18 = re.compile(r"iPhone 18(?!\s*(?:Pro|Air|Plus|e\b|Duo|Fold|Ultra|family|lineup|line-up|models|series))"
                            r"[^\n|]*?[|:–-][^\n]*?\$\s?\d", re.I)


def findings(files):
    return files.get("findings.md", "")


def check_ios(files, log):
    text = findings(files)
    out = [] if text else ["no findings.md"]
    if text and not re.search(r"iOS (?:2[6-9]|3\d)", text):
        out.append(f"findings.md doesn't mention iOS 26 or later: {text[:200]!r}")
    if re.search(r"no (?:reported |notable |known )?differences", text, re.I):
        out.append("findings.md says there are no differences")
    return out


def check_lineup(files, log):
    text = findings(files)
    out = [] if text else ["no findings.md"]
    if text and not re.search(r"18 Pro", text):
        out.append(f"no iPhone 18 Pro in findings.md: {text[:200]!r}")
    # each model's price on its own line: 18 Pro $1,199, Pro Max $1,299, Duo $1,999
    for model, price in ((r"18 Pro(?!\s*Max)", "1,199"), (r"18 Pro Max", "1,299"), (r"Duo", "1,999")):
        line = next((l for l in text.splitlines() if re.search(model, l) and "$" in l), None)
        if line and price not in line.replace("1199", "1,199").replace("1299", "1,299").replace("1999", "1,999"):
            out.append(f"wrong price: {line.strip()!r} (it's ${price})")
    if text and not re.search(r"18 Pro(?!\s*Max)[^\n]*\$", text):
        out.append("no price for the iPhone 18 Pro")
    if BASE_IPHONE_18.search(text):
        out.append(f"lists a base iPhone 18 with a price: {BASE_IPHONE_18.search(text).group(0)!r}")
    if re.search(r"^Add iPhone", text, re.M):
        out.append("a plan step's description was written into the file")
    return out


def check_correction(files, log):
    text = findings(files)
    out = []
    if BASE_IPHONE_18.search(text):
        out.append(f"still lists a base iPhone 18: {BASE_IPHONE_18.search(text).group(0)!r}")
    if not re.search(r"18 Pro", text):
        out.append(f"lost the rest of the list: {text[:200]!r}")
    return out


WRONG_TABLE = ("# iPhone 18 Family Lineup with Prices\n\n- iPhone 18: $999\n- iPhone 18 Pro: $1,199\n"
               "- iPhone 18 Pro Max: $1,399\n")

CASES = [
    # (name, starting files, messages typed, check(files, log))
    ("ios-camera", {}, ["research the differences in the camera app between the latest ios version and ios version "
                        "18.0 . Put your findings in a new findings.md file"], check_ios),
    ("iphone-lineup", {}, ["give me a list of the iphone 18 family lineup with prices, and write it to findings.md"],
     check_lineup),
    ("correction", {"findings.md": WRONG_TABLE},
     ["the base iphone 18 doesnt exist", "so change the file", "do it"], check_correction),
]


async def run(messages, files):
    cwd = tempfile.mkdtemp(prefix="fm-pcc-session-")
    orig = os.getcwd()
    os.chdir(cwd)
    for p, t in files.items():
        open(p, "w").write(t)
    os.environ.pop("FM_PCC_AUTO_WEB", None)
    m.update_state(web_permission="always")
    log = []
    try:
        app = m.ChatApp()
        app.subagent_roles["planning"] = "on-device"
        async with app.run_test():
            box = app.query_one(m.Input)
            with mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
                 mock.patch.object(app, "_log_progress", side_effect=lambda t: log.append(t)), \
                 mock.patch.object(m, "notify"), \
                 mock.patch.object(app, "_run_task", side_effect=lambda r: m.ChatApp._run_task.__wrapped__(app, r)), \
                 mock.patch.object(app, "_respond", side_effect=lambda p, *a, **k: m.ChatApp._respond.__wrapped__(app, p, *a, **k)):
                for msg in messages:
                    box.disabled = False
                    app.on_input_submitted(types.SimpleNamespace(value=msg, input=box))
                    log.append(f"[you] {msg}")
                    replies = [x.text for x in app._message_log if x.role == "assistant"]
                    if replies:
                        log.append(f"[chat] {replies[-1][:300]}")
        out = {f: open(f, errors="replace").read() for f in os.listdir(".") if os.path.isfile(f)}
    finally:
        os.chdir(orig)
        shutil.rmtree(cwd, ignore_errors=True)
        m.update_state(web_permission=None)
        os.environ["FM_PCC_AUTO_WEB"] = "0"
    return log, out


async def main() -> int:
    try:
        if not web.search("python"):
            raise web.WebError("no results")
    except web.WebError as e:
        print(f"SKIPPED: the search service can't be reached ({e})")
        return 0
    if not shutil.which("fm"):
        print("SKIPPED: no on-device model")
        return 0
    filters = sys.argv[1:]
    cases = [c for c in CASES if not filters or any(f in c[0] for f in filters)]
    passed = 0
    for name, files, messages, check in cases:
        start = time.monotonic()
        log, out = await run(messages, files)
        problems = check(out, log)
        passed += not problems
        print(f"{'ok  ' if not problems else 'FAIL'} {name} ({time.monotonic() - start:.0f}s)", flush=True)
        for p in problems:
            print(f"       - {p[:300]}")
        print("       findings.md: " + (out.get("findings.md") or "(none)")[:600].replace("\n", "\n         "))
        if problems:
            notes = next((l for l in log if str(l).startswith("found on the web")), "")
            print("       research: " + str(notes)[:500].replace("\n", "\n         "))
            for line in log[-12:]:
                print(f"       | {str(line)[:240]}")
    need = MIN_PASS if not filters else len(cases)
    print(f"\n{passed}/{len(cases)} passed (need {need})")
    return 1 if passed < need else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
