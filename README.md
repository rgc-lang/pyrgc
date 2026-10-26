# pyrgc

**The official Python binding framework for RGC** — modeled after the developer
experience of [PyO3](https://github.com/PyO3/pyo3) /
[maturin](https://github.com/PyO3/maturin): export
[RGC](https://github.com/rgc-lang/rgc) functions as CPython extension
modules that Python can `import` directly.

The implementation works directly against the **CPython C API**: for every
exported function it generates `METH_FASTCALL` (vectorcall) C glue with an
explicit arity check, per-argument type extraction, a `PyMethodDef` method
table, and a `PyInit_<module>` entry point, then compiles the RGC code with
`rgc --emit-object --emit-exports` and links a `.so` (no libpython dependency).

Signatures are not scraped from source text. `rgc --emit-exports` writes a JSON
sidecar with every top-level function's parameter types, return type, whether it
was declared `export def`, and — for `str` returns — an ownership class that
tells the glue whether the returned buffer was freshly allocated and may be
freed. The compiler owns the export marker, so pyrgc never scans the source.

## Installation

```bash
pip install pyrgc
```

To hack on pyrgc itself, install the checkout in editable mode instead:
`pip install -e .`.

pyrgc drives a released [RGC](https://github.com/rgc-lang/rgc) compiler, found
as `rgc` on `PATH` or through `RGC=<path>`, and links the generated glue with a
C compiler (`cc` by default, override with `--cc`).

## Quick start (maturin-style workflow)

```bash
pyrgc new string_sum        # scaffold a project: string_sum.rg + pyproject.toml
cd string_sum
pyrgc build                 # -> dist/string_sum.cpython-3xx-x86_64-linux-gnu.so
python3 -c "import string_sum; print(string_sum.add(3, 4))"
```

On the RGC side (`string_sum.rg`):

```python
# The # comments above a function become the Python __doc__ (like PyO3 doc comments).
# Computes the sum of two integers.
export def add(a: int, b: int) -> int {
    return a + b
}
```

`export def` is RGC's host-visibility modifier, the counterpart of PyO3's
`#[pyfunction]`: it marks a function for the binding generator while leaving the
rest of the module alone. It is orthogonal to `public` (visibility to other RGC
modules) — use `public export def` for both.

## CLI

| Command | Description |
|---------|-------------|
| `pyrgc new <dir>` | Scaffold an extension project (`.rg` + `pyproject.toml` + README) |
| `pyrgc build [path]` | Build; `path` is a `.rg` file or a project directory with `[tool.pyrgc]` |
| `pyrgc develop [path]` | Build and install into the active virtualenv (like `maturin develop`) |
| `pyrgc --version` | Version |

`build` options: `-n/--name` module name, `-o/--out` output directory, `--rgc`
compiler path, `--cc` C compiler, `--debug` disable optimization (`rgc -O0`).

## pyproject.toml

Inside an extension project, `pyrgc build` discovers `pyproject.toml` by walking up:

```toml
[tool.pyrgc]
name = "string_sum"     # extension module name (defaults to the project directory name)
source = "string_sum.rg"
out-dir = "dist"
# rgc = "build/rgc"     # compiler path (defaults to RGC env / rgc on PATH)
# cc = "cc"
# release = true        # false -> rgc -O0
# link-args = ""
```

## Type mapping (RGC ↔ Python)

| RGC | Python | Argument extraction | Return value construction |
|-----|--------|---------------------|---------------------------|
| `int` | `int` | `PyLong_AsLongLong` (accepts `__index__`) | `PyLong_FromLongLong` |
| `float` | `float` | `PyFloat_AsDouble` (accepts `__float__`) | `PyFloat_FromDouble` |
| `bool` | `bool` | `PyObject_IsTrue` | `PyBool_FromLong` |
| `str` | `str` | `PyUnicode_Check` + `PyUnicode_AsUTF8AndSize` | `PyUnicode_FromString` (+ `free` when owned) |
| `None` | `None` | — | `Py_RETURN_NONE` |

The extraction calls are the ones `PyArg_ParseTuple`'s `"L"` / `"d"` / `"p"` /
`"s"` formats used internally, so accepted inputs, overflow behaviour, and
exception types are unchanged. `str` arguments reject embedded NULs with
`ValueError` rather than truncating at them.

## String return ownership

RGC has no garbage collector and a `str` is a plain NUL-terminated `malloc`
buffer, so a returned pointer may be one of three things: a static string
literal, the caller's own argument buffer, or a fresh allocation. Freeing the
first two would corrupt the heap or CPython's string cache, so the compiler
classifies each `str` return and only the provably fresh one is freed:

| Class | Returned expression | Glue behaviour |
|-------|--------------------|----------------|
| `owned` | a concatenation (`a + b`) | `free` after building the Python object |
| `static` | a string literal | not freed (rodata) |
| `borrowed` | a parameter or a chain of them | not freed (caller's buffer) |
| `unknown` | anything else, e.g. a call result | not freed; `pyrgc build` prints a warning |

`owned` is concluded only from `+` on a `str`-typed expression, which always
lowers to a fresh allocation — RGC has no operator overloading, so that is
sound. Everything else stays `unknown` and keeps its buffer: the analysis errs
toward a leak, never toward a double free. Buffers that a chain of `+` creates
and consumes internally are freed by the compiler itself.

## Current limitations

- Only scalar functions in the entry file are supported; classes/container exports,
  keyword arguments, and exception propagation are not yet supported.
- Exported functions keep their RGC name; renamed exports are not supported yet.
- `str` argument pointers are only valid during the call.
- A `str` return classified `unknown` is never reclaimed; returning a
  concatenation makes it `owned` and leak-free.

## Testing

```bash
pip install -e '.[test]'   # pytest
python3 -m pytest tests/ -v
# End-to-end cases require rgc and a C compiler; they are skipped automatically when missing.
# Two of them build examples/pyrgc_demo.rg, so they also need an rgc checkout beside this repo.
```

The compiler side is covered by the RGC repository's own suites:
`tests/pyrgc/suite.rg` (build + real CPython `import` assertions),
`tests/exports/suite.rg` (signatures and `str` ownership classification) and
`tests/unit/str_concat.rg` (concatenation buffer lifetimes).

## License

Apache-2.0.