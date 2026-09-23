"""Reliability corpus for /task on the real on-device model.

Each case sets up a scratch directory, runs one /task request against the
real on-device model (planning AND building forced on-device), and checks
the resulting filesystem/git state -- varied phrasings of the four core
jobs: edit, rename, move, commit (plus combinations of them), since a fix
that only works for the one phrasing a test happens to use isn't a fix.

Run: tests/run.sh slow   (or directly, optionally with case-name filters:
     uv run --with textual --with rich python3 tests/slow/test_task_reliability.py rename)
Exit 0 = every case passed (or no on-device model here), 1 = any failed.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest.mock as mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

import fm_pcc.app as m  # noqa: E402


def on_device_available() -> bool:
    try:
        result = subprocess.run(
            ["fm", "available", "--model", "system"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def read(path: str) -> str:
    with open(path) as f:
        return f.read()


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def git_repo(files: dict[str, str], dirty: dict[str, str] | None = None, remote: bool = False) -> None:
    """Init a repo in cwd with `files` committed, then apply `dirty` edits."""
    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    for path, content in files.items():
        write(path, content)
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    if remote:
        bare = tempfile.mkdtemp(prefix="fm-pcc-reliability-bare-")
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        git("remote", "add", "origin", bare)
        git("push", "-q", "-u", "origin", "main")
    for path, content in (dirty or {}).items():
        write(path, content)


def last_commit_message() -> str:
    return git("log", "-1", "--format=%s").strip()


def commit_count() -> int:
    return int(git("rev-list", "--count", "HEAD").strip())


def clean_tree() -> bool:
    return git("status", "--porcelain").strip() == ""


def plain(files: dict[str, str]):
    def setup():
        for path, content in files.items():
            if path.endswith("/"):
                os.makedirs(path, exist_ok=True)
            else:
                write(path, content)
    return setup


# Each case: (name, setup, task, check). check() returns a list of failure
# strings (empty = pass), run with cwd at the scratch directory.
def exists(*paths):
    return [f"{p} should exist" for p in paths if not os.path.exists(p)]


def missing(*paths):
    return [f"{p} should no longer exist" for p in paths if os.path.exists(p)]


def contains(path, *needles):
    if not os.path.isfile(path):
        return [f"{path} missing"]
    text = read(path)
    return [f"{path} should contain {n!r}; has {text!r}" for n in needles if n not in text]


def lacks(path, *needles):
    if not os.path.isfile(path):
        return [f"{path} missing"]
    text = read(path)
    return [f"{path} should not contain {n!r}; has {text!r}" for n in needles if n in text]


def committed(message=None, commits=2):
    out = []
    if commit_count() != commits:
        out.append(f"expected {commits} commits, found {commit_count()}")
    if message is not None and last_commit_message() != message:
        out.append(f"last commit message {last_commit_message()!r} != {message!r}")
    if not clean_tree():
        out.append(f"tree not clean: {git('status', '--porcelain')!r}")
    return out


CASES = [
    # ---- edit ----
    ("edit/append-line", plain({"notes.txt": "hello\n"}),
     "add a line that says world to notes.txt",
     lambda: contains("notes.txt", "hello", "world")),
    ("edit/change-value", plain({"config.txt": "color=red\nsize=10\n"}),
     "change the color in config.txt to blue",
     lambda: contains("config.txt", "color=blue", "size=10") + lacks("config.txt", "red")),
    ("edit/remove-item", plain({"todo.md": "# Todo\n- buy milk\n- walk dog\n- call mom\n"}),
     "remove the walk dog item from todo.md",
     lambda: contains("todo.md", "buy milk", "call mom") + lacks("todo.md", "walk dog")),
    ("edit/python-return", plain({"app.py": "def greet():\n    return 'hi'\n"}),
     "change greet in app.py so it returns 'hello' instead of 'hi'",
     lambda: contains("app.py", "def greet():", "hello") + lacks("app.py", "'hi'")),
    ("edit/replace-word", plain({"story.txt": "The cat sat on the mat.\n"}),
     "in story.txt replace cat with dog",
     lambda: contains("story.txt", "dog", "mat") + lacks("story.txt", "cat")),
    ("edit/one-of-many-files", plain({"a.txt": "alpha\n", "b.txt": "beta\n", "c.txt": "gamma\n"}),
     "append the line 'delta' to b.txt",
     lambda: contains("b.txt", "beta", "delta") + lacks("a.txt", "delta") + lacks("c.txt", "delta")),

    # ---- rename ----
    ("rename/file", plain({"old.txt": "hello\n"}),
     "rename old.txt to new.txt",
     lambda: missing("old.txt") + exists("new.txt") + contains("new.txt", "hello")),
    ("rename/folder", plain({"draft/": "", "draft/x.txt": "x\n"}),
     "rename the draft folder to final",
     lambda: missing("draft") + exists("final/x.txt")),
    ("rename/change-the-name", plain({"data.csv": "a,b\n"}),
     "change the name of data.csv to results.csv",
     lambda: missing("data.csv") + exists("results.csv")),
    ("rename/extension", plain({"notes.txt": "hi\n"}),
     "rename notes.txt to notes.md",
     lambda: missing("notes.txt") + exists("notes.md")),
    ("rename/among-others", plain({"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"}),
     "rename b.txt to beta.txt",
     lambda: missing("b.txt") + exists("a.txt", "beta.txt", "c.txt")),
    ("rename/nested", plain({"docs/intro.md": "hi\n"}),
     "rename docs/intro.md to docs/overview.md",
     lambda: missing("docs/intro.md") + exists("docs/overview.md")),

    # ---- move ----
    ("move/file-into-folder", plain({"archive/": "", "report.txt": "r\n"}),
     "move report.txt into the archive folder",
     lambda: missing("report.txt") + exists("archive/report.txt")),
    ("move/put-in", plain({"docs/": "", "notes.txt": "n\n"}),
     "put notes.txt in the docs folder",
     lambda: missing("notes.txt") + exists("docs/notes.txt")),
    ("move/folder-into-folder", plain({"assets/": "", "images/logo.png": "png\n"}),
     "move the images folder into assets",
     lambda: missing("images") + exists("assets/images/logo.png")),
    ("move/to-top-level", plain({"docs/guide.md": "g\n"}),
     "move docs/guide.md to the top level of the project",
     lambda: missing("docs/guide.md") + exists("guide.md")),
    ("move/two-files", plain({"backup/": "", "a.txt": "a\n", "b.txt": "b\n"}),
     "move a.txt and b.txt into the backup folder",
     lambda: missing("a.txt", "b.txt") + exists("backup/a.txt", "backup/b.txt")),
    ("move/new-folder", plain({"report.txt": "r\n"}),
     "move report.txt into a new folder called reports",
     lambda: missing("report.txt") + exists("reports/report.txt")),

    # ---- commit ----
    ("commit/with-message", lambda: git_repo({"a.txt": "a\n"}, {"a.txt": "a\nb\n"}),
     "commit the current changes with the message 'update a.txt'",
     lambda: committed("update a.txt")),
    ("commit/everything", lambda: git_repo({"a.txt": "a\n"}, {"a.txt": "a\nb\n", "new.txt": "n\n"}),
     "commit everything",
     lambda: committed()),
    ("commit/stage-and-commit", lambda: git_repo({"a.txt": "a\n"}, {"a.txt": "changed\n"}),
     "stage and commit my changes as 'wip'",
     lambda: committed("wip")),
    ("commit/double-quotes", lambda: git_repo({"a.txt": "a\n"}, {"b.txt": "b\n"}),
     'commit with message "add b.txt"',
     lambda: committed("add b.txt")),
    ("commit/and-push-not-pushed", lambda: git_repo({"a.txt": "a\n"}, {"a.txt": "a\nb\n"}, remote=True),
     "commit the changes with message 'update a' and push them",
     lambda: committed("update a") + (
         ["pushed without /push"] if "update a" in git("log", "origin/main", "-1", "--format=%s") else [])),

    # ---- combinations ----
    ("combo/rename-then-commit", lambda: git_repo({"old.txt": "hello\n"}),
     "rename old.txt to new.txt and commit it with the message 'rename file'",
     lambda: missing("old.txt") + exists("new.txt") + committed("rename file")),
    ("combo/edit-then-commit", lambda: git_repo({"notes.txt": "hello\n"}),
     "add a line saying done to notes.txt, then commit with message 'add done'",
     lambda: contains("notes.txt", "hello", "done") + committed("add done")),
    ("combo/move-then-commit", lambda: git_repo({"report.txt": "r\n", "archive/keep.txt": "k\n"}),
     "move report.txt into archive and commit with message 'archive report'",
     lambda: missing("report.txt") + exists("archive/report.txt") + committed("archive report")),
    # ---- second batch: phrasings written after the pipeline was built ----
    ("edit2/json-value", plain({"package.json": '{\n  "name": "demo",\n  "version": "1.0.0"\n}\n'}),
     "bump the version in package.json to 1.1.0",
     lambda: contains("package.json", '"version": "1.1.0"', '"name": "demo"') + lacks("package.json", "1.0.0")),
    ("edit2/markdown-heading", plain({"README.md": "# Old Title\n\nSome text.\n"}),
     "change the heading in README.md to New Title",
     lambda: contains("README.md", "New Title", "Some text.") + lacks("README.md", "Old Title")),
    ("edit2/add-function", plain({"math_utils.py": "def add(a, b):\n    return a + b\n"}),
     "add a subtract function to math_utils.py",
     lambda: contains("math_utils.py", "def add(a, b):", "def subtract")),
    ("edit2/delete-line", plain({"hosts.txt": "alpha.local\nbeta.local\ngamma.local\n"}),
     "delete the line beta.local from hosts.txt",
     lambda: contains("hosts.txt", "alpha.local", "gamma.local") + lacks("hosts.txt", "beta.local")),
    ("edit2/insert-top", plain({"log.txt": "entry one\nentry two\n"}),
     "insert a line at the top of log.txt that says START",
     lambda: contains("log.txt", "START", "entry one", "entry two")),
    ("edit2/uppercase", plain({"shout.txt": "hello there\n"}),
     "make the text in shout.txt uppercase",
     lambda: contains("shout.txt", "HELLO THERE")),
    ("edit2/readme-stem", plain({"README.md": "# Demo\n", "main.py": "print(1)\n"}),
     "add a line to the readme saying Run main.py to start",
     lambda: contains("README.md", "# Demo", "Run main.py to start")),
    ("edit2/js-const", plain({"config.js": "const PORT = 3000;\nconst HOST = 'localhost';\n"}),
     "set PORT to 8080 in config.js",
     lambda: contains("config.js", "8080", "HOST") + lacks("config.js", "3000")),
    ("rename2/quoted", plain({"my notes.txt": "x\n"}),
     "rename 'my notes.txt' to 'notes.txt'",
     lambda: missing("my notes.txt") + exists("notes.txt")),
    ("rename2/call-it", plain({"tmp.log": "x\n"}),
     "rename tmp.log as debug.log",
     lambda: missing("tmp.log") + exists("debug.log")),
    ("rename2/the-file", plain({"index.htm": "<p>x</p>\n"}),
     "Rename the file index.htm to index.html.",
     lambda: missing("index.htm") + exists("index.html")),
    ("rename2/directory", plain({"src/": "", "src/main.py": "x\n"}),
     "rename the src directory to lib",
     lambda: missing("src") + exists("lib/main.py")),
    ("move2/into-subfolder", plain({"docs/api/": "", "api.md": "x\n"}),
     "move api.md into docs/api",
     lambda: missing("api.md") + exists("docs/api/api.md")),
    ("move2/three-files", plain({"old/": "", "a.log": "a", "b.log": "b", "c.log": "c"}),
     "move a.log, b.log, and c.log into old",
     lambda: missing("a.log", "b.log", "c.log") + exists("old/a.log", "old/b.log", "old/c.log")),
    ("move2/out-of-folder", plain({"tmp/data.csv": "x\n"}),
     "move data.csv out of tmp and into the root folder",
     lambda: missing("tmp/data.csv") + exists("data.csv")),
    ("move2/place", plain({"img/": "", "logo.png": "x"}),
     "place logo.png inside img",
     lambda: missing("logo.png") + exists("img/logo.png")),
    ("move2/capitalized", plain({"Archive/": "", "report.txt": "r\n"}),
     "Move report.txt to the Archive folder",
     lambda: missing("report.txt") + exists("Archive/report.txt")),
    ("commit2/please", lambda: git_repo({"a.txt": "a\n"}, {"a.txt": "b\n"}),
     "please commit my work with the message 'save progress'",
     lambda: committed("save progress")),
    ("commit2/unquoted-message", lambda: git_repo({"a.txt": "a\n"}, {"a.txt": "b\n"}),
     "commit with message fix typo",
     lambda: committed("fix typo")),
    ("commit2/git-commit", lambda: git_repo({"a.txt": "a\n"}, {"c.txt": "c\n"}),
     "git commit -m 'add c'",
     lambda: committed("add c")),
    ("commit2/add-and-commit", lambda: git_repo({"a.txt": "a\n"}, {"a.txt": "z\n"}),
     "add and commit everything as 'checkpoint'",
     lambda: committed("checkpoint")),
    ("combo2/move-rename-commit", lambda: git_repo({"draft.md": "d\n", "posts/keep.md": "k\n"}),
     "move draft.md into posts, rename it to first-post.md, and commit with message 'publish'",
     lambda: missing("draft.md", "posts/draft.md") + exists("posts/first-post.md") + committed("publish")),
    ("combo2/create-edit-commit", lambda: git_repo({"a.txt": "a\n"}),
     "create a file called todo.txt that says buy milk, then commit it with message 'add todo'",
     lambda: contains("todo.txt", "buy milk") + committed("add todo")),
    ("combo2/rename-edit", plain({"hello.py": "print('hi')\n"}),
     "rename hello.py to greet.py and change it to print hello world",
     lambda: missing("hello.py") + contains("greet.py", "hello world")),
    ("combo2/folder-move", plain({"a.txt": "a\n", "b.txt": "b\n"}),
     "make a folder called texts and move a.txt and b.txt into it",
     lambda: missing("a.txt", "b.txt") + exists("texts/a.txt", "texts/b.txt")),
    # ---- third batch: model-dependent edits and planner fallbacks ----
    ("edit3/add-comment", plain({"calc.py": "def area(r):\n    return 3.14 * r * r\n"}),
     "add a comment above the area function in calc.py explaining it computes a circle's area",
     lambda: contains("calc.py", "#", "def area(r):", "return 3.14 * r * r")),
    ("edit3/css-color", plain({"style.css": "body {\n  color: black;\n  margin: 0;\n}\n"}),
     "make the body text red in style.css",
     lambda: contains("style.css", "red", "margin: 0;") + lacks("style.css", "black")),
    ("edit3/html-title", plain({"index.html": "<html>\n<head>\n<title>Home</title>\n</head>\n<body></body>\n</html>\n"}),
     "change the page title in index.html to Welcome",
     lambda: contains("index.html", "<title>Welcome</title>", "<body></body>") + lacks("index.html", "Home")),
    ("edit3/add-bullet", plain({"todo.md": "# Todo\n- buy milk\n"}),
     "add 'water plants' to the list in todo.md",
     lambda: contains("todo.md", "buy milk", "water plants")),
    ("edit3/large-file", plain({"helpers.py": "def helper_0(x):\n    return x + 0\n\ndef helper_1(x):\n    return x + 1\n\ndef helper_2(x):\n    return x + 2\n\ndef helper_3(x):\n    return x + 3\n\ndef helper_4(x):\n    return x + 4\n\ndef helper_5(x):\n    return x + 5\n\ndef helper_6(x):\n    return x + 6\n\ndef helper_7(x):\n    return x + 7\n\ndef helper_8(x):\n    return x + 8\n\ndef helper_9(x):\n    return x + 9\n\ndef helper_10(x):\n    return x + 10\n\ndef helper_11(x):\n    return x + 11\n\ndef helper_12(x):\n    return x + 12\n\ndef helper_13(x):\n    return x + 13\n\ndef helper_14(x):\n    return x + 14\n\ndef helper_15(x):\n    return x + 15\n\ndef helper_16(x):\n    return x + 16\n\ndef helper_17(x):\n    return x + 17\n\ndef helper_18(x):\n    return x + 18\n\ndef helper_19(x):\n    return x + 19\n\ndef helper_20(x):\n    return x + 20\n\ndef helper_21(x):\n    return x + 21\n\ndef helper_22(x):\n    return x + 22\n\ndef helper_23(x):\n    return x + 23\n\ndef helper_24(x):\n    return x + 24\n\ndef helper_25(x):\n    return x + 25\n\ndef helper_26(x):\n    return x + 26\n\ndef helper_27(x):\n    return x + 27\n\ndef helper_28(x):\n    return x + 28\n\ndef helper_29(x):\n    return x + 29\n\ndef helper_30(x):\n    return x + 30\n\ndef helper_31(x):\n    return x + 31\n\ndef helper_32(x):\n    return x + 32\n\ndef helper_33(x):\n    return x + 33\n\ndef helper_34(x):\n    return x + 34\n\ndef helper_35(x):\n    return x + 35\n\ndef helper_36(x):\n    return x + 36\n\ndef helper_37(x):\n    return x + 37\n\ndef helper_38(x):\n    return x + 38\n\ndef helper_39(x):\n    return x + 39\n\ndef helper_40(x):\n    return x + 40\n\ndef helper_41(x):\n    return x + 41\n\ndef helper_42(x):\n    return x + 42\n\ndef helper_43(x):\n    return x + 43\n\ndef helper_44(x):\n    return x + 44\n\ndef helper_45(x):\n    return x + 45\n\ndef helper_46(x):\n    return x + 46\n\ndef helper_47(x):\n    return x + 47\n\ndef helper_48(x):\n    return x + 48\n\ndef helper_49(x):\n    return x + 49\n\ndef helper_50(x):\n    return x + 50\n\ndef helper_51(x):\n    return x + 51\n\ndef helper_52(x):\n    return x + 52\n\ndef helper_53(x):\n    return x + 53\n\ndef helper_54(x):\n    return x + 54\n\ndef helper_55(x):\n    return x + 55\n\ndef helper_56(x):\n    return x + 56\n\ndef helper_57(x):\n    return x + 57\n\ndef helper_58(x):\n    return x + 58\n\ndef helper_59(x):\n    return x + 59\n\ndef helper_60(x):\n    return x + 60\n\ndef helper_61(x):\n    return x + 61\n\ndef helper_62(x):\n    return x + 62\n\ndef helper_63(x):\n    return x + 63\n\ndef helper_64(x):\n    return x + 64\n\ndef helper_65(x):\n    return x + 65\n\ndef helper_66(x):\n    return x + 66\n\ndef helper_67(x):\n    return x + 67\n\ndef helper_68(x):\n    return x + 68\n\ndef helper_69(x):\n    return x + 69\n\ndef helper_70(x):\n    return x + 70\n\ndef helper_71(x):\n    return x + 71\n\ndef helper_72(x):\n    return x + 72\n\ndef helper_73(x):\n    return x + 73\n\ndef helper_74(x):\n    return x + 74\n\ndef helper_75(x):\n    return x + 75\n\ndef helper_76(x):\n    return x + 76\n\ndef helper_77(x):\n    return x + 77\n\ndef helper_78(x):\n    return x + 78\n\ndef helper_79(x):\n    return x + 79\n\ndef helper_80(x):\n    return x + 80\n\ndef helper_81(x):\n    return x + 81\n\ndef helper_82(x):\n    return x + 82\n\ndef helper_83(x):\n    return x + 83\n\ndef helper_84(x):\n    return x + 84\n\ndef helper_85(x):\n    return x + 85\n\ndef helper_86(x):\n    return x + 86\n\ndef helper_87(x):\n    return x + 87\n\ndef helper_88(x):\n    return x + 88\n\ndef helper_89(x):\n    return x + 89\n\ndef helper_90(x):\n    return x + 90\n\ndef helper_91(x):\n    return x + 91\n\ndef helper_92(x):\n    return x + 92\n\ndef helper_93(x):\n    return x + 93\n\ndef helper_94(x):\n    return x + 94\n\ndef helper_95(x):\n    return x + 95\n\ndef helper_96(x):\n    return x + 96\n\ndef helper_97(x):\n    return x + 97\n\ndef helper_98(x):\n    return x + 98\n\ndef helper_99(x):\n    return x + 99\n\ndef helper_100(x):\n    return x + 100\n\ndef helper_101(x):\n    return x + 101\n\ndef helper_102(x):\n    return x + 102\n\ndef helper_103(x):\n    return x + 103\n\ndef helper_104(x):\n    return x + 104\n\ndef helper_105(x):\n    return x + 105\n\ndef helper_106(x):\n    return x + 106\n\ndef helper_107(x):\n    return x + 107\n\ndef helper_108(x):\n    return x + 108\n\ndef helper_109(x):\n    return x + 109\n\ndef helper_110(x):\n    return x + 110\n\ndef helper_111(x):\n    return x + 111\n\ndef helper_112(x):\n    return x + 112\n\ndef helper_113(x):\n    return x + 113\n\ndef helper_114(x):\n    return x + 114\n\ndef helper_115(x):\n    return x + 115\n\ndef helper_116(x):\n    return x + 116\n\ndef helper_117(x):\n    return x + 117\n\ndef helper_118(x):\n    return x + 118\n\ndef helper_119(x):\n    return x + 119\n\n"}),
     "change helper_57 in helpers.py so it returns x * 57",
     lambda: contains("helpers.py", "x * 57", "def helper_56(x):", "return x + 56", "def helper_119(x):")
     + lacks("helpers.py", "return x + 57\n")),
    ("edit3/env", plain({".env.example": "", "settings.ini": "[server]\nport = 80\ndebug = false\n"}),
     "turn debug on in settings.ini",
     lambda: contains("settings.ini", "port = 80", "true") + lacks("settings.ini", "debug = false")),
    ("plan3/organize", plain({"a.jpg": "x", "b.jpg": "y", "notes.txt": "n\n"}),
     "put the jpg files into a folder called photos",
     lambda: missing("a.jpg", "b.jpg") + exists("photos/a.jpg", "photos/b.jpg", "notes.txt")),
    ("plan3/save-work", lambda: git_repo({"a.txt": "a\n"}, {"a.txt": "b\n"}),
     "save my work to git with a note saying 'daily save'",
     lambda: committed("daily save")),
    ("plan3/new-readme", plain({"main.py": "print('hi')\n"}),
     "write a README.md that says this project prints hi",
     lambda: contains("README.md", "hi")),
    # ---- fourth batch ----
    ("edit4/two-changes", plain({"app.cfg": "name=demo\nport=80\nmode=dev\n"}),
     "in app.cfg set port to 443 and mode to prod",
     lambda: contains("app.cfg", "name=demo", "443", "prod") + lacks("app.cfg", "port=80", "dev")),
    ("edit4/fix-typo", plain({"intro.txt": "Welcom to the project.\n"}),
     "fix the spelling mistake in intro.txt",
     lambda: contains("intro.txt", "Welcome to the project.")),
    ("edit4/rename-variable", plain({"calc.js": "let total = 0;\ntotal += 5;\nconsole.log(total);\n"}),
     "rename the variable total to sum in calc.js",
     lambda: contains("calc.js", "let sum = 0;", "sum += 5;", "console.log(sum);") + lacks("calc.js", "total")),
    ("edit4/append-end", plain({"shopping.txt": "eggs\nbread\n"}),
     "add cheese to the end of shopping.txt",
     lambda: contains("shopping.txt", "eggs", "bread", "cheese")),
    ("edit4/remove-function", plain({"util.py": "def keep():\n    return 1\n\n\ndef drop():\n    return 2\n"}),
     "remove the drop function from util.py",
     lambda: contains("util.py", "def keep():", "return 1") + lacks("util.py", "def drop")),
    ("rename4/no-ext-dest", plain({"notes": "x\n"}),
     "rename notes to notes.txt",
     lambda: missing("notes") + exists("notes.txt")),
    ("move4/nested-new", plain({"a.txt": "a\n"}),
     "move a.txt into archive/2024",
     lambda: missing("a.txt") + exists("archive/2024/a.txt")),
    ("move4/folder-to-root", plain({"src/lib/": "", "src/lib/x.py": "x\n"}),
     "move src/lib to the root",
     lambda: missing("src/lib") + exists("lib/x.py")),
    ("commit4/then-push-branch", lambda: git_repo({"a.txt": "a\n"}, {"a.txt": "b\n"}),
     "commit with message 'wip', then create a branch called experiment",
     lambda: committed("wip")),
    ("commit4/clean-tree", lambda: git_repo({"a.txt": "a\n"}),
     "commit my changes",
     lambda: (["commit made on clean tree"] if commit_count() != 1 else [])),
    ("combo4/edit-two-files-commit", lambda: git_repo({"a.txt": "a\n", "b.txt": "b\n"}),
     "add a line saying one to a.txt, add a line saying two to b.txt, and commit as 'both'",
     lambda: contains("a.txt", "a", "one") + contains("b.txt", "b", "two") + committed("both")),
    ("plan3/belong-in", plain({"a.md": "x\n", "b.md": "y\n", "main.py": "p\n"}),
     "tidy up: all the markdown files belong in docs",
     lambda: missing("a.md", "b.md") + exists("docs/a.md", "docs/b.md", "main.py")),
    # ---- fifth batch: paraphrases only the model planner can read ----
    ("plan5/should-be-called", plain({"config.toml": "x\n"}),
     "config.toml should be called settings.toml",
     lambda: missing("config.toml") + exists("settings.toml")),
    ("plan5/get-out-of", plain({"tmp/data.csv": "x\n", "tmp/keep.txt": "k\n"}),
     "get data.csv out of the tmp folder",
     lambda: missing("tmp/data.csv") + exists("data.csv", "tmp/keep.txt")),
    ("plan5/group-by-type", plain({"a.png": "x", "b.png": "y", "c.txt": "z"}),
     "group the png images into an images folder",
     lambda: missing("a.png", "b.png") + exists("images/a.png", "images/b.png", "c.txt")),
    ("plan5/needs-new-name", plain({"untitled.txt": "x\n"}),
     "untitled.txt needs a better name: call it ideas.txt",
     lambda: missing("untitled.txt") + exists("ideas.txt")),
    ("plan5/record-changes", lambda: git_repo({"a.txt": "a\n"}, {"a.txt": "b\n"}),
     "record these changes in git as 'snapshot 1'",
     lambda: committed("snapshot 1")),
    # ---- sixth batch: written after all tuning, run untuned ----
    ("b6/edit-add-import", plain({"main.py": "def run():\n    print(os.getcwd())\n"}),
     "add an import os line at the top of main.py",
     lambda: contains("main.py", "import os", "def run():", "print(os.getcwd())")),
    ("b6/edit-change-greeting", plain({"hello.txt": "Hello, World!\n"}),
     "change World to Universe in hello.txt",
     lambda: contains("hello.txt", "Hello, Universe!") + lacks("hello.txt", "World")),
    ("b6/edit-remove-debug", plain({"app.js": "const x = 1;\nconsole.log('debug');\nexport default x;\n"}),
     "remove the console.log line from app.js",
     lambda: contains("app.js", "const x = 1;", "export default x;") + lacks("app.js", "console.log")),
    ("b6/edit-yaml", plain({"config.yml": "name: demo\nreplicas: 1\n"}),
     "set replicas to 3 in config.yml",
     lambda: contains("config.yml", "name: demo", "replicas: 3") + lacks("config.yml", "replicas: 1")),
    ("b6/edit-append-sentence", plain({"bio.md": "# About\n\nI write code.\n"}),
     "append a sentence to bio.md saying I also like hiking",
     lambda: contains("bio.md", "# About", "I write code.", "hiking")),
    ("b6/rename-capital", plain({"Readme.txt": "x\n"}),
     "rename Readme.txt to README.md",
     lambda: missing("Readme.txt") + exists("README.md")),
    ("b6/rename-dir-quoted", plain({"old stuff/a.txt": "a\n"}),
     'rename the "old stuff" folder to "archive"',
     lambda: missing("old stuff") + exists("archive/a.txt")),
    ("b6/rename-should-be", plain({"index.jsx": "x\n"}),
     "index.jsx should be renamed to index.tsx",
     lambda: missing("index.jsx") + exists("index.tsx")),
    ("b6/move-into-nested-existing", plain({"src/components/": "", "Button.tsx": "x\n"}),
     "move Button.tsx into src/components",
     lambda: missing("Button.tsx") + exists("src/components/Button.tsx")),
    ("b6/move-relocate", plain({"notes/": "", "meeting.txt": "m\n"}),
     "relocate meeting.txt to the notes folder",
     lambda: missing("meeting.txt") + exists("notes/meeting.txt")),
    ("b6/move-should-go", plain({"lib/": "", "utils.py": "u\n"}),
     "utils.py should go in lib",
     lambda: missing("utils.py") + exists("lib/utils.py")),
    ("b6/commit-simple-message", lambda: git_repo({"a.txt": "a\n"}, {"a.txt": "b\n"}),
     "commit this as 'first draft'",
     lambda: committed("first draft")),
    ("b6/commit-colon-message", lambda: git_repo({"a.txt": "a\n"}, {"a.txt": "b\n"}),
     "commit with the message: update docs",
     lambda: committed("update docs")),
    ("b6/combo-rename-move-commit", lambda: git_repo({"tmp.txt": "t\n", "docs/k.md": "k\n"}),
     "rename tmp.txt to notes.txt, then move notes.txt into docs and commit as 'organize'",
     lambda: missing("tmp.txt", "notes.txt") + exists("docs/notes.txt") + committed("organize")),
    ("b6/combo-edit-commit-please", lambda: git_repo({"todo.txt": "- a\n"}),
     "please add a line saying - b to todo.txt and commit it with the message 'more todos'",
     lambda: contains("todo.txt", "- a", "- b") + committed("more todos")),
    ("combo/create-folder-and-file", plain({}),
     'create a folder named "test" with the file "path.txt" inside it',
     lambda: (["test is not a directory"] if not os.path.isdir("test") else []) + exists("test/path.txt")),
]


def sync_call_from_thread(fn, *args, **kwargs):
    return fn(*args, **kwargs)


async def run_case(name, setup, task, check) -> tuple[list[str], list[str], float]:
    cwd = tempfile.mkdtemp(prefix="fm-pcc-reliability-")
    orig = os.getcwd()
    os.chdir(cwd)
    log: list[str] = []
    start = time.monotonic()
    try:
        setup()
        app = m.ChatApp()
        app.subagent_roles["planning"] = "on-device"
        app.subagent_roles["building"] = "on-device"
        async with app.run_test():
            orig_log = app._log_progress

            def capture(text):
                log.append(text)
                orig_log(text)

            with mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread), \
                 mock.patch.object(app, "_log_progress", side_effect=capture), \
                 mock.patch.object(m, "notify"):
                app._run_task.__wrapped__(app, task)
        failures = check()
        failures += [f"task reported: {line}" for line in log if line.startswith(("error", "stopped"))]
    except Exception as e:
        failures = [f"raised {type(e).__name__}: {e}"]
    finally:
        os.chdir(orig)
        shutil.rmtree(cwd, ignore_errors=True)
    return failures, log, time.monotonic() - start


async def main() -> int:
    if not on_device_available():
        print("SKIPPED: on-device model isn't available here")
        return 0

    filters = sys.argv[1:]
    cases = [c for c in CASES if not filters or any(f in c[0] for f in filters)]
    failed = []
    for name, setup, task, check in cases:
        failures, log, elapsed = await run_case(name, setup, task, check)
        status = "ok  " if not failures else "FAIL"
        print(f"{status} {name} ({elapsed:.0f}s): {task}")
        if failures:
            failed.append(name)
            for f in failures:
                print(f"       - {f}")
            for line in log:
                print(f"       | {line}")

    print(f"\n{len(cases) - len(failed)}/{len(cases)} cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
