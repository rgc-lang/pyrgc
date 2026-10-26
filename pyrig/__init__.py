"""pyrig — the official RGC Python binding framework.

Modeled after the PyO3 / maturin experience: export RGC functions as CPython
extension modules.

    from pyrig import build
    build("examples/pyrig_demo.rg", name="pyrig_demo", outdir="build/pyrig")

Or use the CLI (available after `pip install -e pyrig/`):

    pyrig new <name>     # scaffold a new extension project
    pyrig build          # build an extension module (auto-discovers pyproject.toml [tool.pyrig])
    pyrig develop        # build and install into the active virtualenv
"""

from ._version import __version__
from .builder import PyrigError, build, discover_project
from .shim import collect_exports, gen_shim

__all__ = [
    "__version__",
    "build",
    "discover_project",
    "collect_exports",
    "gen_shim",
    "PyrigError",
]
