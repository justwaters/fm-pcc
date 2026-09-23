"""Offline unit tests for fm_pcc.taskplan: the deterministic parser, the
model-plan normalizer, and the edit checks. No model calls."""
import sys
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
from fm_pcc import taskplan as t


def plan(task, files=(), folders=()):
    return [
        {k: v for k, v in s.items() if v not in ("", None) and k not in ("files", "folders")}
        for s in t.parse_task(task, list(files), list(folders))
    ]


def eq(got, want):
    assert got == want, f"\n got: {got}\nwant: {want}"


# ---- rename ----
eq(plan("rename old.txt to new.txt", ["old.txt"]),
   [{"action": "RENAME", "path": "old.txt", "destination": "new.txt"}])
eq(plan("rename the draft folder to final", ["draft/x.txt"], ["draft"]),
   [{"action": "RENAME", "path": "draft", "destination": "final"}])
eq(plan("change the name of data.csv to results.csv", ["data.csv"]),
   [{"action": "RENAME", "path": "data.csv", "destination": "results.csv"}])
eq(plan("rename docs/intro.md to overview.md", ["docs/intro.md"], ["docs"]),
   [{"action": "RENAME", "path": "docs/intro.md", "destination": "docs/overview.md"}])
eq(plan("rename 'my notes.txt' to 'notes.txt'", ["my notes.txt"]),
   [{"action": "RENAME", "path": "my notes.txt", "destination": "notes.txt"}])
eq(plan("Rename the file index.htm to index.html.", ["index.htm"]),
   [{"action": "RENAME", "path": "index.htm", "destination": "index.html"}])
eq(plan("rename missing.txt to x.txt", ["old.txt"])[0]["action"], "UNPARSED")
print("rename OK")

# ---- move ----
eq(plan("move report.txt into the archive folder", ["report.txt"], ["archive"]),
   [{"action": "MOVE", "path": "report.txt", "destination": "archive/report.txt"}])
eq(plan("move the images folder into assets", ["images/logo.png"], ["assets", "images"]),
   [{"action": "MOVE", "path": "images", "destination": "assets/images"}])
eq(plan("move docs/guide.md to the top level of the project", ["docs/guide.md"], ["docs"]),
   [{"action": "MOVE", "path": "docs/guide.md", "destination": "guide.md"}])
eq(plan("move a.log, b.log, and c.log into old", ["a.log", "b.log", "c.log"], ["old"]),
   [{"action": "MOVE", "path": f"{n}.log", "destination": f"old/{n}.log"} for n in "abc"])
eq(plan("move report.txt into a new folder called reports", ["report.txt"]),
   [{"action": "CREATE_FOLDER", "path": "reports"},
    {"action": "MOVE", "path": "report.txt", "destination": "reports/report.txt"}])
eq(plan("move notes.txt to docs/renamed.txt", ["notes.txt"], ["docs"]),
   [{"action": "MOVE", "path": "notes.txt", "destination": "docs/renamed.txt"}])
eq(plan("make a folder called texts and move a.txt and b.txt into it", ["a.txt", "b.txt"]),
   [{"action": "CREATE_FOLDER", "path": "texts"},
    {"action": "MOVE", "path": "a.txt", "destination": "texts/a.txt"},
    {"action": "MOVE", "path": "b.txt", "destination": "texts/b.txt"}])
print("move OK")

# ---- commit / git ----
eq(plan("commit the current changes with the message 'update a.txt'"),
   [{"action": "COMMIT", "details": "update a.txt"}])
eq(plan("commit everything"), [{"action": "COMMIT"}])
eq(plan("stage and commit my changes as 'wip'"), [{"action": "STAGE"}, {"action": "COMMIT", "details": "wip"}])
eq(plan('commit with message "add b.txt and push"'), [{"action": "COMMIT", "details": "add b.txt and push"}])
eq(plan("commit with message fix typo"), [{"action": "COMMIT", "details": "fix typo"}])
eq(plan("git commit -m 'add c'"), [{"action": "COMMIT", "details": "add c"}])
eq(plan("commit the changes with message 'update a' and push them"),
   [{"action": "COMMIT", "details": "update a"}, {"action": "PUSH"}])
