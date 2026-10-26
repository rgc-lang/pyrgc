"""CPython C API glue generation.

Scans RGC source for ``pyrgc.export("name")`` markers (the equivalent of PyO3's
``#[pyfunction]`` + ``m.def(...)``), takes each exported function's signature
from ``rgc --emit-exports`` metadata rather than from the source text, and
generates C source containing vectorcall (``METH_FASTCALL``) argument
extraction, type conversions, a ``PyMethodDef`` method table, and a
``PyInit_<module>`` module entry point.

The metadata also carries the ownership class of a ``str`` return value, which
decides whether the glue may ``free()`` the buffer it just copied into a Python
string; see ``rgc/src/src/exports.rg``.
"""

from __future__ import annotations

import json
import re
from ._version import __version__
from dataclasses import dataclass, field
from typing import Dict, List

EXPORT_RE = re.compile(
    r'pyrgc\.export(?:_as)?\s*\(\s*"([^"]+)"\s*(?:,\s*"([^"]+)")?\s*\)'
)

# Version of the rgc --emit-exports JSON this module understands.
METADATA_FORMAT = 1

# Ownership classes rgc reports for a returned str buffer.
OWNED, STATIC, BORROWED, UNKNOWN = "owned", "static", "borrowed", "unknown"

# RGC type -> (C ABI type, argument local type, extraction expression, failure
# predicate, return-value wrapper fragment, local variable suffix).
# The extraction calls are the same CPython APIs PyArg_ParseTuple uses for the
# "L"/"d"/"p" formats, so accepted inputs and error types stay identical while
# the tuple construction and format-string parsing disappear.
TYPES = {
    "int": ("long long", "long long", "PyLong_AsLongLong({v})",
            "{x} == -1 && PyErr_Occurred()",
            "PyLong_FromLongLong((long long){v})", "i"),
    "float": ("double", "double", "PyFloat_AsDouble({v})",
              "{x} == -1.0 && PyErr_Occurred()",
              "PyFloat_FromDouble((double){v})", "f"),
    "bool": ("unsigned char", "int", "PyObject_IsTrue({v})",
             "{x} < 0",
             "PyBool_FromLong((long)(({v}) != 0))", "b"),
    "str": ("const char *", "const char *", None, None, None, "s"),
}

# str arguments need an explicit type check so the error message keeps the
# wording PyArg_ParseTuple's "s" format produced (including CPython's own
# special case that prints None instead of NoneType). PyUnicode_AsUTF8 would
# silently truncate at an embedded NUL, so the buffer is length-checked the way
# the "s" format checks it.
STR_EXTRACT = """\
    if (!PyUnicode_Check(args[{i}])) {{
        PyErr_Format(PyExc_TypeError, "{fn}() argument {n} must be str, not %.50s",
                     args[{i}] == Py_None ? "None" : Py_TYPE(args[{i}])->tp_name);
        return NULL;
    }}
    {{
        Py_ssize_t sz;
        const char *p = PyUnicode_AsUTF8AndSize(args[{i}], &sz);
        if (!p) return NULL;
        if (memchr(p, 0, (size_t)sz)) {{
            PyErr_SetString(PyExc_ValueError, "embedded null character");
            return NULL;
        }}
        {local} = p;
    }}"""

SUPPORTED = "int/float/bool/str/None"


class PyrgcError(Exception):
    """A pyrgc build error (human-readable, shown directly to the user)."""


@dataclass
class Param:
    name: str
    type: str


@dataclass
class Export:
    py_name: str
    rgc_name: str
    params: List[Param] = field(default_factory=list)
    ret: str = "None"
    str_ret: str = ""
    doc: str = ""


def load_functions(text: str, path: str = "<exports>") -> Dict[str, dict]:
    """Parse ``rgc --emit-exports`` JSON into a function-name -> signature map.

    Signatures come from the compiler rather than from the source text: a regex
    scanner and the type checker drift apart silently, and only the compiler knows
    where a returned str buffer came from.
    """
    try:
        meta = json.loads(text)
    except ValueError as e:
        raise PyrgcError("%s: cannot parse export metadata: %s" % (path, e))
    if meta.get("format") != METADATA_FORMAT:
        raise PyrgcError(
            "%s: unsupported export metadata format %r (pyrgc reads format %d)"
            % (path, meta.get("format"), METADATA_FORMAT))
    return dict((fn["name"], fn) for fn in meta.get("functions", []))


