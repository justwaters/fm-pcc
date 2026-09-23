import sys, os, asyncio, subprocess, unittest.mock as mock
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m

async def main():
    app = m.ChatApp()
    async with app.run_test() as pilot:
        # branch detection against the real repo (whatever branch/clone
        # directory the tests happen to be run from)
        expected_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert expected_branch and app._branch == expected_branch, (app._branch, expected_branch)
        print("branch detected OK:", app._branch)

        # on-device: no transcript yet -> 0/4096
        app.model = "on-device"
        used, mx = app.backend.context_usage("on-device")
        assert (used, mx) == (0, 4096), (used, mx)
        print("on-device empty OK")

        line = app._status_line()
        location = f"{os.path.basename(os.getcwd())} ({expected_branch})"
        assert line.startswith(f"{location} | on-device | Context:0%"), line
        print("status line format OK:", line)

        # cloud heuristic
        app.backend._cloud_history["cloud-pro"] = [("q" * 40, "a" * 200)]
        used2, mx2 = app.backend.context_usage("cloud-pro")
        assert mx2 == 32000
        assert used2 == 60, used2  # (40+200)//4
        print("cloud heuristic OK:", used2, mx2)

        # ollama: unknown until first real call
        used3, mx3 = app.backend.context_usage("ollama:doesnotexist")
        assert used3 is None, used3
        print("ollama unknown-tag OK (used=None):", used3, mx3)

        # ollama: with fake cached tokens + context length
        app.backend._ollama_context_tokens["fake-model"] = 500
        app.backend._ollama_context_length["fake-model"] = 2000
        used4, mx4 = app.backend.context_usage("ollama:fake-model")
        assert (used4, mx4) == (500, 2000), (used4, mx4)
        print("ollama cached OK")

        # non-repo / non-git cwd -> no "(branch)" suffix
        with mock.patch.object(m, "git_branch", return_value=None):
            app2 = m.ChatApp()
        assert app2._branch is None
        async with app2.run_test() as pilot2:
            line2 = app2._status_line()
            assert "(" not in line2.split("|")[0], line2
            print("no-branch format OK:", line2)

asyncio.run(main())
print("ALL STATUSLINE TESTS PASSED")