eq(plan("create a new git branch named feature-x and switch to it"),
   [{"action": "BRANCH_CREATE", "path": "feature-x"}])
eq(plan("switch to the develop branch"), [{"action": "BRANCH_SWITCH", "path": "develop"}])
eq(plan("pull"), [{"action": "PULL"}])
print("git OK")

# ---- create ----
eq(plan('create a folder named "test" with the file "path.txt" inside it'),
   [{"action": "CREATE_FOLDER", "path": "test"}, {"action": "CREATE_FILE", "path": "test/path.txt"}])
eq(plan("create an empty folder named assets"), [{"action": "CREATE_FOLDER", "path": "assets"}])
eq(plan("create a file named notes.txt containing the word hello"),
   [{"action": "CREATE_FILE", "path": "notes.txt", "details": "containing the word hello"}])
print("create OK")

# ---- edits and combinations ----
eq(plan("add a line that says world to notes.txt", ["notes.txt"]),
   [{"action": "EDIT", "path": "notes.txt", "details": "add a line that says world to notes.txt"}])
eq(plan("fix the typo in the readme", ["README.md", "main.py"])[0]["path"], "README.md")
eq(plan("rename hello.py to greet.py and change it to print hello world", ["hello.py"]),
   [{"action": "RENAME", "path": "hello.py", "destination": "greet.py"},
    {"action": "EDIT", "path": "greet.py", "details": "change it to print hello world"}])
eq(plan("in config.txt change color to blue and set size to 20", ["config.txt"]),
   [{"action": "EDIT", "path": "config.txt", "details": "in config.txt change color to blue and set size to 20"}])
eq(plan("move draft.md into posts, rename it to first-post.md, and commit with message 'publish'",
        ["draft.md"], ["posts"]),
   [{"action": "MOVE", "path": "draft.md", "destination": "posts/draft.md"},
    {"action": "RENAME", "path": "posts/draft.md", "destination": "posts/first-post.md"},
    {"action": "COMMIT", "details": "publish"}])
eq(plan("delete notes.txt", ["notes.txt"])[0]["action"], "UNSUPPORTED")
eq(plan("tidy things up", ["a.txt"])[0]["action"], "UNPARSED")
print("edit/combo OK")

# ---- normalizing a model plan ----
norm = t.normalize_model_steps(
    [{"action": "EDIT", "path": "data.csv", "destination": "", "details": "rename"},
     {"action": "MOVE", "path": "data.csv", "destination": "results.csv", "details": ""},
     {"action": "COMMIT", "path": "", "destination": "", "details": "x"}],
    "change the name of data.csv to results.csv", ["data.csv"], [],
)
eq([s["action"] for s in norm], ["MOVE"])  # redundant EDIT + unrequested COMMIT dropped
norm = t.normalize_model_steps(
    [{"action": "CREATE_FOLDER", "path": "docs", "destination": "", "details": ""},
     {"action": "MOVE", "path": "notes.txt", "destination": "docs", "details": ""}],
    "put notes.txt in the docs folder", ["notes.txt"], ["docs"],
)
eq([(s["action"], s["destination"]) for s in norm], [("MOVE", "docs/notes.txt")])
norm = t.normalize_model_steps(
    [{"action": "COMMIT", "path": "", "destination": "", "details": "stage and commit 'wip' DONE"}],
    "tidy up and commit as 'wip'", [], [],
)
eq(norm[0]["details"], "wip")
norm = t.normalize_model_steps(
    [{"action": "CREATE_FOLDER", "path": ".", "destination": "docs", "details": ""},
     {"action": "MOVE", "path": "a.md", "destination": "docs/a.md", "details": ""}],
    "all the markdown files belong in docs", ["a.md"], [],
)
eq([(s["action"], s["path"]) for s in norm], [("CREATE_FOLDER", "docs"), ("MOVE", "a.md")])
norm = t.normalize_model_steps(  # folder the model forgot to create
    [{"action": "MOVE", "path": "a.md", "destination": "docs/a.md", "details": ""}],
    "a.md belongs in docs", ["a.md"], [],
)
eq([(s["action"], s["path"]) for s in norm], [("CREATE_FOLDER", "docs"), ("MOVE", "a.md")])
print("normalize OK")