def extract_doc(src: str, name: str) -> str:
    """Extract the contiguous ``#`` comments above a function definition as its
    docstring (mirroring PyO3's Rust doc comment -> Python __doc__ behavior)."""
    m = re.search(
        r"^[ \t]*(?:public[ \t]+)?def[ \t]+" + re.escape(name) + r"\b", src, re.M)
    if not m:
        return ""
    lines = src[:m.start()].splitlines()
    doc: List[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            doc.append(stripped.lstrip("#").strip())
        else:
            break  # the comment block must be adjacent to def (blank lines/code mean no doc)
    doc.reverse()
    return "\n".join(doc).strip()


def collect_exports(src: str, functions: Dict[str, dict],
                    path: str = "<source>") -> List[Export]:
    """Resolve ``pyrgc.export`` markers against the compiler's function metadata."""
    out: List[Export] = []
    seen = set()
    for m in EXPORT_RE.finditer(src):
        py_name = m.group(1)
        rgc_name = m.group(2) or m.group(1)
        if py_name in seen:
            raise PyrgcError("duplicate export '%s' in %s" % (py_name, path))
        seen.add(py_name)
        fn = functions.get(rgc_name)
        if fn is None:
            raise PyrgcError(
                "export '%s': function '%s' not found in %s "
                "(entry-file functions only)" % (py_name, rgc_name, path))
        params: List[Param] = []
        for p in fn.get("params", []):
            if p["type"] not in TYPES:
                raise PyrgcError(
                    "function '%s': parameter '%s' has unsupported type '%s' "
                    "(supported: %s)" % (rgc_name, p["name"], p["type"],
                                         "int/float/bool/str"))
            params.append(Param(p["name"], p["type"]))
        ret = fn.get("ret", "None")
        if ret != "None" and ret not in TYPES:
            raise PyrgcError(
                "export '%s': return type '%s' unsupported (supported: %s)"
                % (py_name, ret, SUPPORTED))
        out.append(Export(py_name=py_name, rgc_name=rgc_name, params=params,
                          ret=ret, str_ret=fn.get("str_ret", ""),
                          doc=extract_doc(src, rgc_name)))
    if not out:
        raise PyrgcError("no pyrgc.export(...) found in %s" % path)
    return out


def ownership_warnings(exports: List[Export]) -> List[str]:
    """Notices for str returns whose buffer pyrgc must leak because rgc could not
    prove it was freshly allocated."""
    out: List[str] = []
    for e in exports:
        if e.ret == "str" and e.str_ret == UNKNOWN:
            out.append(
                "'%s' returns a str whose ownership is not provable, so the buffer "
                "is not freed and each call may leak; returning a concatenation "
                "(`a + b`) makes it owned" % e.py_name)
    return out


CALL_ARG = {
    "int": "{x}",
    "float": "{x}",
    "bool": "(unsigned char)({x} ? 1 : 0)",
    "str": "{x}",
}


def _arity_check(fn: str, n: int) -> List[str]:
    """Emit the positional-arity guard, worded like PyArg_ParseTuple's own errors."""
    if n == 0:
        msg = '%s() takes no arguments (%%zd given)' % fn
    else:
        msg = '%s() takes exactly %d %s (%%zd given)' % (
            fn, n, "argument" if n == 1 else "arguments")
    return ["    if (nargs != %d) {" % n,
            '        PyErr_Format(PyExc_TypeError, "%s", nargs);' % msg,
            "        return NULL;",
            "    }"]


def gen_shim(module: str, exports: List[Export], src_name: str = "<source>") -> str:
    """Generate CPython C API glue source."""
    lines = [
        "/* generated by pyrgc %s (RGC Python binding framework) — do not edit */"
        % __version__,
        "#define PY_SSIZE_T_CLEAN",
        "#include <Python.h>",
        "#include <stdlib.h>",
        "#include <string.h>",
        "",
        "/* RGC functions (entry-file symbols are bare function names; bool returns are read as i1/AL) */",
    ]
    for e in exports:
        ret_c = "const char *" if e.ret == "str" else \
            ("void" if e.ret == "None" else TYPES[e.ret][0])
        ptypes = ", ".join(TYPES[p.type][0] + " " + p.name for p in e.params) or "void"
        lines.append("extern %s %s(%s);" % (ret_c, e.rgc_name, ptypes))
    lines.append("")

    for e in exports:
        lines.append(
            "static PyObject *pyrgc_w_%s(PyObject *self, PyObject *const *args, "
            "Py_ssize_t nargs) {" % e.py_name)
        lines.extend(_arity_check(e.py_name, len(e.params)))
        for i, p in enumerate(e.params):
            local = "%s_%s" % (p.name, TYPES[p.type][5])
            lines.append("    %s %s;" % (TYPES[p.type][1], local))
            if p.type == "str":
                lines.append(STR_EXTRACT.format(i=i, fn=e.py_name, n=i + 1,
                                                local=local))
            else:
                lines.append("    %s = %s;" % (
                    local, TYPES[p.type][2].format(v="args[%d]" % i)))
                lines.append("    if (%s) return NULL;" %
                             TYPES[p.type][3].format(x=local))
        call = "%s(%s)" % (e.rgc_name, ", ".join(
            CALL_ARG[p.type].format(x="%s_%s" % (p.name, TYPES[p.type][5]))
            for p in e.params))
        if e.ret == "None":
            lines.append("    %s;" % call)
            lines.append("    Py_RETURN_NONE;")
        elif e.ret == "str":
            lines.append("    const char *r = %s;" % call)
            lines.append("    if (!r) Py_RETURN_NONE;")
            lines.append("    PyObject *o = PyUnicode_FromString(r);")
            if e.str_ret == OWNED:
                lines.append("    free((void *)r);")
            lines.append("    return o;")
        else:
            lines.append("    return %s;" % TYPES[e.ret][4].format(v=call))
        lines.append("}")
        lines.append("")

    lines.append("static PyMethodDef pyrgc_methods[] = {")
    for e in exports:
        doc = e.doc.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        lines.append('    {"%s", (PyCFunction)(void *)pyrgc_w_%s, METH_FASTCALL, "%s"},'
                     % (e.py_name, e.py_name, doc))
    lines.append("    {NULL, NULL, 0, NULL}")
    lines.append("};")
    lines.append("")
    lines.append("static struct PyModuleDef pyrgc_module = {")
    lines.append('    PyModuleDef_HEAD_INIT, "%s",' % module)
    lines.append('    "RGC module \'%s\' built by pyrgc", -1, pyrgc_methods,' % module)
    lines.append("    NULL, NULL, NULL, NULL")
    lines.append("};")
    lines.append("")
    lines.append("PyMODINIT_FUNC PyInit_%s(void) {" % module)
    lines.append("    return PyModule_Create(&pyrgc_module);")
    lines.append("}")
    return "\n".join(lines) + "\n"
