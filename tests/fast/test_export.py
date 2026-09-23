"""/export writes everything on screen -- including /task plans and diffs
-- as Markdown (default), plain text, or JSON, and never overwrites."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m  # noqa: E402
from textual.containers import VerticalScroll  # noqa: E402


def last(app):
    return str(app.query_one("#log", VerticalScroll).children[-1].render())


async def main():
    orig = os.getcwd()
    cwd = tempfile.mkdtemp(prefix="fm-pcc-export-")
    os.chdir(cwd)
    try:
        app = m.ChatApp()
        async with app.run_test():
            app._handle_command("/export")
            assert "nothing to export" in last(app), last(app)

            app._add_message(m.Message("user", "make it green"))
            app._add_message(m.Message("assistant", "Sure -- **green** it is."))
            app._add_message(m.Message("system", "plan:\n1. edit a.css"))
            app._add_message(m.Message("system", "wrote a.css\n\n---\n+++\n@@ -1 +1 @@\n-a\n+b\n"))
            app._add_message(m.Message("system", "done after 1 step(s)."))
            app._add_message(m.Message("user", "/export"))  # the echoed command itself
            app._handle_command("/export")
            exported = [f for f in os.listdir(cwd) if f.startswith("fm-pcc-transcript-")]
            assert len(exported) == 1 and exported[0].endswith(".md"), exported
            md = open(exported[0]).read()
            assert "**you ›** make it green" in md and "**fm-pcc ›**" in md, md
            assert "```diff" in md and "+b" in md and "```text\nplan:" in md, md
            assert "> done after 1 step(s)." in md, md
            assert "/export" not in md.split("\n", 3)[-1], md
            print("default markdown export OK")

            app._handle_command("/export notes/chat.txt")
            txt = open("notes/chat.txt").read()
            assert "you › make it green" in txt and "fm-pcc › Sure" in txt, txt
            print("txt export (with a new folder) OK")

            app._handle_command("/export chat.json")
            data = json.load(open("chat.json"))
            assert data["messages"][0] == {"role": "user", "text": "make it green"}, data
            assert len(data["messages"]) == 5, data  # no /export lines or export status lines
            print("json export OK")

            app._handle_command("/export chat.json")
            assert "already exists" in last(app), last(app)
            print("refuses to overwrite OK")

            with mock.patch.object(m.subprocess, "run") as run:
                app._handle_command("/export copy")
            assert run.call_args.args[0] == ["pbcopy"] and "make it green" in run.call_args.kwargs["input"]
            assert "clipboard" in last(app)
            print("copy to clipboard OK")
    finally:
        os.chdir(orig)
        shutil.rmtree(cwd, ignore_errors=True)


asyncio.run(main())
print("ALL EXPORT TESTS PASSED")
