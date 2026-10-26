"""pyrgc unit tests: glue generation and project configuration parsing (no rgc needed).

The export set and the signatures both come from ``rgc --emit-exports`` metadata, so the
fixture below is the JSON the compiler would emit for SRC. That keeps these tests free of a
compiler while still exercising the real metadata format: ``pyrgc`` never scans the source for
markers, it only reads the compiler's ``"export"`` flag.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pyrgc.shim import (PyrgcError, collect_exports, extract_doc, gen_shim,
                        load_functions, ownership_warnings)
from pyrgc.builder import parse_tool_pyrgc

SRC = '''\
# Adds two numbers.
# Supports negatives too.
export def add(a: int, b: int) -> int {
    return a + b
}

export def shout(s: str) -> str {
    return s + "!"
}

export def is_even(n: int) -> bool {
    return n % 2 == 0
}

public export def both(a: bool, b: bool) -> bool {
    return a and b
}

export def nothing() -> None {
    return
}

# Returns a string literal.
export def lit() -> str {
    return "hi"
}

# Hands the caller's own buffer back.
export def echo(s: str) -> str {
    return s
}

# Ownership the compiler cannot prove.
export def rendered(n: int) -> str {
    return to_string(n)
}

# public but not exported: module API, invisible to the host runtime.
public def helper() -> int {
    return 0
}
'''

META = '''\
{
  "format": 2,
  "source": "demo.rg",
  "functions": [
    {"name": "add", "public": false, "export": true, "params": [{"name": "a", "type": "int"}, {"name": "b", "type": "int"}], "ret": "int", "str_ret": ""},
    {"name": "shout", "public": false, "export": true, "params": [{"name": "s", "type": "str"}], "ret": "str", "str_ret": "owned"},
    {"name": "is_even", "public": false, "export": true, "params": [{"name": "n", "type": "int"}], "ret": "bool", "str_ret": ""},
    {"name": "both", "public": true, "export": true, "params": [{"name": "a", "type": "bool"}, {"name": "b", "type": "bool"}], "ret": "bool", "str_ret": ""},
    {"name": "nothing", "public": false, "export": true, "params": [], "ret": "None", "str_ret": ""},
    {"name": "lit", "public": false, "export": true, "params": [], "ret": "str", "str_ret": "static"},
    {"name": "echo", "public": false, "export": true, "params": [{"name": "s", "type": "str"}], "ret": "str", "str_ret": "borrowed"},
    {"name": "rendered", "public": false, "export": true, "params": [{"name": "n", "type": "int"}], "ret": "str", "str_ret": "unknown"},
    {"name": "helper", "public": true, "export": false, "params": [], "ret": "int", "str_ret": ""}
  ]
}
'''

FUNCS = load_functions(META)

EXPORTED = ["add", "shout", "is_even", "both", "nothing", "lit", "echo", "rendered"]


def _fn(name, params=(), ret="None", str_ret="", export=True):
    return {name: {"name": name, "public": True, "export": export,
                   "params": [{"name": n, "type": t} for n, t in params],
                   "ret": ret, "str_ret": str_ret}}


def _body(shim, name):
    """The generated wrapper body of one export."""
    return shim.split("pyrgc_w_%s(" % name)[1].split("\n}")[0]


def test_load_functions():
    assert sorted(FUNCS) == sorted(EXPORTED + ["helper"])
    assert FUNCS["shout"]["str_ret"] == "owned"
    assert FUNCS["add"]["export"] is True
    assert FUNCS["helper"]["export"] is False
    assert [(p["name"], p["type"]) for p in FUNCS["add"]["params"]] == [
        ("a", "int"), ("b", "int")]


def test_load_functions_rejects_bad_metadata():
    with pytest.raises(PyrgcError, match="cannot parse export metadata"):
        load_functions("{not json")
    with pytest.raises(PyrgcError, match="unsupported export metadata format"):
        load_functions('{"format": 99, "functions": []}')


def test_load_functions_rejects_format_1():
    """Format 1 predates `export def`: without the flag every function would look unexported."""
    with pytest.raises(PyrgcError, match="format 1"):
        load_functions('{"format": 1, "functions": []}')


def test_collect_exports():
    exports = collect_exports(FUNCS, SRC)
    assert [e.name for e in exports] == EXPORTED
    add = exports[0]
    assert [(p.name, p.type) for p in add.params] == [("a", "int"), ("b", "int")]
    assert add.ret == "int"
    assert add.doc == "Adds two numbers.\nSupports negatives too."
    assert exports[1].str_ret == "owned"


def test_export_flag_selects_functions():
    """`public` is module visibility, `export` is host visibility: only the latter counts."""
    functions = _fn("visible", export=True)
    functions.update(_fn("hidden", export=False))
    assert [e.name for e in collect_exports(functions, "")] == ["visible"]

    # public + not exported stays out; private + exported still goes in
    functions = {"pub": {"name": "pub", "public": True, "export": False,
                         "params": [], "ret": "None", "str_ret": ""},
                 "priv": {"name": "priv", "public": False, "export": True,
                          "params": [], "ret": "None", "str_ret": ""}}
    assert [e.name for e in collect_exports(functions, "")] == ["priv"]


def test_unknown_param_type_rejected():
    with pytest.raises(PyrgcError, match="unsupported type"):
        collect_exports(_fn("f", params=[("x", "List[int]")]))


def test_unknown_return_type_rejected():
    with pytest.raises(PyrgcError, match="return type"):
        collect_exports(_fn("f", ret="List[int]"))


def test_no_exports_rejected():
    with pytest.raises(PyrgcError, match="no `export def`"):
        collect_exports(_fn("f", export=False), "def f() -> None", "demo.rg")


def test_gen_shim_contains_module_entry():
    shim = gen_shim("demo", collect_exports(FUNCS, SRC))
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
    # an unexported function never reaches the method table
    assert "pyrgc_w_helper" not in shim
    assert '"helper"' not in shim


def test_gen_shim_uses_vectorcall():
    """METH_FASTCALL wrappers take (self, args, nargs); no tuple/format parsing."""
    shim = gen_shim("demo", collect_exports(FUNCS, SRC))
    assert "PyArg_ParseTuple" not in shim
    assert "METH_VARARGS" not in shim
    assert shim.count("METH_FASTCALL") == len(EXPORTED)
    assert ("static PyObject *pyrgc_w_add(PyObject *self, PyObject *const *args, "
            "Py_ssize_t nargs) {") in shim


def test_gen_shim_arity_guards():
    shim = gen_shim("demo", collect_exports(FUNCS, SRC))
    assert 'add() takes exactly 2 arguments (%zd given)' in shim
    assert 'shout() takes exactly 1 argument (%zd given)' in shim
    assert 'nothing() takes no arguments (%zd given)' in shim


def test_gen_shim_argument_extraction():
    shim = gen_shim("demo", collect_exports(FUNCS, SRC))
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
    shim = gen_shim("demo", collect_exports(FUNCS, SRC))
    assert "#include <stdlib.h>" in shim
    assert shim.count("free((void *)r);") == 1
    assert "free((void *)r);" in _body(shim, "shout")
    for name in ("lit", "echo", "rendered"):
        assert "free(" not in _body(shim, name)


def test_ownership_warnings():
    warnings = ownership_warnings(collect_exports(FUNCS, SRC))
    assert len(warnings) == 1
    assert "'rendered'" in warnings[0]
    assert "not freed" in warnings[0]


def test_extract_doc_skips_blank_gap():
    src = "# doc line\n\ndef f() -> None\n"
    assert extract_doc(src, "f") == ""  # blank lines mean no doc
    src2 = "# one\n# two\ndef f() -> None\n"
    assert extract_doc(src2, "f") == "one\ntwo"


def test_extract_doc_handles_export_modifiers():
    assert extract_doc("# doc\nexport def f() -> None\n", "f") == "doc"
    assert extract_doc("# doc\npublic export def f() -> None\n", "f") == "doc"
    assert extract_doc("# doc\nexport public def f() -> None\n", "f") == "doc"


def test_parse_tool_pyrgc():
    text = '''
[project]
name = "demo"

[tool.pyrgc]
name = "demo_ext"
source = "demo.rg"
release = false
out-dir = "dist"

[tool.other]
x = 1
'''
    cfg = parse_tool_pyrgc(text)
    assert cfg == {"name": "demo_ext", "source": "demo.rg",
                   "release": False, "out-dir": "dist"}
