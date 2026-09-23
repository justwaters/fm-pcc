import sys
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m

class FakeBackend:
    def __init__(self, reply):
        self.reply = reply
    def classify(self, prompt, model):
        return self.reply
    def classify_with_fallback(self, prompt, model):
        return self.reply, model

def plan(reply, files=None, folders=None):
    return m.plan_next_step("t", files or [], folders or [], "/tmp", [], FakeBackend(reply), "on-device")

def without_model_used(d):
    return {k: v for k, v in d.items() if k != "model_used"}

# DONE
assert plan("DONE") is None
print("DONE OK")

# CREATE_FOLDER
r = plan("ACTION: CREATE_FOLDER\nTARGET: test\nINSTRUCTIONS: ")
assert without_model_used(r) == {"action": "CREATE_FOLDER", "target": "test", "instructions": ""}, r
assert r["model_used"] == "on-device"
print("CREATE_FOLDER OK:", r)

# CREATE_FILE nested
r = plan("ACTION: CREATE_FILE\nTARGET: test/path.txt\nINSTRUCTIONS: write hello", folders=["test"])
assert without_model_used(r) == {"action": "CREATE_FILE", "target": "test/path.txt", "instructions": "write hello"}, r
print("CREATE_FILE nested OK:", r)

# EDIT with fuzzy match
r = plan("ACTION: EDIT\nTARGET: the index.html file\nINSTRUCTIONS: add a title", files=["index.html"])
assert r["target"] == "index.html", r
print("EDIT fuzzy match OK:", r)

# RENAME
r = plan("ACTION: RENAME\nTARGET: old.txt\nINSTRUCTIONS: new.txt", files=["old.txt"])
assert without_model_used(r) == {"action": "RENAME", "target": "old.txt", "instructions": "new.txt"}, r
print("RENAME OK:", r)

# RENAME missing destination -> error
try:
    plan("ACTION: RENAME\nTARGET: old.txt\nINSTRUCTIONS: ")
    assert False
except m.EditError:
    print("RENAME missing destination rejected OK")

# MOVE
r = plan("ACTION: MOVE\nTARGET: a.txt\nINSTRUCTIONS: sub/a.txt", files=["a.txt"], folders=["sub"])
assert without_model_used(r) == {"action": "MOVE", "target": "a.txt", "instructions": "sub/a.txt"}, r
print("MOVE OK:", r)

# GIT_ADD
r = plan("ACTION: GIT_ADD\nTARGET: \nINSTRUCTIONS: ")
assert without_model_used(r) == {"action": "GIT_ADD", "target": "", "instructions": ""}, r
print("GIT_ADD OK:", r)

# GIT_COMMIT with default message
r = plan("ACTION: GIT_COMMIT\nTARGET: \nINSTRUCTIONS: ")
assert r["instructions"] == "automated changes", r
print("GIT_COMMIT default message OK:", r)

# GIT_COMMIT with real message
r = plan("ACTION: GIT_COMMIT\nTARGET: \nINSTRUCTIONS: add feature X")
assert r["instructions"] == "add feature X", r
print("GIT_COMMIT real message OK:", r)

# GIT_BRANCH_CREATE
r = plan("ACTION: GIT_BRANCH_CREATE\nTARGET: feature-x\nINSTRUCTIONS: ")
assert without_model_used(r) == {"action": "GIT_BRANCH_CREATE", "target": "feature-x", "instructions": ""}, r
print("GIT_BRANCH_CREATE OK:", r)

# GIT_BRANCH_CREATE missing name -> error
try:
    plan("ACTION: GIT_BRANCH_CREATE\nTARGET: \nINSTRUCTIONS: ")
    assert False
except m.EditError:
    print("GIT_BRANCH_CREATE missing name rejected OK")

# unknown action -> error
try:
    plan("ACTION: DELETE_EVERYTHING\nTARGET: x\nINSTRUCTIONS: ")
    assert False
except m.EditError:
    print("unknown action rejected OK")

# path safety: absolute / traversal rejected
for bad in ["/etc/passwd", "..", "."]:
    try:
        plan(f"ACTION: CREATE_FOLDER\nTARGET: {bad}\nINSTRUCTIONS: ")
        assert False, bad
    except m.EditError:
        pass
print("path safety rejections OK")

print("ALL PLAN_NEXT_STEP TESTS PASSED")
