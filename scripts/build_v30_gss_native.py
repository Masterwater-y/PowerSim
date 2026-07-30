#!/usr/bin/env python3
"""Build the small pybind11 v30 GSS hot-path extension in place."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sysconfig


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "tcsim" / "v30" / "native_gss.cpp"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cxx", default=os.environ.get("CXX", "c++"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    try:
        import pybind11
    except ImportError as error:
        raise SystemExit("pybind11 is required to build v30 native GSS") from error
    suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    output = SOURCE.with_name(f"_gss_native{suffix}")
    temporary = output.with_name(output.name + f".tmp.{os.getpid()}")
    command = [
        args.cxx, "-O3", "-DNDEBUG", "-std=c++17", "-shared", "-fPIC",
        f"-I{pybind11.get_include()}",
        f"-I{sysconfig.get_paths()['include']}",
        str(SOURCE), "-o", str(temporary),
    ]
    if args.verbose:
        print(" ".join(shlex.quote(value) for value in command))
    try:
        subprocess.run(command, check=True)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
