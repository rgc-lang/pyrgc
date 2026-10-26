"""pyrig end-to-end tests: require rgc and a C compiler (skipped when missing)."""

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = Path(__file__).resolve().parents[1]


def _rgc_root():
    """The rgc checkout: pyrig lives either inside it (rgc/pyrig) or beside it."""
    for cand in (REPO, REPO / "rgc"):
        if (cand / "examples" / "pyrig_demo.rg").is_file():
            return cand
    return None


RGC_ROOT = _rgc_root()
# The rgc checkout's own build is canonical: it matches the sources under test, while a
# workspace-level build/ may be an older artifact.
RGC = os.environ.get("RGC") or next(
    (p for p in (RGC_ROOT / "build" / "rgc" if RGC_ROOT else None,
                 REPO / "build" / "rgc") if p and p.is_file()), None)

pytestmark = pytest.mark.skipif(
    RGC is None or RGC_ROOT is None or shutil.which("cc") is None,
    reason="rgc or cc not available")

sys.path.insert(0, str(SRC))

from pyrig.builder import build  # noqa: E402
from pyrig.scaffold import new_project  # noqa: E402


def _import_module(out_path: Path, module: str):
    sys.path.insert(0, str(out_path.parent))
    try:
        import importlib
        mod = importlib.import_module(module)
        assert mod.__file__ == str(out_path)
        return mod
    finally:
        sys.path.pop(0)
        for name in [m for m in sys.modules if m == module]:
            del sys.modules[name]


def test_build_pyrig_demo(tmp_path):
    out = build(RGC_ROOT / "examples" / "pyrig_demo.rg", name="pyrig_demo_e2e",
                outdir=tmp_path, rgc=RGC, release=False)
    assert out.exists()
    m = _import_module(out, "pyrig_demo_e2e")
    assert m.add(3, 4) == 7
    assert m.greet("rgc") == "hello, rgc!"
    assert m.is_even(10) is True and m.is_even(7) is False
    assert abs(m.dist(0, 0, 3, 4) - 5.0) < 1e-9
    assert m.say_hi() is None


def test_new_and_build_workflow(tmp_path):
    project = new_project(tmp_path / "myext")
    out = build(project / "myext.rg", name="myext", outdir=project / "dist", rgc=RGC)
    m = _import_module(out, "myext")
    assert m.add(2, 3) == 5
    assert m.add.__doc__ == "Computes the sum of two integers."
    assert m.greet("pyrig") == "hello, pyrig!"


def test_argument_semantics(tmp_path):
    """Behaviour the vectorcall glue must preserve (and one bug it fixes)."""
    out = build(RGC_ROOT / "examples" / "pyrig_demo.rg", name="pyrig_sem_e2e",
                outdir=tmp_path, rgc=RGC, release=False)
    m = _import_module(out, "pyrig_sem_e2e")

    # PyArg_ParseTuple's "s" rejected bytes and embedded NULs; the hand-written
    # extraction must too (PyUnicode_AsUTF8 would silently truncate at the NUL)
    with pytest.raises(TypeError, match="must be str, not bytes"):
        m.greet(b"rgc")
    with pytest.raises(ValueError, match="embedded null character"):
        m.greet("a\0b")
    with pytest.raises(TypeError, match="must be str, not None"):
        m.greet(None)

    # "L"/"d"/"p" accepted __index__/__float__/truthiness; unchanged
    class Idx:
        def __index__(self):
            return 7

    assert m.add(Idx(), Idx()) == 14
    assert m.dist(Idx(), Idx(), 10, 11) == pytest.approx(5.0)
    assert m.is_even(True) is False  # bool is an int subclass, accepted as before
    with pytest.raises(TypeError, match="cannot be interpreted as an integer"):
        m.is_even(2.0)

    # overflow still surfaces as OverflowError
    with pytest.raises(OverflowError):
        m.add(2 ** 70, 0)

    # bug fix: a zero-arity export used to swallow extra positional arguments
    # (PyArg_ParseTuple skips the arity check when the format string is empty)
    with pytest.raises(TypeError, match="takes no arguments"):
        m.say_hi(1)


def test_cli_build_via_pyproject(tmp_path):
    project = new_project(tmp_path / "cliext")
    r = subprocess.run(
        [sys.executable, "-m", "pyrig", "build"], cwd=project,
        env={**os.environ, "PYTHONPATH": str(SRC), "RGC": str(RGC)},
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "built:" in r.stdout
    suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    assert (project / "dist" / ("cliext" + suffix)).exists()


OWN_SRC = '''\
public def owned(s: str) -> str {
    return "x" + s
}

public def chained(s: str) -> str {
    return "a" + s + "b" + s + "c"
}

public def literal() -> str {
    return "static text"
}

public def borrowed(s: str) -> str {
    return s
}

public def opaque(n: int) -> str {
    return to_string(n)
}

pyrig.export("owned")
pyrig.export("chained")
pyrig.export("literal")
pyrig.export("borrowed")
pyrig.export("opaque")
'''


def _rss_kb() -> int:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    raise AssertionError("VmRSS not reported")


def _build_ownership_module(tmp_path, name):
    src = tmp_path / "own.rg"
    src.write_text(OWN_SRC, encoding="utf-8")
    return build(src, name=name, outdir=tmp_path, rgc=RGC)


def test_str_return_ownership(tmp_path, capsys):
    """Only a provably fresh buffer is freed; freeing a literal aborts in glibc and
    freeing a borrowed one corrupts CPython's string cache."""
    out = _build_ownership_module(tmp_path, "own_e2e")
    err = capsys.readouterr().err
    assert "'opaque'" in err and "not freed" in err  # the unprovable case warns
    assert err.count("not freed") == 1  # and only that one
    m = _import_module(out, "own_e2e")
    assert m.owned("a") == "xa"
    assert m.chained("Z") == "aZbZc"
    assert m.literal() == "static text"
    assert m.borrowed("keep") == "keep"
    assert m.opaque(42) == "42"
    for _ in range(2000):
        assert m.literal() == "static text"
        assert m.borrowed("keep") == "keep"
        assert m.owned("a") == "xa"
        assert m.chained("Z") == "aZbZc"


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc/self/status")
def test_owned_str_return_does_not_leak(tmp_path):
    out = _build_ownership_module(tmp_path, "own_leak_e2e")
    m = _import_module(out, "own_leak_e2e")
    m.owned("warm up")
    m.chained("warm up")
    before = _rss_kb()
    for _ in range(200000):
        m.owned("leak check")
        m.chained("leak check")
    growth = _rss_kb() - before
    # Two independent frees keep this flat: the glue releases the returned buffer (owned), and
    # codegen releases the three intermediates of chained's `+` chain. Regressing either retains
    # ~6 MB (glue) or ~19 MB (intermediates) over these 200k iterations.
    assert growth < 4096, "str return leaked %d KB" % growth
