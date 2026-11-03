#!/usr/bin/env python3
"""Collect approved native common-end uarch and TSO baseline experiments."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time

from collect_spec2026_uarch_fs import effective_profile, fastsim_expected, static_preflight
from collect_common_end_fst import finalize_capture, sha256
from common_end_capture import POLICY, validate_common_end
from run_native_kernel_fastsim_validation import config_include_chain

ROOT = Path(__file__).resolve().parents[1]
TCSIM = Path("/data00/yinhaolang/TCSim")
SCHEMA = "fastsim-spec2026-fs-uarch-sweep-v3"
TSO_BASELINE_SCHEMAS = {
    "fastsim-spec2026-fs-tso-baseline-v1",
    "fastsim-spec2026-fs-tso-baseline-v2",
    "fastsim-spec2026-fs-tso-baseline-v3",
}
TSO_BASELINE_BINARY_SHA256 = {
    "fastsim-spec2026-fs-tso-baseline-v1": (
        "74d101122e4775f3cbf0c489af403ccbddb201a9a738ec13f3125edfa51d1e7a"
    ),
    "fastsim-spec2026-fs-tso-baseline-v2": (
        "468205d2b8f0cbd3ae3b23951e16ab4c48296621ab31f8187a4af14a635e398f"
    ),
    "fastsim-spec2026-fs-tso-baseline-v3": (
        "468205d2b8f0cbd3ae3b23951e16ab4c48296621ab31f8187a4af14a635e398f"
    ),
}
TSO_BASELINE_RUNTIME_LIBSTDCXX_SHA256 = (
    "9ac7e2ec283e4a4a9a1ffdf0a17d688ca90e7f9d020cb8f372af7fa5194618f0"
)
ZSTD_PREFORK_BINARY_SHA256 = (
    "76efb6c3126b105026700567023c47473232194d3ee272b7478b8116a8824919"
)
ZSTD_PREFORK_DISK_SHA256 = (
    "d2053efd398881db97325591ef5f8a8e2ad6b3d2d0db2e20a9322917d078b8a7"
)
GIB = 1 << 30
DEFAULT_OUTPUT = ROOT / "tmp/uarch-generalization-c4-c8-20260917"
DEFAULT_MATRIX = ROOT / "configs/spec2026-uarch-generalization-c4-c8-v3.json"
# Combined-fix collector: adds the Busy-directory functional-read fallback
# (clean I->IM lines resolve against valid backing DRAM instead of a fatal) and
# the empty-measurement-core common-end fix (a participant that never observed a
# measured user event still anchors its CPL row at the shared common-end tick).
# Rebuilt from tmp/se-fs-paired-c4-20260820/gem5-se-isolated on 2026-09-17.
DEFAULT_BINARY = ROOT / "tmp/uarch-common-end-fix-20260917/gem5.opt"
FACTORS = ("l1d_size", "l2_size", "l3_size_per_bank", "rob_entries")
LEVELS = (("16KiB", "64KiB"), ("512KiB", "2MiB"), ("4MiB", "16MiB"), (96, 256))


def now():
    return datetime.now(timezone.utc).isoformat()


def load(path):
    return json.loads(Path(path).read_text())


def save(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def emit(message, *, file=None):
    stream = sys.stdout if file is None else file
    try:
        print(message, file=stream, flush=True)
    except BrokenPipeError:
        return False
    return True


def freeze_json(path, value):
    if not path.exists():
        save(path, value)
        return
    previous = load(path)
    if previous == value:
        return
    if path.name == "run-contract.json":
        immutable_keys = (set(previous) | set(value)) - {"capture_phases", "inputs"}
        immutable = {key: previous.get(key) for key in immutable_keys}
        expected = {key: value.get(key) for key in immutable_keys}
        if immutable == expected:
            save(path, value)
            return
    raise ValueError(f"frozen contract changed: {path}; choose a new --output")


def validate_design(matrix):
    schema = matrix.get("schema")
    if schema != SCHEMA and schema not in TSO_BASELINE_SCHEMAS:
        raise ValueError("an approved common-end matrix is required")
    if matrix.get("measurement_policy") != POLICY:
        raise ValueError("first-core common-end policy is required")
    names = [w["name"] for w in matrix["workloads"]]
    if matrix.get("target_records_per_core") != 10000000 or matrix.get("smoke_records_per_core") != 10000:
        raise ValueError("formal/smoke targets must be 10M/10K user UOP")
    baseline = dict(clk="3GHz", mem_size="3GiB", rob_entries=192,
        l1i_size="32KiB", l1i_assoc=8, l1d_size="32KiB", l1d_assoc=8,
        l2_size="1MiB", l2_assoc=8, l3_size_per_bank="8MiB", l3_assoc=16,
        num_l3_banks=8, mem_channels=8)
    if matrix["common"] != baseline:
        raise ValueError("fixed hardware baseline changed")
    profiles = matrix["profiles"]
    if schema in TSO_BASELINE_SCHEMAS:
        if matrix.get("required_binary_sha256") != (
            TSO_BASELINE_BINARY_SHA256[schema]
        ):
            raise ValueError(
                f"{schema} requires its frozen gem5 binary SHA256"
            )
        expected_names = [
            "706.stockfish_r", "710.omnetpp_r", "777.zstd_r", "782.lbm_r",
            "803.sph_exa_s", "811.tealeaf_s", "816.nab_s",
            "854.graph500_s", "857.namd_s", "881.neutron_s",
        ]
        if matrix.get("core_counts") != [4, 8, 16, 32]:
            raise ValueError("TSO baseline requires C4/C8/C16/C32")
        if names != expected_names:
            raise ValueError("the frozen 10-workload baseline is required")
        if len(profiles) != 1 or profiles[0] != {
            "id": "baseline",
            "description": (
                "ROB192, L1D 32KiB, private L2 1MiB, total LLC 64MiB"
            ),
            "domain": "baseline",
            "design_class": "baseline",
            "gem5": {},
            "fastsim": {
                "core.rob_entries": 192,
                "cache.l1d.size": "32KiB",
                "cache.l1d.associativity": 8,
                "cache.l2.size": "1MiB",
                "cache.l2.associativity": 8,
                "cache.llc.size": "64MiB",
                "cache.llc.associativity": 16,
                "uncore.cha_count": 8,
                "dram.channels": 8,
                "dram.size": "3GiB",
            },
        }:
            raise ValueError("TSO baseline requires the frozen baseline profile")
        required_tso = matrix.get("required_tso")
        if required_tso != {
            "mode": "buffer",
            "buffer_size": 114,
            "buffer_active": True,
        }:
            raise ValueError("TSO baseline requires buffer mode with 114 entries")
        if schema in {
            "fastsim-spec2026-fs-tso-baseline-v2",
            "fastsim-spec2026-fs-tso-baseline-v3",
        } and matrix.get(
            "required_checkpoint_recovery"
        ) != {
            "timeout_seconds": 7200,
            "max_output_mib": 256,
            "post_workbegin_insts_per_core": 1,
            "tao_emit_records": False,
        }:
            raise ValueError(
                f"{schema} requires the bounded no-record WORKBEGIN gate"
            )
        if schema in {
            "fastsim-spec2026-fs-tso-baseline-v2",
            "fastsim-spec2026-fs-tso-baseline-v3",
        } and matrix.get(
            "required_runtime_libstdcxx_sha256"
        ) != TSO_BASELINE_RUNTIME_LIBSTDCXX_SHA256:
            raise ValueError(
                f"{schema} requires the frozen runtime libstdc++ SHA256"
            )
        if schema == "fastsim-spec2026-fs-tso-baseline-v3":
            zstd = next(
                workload for workload in matrix["workloads"]
                if workload["name"] == "777.zstd_r"
            )
            if zstd.get(
                "required_workload_binary_sha256"
            ) != ZSTD_PREFORK_BINARY_SHA256:
                raise ValueError(
                    "TSO baseline v3 requires the frozen Zstd pre-fork binary"
                )
            if zstd.get("required_aux_disk_sha256") != ZSTD_PREFORK_DISK_SHA256:
                raise ValueError(
                    "TSO baseline v3 requires the frozen Zstd pre-fork disk"
                )
        fastsim_expected(matrix, profiles[0])
        return matrix
    if matrix.get("core_counts") != [4, 8]:
        raise ValueError("both C4 and C8 are required")
    if len(names) != 4 or set(names) != {
        "706.stockfish_r", "731.astcenc_r", "811.tealeaf_s", "854.graph500_s",
    }:
        raise ValueError("the four approved workloads are required")
    if len(profiles) != 17 or len({p["id"] for p in profiles}) != 17:
        raise ValueError("exactly 17 distinct profiles are required")
    seen, classes, joints, singles = set(), [], [], set()
    for profile in profiles:
        if not re.fullmatch(r"[a-z0-9_]+", profile["id"]):
            raise ValueError("unsafe profile ID")
        changes = profile.get("gem5", {})
        if not set(changes) <= set(FACTORS):
            raise ValueError("only the four capacity parameters may vary")
        for key, value in changes.items():
            if value not in LEVELS[FACTORS.index(key)]:
                raise ValueError(f"invalid endpoint for {key}")
        cls = "baseline" if not changes else "single" if len(changes) == 1 else "multi"
        if profile.get("design_class") != cls:
            raise ValueError("incorrect design_class")
        classes.append(cls)
        effective = effective_profile(matrix, profile)
        geometry = tuple(effective[k] for k in FACTORS)
        if geometry in seen:
            raise ValueError("duplicate effective hardware configuration")
        seen.add(geometry)
        fastsim_expected(matrix, profile)
        if cls == "single":
            singles.add(next(iter(changes.items())))
        if cls == "multi":
            if len(changes) != 4:
                raise ValueError("joint profiles must change all four factors")
            signs = [(-1 if effective[k] == levels[0] else 1) for k, levels in zip(FACTORS, LEVELS)]
            if signs[3] != signs[0] * signs[1] * signs[2]:
                raise ValueError("joint corner does not satisfy D=ABC")
            joints.append(geometry)
    if [classes.count(c) for c in ("baseline", "single", "multi")] != [1, 8, 8]:
        raise ValueError("expected baseline + 8 single + 8 joint profiles")
    if singles != {(k, v) for k, values in zip(FACTORS, LEVELS) for v in values}:
        raise ValueError("missing single-factor endpoint")
    return matrix


def tasks_for(matrix):
    return [dict(case_id=f"uarch-{p['id']}-c{c:02d}-{w['name']}", profile=p,
                 workload=w, cores=c, design_class=p["design_class"])
            for p in matrix["profiles"] for c in matrix["core_counts"] for w in matrix["workloads"]]


def choose_jobs(cases, cpus, available_bytes, per_job_gib, reserve_gib):
    if per_job_gib <= 0 or reserve_gib < 0:
        raise ValueError("invalid memory budget")
    slots = int((available_bytes - reserve_gib * GIB) // (per_job_gib * GIB))
    result = min(cases, cpus, slots)
    if result < 1:
        raise ValueError("insufficient available CPU/memory for one collection")
    return result


def geometry_args(task, matrix):
    g = effective_profile(matrix, task["profile"])
    mapping = {"mem_size": "mem-size", "rob_entries": "rob-entries",
        "l1i_size": "l1i-size", "l1i_assoc": "l1i-assoc", "l1d_size": "l1d-size",
        "l1d_assoc": "l1d-assoc", "l2_size": "l2-size", "l2_assoc": "l2-assoc",
        "l3_size_per_bank": "l3-size", "l3_assoc": "l3-assoc",
        "num_l3_banks": "num-l3-banks", "mem_channels": "mem-channels"}
    return [part for k, flag in mapping.items() for part in ("--" + flag, str(g[k]))]


def capture_command(
    task, matrix, binary, tcsim_root, resources, checkpoint, output, target,
    tso_store="serial", tso_store_buffer_size=114,
):
    command = [str(binary), "-d", str(output),
        str(tcsim_root / "configs/gem5/x86_fs_kvm_boot_checkpoint_tao.py"),
        "--action", "restore", "--restore-cpu-type", "o3", "--resume-at-roi",
        "--num-cores", str(task["cores"]), "--checkpoint-dir", str(checkpoint),
        "--cache-hierarchy", "mesi-three-level", "--clk", matrix["common"]["clk"],
        *geometry_args(task, matrix), "--resource-dir", str(resources),
        "--aux-disk", task["workload"]["aux_disk"], "--detailed-warmup-insts", "0",
        "--stats-outfile", str(output / "stats.txt"),
        "--roi-user-records-per-core", str(target), "--roi-stop-policy", "any-core",
        "--roi-safety-max-insts-per-core", str(max(10000000, target * 100)),
        "--wait-for-roi-workbegin", "--tao-trace-dir", str(output / "trace-scratch"),
        "--tao-trace-format", "fst", "--tao-measure-cpl", "--tao-native-anomaly-limit", "32",
        "--tao-functional-include-kernel", "--tao-functional-warmup",
        "--tao-functional-user-target", str(target)]
    if tso_store == "buffer":
        command.extend([
            "--tso-store", "buffer",
            "--tso-store-buffer-size", str(tso_store_buffer_size),
        ])
    return command


def checkpoint_recovery_command(
    task, matrix, binary, tcsim_root, resources, checkpoint, output,
    tso_store="serial", tso_store_buffer_size=114,
):
    command = [str(binary), "-d", str(output),
        str(tcsim_root / "configs/gem5/x86_fs_kvm_boot_checkpoint_tao.py"),
        "--action", "restore", "--restore-cpu-type", "o3", "--resume-at-roi",
        "--num-cores", str(task["cores"]), "--checkpoint-dir", str(checkpoint),
        "--cache-hierarchy", "mesi-three-level", "--clk", matrix["common"]["clk"],
        *geometry_args(task, matrix), "--resource-dir", str(resources),
        "--aux-disk", task["workload"]["aux_disk"], "--detailed-warmup-insts", "0",
        "--stats-outfile", str(output / "stats.txt"), "--max-insts-per-core", "1",
        "--wait-for-roi-workbegin", "--tao-trace-dir", str(output / "trace-scratch"),
        "--tao-trace-format", "fst", "--tao-no-records"]
    if tso_store == "buffer":
        command.extend([
            "--tso-store", "buffer",
            "--tso-store-buffer-size", str(tso_store_buffer_size),
        ])
    return command


def complete_set(expected, states):
    return set(states) == set(expected) and all(states[k] == "complete" for k in expected)


def file_stamp(path, *, digest=False):
    stat = path.stat()
    row = dict(bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
    if digest or stat.st_size <= 16 * 1024**2:
        row["sha256"] = sha256(path)
    return row


def artifact_snapshot(root):
    return {str(p.relative_to(root)): file_stamp(p) for p in sorted(root.rglob("*"))
            if p.is_file() and p.name != "collection-status.json"
            and not p.name.endswith((".log", ".tmp"))}


def artifact_bytes(root):
    return sum(
        path.stat().st_size for path in Path(root).rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def check_snapshot(root, snapshot):
    for relative, expected in snapshot.items():
        path = root / relative
        if not path.is_file():
            raise ValueError(f"missing artifact: {path}")
        actual = file_stamp(path, digest="sha256" in expected)
        if actual != expected:
            raise ValueError(f"artifact changed: {path}")


def checkpoint_snapshot(checkpoint):
    if not (checkpoint / "metadata.json").is_file():
        raise ValueError(f"checkpoint metadata missing: {checkpoint}")
    content = (checkpoint / "m5.cpt").read_text()
    payloads = re.findall(r"^filename\s*=\s*(.+)$", content, flags=re.MULTILINE)
    if not payloads:
        raise ValueError(f"checkpoint has no memory payload: {checkpoint}")
    for relative in payloads:
        path = checkpoint / relative.strip()
        if not path.resolve().is_relative_to(checkpoint.resolve()) or not path.is_file():
            raise ValueError(f"checkpoint memory payload missing/unsafe: {path}")
    return artifact_snapshot(checkpoint)


def require_native_gate(audit, cores, participants):
    if participants != set(range(cores)) or audit.get("cores") != cores:
        raise ValueError("native summary participant set mismatch")
    for key in ("native_hierarchy_semantic_comparable", "structural_conservation", "target_drain_complete"):
        if audit.get(key) is not True:
            raise ValueError(f"native gate failed: {key}")


class Supervisor:
    """Own child sessions so a timeout/stop also terminates gem5 grandchildren."""
    def __init__(self):
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.active = {}

    @staticmethod
    def kill_group(process):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            if sig == signal.SIGTERM:
                time.sleep(0.3)
        process.wait()

    def stop(self):
        self.stopped.set()

    def run(
        self, command, log, environment, timeout, *,
        output_root=None, max_output_bytes=None,
    ):
        if self.stopped.is_set():
            raise InterruptedError("collection stopped")
        if (output_root is None) != (max_output_bytes is None):
            raise ValueError("output_root and max_output_bytes must be set together")
        if max_output_bytes is not None and max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w") as output:
            with self.lock:
                if self.stopped.is_set():
                    raise InterruptedError("collection stopped")
                process = subprocess.Popen(command, cwd=ROOT, env=environment,
                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                self.active[process.pid] = command
            start = time.monotonic()
            next_output_check = start
            try:
                while process.poll() is None:
                    if self.stopped.wait(0.2):
                        raise InterruptedError("collection stopped")
                    current = time.monotonic()
                    if current - start > timeout:
                        raise TimeoutError(f"process exceeded {timeout}s: {log}")
                    if output_root is not None and current >= next_output_check:
                        used = artifact_bytes(output_root)
                        if used > max_output_bytes:
                            raise RuntimeError(
                                f"process output exceeded {max_output_bytes} bytes "
                                f"at {output_root}: {used} bytes"
                            )
                        next_output_check = current + 1.0
                if process.returncode:
                    raise RuntimeError(f"exit {process.returncode}; see {log}")
            except BaseException:
                self.kill_group(process)
                raise
            finally:
                with self.lock:
                    self.active.pop(process.pid, None)


def environment(binary=None, runtime_lib=None):
    result = dict(os.environ)
    result.pop("PYTHONHOME", None)
    result.pop("GEM5_PYTHONHOME", None)
    result.update(PYTHONUNBUFFERED="1",
        FASTSIM_EFFECTIVE_TARGET_GENERATOR=str(ROOT / "tools/generate_fs_effective_target.py"),
        FASTSIM_EFFECTIVE_TARGET_PYTHON=sys.executable,
        FASTSIM_EVENT_DICTIONARY=str(ROOT / "configs/pmu-event-dictionary-v1.json"),
        TAOGEN_SHARED="/data00/yinhaolang/taogen/shared")
    if runtime_lib is not None:
        runtime_lib = Path(runtime_lib).resolve()
        if not (runtime_lib / "libstdc++.so.6").exists():
            raise ValueError(
                f"runtime library directory lacks libstdc++.so.6: {runtime_lib}"
            )
        existing = result.get("LD_LIBRARY_PATH", "")
        result["LD_LIBRARY_PATH"] = (
            str(runtime_lib) + (os.pathsep + existing if existing else "")
        )
    return result


def validate_case(result, task, matrix, target):
    from validate_fs_oracle_identity import validate_result_identity
    from audit_p1_native_response_sideband import audit_result
    effective = load(result / "effective-target.json")
    expected = fastsim_expected(matrix, task["profile"])
    pairs = [("cores", effective["core"]["count"], task["cores"]),
        ("ROB", effective["core"]["pipeline"]["numROBEntries"], expected["core.rob_entries"]),
        ("clock", effective["clock"]["clock_period_ticks"], 333),
        ("protocol", effective["coherence"]["protocol"], "MESI_Three_Level"),
        ("LLC banks", effective["cache"]["l3"]["num_banks"], 8),
        ("DRAM channels", effective["dram"]["num_channels"], 8),
        ("DRAM size", effective["dram"]["size_b"], 3 * GIB),
        ("L1I size", effective["cache"]["l1i"]["size_b"], 32768),
        ("L1I assoc", effective["cache"]["l1i"]["assoc"], 8)]
    for native, fastsim in (("l1d", "l1d"), ("l2", "l2"), ("l3", "llc")):
        for nk, fk in (("size_b", "size"), ("assoc", "associativity")):
            pairs.append((native + "." + nk, effective["cache"][native][nk], expected[f"cache.{fastsim}.{fk}"]))
    for label, actual, wanted in pairs:
        if actual != wanted:
            raise ValueError(f"requested hardware mismatch {label}: {actual} != {wanted}")
    identity = validate_result_identity(result)
    if identity.get("valid") is not True:
        raise ValueError(f"native identity validation failed: {identity}")
    trace = load(result / "tao_trace/trace.json")
    if (trace.get("measurement_policy") != POLICY or
            trace.get("functional_warmup_enabled") is not True or
            trace.get("trace_scope") != "user-plus-kernel" or
            set(trace["per_core"]) != {str(i) for i in range(task["cores"])}):
        raise ValueError("trace policy/warmup/scope/participants mismatch")
    boundaries = [load(result / f"tao_trace/functional-boundary-core{i}.json") for i in range(task["cores"])]
    cpi = load(result / "oracle/cpi.json")
    cpl = [json.loads(line) for line in (result / "oracle/cpl_class.jsonl").read_text().splitlines() if line.strip()]
    gate = validate_common_end(boundaries, cpi["per_core"], cpl, cores=task["cores"], target=target)
    if sum(b["warmup_records"] for b in boundaries) <= 0:
        raise ValueError("source warmup records missing")
    manifest = []
    for core, boundary in enumerate(boundaries):
        fst = result / f"tao_trace/core{core}.fst"
        with fst.open("rb") as handle:
            header = struct.unpack("<8sIIIIQQ4Q", handle.read(72))
        row = trace["per_core"][str(core)]
        if header[4] != core or header[5] != boundary["total_records"] or header[6] & 48 != 48:
            raise ValueError(f"FST participant/record/features mismatch core{core}")
        bound_fst = Path(row["fst"])
        if not bound_fst.is_absolute():
            bound_fst = ROOT / bound_fst
        if row["records"] != header[5] or bound_fst.resolve() != fst.resolve():
            raise ValueError("trace file binding mismatch")
        deps = Path(str(fst) + ".deps")
        with deps.open("rb") as handle:
            dep = struct.unpack("<8sIIIIQQQ", handle.read(48))
        if dep[:6] != (b"FSTDEP1\0", 1, 48, core, 0, header[5]) or deps.stat().st_size != 48 + dep[6]*16 + dep[7]*4:
            raise ValueError("incomplete FST dependencies")
        for key in ("virtual_page_map", "address_space_map", "static_instruction_map", "dependency_companion"):
            if key in row:
                companion = Path(row[key])
                if not companion.is_absolute():
                    companion = ROOT / companion
                if not companion.is_file():
                    raise ValueError(f"missing companion: {row[key]}")
        manifest.append(f"{core} fastsim-binary-warmup-slice core{core}.fst {core} "
            f"{boundary['warmup_instructions']} {boundary['measurement_instructions']} "
            f"{boundary['warmup_records']} {boundary['measurement_records']}")
    if (result / "tao_trace/manifest.txt").read_text().splitlines() != manifest:
        raise ValueError("manifest population/binding mismatch")
    participants = {int(p.stem.removeprefix("native-summary-core"))
                    for p in (result / "oracle").glob("native-summary-core*.json")}
    audit = audit_result(result)
    require_native_gate(audit, task["cores"], participants)
    return dict(valid=True, common_end=gate, native=audit, identity=identity)


def preflight(args, matrix):
    validate_design(matrix)
    static = static_preflight(matrix)
    tasks = tasks_for(matrix)
    case_count = len(tasks)
    core_counts = [int(value) for value in matrix["core_counts"]]
    if sys.version_info < (3, 10):
        raise ValueError("Python 3.10+ required; use the shell entrypoint")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise ValueError("/dev/kvm is not readable/writable for offline ROI preparation")
    required_binary_sha256 = matrix.get(
        "required_binary_sha256",
        "7814e0e305581c1e87ea04e5b99f762f849103c2b97e0b771a342a33d98ef6e5",
    )
    if sha256(args.binary) != required_binary_sha256:
        raise ValueError("binary is not the validated first-core common-end collector")
    validate_tso_selection(args, matrix)
    validate_checkpoint_recovery_selection(args, matrix)
    runtime_library = validate_runtime_library(args, matrix)
    paths = [args.binary, TCSIM / "scripts/gem5_fs_roi.py", TCSIM / "scripts/materialize_fs_workload.py",
        TCSIM / "configs/gem5/spec2026_test_workloads.json",
        TCSIM / "configs/gem5/readfile/run_workload.template.sh",
        TCSIM / "configs/gem5/readfile/roi_prepare/prepare_roi.template.sh"]
    if runtime_library is not None:
        paths.append(runtime_library)
    paths += sorted((TCSIM / "configs/gem5").glob("*.py"))
    paths += sorted((ROOT / "tools").glob("*.py"))
    paths += sorted(Path("/data00/yinhaolang/taogen/shared").glob("*.hh"))
    paths += [ROOT / "configs/pmu-event-dictionary-v1.json"] + config_include_chain(args.config)
    # Images are large: freeze stat identities here; the maintained prepare runner
    # additionally records their content SHA256 in each content-addressed request.
    paths += [Path(w["aux_disk"]) for w in matrix["workloads"]]
    # Do not import this gem5-only module into host Python. Read its literal defaults.
    text = (TCSIM / "configs/gem5/x86_fs_kvm_boot_checkpoint.py").read_text()
    constants = dict(re.findall(r'^DEFAULT_(KERNEL_ID|KERNEL_VERSION|DISK_IMAGE_ID|DISK_IMAGE_VERSION)\s*=\s*"([^"]+)"', text, re.M))
    for kind in ("KERNEL", "DISK_IMAGE"):
        paths.append(args.resources / f"{constants[kind + '_ID']}-{constants[kind + '_VERSION']}")
    manifest = load(TCSIM / "configs/gem5/spec2026_test_workloads.json")["workloads"]
    for w in matrix["workloads"]:
        paths.append(TCSIM / "workloads/spec2026/benchspec/CPU" / w["name"] /
                     "run/run_base_test_gem5-x86-linux.0000" / manifest[w["name"]]["binary"])
    for cores in core_counts:
        checkpoint = TCSIM / f"ckpt/gem5-fs-ubuntu2404/mesi-three-level-3GiB/cpt.{cores}c.booted"
        checkpoint_snapshot(checkpoint)
        paths += [checkpoint / "m5.cpt", checkpoint / "metadata.json"]
    inputs = {str(p.resolve()): file_stamp(p, digest=p == args.binary) for p in sorted(set(paths))}
    cpus = len(os.sched_getaffinity(0))
    available = int(re.search(r"^MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text(), re.M)[1]) * 1024
    maximum = choose_jobs(case_count, cpus, available, args.memory_per_job_gib, args.memory_reserve_gib)
    jobs = args.jobs or maximum
    if jobs < 1 or jobs > maximum:
        raise ValueError(f"--jobs {jobs} exceeds current CPU/memory budget {maximum}")
    prepare_jobs = args.prepare_jobs or min(jobs, max(1, cpus // 8))
    if prepare_jobs > min(jobs, max(1, cpus // 8)) or prepare_jobs < 1:
        raise ValueError("--prepare-jobs exceeds available 8-vCPU slots")
    parent = args.output
    while not parent.exists():
        parent = parent.parent
    free = shutil.disk_usage(parent).free
    required = args.disk_reserve_gib * GIB
    if not (args.output / "run-contract.json").exists():
        required += args.disk_budget_gib * GIB
    if free < required:
        raise ValueError(f"free disk {free/GIB:.1f} GiB < required {required/GIB:.1f} GiB")
    return dict(valid=True, checked_at=now(), cases=case_count,
        formal_fst_files=sum(task["cores"] for task in tasks),
        smoke_cases=case_count, jobs=jobs, prepare_jobs=prepare_jobs,
        finalize_jobs=min(jobs, args.finalize_jobs),
        affinity_cpus=cpus, available_memory_gib=available/GIB, disk_free_gib=free/GIB,
        disk_budget_gib=args.disk_budget_gib, disk_reserve_gib=args.disk_reserve_gib,
        static=static, inputs=inputs)


def validate_tso_selection(args, matrix):
    required_tso = matrix.get("required_tso")
    if required_tso is None:
        return
    if (
        args.tso_store != required_tso["mode"]
        or args.tso_store_buffer_size != required_tso["buffer_size"]
    ):
        raise ValueError(
            "matrix requires "
            f"--tso-store={required_tso['mode']} "
            f"--tso-store-buffer-size={required_tso['buffer_size']}"
        )


def validate_checkpoint_recovery_selection(args, matrix):
    required = matrix.get("required_checkpoint_recovery")
    if required is None:
        return
    actual = {
        "timeout_seconds": args.checkpoint_recovery_timeout,
        "max_output_mib": args.checkpoint_recovery_max_mib,
        "post_workbegin_insts_per_core": 1,
        "tao_emit_records": False,
    }
    if actual != required:
        raise ValueError(
            f"matrix requires checkpoint recovery gate {required}, got {actual}"
        )


def validate_runtime_library(args, matrix):
    required_sha256 = matrix.get("required_runtime_libstdcxx_sha256")
    runtime_lib = getattr(args, "runtime_lib", None)
    if runtime_lib is None:
        if required_sha256 is not None:
            raise ValueError("matrix requires an explicit runtime library directory")
        return None
    library = (runtime_lib / "libstdc++.so.6").resolve()
    if not library.is_file():
        raise ValueError(f"runtime libstdc++.so.6 is missing: {library}")
    actual_sha256 = sha256(library)
    if required_sha256 is not None and actual_sha256 != required_sha256:
        raise ValueError(
            f"runtime libstdc++.so.6 SHA256 {actual_sha256} != "
            f"required {required_sha256}"
        )
    return library


def freeze_runtime(args, matrix, report):
    args.output.mkdir(parents=True, exist_ok=True)
    freeze_json(args.output / "matrix.json", matrix)
    contract = dict(schema=matrix["schema"], matrix=matrix, inputs=report["inputs"],
                    output=str(args.output), binary=str(args.binary), config=str(args.config),
                    resources=str(args.resources), measurement_policy=POLICY,
                    capture_phases=[getattr(args, "only", None)] if getattr(args, "only", None) else ["smoke", "formal"])
    if getattr(args, "runtime_lib", None) is not None:
        contract["runtime_lib"] = str(args.runtime_lib)
    if matrix.get("required_checkpoint_recovery") is not None:
        contract["checkpoint_recovery"] = dict(
            schema="fastsim-checkpoint-workbegin-gate-v1",
            timeout_seconds=args.checkpoint_recovery_timeout,
            max_output_mib=args.checkpoint_recovery_max_mib,
            post_workbegin_insts_per_core=1,
            tao_emit_records=False,
        )
    if args.tso_store == "buffer":
        contract.update(
            tso_store="buffer",
            tso_store_buffer_size=args.tso_store_buffer_size,
            tso_store_buffer_active=True,
        )
    freeze_json(args.output / "run-contract.json", contract)
    save(args.output / "preflight.json", report)
    gem5_root = args.output / "runtime/gem5"
    link = gem5_root / "build/X86_MESI_Three_Level/gem5.opt"
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.exists():
        link.symlink_to(args.binary)
    if link.resolve() != args.binary.resolve():
        raise ValueError("compatibility binary path changed")
    configs = args.output / "runtime/configs"
    configs.mkdir(parents=True, exist_ok=True)
    chain = config_include_chain(args.config)
    for i, source in enumerate(chain):
        content = source.read_text()
        if i + 1 < len(chain):
            content = re.sub(r"(?m)^\s*config\.include\s*=.*$", f"config.include = {i+1}.cfg", content)
        path = configs / f"{i}.cfg"
        if path.exists() and path.read_text() != content:
            raise ValueError(f"frozen config changed: {path}")
        path.write_text(content)
    return contract


def prepare_command(args, matrix, task, folder):
    command = [sys.executable, str(TCSIM / "scripts/gem5_fs_roi.py"), "prepare",
        task["workload"]["name"], str(task["cores"]),
        "--cache-hierarchy", "mesi-three-level", *geometry_args(task, matrix),
        "--gem5-root", str(args.output / "runtime/gem5"), "--resource-dir", str(args.resources),
        "--checkpoint-root", str(args.output / "checkpoints"), "--tmp-root", str(folder / "tmp"),
        "--result-root", str(folder / "logs"), "--aux-disk", task["workload"]["aux_disk"]]
    tso_store = getattr(args, "tso_store", "serial")
    tso_store_buffer_size = getattr(args, "tso_store_buffer_size", 114)
    if tso_store == "buffer":
        command.extend([
            "--tso-store", "buffer",
            "--tso-store-buffer-size", str(tso_store_buffer_size),
        ])
    return command


def preserve_checkpoint(args, checkpoint):
    if (checkpoint.name != "checkpoint" or checkpoint.is_symlink() or
            not checkpoint.resolve().is_relative_to((args.output / "checkpoints").resolve())):
        raise ValueError(f"refusing to move checkpoint outside collection: {checkpoint}")
    if checkpoint.exists():
        checkpoint.rename(checkpoint.with_name(f"checkpoint.failed-{time.time_ns()}"))


def reject_preparation_after_recovery_failure(args, task, preparation, error):
    folder = args.output / "preparation" / task["case_id"]
    checkpoint = Path(preparation["checkpoint"])
    stamp = time.time_ns()
    preserve_checkpoint(args, checkpoint)
    receipt = folder / "prepared.json"
    if receipt.exists():
        receipt.rename(folder / f"prepared.recovery-failed-{stamp}.json")
    save(folder / f"checkpoint-recovery-rejection-{stamp}.json", {
        "state": "rejected",
        "finished": now(),
        "checkpoint": str(checkpoint),
        "error": str(error),
    })


def prepare_task(args, matrix, task, supervisor):
    if supervisor.stopped.is_set():
        raise InterruptedError("collection stopped")
    folder = args.output / "preparation" / task["case_id"]
    folder.mkdir(parents=True, exist_ok=True)
    receipt = folder / "prepared.json"
    if receipt.exists():
        row = load(receipt)
        checkpoint = Path(row["checkpoint"])
        try:
            check_snapshot(checkpoint, row["files"])
            if checkpoint_snapshot(checkpoint) != row["files"] or load(checkpoint.parent / "request.json") != row["request"]:
                raise ValueError(f"prepared checkpoint changed: {checkpoint}")
            return row
        except (OSError, ValueError) as error:
            emit(f"preserving invalid prepared checkpoint: {checkpoint}: {error}")
            preserve_checkpoint(args, checkpoint)
            receipt.rename(folder / f"prepared.failed-{time.time_ns()}.json")
    # A previous prepare may have crashed after writing only some payloads,
    # before the receipt. The runner itself checks only m5.cpt + metadata.json.
    old_log = folder / "prepare.log"
    if old_log.exists():
        entries = re.findall(r"^\[gem5-fs-roi\] entry=(.+)$", old_log.read_text(), re.M)
        for entry in entries:
            checkpoint = Path(entry) / "checkpoint"
            if checkpoint.exists():
                try:
                    checkpoint_snapshot(checkpoint)
                except (OSError, ValueError):
                    preserve_checkpoint(args, checkpoint)
        old_log.rename(folder / f"prepare.previous-{time.time_ns()}.log")
    command = prepare_command(args, matrix, task, folder)
    save(folder / "command.json", command)
    supervisor.run(
        command,
        folder / "prepare.log",
        environment(args.binary, getattr(args, "runtime_lib", None)),
        args.prepare_timeout,
    )
    entries = re.findall(r"^\[gem5-fs-roi\] entry=(.+)$", (folder / "prepare.log").read_text(), re.M)
    if len(entries) != 1:
        raise ValueError(f"expected one strict checkpoint entry: {folder / 'prepare.log'}")
    checkpoint = Path(entries[0]) / "checkpoint"
    if not checkpoint.resolve().is_relative_to((args.output / "checkpoints").resolve()):
        raise ValueError("checkpoint escaped the collection output")
    row = dict(checkpoint=str(checkpoint), files=checkpoint_snapshot(checkpoint),
               request=load(checkpoint.parent / "request.json"))
    save(receipt, row)
    return row


def checkpoint_recovery_contract(args, matrix, task, preparation):
    wrapper = TCSIM / "configs/gem5/x86_fs_kvm_boot_checkpoint_tao.py"
    checkpoint = Path(preparation["checkpoint"])
    return {
        "schema": "fastsim-checkpoint-workbegin-gate-v1",
        "checkpoint": preparation["checkpoint"],
        "checkpoint_files": preparation["files"],
        "checkpoint_metadata": load(checkpoint / "metadata.json"),
        "checkpoint_request": preparation["request"],
        "gem5_binary_sha256": matrix.get(
            "required_binary_sha256",
            "7814e0e305581c1e87ea04e5b99f762f849103c2b97e0b771a342a33d98ef6e5",
        ),
        "gem5_config_sha256": sha256(wrapper),
        "case_id": task["case_id"],
        "workload": task["workload"],
        "cores": task["cores"],
        "geometry": effective_profile(matrix, task["profile"]),
        "tso_store": args.tso_store,
        "tso_store_buffer_size": (
            args.tso_store_buffer_size if args.tso_store == "buffer" else None
        ),
        "probe": {
            "cpu_type": "o3",
            "wait_for_roi_workbegin": True,
            "post_workbegin_insts_per_core": 1,
            "tao_emit_records": False,
            "timeout_seconds": args.checkpoint_recovery_timeout,
            "max_output_bytes": args.checkpoint_recovery_max_mib * 1024 * 1024,
        },
    }


def validate_checkpoint_recovery_log(log):
    text = Path(log).read_text(errors="replace")
    workbegin = list(re.finditer(
        r"^\[fs-ckpt\] WORKBEGIN tick=(\d+)\s*$", text, flags=re.MULTILINE
    ))
    roi_exit = list(re.finditer(
        r"^\[fs-ckpt\] MAX_INSTS reached at tick=(\d+) "
        r"phase=roi target=1\b.*$",
        text,
        flags=re.MULTILINE,
    ))
    if not workbegin:
        raise ValueError(
            f"checkpoint recovery did not reach source WORKBEGIN: {log}"
        )
    if not roi_exit:
        raise ValueError(
            f"checkpoint recovery did not complete the post-WORKBEGIN probe: {log}"
        )
    final_exit = roi_exit[-1]
    preceding_workbegin = [
        marker for marker in workbegin if marker.start() < final_exit.start()
    ]
    if not preceding_workbegin:
        raise ValueError(
            f"checkpoint recovery ROI exit preceded source WORKBEGIN: {log}"
        )
    final_workbegin = preceding_workbegin[-1]
    workbegin_tick = int(final_workbegin.group(1))
    exit_tick = int(final_exit.group(1))
    if exit_tick < workbegin_tick:
        raise ValueError(
            f"checkpoint recovery ROI exit tick precedes WORKBEGIN tick: {log}"
        )
    return {
        "workbegin_tick": workbegin_tick,
        "post_workbegin_exit_tick": exit_tick,
        "workbegin_events": len(workbegin),
    }


def probe_checkpoint_recovery(args, matrix, task, preparation, supervisor):
    folder = args.output / "preparation" / task["case_id"]
    result = folder / "checkpoint-recovery"
    receipt = folder / "checkpoint-recovery.json"
    contract = checkpoint_recovery_contract(args, matrix, task, preparation)
    checkpoint = Path(preparation["checkpoint"])
    check_snapshot(checkpoint, preparation["files"])
    if receipt.exists():
        try:
            previous = load(receipt)
            if previous.get("state") != "complete":
                raise ValueError("previous checkpoint recovery was not complete")
            if previous.get("contract") != contract:
                raise ValueError("checkpoint recovery contract changed")
            check_snapshot(result, previous["files"])
            if file_stamp(
                result / "recovery.log", digest=True
            ) != previous["log"]:
                raise ValueError("checkpoint recovery log changed")
            validate_checkpoint_recovery_log(result / "recovery.log")
            if list(result.rglob("*.fst")):
                raise ValueError(
                    "checkpoint recovery receipt gained FST record output"
                )
            used = artifact_bytes(result)
            if used > contract["probe"]["max_output_bytes"]:
                raise ValueError(
                    f"checkpoint recovery receipt exceeds output contract: "
                    f"{used} bytes"
                )
            if used != previous.get("output_bytes"):
                raise ValueError("checkpoint recovery output size changed")
            return previous
        except (OSError, ValueError, KeyError) as error:
            emit(
                f"preserving invalid checkpoint recovery receipt for "
                f"{task['case_id']}: {error}"
            )
            stamp = time.time_ns()
            receipt.rename(
                folder / f"checkpoint-recovery.failed-{stamp}.json"
            )
            if result.exists():
                result.rename(folder / f"checkpoint-recovery.failed-{stamp}")
    elif result.exists():
        result.rename(
            folder / f"checkpoint-recovery.interrupted-{time.time_ns()}"
        )
    result.mkdir(parents=True)
    command = checkpoint_recovery_command(
        task, matrix, args.binary, TCSIM, args.resources, checkpoint, result,
        args.tso_store, args.tso_store_buffer_size,
    )
    save(result / "command.json", command)
    log = result / "recovery.log"
    started = now()
    start = time.monotonic()
    try:
        supervisor.run(
            command,
            log,
            environment(args.binary, getattr(args, "runtime_lib", None)),
            args.checkpoint_recovery_timeout,
            output_root=result,
            max_output_bytes=args.checkpoint_recovery_max_mib * 1024 * 1024,
        )
        check_snapshot(checkpoint, preparation["files"])
        gate = validate_checkpoint_recovery_log(log)
        record_files = sorted(result.rglob("*.fst"))
        if record_files:
            raise ValueError(
                "checkpoint recovery probe unexpectedly emitted FST records: "
                + ", ".join(str(path) for path in record_files)
            )
        used = artifact_bytes(result)
        if used > contract["probe"]["max_output_bytes"]:
            raise ValueError(
                f"checkpoint recovery output exceeded contract: {used} bytes"
            )
        row = {
            "state": "complete",
            "started": started,
            "finished": now(),
            "wall_seconds": time.monotonic() - start,
            "contract": contract,
            "gate": gate,
            "output_bytes": used,
            "files": artifact_snapshot(result),
            "log": file_stamp(log, digest=True),
        }
        save(receipt, row)
        return row
    except (Exception, SystemExit) as error:
        save(receipt, {
            "state": "failed",
            "started": started,
            "finished": now(),
            "wall_seconds": time.monotonic() - start,
            "contract": contract,
            "error": str(error),
        })
        raise


def quarantine(result):
    if result.exists():
        failed = result.parent.parent / "failed"
        failed.mkdir(parents=True, exist_ok=True)
        result.rename(failed / f"{result.name}-{time.time_ns()}")


def write_replay(result, task, matrix, args):
    overrides = fastsim_expected(matrix, task["profile"])
    overrides.update({"sim.cores": task["cores"], "core.frequency_hz": 3000000000,
                      "sim.reference_frequency_hz": 3000000000})
    (result / "fastsim.cfg").write_text(
        f"config.include = {args.output / 'runtime/configs/0.cfg'}\n" +
        "".join(f"{key} = {value}\n" for key, value in overrides.items()))
    stats = args.output / "future-stats" / f"{task['case_id']}.json"
    stats.parent.mkdir(parents=True, exist_ok=True)
    save(result / "replay-command.json", [str(ROOT / "build/fastsim"), "simulate",
        "--config", str(result / "fastsim.cfg"), "--manifest", str(result / "tao_trace/manifest.txt"),
        "--measurement-scope", "user-plus-kernel", "--cores", str(task["cores"]), "--output", str(stats)])


def capture_task(args, matrix, task, preparation, phase, target, supervisor, finalize_slots):
    if supervisor.stopped.is_set():
        raise InterruptedError("collection stopped")
    result = args.output / phase / "source" / task["case_id"]
    status = result / "collection-status.json"
    if status.exists():
        previous = load(status)
        if previous.get("state") == "complete":
            try:
                check_snapshot(result, previous["files"])
                validate_case(result, task, matrix, target)
                return dict(case_id=task["case_id"], state="complete", resumed=True, result_dir=str(result))
            except (Exception, SystemExit) as error:
                emit(f"{phase}/{task['case_id']}: invalid receipt, preserving and recapturing: {error}")
    for attempt in range(args.transient_retries + 1):
        if supervisor.stopped.is_set():
            raise InterruptedError("collection stopped")
        quarantine(result)
        result.mkdir(parents=True)
        checkpoint = Path(preparation["checkpoint"])
        check_snapshot(checkpoint, preparation["files"])
        request = dict(preparation["request"])
        wrapper = TCSIM / "configs/gem5/x86_fs_kvm_boot_checkpoint_tao.py"
        request.update(roi_checkpoint=str(checkpoint), uarch_profile_id=task["profile"]["id"],
            case_id=task["case_id"], collection_phase=phase)
        request["gem5"] = dict(binary=str(args.binary), binary_sha256=preparation["request"]["gem5"]["binary_sha256"],
                               config=str(wrapper), config_sha256=sha256(wrapper))
        request["sampling"] = dict(warmup_mode="source", detailed_warmup_insts=0,
            roi_insts=target, roi_target_domain="user-fst", roi_stop_policy="any-core",
            measurement_policy=POLICY, functional_trace=True, trace_format="fst", measure_cpl=True,
            functional_user_only=False, functional_include_kernel=True, functional_warmup=True,
            native_response_jsonl=False, native_anomaly_limit=32, wrong_path_oracle=False,
            functional_trace_dir=str(result / "tao_trace"), oracle_dir=str(result / "oracle"))
        if args.tso_store == "buffer":
            request["sampling"].update(
                tso_store="buffer",
                tso_store_buffer_size=args.tso_store_buffer_size,
                tso_store_buffer_active=True,
            )
        save(result / "request.json", request)
        save(result / "task.json", dict(task=task, target=target))
        command = capture_command(
            task, matrix, args.binary, TCSIM, args.resources, checkpoint,
            result, target, args.tso_store, args.tso_store_buffer_size,
        )
        save(result / "command.json", command)
        write_replay(result, task, matrix, args)
        save(status, dict(state="running", phase=phase, started=now(), attempt=attempt))
        try:
            supervisor.run(
                command,
                result / "gem5.log",
                environment(args.binary, getattr(args, "runtime_lib", None)),
                args.sample_timeout,
            )
            with finalize_slots:
                supervisor.run([sys.executable, str(Path(__file__).resolve()), "finalize",
                    "--output", str(args.output), "--case-dir", str(result)],
                    result / "finalize.log",
                    environment(args.binary, getattr(args, "runtime_lib", None)),
                    args.finalize_timeout,
                )
            return dict(case_id=task["case_id"], state="complete", result_dir=str(result))
        except (Exception, SystemExit) as error:
            save(status, dict(state="failed", phase=phase, error=str(error), finished=now()))
            log = result / "gem5.log"
            transient = log.exists() and "functional read failed" in log.read_text(errors="replace").lower()
            if not transient or attempt == args.transient_retries or supervisor.stopped.is_set():
                raise


def finalize(args):
    result = args.case_dir.resolve()
    if not result.is_relative_to(args.output) or result.parent.name != "source":
        raise ValueError("invalid case directory")
    record = load(result / "task.json")
    matrix = validate_design(load(args.output / "matrix.json"))
    finalize_capture(result, cores=record["task"]["cores"], target=record["target"], tcsim_root=TCSIM)
    audit = validate_case(result, record["task"], matrix, record["target"])
    save(result / "collection-audit.json", audit)
    save(result / "collection-status.json", dict(state="complete", finished=now(), files=artifact_snapshot(result)))


def pid_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return None if fields[0] in ("Z", "X") else fields[19]
    except (OSError, IndexError):
        return None


def running(output):
    path = output / "collector-pid.json"
    if not path.exists():
        return None
    row = load(path)
    return row if row.get("start_ticks") is not None and pid_identity(row["pid"]) == row["start_ticks"] else None


@contextmanager
def exclusive(output):
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"another collector owns {output}") from None
        yield lock


def collect(args, inherited_lock=None):
    supervisor = Supervisor()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: supervisor.stop())
    save(args.output / "collector-pid.json", dict(pid=os.getpid(), start_ticks=pid_identity(os.getpid())))
    save(args.output / "collector-state.json", dict(state="preflight", started=now()))
    try:
        matrix = load(args.matrix)
        validate_design(matrix)
        tasks = tasks_for(matrix)
        case_count = len(tasks)
        for phase in ("preparation", "smoke", "formal"):
            marker = args.output / phase / "collection-finished.json"
            if marker.exists() and load(marker).get("state") == "complete":
                marker.rename(marker.with_name(f"collection-finished.previous-{time.time_ns()}.json"))
            save(marker, dict(state="incomplete", expected_cases=case_count, updated=now()))
        report = preflight(args, matrix)
        freeze_runtime(args, matrix, report)
        emit(f"preflight passed: {len(tasks)} cases, sample={report['jobs']}, prepare={report['prepare_jobs']}, finalize={report['finalize_jobs']}")
        def disk_guard():
            while not supervisor.stopped.wait(5):
                if shutil.disk_usage(args.output).free < args.disk_reserve_gib * GIB:
                    emit("disk reserve reached; stopping children")
                    supervisor.stop()
        monitor = threading.Thread(target=disk_guard, daemon=True)
        monitor.start()
        preparation = {}
        def pool_phase(name, jobs, worker):
            states, rows = {}, []
            phase_root = args.output / name
            save(phase_root / "collection-finished.json", dict(state="incomplete", expected_cases=case_count, updated=now()))
            if name in ("smoke", "formal"):
                extra = {p.name for p in (phase_root / "source").glob("*") if p.is_dir()} - {t["case_id"] for t in tasks}
                if extra:
                    raise ValueError(f"unexpected source cases in {name}: {sorted(extra)}")
            save(args.output / "collector-state.json", dict(state="running", phase=name, completed=0, total=case_count, updated=now()))
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                pending = {pool.submit(worker, task): task for task in tasks}
                for future in as_completed(pending):
                    task = pending[future]
                    try:
                        row = future.result()
                        states[task["case_id"]] = "complete"
                    except (Exception, SystemExit) as error:
                        states[task["case_id"]] = "failed"
                        row = dict(case_id=task["case_id"], state="failed", error=str(error))
                    rows.append(row)
                    done = sum(v == "complete" for v in states.values())
                    emit(f"{name} {len(states)}/{case_count} complete={done}: {task['case_id']} {states[task['case_id']]}")
                    save(args.output / name / "inventory.json", dict(cases=rows, states=states, expected_cases=case_count, updated=now()))
                    save(args.output / "collector-state.json", dict(state="running", phase=name, completed=done, processed=len(states), total=case_count, updated=now()))
            if not complete_set([t["case_id"] for t in tasks], states):
                raise RuntimeError(f"{name}: incomplete; inspect {name}/inventory.json and restart to resume")
            check_snapshot(Path("/"), {p.lstrip("/"): v for p, v in report["inputs"].items()})
            save(args.output / name / "collection-finished.json", dict(state="complete", cases=case_count, finished=now()))
        def prepare_worker(task):
            row = prepare_task(args, matrix, task, supervisor)
            preparation[task["case_id"]] = row
            result = dict(
                case_id=task["case_id"],
                state="complete",
                checkpoint=row["checkpoint"],
            )
            if matrix.get("required_checkpoint_recovery") is not None:
                try:
                    recovery = probe_checkpoint_recovery(
                        args, matrix, task, row, supervisor
                    )
                except (Exception, SystemExit) as error:
                    if not supervisor.stopped.is_set():
                        reject_preparation_after_recovery_failure(
                            args, task, row, error
                        )
                    raise
                result.update(
                    workbegin_tick=recovery["gate"]["workbegin_tick"],
                    checkpoint_recovery_output_bytes=recovery["output_bytes"],
                )
            return result
        pool_phase("preparation", report["prepare_jobs"], prepare_worker)
        slots = threading.Semaphore(report["finalize_jobs"])
        phases = [("smoke", matrix["smoke_records_per_core"]),
                  ("formal", matrix["target_records_per_core"])]
        only_phase = getattr(args, "only", None)
        if only_phase:
            phases = [item for item in phases if item[0] == only_phase]
        for phase, target in phases:
            check_snapshot(Path("/"), {p.lstrip("/"): v for p, v in report["inputs"].items()})
            pool_phase(phase, report["jobs"], lambda task, phase=phase, target=target: capture_task(args, matrix, task,
                preparation[task["case_id"]], phase, target, supervisor, slots))
        completed_phases = {phase for phase, _ in phases}
        save(args.output / "collector-state.json", dict(
            state="complete",
            formal_cases=case_count if "formal" in completed_phases else 0,
            smoke_cases=case_count if "smoke" in completed_phases else 0,
            skipped=[phase for phase in ("smoke", "formal") if phase not in completed_phases],
            finished=now()))
    except (Exception, SystemExit) as error:
        save(args.output / "collector-state.json", dict(state="stopped" if supervisor.stopped.is_set() else "failed", error=str(error), finished=now()))
        raise
    finally:
        supervisor.stop()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "start", "run", "status", "stop", "finalize"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    parser.add_argument(
        "--runtime-lib",
        type=Path,
        default=None,
        help="directory containing the frozen libstdc++.so.6 used by gem5",
    )
    parser.add_argument("--resources", type=Path, default=Path("/data00/yinhaolang/gem5-fs/resources"))
    parser.add_argument("--config", type=Path, default=ROOT / "configs/gem5-fs-native-kernel.cfg")
    parser.add_argument("--jobs", type=int, default=0, help="0: fill available CPU/memory, up to all matrix cases")
    parser.add_argument("--prepare-jobs", type=int, default=0, help="0: one 8-vCPU preparation per eight host CPUs")
    parser.add_argument("--finalize-jobs", type=int, default=8)
    parser.add_argument("--memory-per-job-gib", type=float, default=12)
    parser.add_argument("--memory-reserve-gib", type=float, default=64)
    parser.add_argument("--disk-budget-gib", type=float, default=1536)
    parser.add_argument("--disk-reserve-gib", type=float, default=1229)
    parser.add_argument("--prepare-timeout", type=int, default=14400, help="seconds per checkpoint")
    parser.add_argument(
        "--checkpoint-recovery-timeout",
        type=int,
        default=7200,
        help="seconds allowed for a restored source checkpoint to reach WORKBEGIN",
    )
    parser.add_argument(
        "--checkpoint-recovery-max-mib",
        type=int,
        default=256,
        help="maximum files written by one no-record WORKBEGIN recovery probe",
    )
    parser.add_argument("--sample-timeout", type=int, default=86400, help="seconds per gem5 sample")
    parser.add_argument("--finalize-timeout", type=int, default=7200, help="seconds per finalize/hash job")
    parser.add_argument("--transient-retries", type=int, default=2)
    parser.add_argument(
        "--only", choices=("smoke", "formal"), help="skip the other capture phase"
    )
    parser.add_argument(
        "--tso-store", choices=("serial", "buffer"), default="serial"
    )
    parser.add_argument("--tso-store-buffer-size", type=int, default=114)
    parser.add_argument("--case-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    for key in ("output", "matrix", "binary", "resources", "config", "runtime_lib"):
        value = getattr(args, key)
        setattr(args, key, value.resolve() if value is not None else None)
    for key in (
        "finalize_jobs", "prepare_timeout", "checkpoint_recovery_timeout",
        "checkpoint_recovery_max_mib", "sample_timeout", "finalize_timeout",
        "disk_budget_gib", "disk_reserve_gib",
    ):
        if getattr(args, key) <= 0:
            parser.error(f"--{key.replace('_', '-')} must be positive")
    if args.transient_retries < 0:
        parser.error("--transient-retries must be nonnegative")
    if args.tso_store_buffer_size <= 0:
        parser.error("--tso-store-buffer-size must be positive")
    return args


def main():
    args = parse_args()
    if args.action == "preflight":
        report = preflight(args, load(args.matrix))
        emit(json.dumps({k: v for k, v in report.items() if k not in ("inputs", "static")}, indent=2))
    elif args.action == "finalize":
        finalize(args)
    elif args.action == "status":
        state = args.output / "collector-state.json"
        emit(json.dumps(dict(process=running(args.output), status=load(state) if state.exists() else "not started", output=str(args.output)), indent=2))
    elif args.action == "stop":
        process = running(args.output)
        if process:
            os.kill(process["pid"], signal.SIGTERM)
            emit(f"stop requested for collector {process['pid']}; children terminate and completed cases remain")
        else:
            emit("no running collector")
    elif args.action == "start":
        with exclusive(args.output) as lock:
            command = [sys.executable, str(Path(__file__).resolve()), "run", *sys.argv[2:], "--lock-fd", str(lock.fileno())]
            with (args.output / "collector.log").open("a") as log:
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=environment(
                        args.binary, getattr(args, "runtime_lib", None)
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(lock.fileno(),))
            save(args.output / "collector-pid.json", dict(pid=process.pid, start_ticks=pid_identity(process.pid)))
            emit(f"started pid={process.pid}; log={args.output / 'collector.log'}")
    elif args.lock_fd is not None:
        # The detached child inherits the same flock open-file description.
        collect(args, args.lock_fd)
    else:
        with exclusive(args.output):
            collect(args)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        emit(f"collection error: {error}", file=sys.stderr)
        sys.exit(1)
