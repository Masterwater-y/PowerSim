from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import hashlib
from pathlib import Path
from typing import Any

from .environment import (
    TraceRuntime,
    ValidationEnvironment,
    load_trace_runtime,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RECORD_FUNCTIONS = (
    "cpu_microarch_roi_thread_begin|2&"
    "cpu_microarch_roi_thread_end|2"
)
UNSUPPORTED_RE = re.compile(
    r"FASTSIM_UNSUPPORTED reason_code=(?P<reason_code>[a-z0-9_]+) "
    r"pc=(?P<pc>0x[0-9a-fA-F]+): (?P<reason>.+)"
)


class UnsupportedConversion(RuntimeError):
    def __init__(self, *, reason_code: str, pc: int, reason: str) -> None:
        self.reason_code = reason_code
        self.pc = int(pc)
        self.reason = reason
        super().__init__(
            f"{reason_code} at {self.pc:#x}: {reason}"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_cpu_list(text: str) -> list[int]:
    cpus: list[int] = []
    for segment in text.strip().split(","):
        if not segment:
            continue
        if "-" in segment:
            start, end = (int(value) for value in segment.split("-", 1))
            cpus.extend(range(start, end + 1))
        else:
            cpus.append(int(segment))
    return cpus


def dr_runtime_binding(num_cores: int) -> tuple[list[int], int]:
    allowed = set(os.sched_getaffinity(0))
    nodes = sorted(
        Path("/sys/devices/system/node").glob("node[0-9]*"),
        key=lambda path: int(path.name[4:]),
        reverse=True,
    )
    for node_path in nodes:
        cpulist = node_path / "cpulist"
        if not cpulist.is_file():
            continue
        node_cpus = [
            cpu for cpu in _parse_cpu_list(cpulist.read_text(encoding="utf-8"))
            if cpu in allowed
        ]
        for index in range(0, len(node_cpus) - num_cores + 1):
            selected = node_cpus[index:index + num_cores]
            if selected == list(range(selected[0], selected[0] + num_cores)):
                return selected, int(node_path.name[4:])
    raise RuntimeError(
        f"no contiguous {num_cores}-CPU range is available in the current affinity"
    )


def _run(
    command: list[str],
    *,
    cwd: Path,
    stdout: Path | None = None,
    stderr: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    stdout_handle: Any = (
        stdout.open("w", encoding="utf-8") if stdout else subprocess.PIPE
    )
    stderr_handle: Any = (
        stderr.open("w", encoding="utf-8") if stderr else subprocess.PIPE
    )
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            text=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
            env=env,
            start_new_session=True,
        )
        try:
            process.communicate()
        except BaseException:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
        return subprocess.CompletedProcess(command, process.returncode)
    finally:
        if stdout:
            stdout_handle.close()
        if stderr:
            stderr_handle.close()


def _single_trace_dir(root: Path) -> Path:
    candidates = sorted(
        path for path in root.iterdir()
        if path.is_dir() and path.name.startswith("drmemtrace")
    )
    if len(candidates) != 1:
        raise RuntimeError(
            f"DynamoRIO capture must produce one trace directory under {root}: "
            f"{[path.name for path in candidates]}"
        )
    return candidates[0]


def capture_dr_trace(
    *,
    binary: Path,
    arguments: list[str],
    output_dir: Path,
    environment: ValidationEnvironment,
    use_sudo: bool = False,
) -> dict[str, Any]:
    binary = binary.resolve()
    output_dir = output_dir.resolve()
    drrun = environment.drrun
    if not binary.is_file():
        raise FileNotFoundError(f"DR workload does not exist: {binary}")
    if not drrun.is_file():
        raise FileNotFoundError(f"DynamoRIO drrun does not exist: {drrun}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"DR output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if not arguments:
        raise ValueError("DR workload arguments must start with the core count")
    num_cores = int(arguments[0])
    if not 1 <= num_cores <= 32:
        raise ValueError(f"DR core count must be in [1,32], got {num_cores}")
    cpus, memory_node = dr_runtime_binding(num_cores)
    cpu_range = (
        str(cpus[0]) if len(cpus) == 1 else f"{cpus[0]}-{cpus[-1]}"
    )
    run_env = os.environ.copy()
    run_env["FASTSIM_CPU_BASE"] = str(cpus[0])
    capture_stdout = output_dir / "capture.stdout"
    capture_stderr = output_dir / "capture.stderr"
    sudo_prefix = ["sudo", "-n"] if use_sudo else []
    command = [
        "numactl",
        f"--physcpubind={cpu_range}",
        f"--membind={memory_node}",
        str(drrun),
        "-t", "drmemtrace",
        "-offline",
        "-record_function", RECORD_FUNCTIONS,
        "-raw_compress", "lz4",
        "-outdir", str(output_dir),
        "-subdir_prefix", "drmemtrace",
        "--", str(binary),
        *arguments,
    ]
    command.insert(command.index("-record_function"), "-use_physical")
    if sudo_prefix:
        # Do not allow an interactive password prompt to make a batch matrix
        # hang indefinitely.  The resulting stderr is retained with the trace
        # for diagnosing sudoers, namespace, or capability failures.
        command = [*sudo_prefix, *command]
    result = _run(
        command,
        cwd=PROJECT_ROOT,
        stdout=capture_stdout,
        stderr=capture_stderr,
        env=run_env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"DynamoRIO capture failed rc={result.returncode}: {capture_stderr}"
        )
    trace_dir = _single_trace_dir(output_dir)
    invariant_log = output_dir / "invariant_checker.log"
    result = _run(
        [
            *sudo_prefix,
            str(drrun), "-t", "drmemtrace",
            "-indir", str(trace_dir),
            "-tool", "invariant_checker",
            "-jobs", str(num_cores),
        ],
        cwd=PROJECT_ROOT,
        stdout=invariant_log,
        stderr=output_dir / "invariant_checker.stderr",
    )
    if result.returncode != 0:
        raise RuntimeError(f"DynamoRIO invariant checker failed: {invariant_log}")
    return {
        "trace_dir": trace_dir,
        "cpus": cpus,
        "memory_node": memory_node,
        "invariant_checker": invariant_log,
        "sudo": use_sudo,
    }


def _roi_function_ids(trace_dir: Path) -> tuple[int, int]:
    path = trace_dir / "raw" / "funclist.log"
    if not path.is_file():
        raise FileNotFoundError(f"DynamoRIO ROI function list is missing: {path}")
    names = {
        "cpu_microarch_roi_thread_begin": [],
        "cpu_microarch_roi_thread_end": [],
    }
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split(",")
        if len(fields) < 2:
            continue
        symbol = fields[-1].rsplit("!", 1)[-1]
        if symbol not in names:
            continue
        try:
            names[symbol].append(int(fields[0]))
        except ValueError:
            continue
    for symbol, values in names.items():
        if len(values) != 1:
            raise RuntimeError(
                f"DynamoRIO ROI function {symbol} must resolve exactly once: {values}"
            )
    return (
        names["cpu_microarch_roi_thread_begin"][0],
        names["cpu_microarch_roi_thread_end"][0],
    )


def convert_dr_trace(
    *,
    trace_dir: Path,
    output_dir: Path,
    num_cores: int,
    runtime: TraceRuntime | None = None,
) -> tuple[Path, ...]:
    runtime = runtime or load_trace_runtime()
    trace_dir = trace_dir.resolve()
    output_dir = output_dir.resolve()
    converter = runtime.converter
    config = (
        PROJECT_ROOT / "vendor" / "gem5_patch" / "dr_converter"
        / "config" / "drmemtrace_x86_to_functional.py"
    )
    if not converter.is_file():
        raise FileNotFoundError(
            f"DR converter does not exist: {converter}; run tools/build_gem5.sh"
        )
    if output_dir.exists():
        raise FileExistsError(f"DR raw functional output already exists: {output_dir}")
    if not 1 <= int(num_cores) <= 32:
        raise ValueError(f"DR core count must be in [1,32], got {num_cores}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_dir.with_name(f".{output_dir.name}.staging")
    if temporary.exists():
        raise FileExistsError(
            f"failed conversion staging directory already exists: {temporary}"
        )
    temporary.mkdir()
    begin_id, end_id = _roi_function_ids(trace_dir)
    try:
        gem5_out = temporary / "gem5"
        command = [
            str(converter),
            "--outdir", str(gem5_out),
            str(config),
            "--input-trace", str(trace_dir / "trace"),
            "--output-dir", str(temporary),
            "--roi-begin-func-id", str(begin_id),
            "--roi-end-func-id", str(end_id),
            "--num-cores", str(int(num_cores)),
        ]
        result = _run(
            command,
            cwd=PROJECT_ROOT,
            env=runtime.runtime_environ(),
            stdout=temporary / "converter.stdout",
            stderr=temporary / "converter.stderr",
        )
        if result.returncode != 0:
            stderr_path = temporary / "converter.stderr"
            stderr_text = stderr_path.read_text(
                encoding="utf-8", errors="replace"
            )
            unsupported = UNSUPPORTED_RE.search(stderr_text)
            if unsupported:
                raise UnsupportedConversion(
                    reason_code=unsupported.group("reason_code"),
                    pc=int(unsupported.group("pc"), 16),
                    reason=unsupported.group("reason").strip(),
                )
            raise RuntimeError(
                f"DR raw functional converter failed rc={result.returncode}: "
                f"{stderr_path}"
            )
        paths = []
        for core_id in range(int(num_cores)):
            target = temporary / f"core{core_id}.fst"
            if not target.is_file() or target.stat().st_size <= 72:
                raise RuntimeError(f"DR converter produced empty core {core_id}")
            paths.append(target)
        shutil.rmtree(gem5_out, ignore_errors=True)
        (temporary / "converter.stdout").unlink(missing_ok=True)
        (temporary / "converter.stderr").unlink(missing_ok=True)
        (temporary / "manifest.txt").write_text(
            "".join(
                f"{core} fastsim-binary core{core}.fst\n"
                for core in range(int(num_cores))
            ),
            encoding="utf-8",
        )
        metadata: dict[str, Any] = {
            "schema": "fastsim-dr-fst-v3",
            "strict_physical_address": True,
            "cores": int(num_cores),
            "fst_version": 6,
            "input_trace": str(trace_dir),
            "address_provenance": {
                "path": "address-provenance.json",
                "sha256": _sha256(temporary / "address-provenance.json"),
                "scope": "single_address_space_per_logical_core",
            },
        }
        (temporary / "trace.json").write_text(
            __import__("json").dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output_dir)
        return tuple(output_dir / path.name for path in paths)
    except UnsupportedConversion:
        raise
    except Exception as error:
        raise RuntimeError(
            f"{error}; failed conversion staging retained at {temporary}"
        ) from error
