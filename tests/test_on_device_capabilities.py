#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["textual>=0.60", "rich"]
# ///
"""Real end-to-end tests for /task's on-device file/folder/git capabilities.

Runs actual `/task` runs against the real on-device model in scratch temp
directories -- no mocking of the model itself, since the whole point is to
catch cases where the model's real behavior doesn't match what the code
assumes (that's exactly how the reported bugs this file guards against were
found: a small on-device model given "create a folder" created a file named
"test" instead, because /task's code had no folder-creation action at all).

Run directly: uv run tests/test_on_device_capabilities.py
Exit code 0 = all good (including "skipped, no on-device model available").
Exit code 1 = a real behavioral failure.

Wired up as this repo's pre-commit hook (see scripts/git-hooks/pre-commit) --
every commit runs this first and is blocked if it fails.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest.mock as mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

import fm_pcc.app as m  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"  FAIL: {message}")
    else:
        print(f"  ok: {message}")


def on_device_available() -> bool:
    try:
        result = subprocess.run(
            ["fm", "available", "--model", "system"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


class ScratchRepo:
    """A throwaway directory this test script owns -- created fresh, always
    removed, never touches the real repo it lives in.
    """

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="fm-pcc-capability-test-")
        self._orig_cwd = os.getcwd()
        os.chdir(self.path)
        return self.path

    def __exit__(self, *exc):
        os.chdir(self._orig_cwd)
        shutil.rmtree(self.path, ignore_errors=True)
        return False


def git_init(cwd: str, with_remote: bool = False) -> str | None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=cwd, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=cwd, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=cwd, check=True)
    if not with_remote:
        return None
    bare = tempfile.mkdtemp(prefix="fm-pcc-capability-test-bare-")
    subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
    subprocess.run(["git", "remote", "add", "origin", bare], cwd=cwd, check=True)
    return bare


def sync_call_from_thread(fn, *args, **kwargs):
    return fn(*args, **kwargs)


def make_app() -> "m.ChatApp":
    """A ChatApp with BOTH /task subagent roles forced to on-device -- this
    file targets on-device only. The "planning" role defaults to cloud-pro,
    which would otherwise make these tests depend on Cloud Pro's real
    availability and Apple's usage limits for it, neither of which belongs
    in a test that's supposed to run offline and fast in a pre-commit hook.
    """
    app = m.ChatApp()
    app.subagent_roles["planning"] = "on-device"
    app.subagent_roles["building"] = "on-device"
    return app


async def run_task(app, description: str) -> None:
    with mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread), \
         mock.patch.object(m, "notify"):
        app._run_task.__wrapped__(app, description)


async def test_create_file_and_folder():
    print("\n=== create files and folders ===")
    with ScratchRepo() as cwd:
        app = make_app()
        async with app.run_test():
            await run_task(app, "create a file named notes.txt containing the word hello")
        check(os.path.isfile("notes.txt"), "notes.txt was created as a file")

        app2 = make_app()
        async with app2.run_test():
            await run_task(
                app2, 'create a folder named "test" with the file "path.txt" inside it'
            )
        check(os.path.isdir("test"), "'test' is a real directory, not a file")
        check(
            os.path.isfile(os.path.join("test", "path.txt")),
            "test/path.txt was created inside the new folder",
        )

        app3 = make_app()
        async with app3.run_test():
            await run_task(app3, "create an empty folder named assets")
        check(
            os.path.isdir("assets") and not os.listdir("assets"),
            "an empty folder can be created with nothing inside it",
        )


async def test_rename():
    print("\n=== rename files and folders ===")
    with ScratchRepo() as cwd:
        with open("old.txt", "w") as f:
            f.write("hello\n")
        app = make_app()
        async with app.run_test():
            await run_task(app, "rename old.txt to new.txt")
        check(not os.path.exists("old.txt"), "old.txt no longer exists after rename")
        check(os.path.isfile("new.txt"), "new.txt exists after rename")

        os.makedirs("draft")
        app2 = make_app()
        async with app2.run_test():
            await run_task(app2, "rename the draft folder to final")
        check(not os.path.exists("draft"), "draft folder no longer exists after rename")
        check(os.path.isdir("final"), "final folder exists after rename")


async def test_move():
    print("\n=== move files and folders inside the current directory ===")
    with ScratchRepo() as cwd:
        os.makedirs("archive")
        with open("report.txt", "w") as f:
            f.write("hello\n")
        app = make_app()
        async with app.run_test():
            await run_task(app, "move report.txt into the archive folder")
        check(not os.path.exists("report.txt"), "report.txt no longer at the top level")
        check(
            os.path.isfile(os.path.join("archive", "report.txt")),
            "report.txt now lives inside archive/",
        )


async def test_undo_covers_new_action_types():
    print("\n=== /undo covers folder creation and moves ===")
    with ScratchRepo() as cwd:
        app = make_app()
        async with app.run_test():
            await run_task(app, "create an empty folder named scratch")
            check(os.path.isdir("scratch"), "folder created before undo")
            app._handle_undo()
            check(not os.path.exists("scratch"), "/undo removed the folder it created")


async def test_git_operations():
    print("\n=== git operations: add/commit auto, push/branch/pull gated ===")
    with ScratchRepo() as cwd:
        bare = git_init(cwd, with_remote=True)
        with open("a.txt", "w") as f:
            f.write("hello\n")
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], check=True)
        subprocess.run(["git", "push", "-q", "-u", "origin", "main"], check=True)

        with open("a.txt", "a") as f:
            f.write("more content\n")

        app = make_app()
        async with app.run_test():
            await run_task(
                app, "commit the current changes with the message 'update a.txt' and push them"
            )

        local_log = subprocess.run(
            ["git", "log", "--oneline", "-5"], capture_output=True, text=True
        ).stdout
        check("update a.txt" in local_log, "the local commit was made automatically")

        remote_log = subprocess.run(
            ["git", "log", "--oneline", "-5", "main"], cwd=bare, capture_output=True, text=True
        ).stdout
        check(
            "update a.txt" not in remote_log,
            "push did NOT happen automatically -- /task must never push without /push",
        )

        # the loop should have stopped rather than erroring out
        status = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True
        ).stdout
        check(status.strip() == "", "working tree is clean (committed, just not pushed)")

        # now the user explicitly confirms -- /push should actually push
        app2 = make_app()
        async with app2.run_test():
            app2._handle_push()
            await app2.workers.wait_for_complete()
        remote_log2 = subprocess.run(
            ["git", "log", "--oneline", "-5", "main"], cwd=bare, capture_output=True, text=True
        ).stdout
        check("update a.txt" in remote_log2, "/push, run explicitly, does push")

        shutil.rmtree(bare, ignore_errors=True)


async def test_branch_operations_are_gated_but_work_when_confirmed():
    print("\n=== branch create/switch: gated in /task, work via /branch ===")
    with ScratchRepo() as cwd:
        git_init(cwd)
        with open("a.txt", "w") as f:
            f.write("hello\n")
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], check=True)

        app = make_app()
        async with app.run_test():
            await run_task(app, "create a new git branch named feature-x and switch to it")

        branch = subprocess.run(
            ["git", "branch", "--show-current"], capture_output=True, text=True
        ).stdout.strip()
        check(branch == "main", "/task did not switch branches on its own")

        app2 = make_app()
        async with app2.run_test():
            app2._handle_branch("create feature-x")
            await app2.workers.wait_for_complete()
        branch2 = subprocess.run(
            ["git", "branch", "--show-current"], capture_output=True, text=True
        ).stdout.strip()
        check(branch2 == "feature-x", "/branch create, run explicitly, does switch")


async def main() -> int:
    if not on_device_available():
        print(
            "SKIPPED: on-device model isn't available here (fm license/model "
            "not set up) -- not a code failure, allowing the commit through."
        )
        return 0

    for test in [
        test_create_file_and_folder,
        test_rename,
        test_move,
        test_undo_covers_new_action_types,
        test_git_operations,
        test_branch_operations_are_gated_but_work_when_confirmed,
    ]:
        try:
            await test()
        except Exception as e:
            FAILURES.append(f"{test.__name__} raised {type(e).__name__}: {e}")
            print(f"  FAIL: {test.__name__} raised {type(e).__name__}: {e}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1

    print("ALL CAPABILITY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
