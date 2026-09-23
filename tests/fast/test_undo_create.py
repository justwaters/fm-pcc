import sys, os, asyncio, tempfile
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m

async def main():
    app = m.ChatApp()
    async with app.run_test() as pilot:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "new.txt")
        # simulate what /task's create branch does
        app._push_undo(path, "new.txt", "", existed_before=False)
        with open(path, "w") as f:
            f.write("brand new content\n")
        assert os.path.isfile(path)

        app._handle_undo()
        assert not os.path.exists(path), "file should have been removed, not left empty"
        print("undo-of-create removes the file OK")

        # existing edit-undo behavior unchanged (default existed_before=True)
        path2 = os.path.join(tmp, "existing.txt")
        with open(path2, "w") as f:
            f.write("original\n")
        app._push_undo(path2, "existing.txt", "original\n")
        with open(path2, "w") as f:
            f.write("edited\n")
        app._handle_undo()
        with open(path2) as f:
            assert f.read() == "original\n"
        assert os.path.exists(path2)
        print("undo-of-edit still restores content OK")

asyncio.run(main())
print("ALL UNDO-CREATE TESTS PASSED")
