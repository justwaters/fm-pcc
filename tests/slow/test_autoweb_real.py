"""/task researching on its own, on the real on-device model and the real
web: requests that need facts newer than the model knows ("Research the
latest ai models and build me an html page with a list of them") have to
search -- with "Allow always" set, so no prompt -- and come out with
current facts; plain coding requests must not search at all.

Each research request also runs with automatic search off, to show what
the model writes from its own knowledge. The checks are minimums as of
October 2026 ("Python 3.14 or later"), so they keep passing as newer
versions come out.

Needs network; skipped if the search service can't be reached.
Run: tests/run.sh slow   (or directly, optionally with case-name filters:
     uv run --with textual --with rich python3 tests/slow/test_autoweb_real.py python)
"""
import asyncio
import glob
import os
import re
import shutil
import sys
import tempfile
import time
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
import fm_pcc.app as m  # noqa: E402
from fm_pcc import web  # noqa: E402

MIN_RESEARCHED = 3      # of the 4 research cases, with the web
# Model names newer than the on-device model knows, or a date from last year
# on: a list of names alone goes stale (a run listed GPT-6 Astra and Claude
# Fable 5.1, from September 2026, which a GPT-5-era pattern didn't know).
CURRENT_AI = (r"GPT-[5-9]|Claude \w+ [4-9]|Gemini [3-9]|Llama [4-9]|Grok [4-9]|DeepSeek[- ]?(?:R[1-9]|V[3-9])|Qwen ?[3-9]|"
              rf"\b20(?:{int(time.strftime('%y')) - 1}|{int(time.strftime('%y'))}|{int(time.strftime('%y')) + 1})\b")


def html_items(files):
    text = "\n".join(t for f, t in files.items() if f.endswith(".html"))
    return [re.sub(r"<[^>]+>", "", i).strip() for i in re.findall(r"<li[^>]*>(.*?)</li>", text, re.S)]


def all_text(files):
    return "\n".join(files.values())


def version_at_least(rx, text, minimum):
    found = [tuple(int(p) for p in v.split(".")) for v in re.findall(rx, text)]
    return any(v >= minimum for v in found), found


CASES = [
    # (name, task, needs research, check(files) -> problems)
    ("ai-models-page", "Research the latest ai models and build me an html page with a list of them", True,
     lambda f: [] if len(html_items(f)) >= 3 and sum(bool(re.search(CURRENT_AI, i)) for i in html_items(f)) >= 2
     else [f"list items: {html_items(f)[:8]}"]),
    ("python-version", "add a line to README.md saying the newest stable Python version, like 'Python X.Y'", True,
     lambda f: [] if version_at_least(r"Python (3\.\d+)", all_text(f), (3, 14))[0]
     else [f"README.md: {f.get('README.md', '(missing)')[-160:]!r}"]),
    ("node-lts", "create node.json with the current Node.js LTS major version, as {\"lts\": N}", True,
     lambda f: [] if any(int(n) >= 22 for n in re.findall(r'"lts"\s*:\s*"?v?(\d+)', all_text(f)))
     else [f"node.json: {f.get('node.json', '(missing)')[:120]!r}"]),
    ("iphones-page", "make an html page listing the newest iPhone models", True,
     lambda f: [] if re.search(r"iPhone 1[7-9]|iPhone [2-9]\d", all_text(f)) else [f"list items: {html_items(f)[:8]}"]),
    ("contact-form", "build me an html page with a contact form", False, lambda f: []),
    ("multiply", "add a multiply function to calc.py", False, lambda f: []),
]


async def run(task, web_on, files=None):
    cwd = tempfile.mkdtemp(prefix="fm-pcc-autoweb-")
    orig = os.getcwd()
    os.chdir(cwd)
    for p, t in (files or {}).items():
        open(p, "w").write(t)
    if web_on:
        os.environ.pop("FM_PCC_AUTO_WEB", None)
        m.update_state(web_permission="always")
    else:
        os.environ["FM_PCC_AUTO_WEB"] = "0"
    log = []
    try:
        app = m.ChatApp()
        app.subagent_roles["planning"] = "on-device"
        async with app.run_test():
            with mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
                 mock.patch.object(app, "_log_progress", side_effect=log.append), mock.patch.object(m, "notify"):
                app._run_task.__wrapped__(app, task)
        out = {f: open(f, errors="replace").read() for f in glob.glob("**/*", recursive=True) if os.path.isfile(f)}
    finally:
        os.chdir(orig)
        shutil.rmtree(cwd, ignore_errors=True)
    return log, out


async def main() -> int:
    try:
        if not web.search("python"):
            raise web.WebError("no results")
    except web.WebError as e:
        print(f"SKIPPED: the search service can't be reached ({e})")
        return 0
    if not os.path.exists("/usr/bin/fm") and not shutil.which("fm"):
        print("SKIPPED: no on-device model")
        return 0
    filters = sys.argv[1:]
    researched, failed = 0, []
    for name, task, needs, check in CASES:
        if filters and not any(f in name for f in filters):
            continue
        files = {"multiply": {"calc.py": "def add(a, b):\n    return a + b\n"},
                 "python-version": {"README.md": "# Demo\n\nA small demo project.\n"}}.get(name, {})
        start = time.monotonic()
        log, out = await run(task, True, files)
        searched = any("searching the web:" in str(line) for line in log)
        problems = check(out) if needs else []
        if needs and not searched:
            problems.insert(0, "didn't search the web")
        if not needs and searched:
            problems = ["searched the web for a plain coding request"]
        ok = not problems
        researched += ok and needs
        print(f"{'ok  ' if ok else 'FAIL'} {name} ({time.monotonic() - start:.0f}s){' -- searched' if searched else ''}",
              flush=True)
        for p in problems:
            print(f"       - {p}")
        if not ok and not needs:
            failed.append(name)
        if needs:
            _log, without = await run(task, False, files)
            print(f"       without the web: {'right anyway' if not check(without) else check(without)[0][:160]}")
    research_cases = sum(1 for c in CASES if c[2] and (not filters or any(f in c[0] for f in filters)))
    need = MIN_RESEARCHED if not filters else research_cases
    print(f"\n{researched}/{research_cases} researched right (need {need}); "
          f"plain requests that searched: {len(failed)} (must be 0)")
    return 1 if researched < need or failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
