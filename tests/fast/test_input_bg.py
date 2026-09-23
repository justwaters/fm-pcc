"""Sent user messages get a gray background band (like Claude Code's CLI);
the input box itself stays unshaded."""
import sys, asyncio
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m
from textual.widgets import Input

async def main():
    app = m.ChatApp()
    async with app.run_test() as pilot:
        app._add_message(m.Message("user", "hello there"))
        app._add_message(m.Message("assistant", "hi"))
        await pilot.pause()
        user = app.query(".msg-user").last()
        reply = app.query(".msg-assistant").last()
        assert user.styles.background.hex.lower() == "#2a3138", user.styles.background
        assert reply.styles.background.a == 0, reply.styles.background
        print("sent message has gray band, reply doesn't OK")

        input_widget = app.query_one(Input)
        assert input_widget.styles.background.a == 0, input_widget.styles.background
        assert app.query_one("#inputbar").styles.background.a == 0
        print("input box unshaded OK")

asyncio.run(main())
print("INPUT BACKGROUND OK")