# ---- edit checks ----
eq(t.check_edit("add a line that says world", "hello\n", "hello\nworld\n"), [])
assert t.check_edit("add a line that says world", "hello\n", "world\n")  # dropped hello
assert t.check_edit("add a docstring to main", "x\n", "x\n")  # unchanged
eq(t.check_edit("change the color to blue", "color=red\nsize=10\n", "color=blue\nsize=10\n"), [])
assert t.check_edit("replace cat with dog", "The cat sat.\n", "The cat sat.\nThe dog sat.\n")
eq(t.check_edit("delete the line beta.local from hosts.txt", "a\nbeta.local\nc\n", "a\nc\n"), [])
eq(t.check_edit("change it to print hello world", "print('hi')\n", "print('hello world')\n"), [])
print("check_edit OK")

# ---- literal edits ----
eq(t.literal_edit("delete the line beta.local from hosts.txt", "alpha.local\nbeta.local\ngamma.local\n"),
   "alpha.local\ngamma.local\n")
eq(t.literal_edit("remove the walk dog item from todo.md", "# Todo\n- buy milk\n- walk dog\n"),
   "# Todo\n- buy milk\n")
eq(t.literal_edit("in story.txt replace cat with dog", "The cat sat on the mat.\n"),
   "The dog sat on the mat.\n")
eq(t.literal_edit("make it return 'hello' instead of 'hi'", "return 'hi'\n"), "return 'hello'\n")
eq(t.literal_edit("remove the word very from a.txt", "It is very good.\n"), "It is good.\n")
eq(t.literal_edit("add a line saying x", "a\n"), None)
eq(t.literal_edit("remove the typo", "a\n"), None)  # names nothing literally present
print("literal_edit OK")

# ---- layout restoration (model collapsed a CSS rule onto one line) ----
css = "body {\n  color: black;\n  margin: 0;\n}"
assert t.looks_reflowed(css, "body { color: red; margin: 0; }")
eq(t.restore_layout(css, "body { color: red; margin: 0; }"), "body {\n  color: red;\n  margin: 0;\n}")
eq(t.restore_layout("def f():\n    a = 1\n    b = 2\n    return a\n", "def f(): a = 1 return a"),
   "def f():\n    a = 1\n    return a\n")
assert not t.looks_reflowed("a\nb\n", "a\nb\nc\n")
print("restore_layout OK")

# ---- "save to git" is a commit, never an edit ----
eq(plan("save my work to git with a note saying 'daily save'", ["a.txt"]),
   [{"action": "COMMIT", "details": "daily save"}])
eq(t.normalize_model_steps(
    [{"action": "EDIT", "path": "a.txt", "destination": "", "details": "save to git"}],
    "save my work to git", ["a.txt"], []), [])
print("save-to-git OK")

# ---- key/value assignments ----
eq(t.literal_edit("in app.cfg set port to 443 and mode to prod", "name=demo\nport=80\nmode=dev\n"),
   "name=demo\nport=443\nmode=prod\n")
eq(t.literal_edit("bump the version in package.json to 1.1.0", '{\n  "name": "d",\n  "version": "1.0.0"\n}\n'),
   '{\n  "name": "d",\n  "version": "1.1.0"\n}\n')
eq(t.literal_edit("set PORT to 8080 in config.js", "const PORT = 3000;\nconst HOST = 'x';\n"),
   "const PORT = 8080;\nconst HOST = 'x';\n")
eq(t.literal_edit("set debug: true in settings.yml", "debug: false\n"), None)  # not a "K to V" pair
assert t.check_edit("set port to 443 and mode to prod", "port=80\nmode=dev\n", "port=80\nmode=dev\nport=443\nmode=prod\n")
print("assignments OK")

