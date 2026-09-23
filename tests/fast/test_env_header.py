import sys, os, tempfile, shutil
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m

tmp = tempfile.mkdtemp()
try:
    os.makedirs(os.path.join(tmp, "subdir"))
    with open(os.path.join(tmp, "a.txt"), "w") as f:
        f.write("x")
    with open(os.path.join(tmp, "b.py"), "w") as f:
        f.write("x")
    with open(os.path.join(tmp, ".hidden"), "w") as f:
        f.write("x")

    header = m.build_environment_header(tmp)
    print(header)
    assert tmp in header
    assert "a.txt" in header
    assert "b.py" in header
    assert "subdir/" in header
    assert ".hidden" not in header
    print("basic listing OK")

    # empty directory
    tmp2 = tempfile.mkdtemp()
    header2 = m.build_environment_header(tmp2)
    print(header2)
    assert "(empty)" in header2
    shutil.rmtree(tmp2)
    print("empty dir OK")

    # nonexistent directory -> graceful, no crash
    header3 = m.build_environment_header("/no/such/dir/at/all")
    print(header3)
    assert "couldn't list contents" in header3
    print("nonexistent dir graceful OK")

    # truncation
    tmp3 = tempfile.mkdtemp()
    for i in range(250):
        open(os.path.join(tmp3, f"f{i:03d}.txt"), "w").close()
    header4 = m.build_environment_header(tmp3)
    assert "more not shown" in header4
    shutil.rmtree(tmp3)
    print("truncation OK")
finally:
    shutil.rmtree(tmp)

print("ALL ENV HEADER UNIT TESTS PASSED")
