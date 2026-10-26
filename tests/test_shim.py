"""pyrig unit tests: glue generation and project configuration parsing (no rgc needed).

Signatures come from ``rgc --emit-exports`` metadata, so the fixtures below are the JSON the
compiler would emit for SRC. That keeps these tests free of a compiler while still exercising
the real metadata format.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pyrig.shim import (PyrigError, collect_exports, extract_doc, gen_shim,
                        load_functions, ownership_warnings)
from pyrig.builder import parse_tool_pyrig

SRC = '''\
# Adds two numbers.
# Supports negatives too.
public def add(a: int, b: int) -> int {
    return a + b
}

def shout(s: str) -> str {
    return s + "!"
}

def is_even(n: int) -> bool {
    return n % 2 == 0
}

def both(a: bool, b: bool) -> bool {
    return a and b
}

def nothing() -> None {
    return
}

# Returns a string literal.
def lit() -> str {
    return "hi"
}

# Hands the caller's own buffer back.
def echo(s: str) -> str {
    return s
}

# Ownership the compiler cannot prove.
def rendered(n: int) -> str {
    return to_string(n)
}

def helper() -> int {
    return 0
}

pyrig.export("add")
pyrig.export("shout")
pyrig.export("is_even")
pyrig.export("nothing")
pyrig.export("both")
pyrig.export("lit")
pyrig.export("echo")
pyrig.export("rendered")
'''

# `helper` is deliberately never exported: the markers, not the metadata, decide the export list.
META = '''\
{
  "format": 1,
  "source": "demo.rg",
  "functions": [
    {"name": "add", "public": true, "params": [{"name": "a", "type": "int"}, {"name": "b", "type": "int"}], "ret": "int", "str_ret": ""},
    {"name": "shout", "public": false, "params": [{"name": "s", "type": "str"}], "ret": "str", "str_ret": "owned"},
    {"name": "is_even", "public": false, "params": [{"name": "n", "type": "int"}], "ret": "bool", "str_ret": ""},
    {"name": "both", "public": false, "params": [{"name": "a", "type": "bool"}, {"name": "b", "type": "bool"}], "ret": "bool", "str_ret": ""},
    {"name": "nothing", "public": false, "params": [], "ret": "None", "str_ret": ""},
    {"name": "lit", "public": false, "params": [], "ret": "str", "str_ret": "static"},
    {"name": "echo", "public": false, "params": [{"name": "s", "type": "str"}], "ret": "str", "str_ret": "borrowed"},
    {"name": "rendered", "public": false, "params": [{"name": "n", "type": "int"}], "ret": "str", "str_ret": "unknown"},
    {"name": "helper", "public": false, "params": [], "ret": "int", "str_ret": ""}
  ]
}
'''

FUNCS = load_functions(META)

EXPORTED = ["add", "shout", "is_even", "nothing", "both", "lit", "echo", "rendered"]


def _fn(name, params=(), ret="None", str_ret=""):
    return {name: {"name": name, "public": True,
                   "params": [{"name": n, "type": t} for n, t in params],
                   "ret": ret, "str_ret": str_ret}}


def _body(shim, py_name):
    """The generated wrapper body of one export."""
    return shim.split("pyrig_w_%s(" % py_name)[1].split("\n}")[0]


def test_load_functions():
    assert sorted(FUNCS) == sorted(EXPORTED + ["helper"])
    assert FUNCS["shout"]["str_ret"] == "owned"
    assert [(p["name"], p["type"]) for p in FUNCS["add"]["params"]] == [
        ("a", "int"), ("b", "int")]


def test_load_functions_rejects_bad_metadata():
    with pytest.raises(PyrigError, match="cannot parse export metadata"):
        load_functions("{not json")
    with pytest.raises(PyrigError, match="unsupported export metadata format"):
        load_functions('{"format": 99, "functions": []}')


def test_collect_exports():
    exports = collect_exports(SRC, FUNCS)
    assert [e.py_name for e in exports] == EXPORTED
    assert [e.rgc_name for e in exports] == EXPORTED
    add = exports[0]
    assert [(p.name, p.type) for p in add.params] == [("a", "int"), ("b", "int")]
    assert add.ret == "int"
    assert add.doc == "Adds two numbers.\nSupports negatives too."
    assert exports[1].str_ret == "owned"


def test_unknown_param_type_rejected():
    with pytest.raises(PyrigError, match="unsupported type"):
        collect_exports('pyrig.export("f")',
                        _fn("f", params=[("x", "List[int]")]))


def test_unknown_return_type_rejected():
    with pytest.raises(PyrigError, match="return type"):
        collect_exports('pyrig.export("f")', _fn("f", ret="List[int]"))


def test_export_without_function_rejected():
    with pytest.raises(PyrigError, match="not found"):
        collect_exports('pyrig.export("nope")', FUNCS)


def test_no_exports_rejected():
    with pytest.raises(PyrigError, match="no pyrig.export"):
        collect_exports("def f() -> None", FUNCS)


def test_duplicate_export_rejected():
    with pytest.raises(PyrigError, match="duplicate"):
        collect_exports('pyrig.export("add")\npyrig.export("add")', FUNCS)


def test_gen_shim_contains_module_entry():
    shim = gen_shim("demo", collect_exports(SRC, FUNCS))
    assert "#include <Python.h>" in shim
    assert "extern long long add(long long a, long long b);" in shim
    assert "PyLong_AsLongLong(args[0])" in shim
    assert "PyLong_AsLongLong(args[1])" in shim
    assert "PyBool_FromLong" in shim
    assert "PyUnicode_FromString" in shim
    assert "Py_RETURN_NONE;" in shim
    assert 'PyMODINIT_FUNC PyInit_demo(void)' in shim
    # the docstring goes into the method table
    assert '"Adds two numbers.\\nSupports negatives too."' in shim


def test_gen_shim_uses_vectorcall():
    """METH_FASTCALL wrappers take (self, args, nargs); no tuple/format parsing."""
    shim = gen_shim("demo", collect_exports(SRC, FUNCS))
    assert "PyArg_ParseTuple" not in shim
    assert "METH_VARARGS" not in shim
    assert shim.count("METH_FASTCALL") == len(EXPORTED)
    assert ("static PyObject *pyrig_w_add(PyObject *self, PyObject *const *args, "
            "Py_ssize_t nargs) {") in shim


def test_gen_shim_arity_guards():
    shim = gen_shim("demo", collect_exports(SRC, FUNCS))
    assert 'add() takes exactly 2 arguments (%zd given)' in shim
    assert 'shout() takes exactly 1 argument (%zd given)' in shim
    assert 'nothing() takes no arguments (%zd given)' in shim


def test_gen_shim_argument_extraction():
    shim = gen_shim("demo", collect_exports(SRC, FUNCS))
    # int/float/bool go through the same CPython calls PyArg_ParseTuple used,
    # so accepted inputs and error types are unchanged
    assert "PyObject_IsTrue(args[0])" in shim
    assert "if (a_b < 0) return NULL;" in shim
    # str: explicit type check (PyArg-compatible message) plus embedded-NUL guard
    assert 'shout() argument 1 must be str, not %.50s' in shim
    assert 'args[0] == Py_None ? "None" : Py_TYPE(args[0])->tp_name' in shim
    assert "PyUnicode_AsUTF8AndSize(args[0], &sz)" in shim
    assert 'PyErr_SetString(PyExc_ValueError, "embedded null character")' in shim
    assert "#include <string.h>" in shim


def test_gen_shim_frees_only_owned_str_returns():
    """Freeing a static literal or the caller's buffer would corrupt the heap."""
    shim = gen_shim("demo", collect_exports(SRC, FUNCS))
    assert "#include <stdlib.h>" in shim
    assert shim.count("free((void *)r);") == 1
    assert "free((void *)r);" in _body(shim, "shout")
    for name in ("lit", "echo", "rendered"):
        assert "free(" not in _body(shim, name)


def test_ownership_warnings():
    warnings = ownership_warnings(collect_exports(SRC, FUNCS))
    assert len(warnings) == 1
    assert "'rendered'" in warnings[0]
    assert "not freed" in warnings[0]


def test_extract_doc_skips_blank_gap():
    src = "# doc line\n\ndef f() -> None\n"
    assert extract_doc(src, "f") == ""  # blank lines mean no doc
    src2 = "# one\n# two\ndef f() -> None\n"
    assert extract_doc(src2, "f") == "one\ntwo"


def test_parse_tool_pyrig():
    text = '''
[project]
name = "demo"

[tool.pyrig]
name = "demo_ext"
source = "demo.rg"
release = false
out-dir = "dist"

[tool.other]
x = 1
'''
    cfg = parse_tool_pyrig(text)
    assert cfg == {"name": "demo_ext", "source": "demo.rg",
                   "release": False, "out-dir": "dist"}
