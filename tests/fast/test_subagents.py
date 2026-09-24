import sys, asyncio
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m
from textual.containers import VerticalScroll

def last_text(app):
    log = app.query_one("#log", VerticalScroll)
    return str(log.children[-1].render())

async def main():
    app = m.ChatApp()
    async with app.run_test() as pilot:
        # On-device for both roles out of the box: no cloud, no Shortcuts.
        assert app.subagent_roles == {"planning": "on-device", "building": "on-device"}, app.subagent_roles
        print("default roles OK")

        app._handle_command("/subagents")
        text = last_text(app)
        assert "planning" in text and "building" in text, text
        assert "cloud/core" not in text
        print("no-arg listing OK")

        app._handle_command("/subagents planning ollama")
        assert "planning" in last_text(app)
        assert app.subagent_roles["planning"] == "ollama"
        print("set planning OK")

        app._handle_command("/subagents building cloud")
        assert app.subagent_roles["building"] == "cloud"
        print("set building OK")

        # old role names should now be rejected
        app._handle_command("/subagents cloud on-device")
        text2 = last_text(app)
        assert "usage:" in text2, text2
        assert app.subagent_roles["planning"] == "ollama"  # unchanged
        print("old role name rejected OK")

        # usage text uses new names
        app._handle_command("/subagents bogus xyz")
        text3 = last_text(app)
        assert "[planning|building]" in text3, text3
        print("usage text OK")

asyncio.run(main())
print("ALL SUBAGENTS RENAME TESTS PASSED")
