"""The ``pyrig new`` scaffold: generate a new extension project (like maturin new)."""

from __future__ import annotations

import re
from pathlib import Path

from .shim import PyrigError

LIB_RG = '''\
# {name} — a pyrig extension module (RGC)
#
# Build: pyrig build        (run in this directory)
# Test:  PYTHONPATH=dist python3 -c "import {name}; print({name}.add(3, 4))"

import pyrig

# Computes the sum of two integers.
public def add(a: int, b: int) -> int {{
    return a + b
}}

# Greets someone by name.
public def greet(name: str) -> str {{
    return "hello, " + name + "!"
}}

# pybind11/PyO3: m.def("add", &add)
pyrig.export("add")
pyrig.export("greet")
'''

PYPROJECT = '''\
[project]
name = "{name}"
version = "0.1.0"
description = "RGC extension module built by pyrig"

[tool.pyrig]
name = "{name}"
source = "{name}.rg"
out-dir = "dist"
'''

README = '''\
# {name}

A native RGC Python extension module built by pyrig (PyO3/maturin-style).

```bash
pyrig build          # -> dist/{name}<ext-suffix>.so
PYTHONPATH=dist python3 -c "import {name}; print({name}.add(3, 4))"
```
'''


def new_project(target: Path) -> Path:
    """Scaffold an extension project under target/<name>/ and return the project directory."""
    name = target.name
    if not re.match(r"[A-Za-z_][A-Za-z0-9_]*$", name):
        raise PyrigError(
            "project name must be a valid Python module name, got '%s'" % name)
    project_dir = target
    if project_dir.exists() and any(project_dir.iterdir()):
        raise PyrigError("directory %s is not empty" % project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / (name + ".rg")).write_text(
        LIB_RG.format(name=name), encoding="utf-8")
    (project_dir / "pyproject.toml").write_text(
        PYPROJECT.format(name=name), encoding="utf-8")
    (project_dir / "README.md").write_text(
        README.format(name=name), encoding="utf-8")
    return project_dir
