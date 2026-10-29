#!/usr/bin/env python3
"""Build an isolated GCC 11.5 Linux x86-64 libgomp with functional markers.

Uses the selected configured build's generated headers and exact source object
list, recompiling ALL objects because the private team layout changes.
Never changes the source installation or the original build directory.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


def build(source: Path, configured: Path, output: Path, compiler: str, jobs: int,
          thread_state: bool = False):
    version = (source / "gcc/BASE-VER").read_text().strip()
    if version != "11.5.0":
        raise ValueError(f"runtime patch requires GCC 11.5.0, found {version}")
    config = (configured / "config.h").read_text()
    if "#define HAVE_TLS 1" not in config:
        raise ValueError("configured runtime requires native TLS")
    if output.exists():
        raise ValueError(f"refusing to overwrite runtime build: {output}")
    output.mkdir(parents=True)
    src = output / "src"
    shutil.copytree(source / "libgomp", src)
    for name in ("config.h", "omp.h", "libgomp_f.h"):
        shutil.copy2(configured / name, output / name)
    patch = Path(__file__).resolve().parents[1] / "patches/libgomp-11.5-functional-sync.patch"
    subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(patch)], cwd=src, check=True)
    wait_patch = Path(__file__).resolve().parents[1] / "patches/libgomp-11.5-thread-state.patch"
    if thread_state:
        subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(wait_patch)], cwd=src, check=True)
    makefile = (configured / "Makefile").read_text()
    objects = re.search(r"am_libgomp_la_OBJECTS = (.*?)\nlibgomp_la_OBJECTS", makefile, re.S)
    if not objects:
        raise ValueError("configured build has no libgomp object list")
    names = re.findall(r"([\w-]+)\.lo", objects[1])
    search = [src / "config/linux/x86", src / "config/linux", src / "config/posix", src]
    includes = [output, src, *search, source / "include"]
    flags = ["-g", "-gdwarf-4", "-O2", "-pthread", "-fPIC", "-DHAVE_CONFIG_H", "-Wall", "-Werror",
             "-ftls-model=initial-exec", "-DUSING_INITIAL_EXEC_TLS", "-fcf-protection", "-mshstk"]
    flags += [f"-I{p}" for p in includes]
    commands = []
    def compile_one(name):
        candidates = [directory / f"{name}.c" for directory in search]
        unit = next((p for p in candidates if p.exists()), None)
        if unit is None:
            raise ValueError(f"cannot locate source for {name}")
        command = [compiler, *flags, "-c", str(unit), "-o", str(output / f"{name}.o")]
        commands.append(command)
        subprocess.run(command, check=True, capture_output=True)
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        list(executor.map(compile_one, names))
    archive = output / "libgomp.a"
    subprocess.run(["ar", "rcs", str(archive), *[str(output / f"{n}.o") for n in names]], check=True)
    manifest = dict(schema="fastsim-sync-runtime-build-v1", gcc_version=version,
                    compiler=compiler, source=str(source), configured=str(configured),
                    objects=len(names), compile_commands=sorted(commands),
                    patch_sha256=hashlib.sha256(patch.read_bytes()).hexdigest(),
                    library_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    if thread_state:
        manifest.update(schema="fastsim-thread-state-runtime-build-v1",
                        wait_patch_sha256=hashlib.sha256(wait_patch.read_bytes()).hexdigest(),
                        wait_marker_symbol="fastsim_wait_marker", wait_marker_abi=1,
                        identity_scope="process-lifetime; no fork after initialization")
    (output / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="GCC source root")
    parser.add_argument("--configured", type=Path, required=True, help="configured libgomp build directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compiler", default="/opt/gcc-11/bin/gcc")
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--thread-state", action="store_true",
                        help="also apply x86-64 per-request wait metadata patch (no-fork pilot)")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("jobs must be positive")
    print(build(args.source.resolve(), args.configured.resolve(), args.output.resolve(),
                args.compiler, args.jobs, args.thread_state))


if __name__ == "__main__":
    main()
