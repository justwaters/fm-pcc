import sys, asyncio
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m
from textual.widgets import Static, Input
from textual.color import Color

def hexcolor(w):
    return w.styles.color.hex.lower()

async def main():
    app = m.ChatApp()
    async with app.run_test() as pilot:
        screen = app.screen
        ids_in_order = [w.id for w in screen.walk_children() if w.id in ("log", "palette", "inputbar", "status")]
        print("DOM order:", ids_in_order)
        assert ids_in_order.index("inputbar") < ids_in_order.index("status"), ids_in_order
        assert ids_in_order.index("palette") < ids_in_order.index("inputbar"), ids_in_order
        print("status is below inputbar in DOM order OK")

        status = app.query_one("#status", Static)

        app.model = "on-device"
        app._update_chrome()
        assert hexcolor(status) == m.STATUS_ACCENTS["on-device"].lower(), hexcolor(status)
        print("on-device color OK:", hexcolor(status))

        app.model = "cloud"
        app._update_chrome()
        c1 = hexcolor(status)
        assert c1 == m.STATUS_ACCENTS["pcc"].lower()
        app.model = "cloud-pro"
        app._update_chrome()
        c2 = hexcolor(status)
        assert c1 == c2, (c1, c2)
        print("cloud and cloud-pro share pcc color OK:", c1)

        app.model = "ollama"
        app._update_chrome()
        assert hexcolor(status) == m.STATUS_ACCENTS["ollama"].lower()
        print("ollama color OK:", hexcolor(status))

        app.model = "ollama:llama3.2"
        app._update_chrome()
        assert hexcolor(status) == m.STATUS_ACCENTS["ollama"].lower()
        print("ollama:tag color OK:", hexcolor(status))

asyncio.run(main())
print("ALL STATUS POSITION/COLOR TESTS PASSED")
