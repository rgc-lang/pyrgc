"""pyrgc build orchestration: rgc --emit-object/--emit-exports -> scan markers -> C glue -> .so.

Mirrors maturin's build model: project configuration is read from the
``[tool.pyrgc]`` section of ``pyproject.toml`` (``pyrgc build`` walks up from the
project directory automatically).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import __version__
from .shim import (PyrgcError, Export, collect_exports, gen_shim, load_functions,
                   ownership_warnings)

PROJECT_SECTION = "[tool.pyrgc]"


@dataclass
class ProjectConfig:
    """``[tool.pyrgc]`` configuration plus CLI overrides."""

    source: Path
    name: Optional[str] = None
    outdir: Path = Path(".")
    rgc: Optional[str] = None
    cc: str = "cc"
    release: bool = True
    link_args: List[str] = field(default_factory=list)


def discover_project(start: Path) -> Optional[Path]:
    """Walk up from start to find a pyproject.toml containing [tool.pyrgc]."""
    cur = start.resolve()
    for cand in [cur, *cur.parents]:
        pyproject = cand / "pyproject.toml"
        if pyproject.is_file():
            text = pyproject.read_text(encoding="utf-8")
            if PROJECT_SECTION in text:
                return pyproject
    return None


def parse_tool_pyrgc(text: str) -> dict:
    """Zero-dependency parse of the [tool.pyrgc] section of pyproject.toml (key = value, bool/str)."""
    cfg: dict = {}
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            inside = line == PROJECT_SECTION
            continue
        if not inside or not line or line.startswith("#"):
            continue
        m = re.match(r'([A-Za-z0-9_-]+)\s*=\s*(.+?)\s*(?:#.*)?$', line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value.lower() in ("true", "false"):
            cfg[key] = value.lower() == "true"
        elif value.startswith('"') and value.endswith('"') and len(value) >= 2:
            cfg[key] = value[1:-1]
        else:
            cfg[key] = value
    return cfg


def load_config(path: Path) -> ProjectConfig:
    """Load [tool.pyrgc] configuration from pyproject.toml."""
    pyproject = discover_project(path)
    if pyproject is None:
        raise PyrgcError(
            "no [tool.pyrgc] section found in pyproject.toml "
            "(searched upward from %s)" % path)
    cfg = parse_tool_pyrgc(pyproject.read_text(encoding="utf-8"))
    root = pyproject.parent
    source = cfg.get("source", "")
    if not source:
        raise PyrgcError("%s: [tool.pyrgc] missing 'source' (RGC entry file)" %
                         pyproject)
    src_path = (root / source).resolve()
    if not src_path.is_file():
        raise PyrgcError("%s: source '%s' does not exist" % (pyproject, source))
    return ProjectConfig(
        source=src_path,
        name=cfg.get("name") or root.name,
        outdir=Path(cfg.get("out-dir", ".")) if cfg.get("out-dir") else root,
        rgc=cfg.get("rgc"),
        cc=cfg.get("cc", "cc"),
        release=cfg.get("release", True),
        link_args=str(cfg.get("link-args", "")).split() if cfg.get("link-args") else [],
    )


def default_rgc() -> str:
    """Locate rgc: environment RGC / PATH / repo build/rgc."""
    env = os.environ.get("RGC")
    if env and Path(env).is_file():
        return env
    which = shutil.which("rgc")
    if which:
        return which
    repo_build = Path(__file__).resolve().parents[2] / "build" / "rgc"
    if repo_build.is_file():  # dev checkout: pyrgc/pyrgc/builder.py
        return str(repo_build)
    return "rgc"


def _run(cmd: List[str], cwd: Optional[Path] = None) -> None:
    proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise PyrgcError("command failed: %s\n%s" %
                         (" ".join(cmd), proc.stdout.strip()))


def build(source: Path, name: Optional[str] = None, outdir: Path = Path("."),
          rgc: Optional[str] = None, cc: str = "cc", release: bool = True,
          link_args: Optional[List[str]] = None) -> Path:
    """Build an RGC source file into a CPython extension module; returns the .so path."""
    source = Path(source)
    if not source.is_file():
        raise PyrgcError("cannot read %s" % source)
    module = re.sub(r"[^A-Za-z0-9_]", "_",
                    name or source.stem)
    if not re.match(r"[A-Za-z_][A-Za-z0-9_]*$", module):
        raise PyrgcError("invalid module name '%s'" % module)

    code = source.read_text(encoding="utf-8")

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    shim_path = outdir / ("pyrgc_shim_%s.c" % module)
    obj_path = outdir / ("pyrgc_%s.o" % module)
    meta_path = outdir / ("pyrgc_%s.exports.json" % module)
    suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    out_path = outdir / (module + suffix)

    compiler = rgc or default_rgc()
    if not Path(compiler).is_file() and not shutil.which(compiler):
        raise PyrgcError("rgc compiler not found: %s (use --rgc or RGC env)" %
                         compiler)
    obj_cmd = [compiler, str(source), "--emit-object", "-o", str(obj_path),
               "--emit-exports", str(meta_path)]
    if not release:
        obj_cmd.append("-O0")
    _run(obj_cmd)
    if not meta_path.is_file():
        raise PyrgcError(
            "%s produced no export metadata; --emit-exports requires a current rgc"
            % compiler)

    functions = load_functions(meta_path.read_text(encoding="utf-8"), str(meta_path))
    exports: List[Export] = collect_exports(code, functions, str(source))
    for warning in ownership_warnings(exports):
        print("pyrgc: warning: %s" % warning, file=sys.stderr)

    shim_path.write_text(gen_shim(module, exports, str(source)), encoding="utf-8")

    inc = sysconfig.get_paths()["include"]
    cmd = [cc, "-shared", "-fPIC", "-O2" if release else "-O0", "-I", inc,
           str(shim_path), str(obj_path), "-lm", "-o", str(out_path)]
    plat_inc = sysconfig.get_paths().get("platinclude")
    if plat_inc and plat_inc != inc:
        cmd[3:3] = ["-I", plat_inc]
    if sys.platform == "darwin":
        # macOS resolves every symbol at link time, but a CPython extension gets its Py*
        # symbols from the interpreter that dlopens it. This is what distutils passes.
        cmd.append("-undefined")
        cmd.append("dynamic_lookup")
    cmd.extend(link_args or [])
    _run(cmd)
    obj_path.unlink()
    return out_path


def build_project(path: Path, overrides: Optional[dict] = None) -> Path:
    """Build according to the pyproject.toml [tool.pyrgc] config; overrides are CLI overrides."""
    cfg = load_config(path)
    if overrides:
        for key, value in overrides.items():
            if value is not None:
                setattr(cfg, key, value)
    return build(cfg.source, name=cfg.name, outdir=cfg.outdir, rgc=cfg.rgc,
                 cc=cfg.cc, release=cfg.release, link_args=cfg.link_args)


def develop(path: Path, overrides: Optional[dict] = None) -> Path:
    """Build and install into the active virtualenv (like maturin develop).

    Only virtual environments are supported: writing directly to system
    site-packages is neither safe nor reversible.
    """
    venv = os.environ.get("VIRTUAL_ENV")
    if not venv:
        raise PyrgcError(
            "pyrgc develop requires an activated virtualenv "
            "(or use `pyrgc build -o <dir>` and set PYTHONPATH)")
    platlib = Path(venv) / "lib" / (
        "python%d.%d" % sys.version_info[:2]) / "site-packages"
    if not platlib.is_dir():
        raise PyrgcError("virtualenv site-packages not found: %s" % platlib)
    return build_project(path, overrides)


def version_string() -> str:
    return "pyrgc %s" % __version__
