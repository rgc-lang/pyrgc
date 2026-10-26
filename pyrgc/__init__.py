"""pyrgc — the official RGC Python binding framework.

Modeled after the PyO3 / maturin experience: export RGC functions as CPython
extension modules.

    from pyrgc import build
    build("examples/pyrgc_demo.rg", name="pyrgc_demo", outdir="build/pyrgc")

Or use the CLI (available after `pip install -e pyrgc/`):

    pyrgc new <name>     # scaffold a new extension project
    pyrgc build          # build an extension module (auto-discovers pyproject.toml [tool.pyrgc])
    pyrgc develop        # build and install into the active virtualenv
"""

from ._version import __version__
from .builder import PyrgcError, build, discover_project
from .shim import collect_exports, gen_shim

__all__ = [
    "__version__",
    "build",
    "discover_project",
    "collect_exports",
    "gen_shim",
    "PyrgcError",
]