# ---- removing a named function/class ----
eq(t.literal_edit("remove the drop function from util.py",
                  "def keep():\n    return 1\n\n\ndef drop():\n    return 2\n"),
   "def keep():\n    return 1\n")
eq(t.literal_edit("delete the function drop",
                  "def drop():\n    return 2\n\n\n@cache\ndef keep():\n    return 1\n"),
   "@cache\ndef keep():\n    return 1\n")
eq(t.literal_edit("remove the drop function",
                  "@route\ndef drop():\n    return 2\n\ndef keep():\n    return 1\n"),
   "def keep():\n    return 1\n")
eq(t.literal_edit("remove the helper function from app.js",
                  "function main() {\n  helper();\n}\n\nfunction helper() {\n  if (x) {\n    y();\n  }\n}\n"),
   "function main() {\n  helper();\n}\n")
eq(t.literal_edit("remove the Foo class", "class Foo:\n    pass\nclass Foo2:\n    pass\n"),
   "class Foo2:\n    pass\n")
eq(t.literal_edit("remove the missing function", "def a():\n    pass\n"), None)
print("remove block OK")

# ---- case conversion ----
eq(t.literal_edit("make the text in shout.txt uppercase", "hello there\n"), "HELLO THERE\n")
eq(t.literal_edit("convert everything to lower case", "Hi There\n"), "hi there\n")
eq(t.literal_edit("uppercase the contents of a.txt", "abc\n"), "ABC\n")
eq(t.literal_edit("make the heading uppercase", "# hi\ntext\n"), None)  # not the whole file
print("case OK")

# ---- declarative rename/move phrasings ----
eq(plan("config.toml should be called settings.toml", ["config.toml"]),
   [{"action": "RENAME", "path": "config.toml", "destination": "settings.toml"}])
eq(plan("untitled.txt needs a better name: call it ideas.txt", ["untitled.txt"]),
   [{"action": "RENAME", "path": "untitled.txt", "destination": "ideas.txt"}])
eq(plan("i want main.js to be named app.js", ["main.js"]),
   [{"action": "RENAME", "path": "main.js", "destination": "app.js"}])
eq(plan("logo.svg belongs in assets", ["logo.svg"], ["assets"]),
   [{"action": "MOVE", "path": "logo.svg", "destination": "assets/logo.svg"}])
eq(plan("get data.csv out of the tmp folder", ["tmp/data.csv"], ["tmp"]),
   [{"action": "MOVE", "path": "tmp/data.csv", "destination": "data.csv"}])
eq(plan("record these changes in git as 'snapshot 1'"), [{"action": "COMMIT", "details": "snapshot 1"}])
eq(plan("make a folder called texts", ["a.txt"]), [{"action": "CREATE_FOLDER", "path": "texts"}])
eq(t.resolve_existing("a", ["a.txt"]), None)  # a 1-letter word isn't a filename stem
eq(plan("please commit my work with the message 'save progress'"), [{"action": "COMMIT", "details": "save progress"}])
eq(plan("can you rename old.txt to new.txt for me, thanks", ["old.txt"]),
   [{"action": "RENAME", "path": "old.txt", "destination": "new.txt"}])
print("declaratives OK")

# ---- repairing confused model plans ----
def norm_(steps, request, files, folders=()):
    return [(s["action"], s["path"], s["destination"]) for s in t.normalize_model_steps(
        [{"action": a, "path": p, "destination": d, "details": x} for a, p, d, x in steps],
        request, list(files), list(folders))]

eq(norm_([("CREATE_FILE", "app.js", "", "main.js"), ("MOVE", "app.js", "main.js", "")],
         "i want main.js to be named app.js", ["main.js"]), [("RENAME", "main.js", "app.js")])
eq(norm_([("CREATE_FILE", "ideas.txt", "", ""), ("MOVE", "ideas.txt", "ideas.txt", "")],
         "untitled.txt needs a better name: call it ideas.txt", ["untitled.txt"]),
   [("RENAME", "untitled.txt", "ideas.txt")])
