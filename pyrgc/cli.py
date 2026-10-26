"""pyrgc command-line entry point (maturin-style: new / build / develop)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .builder import build, build_project, develop, version_string
from .shim import PyrgcError
from .scaffold import new_project


def _common_build_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("path", nargs="?", default=".",
                   help="RGC source file, or a project directory with [tool.pyrgc] (default: current directory)")
    p.add_argument("-n", "--name", help="extension module name (default: source filename / pyproject project name)")
    p.add_argument("-o", "--out", default=None, help="output directory (default: current directory / pyproject out-dir)")
    p.add_argument("--rgc", default=None, help="rgc compiler path (default: RGC env / PATH / repo build/rgc)")
    p.add_argument("--cc", default=None, help="C compiler (default: cc)")
    p.add_argument("--debug", action="store_true", help="build without optimization (rgc -O0); default is release")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="pyrgc",
        description="Build and publish RGC-based Python extension modules "
                    "(CPython C API bindings for the RGC language).")
    ap.add_argument("--version", action="version", version=version_string())
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new", help="scaffold a new extension project (like maturin new)")
    p_new.add_argument("target", help="project directory (the directory name is the module name)")

    p_build = sub.add_parser("build", help="build an extension module")
    _common_build_args(p_build)

    p_dev = sub.add_parser("develop", help="build and install into the active virtualenv (like maturin develop)")
    _common_build_args(p_dev)

    args = ap.parse_args(argv)
    try:
        if args.cmd == "new":
            project = new_project(Path(args.target))
            print("project created: %s" % project)
            print("next: cd %s && pyrgc build" % project)
            return 0

        overrides = {
            "name": args.name,
            "outdir": Path(args.out) if args.out else None,
            "rgc": args.rgc,
            "cc": args.cc,
            "release": not args.debug,
        }
        path = Path(args.path)
        if args.cmd == "build":
            if path.is_file():
                out = build(path, name=args.name,
                            outdir=Path(args.out) if args.out else Path("."),
                            rgc=args.rgc, cc=args.cc or "cc",
                            release=not args.debug)
            else:
                out = build_project(path, overrides)
        else:  # develop
            out = develop(path, overrides)
        print("built: %s" % out)
        return 0
    except PyrgcError as e:
        print("pyrgc: error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
