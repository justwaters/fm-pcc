import sys, os, asyncio
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m
from textual.widgets import Input

home = os.path.expanduser("~")
os.chdir(home)

async def main():
    app = m.ChatApp()
    async with app.run_test() as pilot:
        input_widget = app.query_one(Input)

        async def send(text):
            input_widget.value = text
            await pilot.press("enter")
            for _ in range(200):
                await pilot.pause()
                if not input_widget.disabled:
                    break

        await send("what directory are we in?")
        assert app.turn == 1, app.turn
        turn0_prompt_sent = app.backend  # can't easily inspect prompt directly; check via message log instead

        await send("and again, what directory is it?")
        assert app.turn == 2, app.turn

        for msg in app._message_log:
            print(f"[{msg.role}] {msg.text}")
            print("---")

        # only ONE "context:" system message should have been added (turn 0 only)
        # system messages aren't in _message_log, check the log widget count instead
        from textual.containers import VerticalScroll
        log = app.query_one("#log", VerticalScroll)
        context_msgs = [w for w in log.children if "context:" in str(w.render())]
        assert len(context_msgs) == 1, f"expected exactly 1 context injection, got {len(context_msgs)}"
        print("context injected exactly once across 2 turns OK")

        # /clear resets turn to 0 -> next message should re-inject
        app.action_reset()
        await send("what directory are we in, once more?")
        for msg in app._message_log:
            print(f"[{msg.role}] {msg.text}")
        log2 = app.query_one("#log", VerticalScroll)
        context_msgs2 = [w for w in log2.children if "context:" in str(w.render())]
        assert len(context_msgs2) == 1, f"expected re-injection after /clear, got {len(context_msgs2)}"
        print("re-injected after /clear OK")

asyncio.run(main())
print("DONE")