eq(norm_([("EDIT", "main.js", "", "rename to app.js")], "i want main.js to be named app.js", ["main.js"]),
   [("RENAME", "main.js", "app.js")])
eq(norm_([("CREATE_FILE", "notes.md", "", "")], "create notes.md next to a.txt", ["a.txt"]),
   [("CREATE_FILE", "notes.md", "")])  # real creation wording: stays a create
print("plan repairs OK")

# ---- "change A to B" with A literally in the file ----
eq(t.literal_edit("change World to Universe in hello.txt", "Hello, World!\n"), "Hello, Universe!\n")
assert t.check_edit("change World to Universe", "Hello, World!\n", "Hello, World!\nHello, Universe!\n")
eq(t.literal_edit("change the color in config.txt to blue", "color=red\n"), "color=blue\n")  # key: value changes
eq(t.literal_edit("change the heading to New Title", "# Old Title\n"), None)  # not literal: model's job
print("literal change OK")

# ---- theme recoloring ----
css = ":root {\n\t--blue-dark: #00296b;\n\t--yellow-main: #fdc500;\n\t--bg: #ffffff;\n}\n" \
      "header { background: var(--blue-dark); box-shadow: 0 4px 20px rgba(0, 41, 107, 0.08); }\n"
assert t.is_theme_request("make the ui a green and yellow theme")
assert not t.is_theme_request("change the header text to Welcome")
out = t.recolor_theme(css, "make the ui a green and yellow theme")
assert "--blue-dark: #006b24;" in out, out            # dark blue -> dark green (lightness kept)
assert "--yellow-main: #fdc500;" in out, out          # already yellow: untouched
assert "--bg: #ffffff;" in out and "rgba(0, 107, 36, 0.08)" in out, out
assert t.recolor_theme("a { color: #333; }", "make it green") is None  # nothing chromatic
assert t.color_matches_word("#006b24", "green") and not t.color_matches_word("#00296b", "green")
print("theme recolor OK")

# ---- structure / relevance / whitespace checks ----
assert t.check_structure("a.css", "x", "a {\n}\n/* c */\n", "a {\n}\n/* c\n")          # unclosed comment
assert t.check_structure("a.css", "x", ":root { --a: #fff; }", ":root { --b: #fff; }",
                         whole_file=":root { --a: #fff; } p { color: var(--a); }")          # used var renamed
assert t.check_structure("a.html", "x", "<div><p>hi</p></div>", "<div><p>hi</p></body></div>")
eq(t.check_structure("a.html", "add a footer", "<body>\n</body>", "<body>\n<footer>f</footer>\n</body>"), [])
eq(t.match_indentation("a {\n\tb: 1;\n}", "a {\n    b: 2;\n}"), "a {\n\tb: 2;\n}")
assert t.check_edit("make it green", "<p>\n  hi\n</p>\n", "<p>\nhi\n\n</p>\n")  # whitespace only
assert t.check_relevant("change the background color to green", "<p>hi</p>", "<p>hi</p></body>")
eq(t.check_relevant("change the background color to green", "p {}", "p { background: #0a0; }"), [])
print("structure checks OK")

# ---- planner edits of files the request didn't name ----
norm = t.normalize_model_steps(
    [{"action": "EDIT", "path": "styles/liquidglass.css", "destination": "", "details": "green"},
     {"action": "EDIT", "path": "index.html", "destination": "", "details": "green"}],
    "make the ui a green and yellow theme", ["index.html", "styles/liquid_glass.css"], ["styles"])
eq([(s["path"], s["optional"]) for s in norm], [("styles/liquid_glass.css", True), ("index.html", True)])
eq(t.normalize_model_steps(
    [{"action": "EDIT", "path": "b.txt", "destination": "", "details": "x"}],
    "fix the typo in a.txt", ["a.txt", "b.txt"], []), [])  # named a file: only that file
print("unnamed-file edits OK")

print("ALL TASKPLAN TESTS PASSED")
