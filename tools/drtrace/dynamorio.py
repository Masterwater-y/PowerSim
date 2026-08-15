from __future__ import annotations

import os
import re
import secrets
import signal
import shutil
import subprocess
import hashlib
import json
import struct
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
DIV_SIDECAR_VERSION = 2
DIV_SIDECAR_MAGIC = 0x4653444956455632
DIV_SIDECAR_HEADER = struct.Struct("<QQQqqHHHH")
DIV_SIDECAR_RECORD = struct.Struct("<QQQQQQQBBBBHH")
DIV_SIDECAR_NAME = re.compile(r"div\.(?P<pid>[0-9]+)\.(?P<tid>[0-9]+)\.bin$")
DIV_KINDS = {1, 2}
DIV_OUTCOMES = {1, 2}
DIV_DIVISOR_VALID = 0x0001


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


def _validate_div_sidecars(
    sidecar_dir: Path, capture_id: int | None = None
) -> dict[str, Any]:
    manifest_path = sidecar_dir / "manifest.json"
    if capture_id is None:
        if not manifest_path.is_file():
            raise FileNotFoundError(
                f"canonical DIV sidecar manifest is missing: {manifest_path}; "
                "recollect this trace"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest.get("schema") != "fastsim-dr-div-evidence" or
                manifest.get("version") != DIV_SIDECAR_VERSION):
            raise ValueError(f"invalid DIV sidecar schema in {manifest_path}")
        try:
            capture_id = int(manifest["capture_id"], 16)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                f"invalid DIV capture id in {manifest_path}"
            ) from error
    elif manifest_path.exists():
        raise FileExistsError(f"DIV sidecar manifest already exists: {manifest_path}")

    expected_hi = capture_id >> 64
    expected_lo = capture_id & ((1 << 64) - 1)
    files: list[dict[str, Any]] = []
    seen_threads: set[tuple[int, int]] = set()
    for path in sorted(sidecar_dir.glob("div.*.bin")):
        match = DIV_SIDECAR_NAME.fullmatch(path.name)
        if not match:
            raise ValueError(f"invalid DIV sidecar file name: {path}")
        pid = int(match.group("pid"))
        tid = int(match.group("tid"))
        if (pid, tid) in seen_threads:
            raise ValueError(f"duplicate DIV sidecar stream pid={pid} tid={tid}")
        seen_threads.add((pid, tid))
        size = path.stat().st_size
        if size < DIV_SIDECAR_HEADER.size or (
            size - DIV_SIDECAR_HEADER.size
        ) % DIV_SIDECAR_RECORD.size:
            raise ValueError(f"truncated DIV sidecar: {path}")
        with path.open("rb") as stream:
            header = DIV_SIDECAR_HEADER.unpack(stream.read(DIV_SIDECAR_HEADER.size))
            magic, hi, lo, file_pid, file_tid, version, header_size, record_size, flags = header
            if (
                magic != DIV_SIDECAR_MAGIC
                or version != DIV_SIDECAR_VERSION
                or header_size != DIV_SIDECAR_HEADER.size
                or record_size != DIV_SIDECAR_RECORD.size
                or flags != 0
                or hi != expected_hi
                or lo != expected_lo
                or file_pid != pid
                or file_tid != tid
            ):
                raise ValueError(f"invalid DIV sidecar header: {path}")
            records = 0
            while raw := stream.read(DIV_SIDECAR_RECORD.size):
                (sequence, _pc, _rax, _rdx, _divisor, _post_rax, _post_rdx,
                 div_kind, width, kind, outcome, fault_code, evidence_flags) = (
                    DIV_SIDECAR_RECORD.unpack(raw)
                )
                if (
                    sequence != records
                    or div_kind not in DIV_KINDS
                    or width not in (1, 2, 4, 8)
                    or kind not in (1, 2)
                    or outcome not in DIV_OUTCOMES
                    or evidence_flags & ~DIV_DIVISOR_VALID
                    or (outcome == 1 and fault_code != 0)
                ):
                    raise ValueError(
                        f"invalid DIV sidecar record {records} in {path}"
                    )
                records += 1
        files.append({
            "path": path.name,
            "sha256": _sha256(path),
        })
    if not files:
        raise ValueError(f"DIV capture produced no thread sidecars in {sidecar_dir}")

    validated = {
        "schema": "fastsim-dr-div-evidence",
        "version": DIV_SIDECAR_VERSION,
        "capture_id": f"{capture_id:032x}",
        "files": files,
    }
    if manifest_path.is_file():
        if manifest != validated:
            raise ValueError(f"DIV sidecar manifest content mismatch: {manifest_path}")
    else:
        manifest_path.write_text(
            json.dumps(validated, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return validated


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


def _div_capture_client_inputs() -> tuple[Path, ...]:
    source = PROJECT_ROOT / "tools" / "drtrace" / "client"
    return (*sorted(path for path in source.rglob("*") if path.is_file()),
            PROJECT_ROOT / "include" / "fastsim" / "div_sidecar.h")


def build_div_capture_client(environment: ValidationEnvironment) -> Path:
    """Explicitly build the wrapper that starts drmemtrace and emits evidence."""
    source = PROJECT_ROOT / "tools" / "drtrace" / "client"
    build = PROJECT_ROOT / "build" / "drtrace-div-capture"
    client = build / "bin" / "libfastsim_div_capture.so"
    configure = subprocess.run(
        [
            "cmake", "-S", str(source), "-B", str(build),
            f"-DDynamoRIO_DIR={environment.dynamorio_root / 'cmake'}",
            f"-DFASTSIM_DYNAMORIO_ROOT={environment.dynamorio_root}",
            "-DCMAKE_BUILD_TYPE=Release",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
    )
    if configure.returncode != 0:
        raise RuntimeError(f"failed configuring DIV capture client: {configure.stderr}")
    build_result = subprocess.run(
        ["cmake", "--build", str(build), "--parallel"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
    )
    if build_result.returncode != 0 or not client.is_file():
        raise RuntimeError(f"failed building DIV capture client: {build_result.stderr}")
    return client


def _div_capture_client(environment: ValidationEnvironment) -> Path:
    source = PROJECT_ROOT / "tools" / "drtrace" / "client"
    build = PROJECT_ROOT / "build" / "drtrace-div-capture"
    client = build / "bin" / "libfastsim_div_capture.so"
    if not client.is_file():
        raise FileNotFoundError(
            "DIV evidence client is missing; rerun without --skip-build"
        )
    if any(path.stat().st_mtime > client.stat().st_mtime
           for path in _div_capture_client_inputs()):
        raise RuntimeError(
            "DIV evidence client is stale; rerun without --skip-build"
        )
    return client


def capture_dr_trace(
    *,
    binary: Path,
    arguments: list[str],
    output_dir: Path,
    environment: ValidationEnvironment,
    use_sudo: bool = False,
    build_client: bool = True,
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
    capture_client = (
        build_div_capture_client(environment)
        if build_client else _div_capture_client(environment)
    )
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
    dr_library_dirs = (
        environment.dynamorio_root / "tools" / "lib64" / "release",
        environment.dynamorio_root / "ext" / "lib64" / "release",
        environment.dynamorio_root / "lib64" / "release",
    )
    inherited_library_path = run_env.get("LD_LIBRARY_PATH", "")
    run_env["LD_LIBRARY_PATH"] = ":".join(
        [*(str(path) for path in dr_library_dirs), inherited_library_path]
    ).rstrip(":")
    capture_stdout = output_dir / "capture.stdout"
    capture_stderr = output_dir / "capture.stderr"
    sidecar_dir = output_dir / "div-sidecar"
    sidecar_dir.mkdir()
    capture_id = secrets.randbits(128) or 1
    sudo_prefix = (
        ["sudo", "-n", "env", f"LD_LIBRARY_PATH={run_env['LD_LIBRARY_PATH']}"]
        if use_sudo else []
    )
    command = [
        "numactl",
        f"--physcpubind={cpu_range}",
        f"--membind={memory_node}",
        str(drrun),
        "-c", str(capture_client),
        "-fastsim_div_sidecar_dir", str(sidecar_dir),
        "-fastsim_div_capture_id_hi", str(capture_id >> 64),
        "-fastsim_div_capture_id_lo", str(capture_id & ((1 << 64) - 1)),
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
    _validate_div_sidecars(sidecar_dir, capture_id)
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
    sidecar_dir = trace_dir.parent / "div-sidecar"
    if not sidecar_dir.is_dir():
        raise FileNotFoundError(f"DIV operand sidecar is missing: {sidecar_dir}")
    _validate_div_sidecars(sidecar_dir)
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
            "--div-sidecar-dir", str(sidecar_dir),
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
        os.replace(temporary, output_dir)
        return tuple(output_dir / path.name for path in paths)
    except UnsupportedConversion:
        raise
    except Exception as error:
        raise RuntimeError(
            f"{error}; failed conversion staging retained at {temporary}"
        ) from error
