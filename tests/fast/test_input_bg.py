import sys, asyncio
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m
from textual.widgets import Input

async def main():
    app = m.ChatApp()
    async with app.run_test() as pilot:
        inputbar = app.query_one("#inputbar")
        input_widget = app.query_one(Input)
        print("inputbar bg:", inputbar.styles.background)
        print("input bg:", input_widget.styles.background)
        assert str(inputbar.styles.background.hex).lower() == "#232a31"
        assert str(input_widget.styles.background.hex).lower() == "#232a31"

asyncio.run(main())
print("INPUT BACKGROUND OK")
