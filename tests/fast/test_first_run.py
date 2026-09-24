"""A fresh install is fully on-device from the first second: both /task
roles default to on-device, nothing reaches for Shortcuts or the cloud, and
an on-device model that isn't ready yet is explained at launch."""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m  # noqa: E402


def fake_run(results):
    def run(cmd, *a, **k):
        key = " ".join(cmd[:3])
        out, rc = results[key]
        if isinstance(out, Exception):
            raise out
        return subprocess.CompletedProcess(cmd, rc, out, "")
    return run


# ---- readiness messages ----
ok = {"fm license --status": ("Agreed to license FM1 version 1.0 on Sep 14, 2026.", 0),
      "fm available --model": ("System model available", 0)}
with mock.patch.object(m.subprocess, "run", side_effect=fake_run(ok)):
    assert m.on_device_problem() is None
with mock.patch.object(m.subprocess, "run", side_effect=fake_run({**ok, "fm license --status": ("License has not been agreed to.", 1)})):
    assert "/license" in m.on_device_problem()
with mock.patch.object(m.subprocess, "run", side_effect=fake_run({**ok, "fm available --model": ("Apple Intelligence is not enabled", 1)})):
    assert "Apple Intelligence" in m.on_device_problem()
with mock.patch.object(m.subprocess, "run", side_effect=fake_run({"fm license --status": (FileNotFoundError(), 0)})):
    assert "macOS 27" in m.on_device_problem()
print("readiness messages OK")


async def main():
    cwd = tempfile.mkdtemp(prefix="fm-pcc-first-run-")
    orig = os.getcwd()
    os.chdir(cwd)
    try:
        with open("notes.txt", "w") as f:
            f.write("hi\n")
        app = m.ChatApp()
        async with app.run_test():
            assert app.subagent_roles == {"planning": "on-device", "building": "on-device"}
            assert app.model == "on-device"
            cloud = AssertionError("a fresh install must not touch Shortcuts or the cloud")
            with mock.patch.object(app, "call_from_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
                 mock.patch.object(m, "notify"), \
                 mock.patch.object(m, "ensure_shortcut_installed", side_effect=cloud), \
                 mock.patch.object(app.backend, "_run_shortcut", side_effect=cloud), \
                 mock.patch.object(m, "fm_structured", return_value={"steps": [
                     {"action": "RENAME", "path": "notes.txt", "destination": "todo.txt", "details": ""}]}) as planner:
                # a phrasing only the model planner can read
                app._run_task.__wrapped__(app, "notes.txt would be better as todo.txt")
            assert planner.called and os.path.exists("todo.txt")
            print("fresh /task stays on-device, even when the planner is needed OK")

        # `fm-pcc respond` defaults to on-device too
        with mock.patch.object(sys, "argv", ["fm-pcc", "respond", "hi"]), \
             mock.patch.object(m.Backend, "respond", return_value=("hello", "on-device")) as respond, \
             mock.patch("builtins.print"):
            try:
                m.main()
            except SystemExit:
                pass
        assert respond.call_args.args[1] == "on-device", respond.call_args
        print("fm-pcc respond defaults to on-device OK")
    finally:
        os.chdir(orig)
        shutil.rmtree(cwd, ignore_errors=True)


asyncio.run(main())
print("ALL FIRST-RUN TESTS PASSED")
