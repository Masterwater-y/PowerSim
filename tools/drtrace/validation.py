from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import subprocess
import sys
import hashlib
from pathlib import Path
from typing import Any, Sequence

from .dynamorio import (
    UnsupportedConversion,
    _div_capture_client,
    build_div_capture_client,
    capture_dr_trace,
    convert_dr_trace,
)
from .environment import ValidationEnvironment, load_validation_environment
from .fst_compare import compare_fst_pairs
from .projection import (
    FST_HEADER,
    FST_MAGIC,
    FST_RECORD,
    FST_VERSION,
    DESTINATION_CLASS_MARKER,
    MEMORY_FLAGS,
    PHYSICAL_ADDRESS,
    VIRTUAL_PAGE_TOKEN,
    fst_info,
    iter_fst,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GEM5_CONFIG = PROJECT_ROOT / "vendor/gem5_patch/reference/config/run_mt_mvp.py"
VALIDATION_MATRIX_PATH = PROJECT_ROOT / "configs/workloads/uarch_first.json"
WORKLOAD_ROOT = PROJECT_ROOT / "workloads"
DEFAULT_WORKLOAD_DIR = "dr_validation"
TRACE_ROOT = PROJECT_ROOT / "tmp/dr-traces"
FST_ROOT = PROJECT_ROOT / "tmp/dr-fst"
WORKLOAD_AUDIT = PROJECT_ROOT / "tools/drtrace/audit_workload.py"
CORE_RE = re.compile(r"(?:switch|cores)(\d*)\.core")
WORKLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
MATRIX_WORKLOAD_DIRS = {
    "business-excitation-c4": "business_excitation",
    "uarch-excitation-first-batch": "uarch_excitation",
    "uarch-first-batch": DEFAULT_WORKLOAD_DIR,
}
WORKLOAD_BINARY_PRODUCERS = {
    "gem5": "gem5",
    "dr": "dynamoRIO",
}


@dataclass(frozen=True)
class ValidationOptions:
    output_dir: Path
    matrix_path: Path = VALIDATION_MATRIX_PATH
    cores: int | None = None
    scale: int | None = None
    seed: int | None = None
    workloads: tuple[str, ...] = ()
    workload_dir: str | None = None
    trace_root: Path = TRACE_ROOT
    fst_root: Path = FST_ROOT


@dataclass(frozen=True)
class MatrixActionOptions:
    matrix_path: Path = VALIDATION_MATRIX_PATH
    cores: int | None = None
    scale: int | None = None
    seed: int | None = None
    workloads: tuple[str, ...] = ()
    trace_root: Path = TRACE_ROOT
    fst_root: Path = FST_ROOT
    force: bool = False
    skip_build: bool = False
    gem5_config: Path = GEM5_CONFIG
    fastsim_binary: Path | None = None
    dr_capture_sudo: bool = False
    workload_dir: str | None = None


@dataclass(frozen=True)
class MatrixWorkload:
    name: str
    group: str
    scale: int
    workload_dir: str
    domain: str | None = None
    expected_profiles: tuple[str, ...] = ()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _matrix_root(base: Path, matrix_path: Path) -> Path:
    normalized = base if base.is_absolute() else PROJECT_ROOT / base
    if normalized.resolve() in {TRACE_ROOT.resolve(), FST_ROOT.resolve()}:
        return base / matrix_path.stem
    return base


def _fastsim_binary(configured: Path | None = None) -> Path:
    env_configured = os.environ.get("FASTSIM_BINARY")
    candidates = [
        configured.resolve() if configured else Path(),
        Path(env_configured).resolve() if env_configured else Path(),
        PROJECT_ROOT / "build/fastsim",
        PROJECT_ROOT / "build-dr/fastsim",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError("FastSim binary is missing; build it or set FASTSIM_BINARY")


def _raw_core(path: Path) -> int:
    match = CORE_RE.search(path.name)
    if not match:
        raise ValueError(f"cannot determine gem5 core from {path.name}")
    return int(match.group(1) or 0)


def _default_cores(matrix: dict[str, Any]) -> int:
    if "default_cores" in matrix:
        return int(matrix["default_cores"])
    core_counts = matrix.get("core_counts")
    if isinstance(core_counts, list) and core_counts:
        return int(core_counts[0])
    return 4


def _default_seed(matrix: dict[str, Any]) -> int:
    if "default_seed" in matrix:
        return int(matrix["default_seed"])
    return int(matrix.get("seed", 0))


def _matrix_workload_dir(path: Path, matrix: dict[str, Any], override: str | None = None) -> str:
    workload_dir = str(
        override
        or matrix.get("workload_dir")
        or MATRIX_WORKLOAD_DIRS.get(path.stem, DEFAULT_WORKLOAD_DIR)
    )
    if (
        not workload_dir
        or Path(workload_dir).is_absolute()
        or ".." in Path(workload_dir).parts
        or not WORKLOAD_NAME_RE.fullmatch(workload_dir)
    ):
        raise ValueError(f"invalid matrix workload_dir: {workload_dir}")
    return workload_dir


def _load_matrix(path: Path, *, workload_dir: str | None = None) -> dict[str, Any]:
    matrix = json.loads(path.read_text(encoding="utf-8"))
    workload_dir = _matrix_workload_dir(path, matrix, workload_dir)
    matrix["default_cores"] = _default_cores(matrix)
    matrix["default_seed"] = _default_seed(matrix)
    workloads = matrix.get("workloads")
    if not isinstance(workloads, list) or not workloads:
        raise ValueError(f"validation matrix has no workloads: {path}")
    profile_ids: set[str] = set()
    profiles = matrix.get("profiles", [])
    if profiles:
        if not isinstance(profiles, list):
            raise ValueError(f"matrix profiles must be a list: {path}")
        for profile in profiles:
            if not isinstance(profile, dict):
                raise ValueError("matrix profile entries must be objects")
            profile_id = str(profile.get("id", ""))
            if not profile_id or profile_id in profile_ids:
                raise ValueError(f"matrix has invalid duplicate profile id: {profile_id}")
            profile_ids.add(profile_id)
    seen: set[str] = set()
    parsed: list[MatrixWorkload] = []
    for item in workloads:
        if not isinstance(item, dict):
            raise ValueError("matrix workload entries must be objects")
        name = str(item.get("name", ""))
        group = str(item.get("group", ""))
        if not WORKLOAD_NAME_RE.fullmatch(name):
            raise ValueError(f"matrix workload has invalid binary name: {name}")
        if not group or group == "None":
            group = "train"
        if not WORKLOAD_NAME_RE.fullmatch(group):
            raise ValueError(f"matrix workload has invalid group: {name}")
        try:
            scale = int(item["scale"])
        except KeyError as error:
            raise ValueError(f"matrix workload missing scale: {name}") from error
        except (TypeError, ValueError) as error:
            raise ValueError(f"matrix workload has invalid scale: {name}") from error
        if scale <= 0:
            raise ValueError(f"matrix workload scale must be positive: {name}")
        domain = item.get("domain")
        if domain is not None:
            domain = str(domain)
            if not WORKLOAD_NAME_RE.fullmatch(domain):
                raise ValueError(f"matrix workload has invalid domain: {name}")
        raw_expected = item.get("expected_profiles", [])
        if not isinstance(raw_expected, list) or not all(
            isinstance(value, str) and value for value in raw_expected
        ):
            raise ValueError(f"matrix workload has invalid expected_profiles: {name}")
        expected_profiles = tuple(raw_expected)
        missing_profiles = sorted(set(expected_profiles) - profile_ids)
        if missing_profiles:
            raise ValueError(
                f"matrix workload references unknown profile(s): {name}: "
                f"{', '.join(missing_profiles)}"
            )
        if name in seen:
            raise ValueError(f"duplicate matrix workload: {name}")
        seen.add(name)
        parsed.append(
            MatrixWorkload(
                name=name,
                group=group,
                scale=scale,
                workload_dir=workload_dir,
                domain=domain,
                expected_profiles=expected_profiles,
            )
        )
    matrix["parsed_workloads"] = parsed
    return matrix


def _selected_workloads(
    matrix: dict[str, Any], filters: Sequence[str]
) -> list[MatrixWorkload]:
    workloads = list(matrix["parsed_workloads"])
    if not filters:
        return workloads
    requested = set(filters)
    aliases = {item.name: item for item in workloads}
    aliases.update({
        f"W_{item.name}": item
        for item in workloads
        if item.workload_dir == DEFAULT_WORKLOAD_DIR and item.name.startswith("v28_")
    })
    selected = []
    seen: set[str] = set()
    for name in filters:
        item = aliases.get(name)
        if item is None or item.name in seen:
            continue
        selected.append(item)
        seen.add(item.name)
    missing = requested - set(aliases)
    if missing:
        raise ValueError(f"unknown workload(s): {', '.join(sorted(missing))}")
    return selected


def _git_identity() -> dict[str, str]:
    def run(args: list[str]) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()

    return {
        "head": run(["rev-parse", "HEAD"]),
        "branch": run(["rev-parse", "--abbrev-ref", "HEAD"]),
    }


def _convert_gem5_records(
    raw_dir: Path, output: Path, cores: int, fastsim: Path
) -> list[Path]:
    raw = {_raw_core(path): path for path in raw_dir.glob("*.records.micro.jsonl")}
    if sorted(raw) != list(range(cores)):
        raise ValueError(f"gem5 trace cores differ: {sorted(raw)}")
    output.mkdir(parents=True)
    run_env = os.environ.copy()
    run_env["LD_LIBRARY_PATH"] = "/opt/gcc-11.5.0/lib64"
    paths: list[Path] = []
    for core in range(cores):
        target = output / f"core{core}.fst"
        log = output / f"convert-core{core}.log"
        result = subprocess.run(
            [str(fastsim), "convert-gem5", "--input", str(raw[core]),
             "--output", str(target), "--core", str(core)],
            text=True,
            env=run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        log.write_text(result.stdout, encoding="utf-8")
        if result.returncode != 0:
            raise RuntimeError(f"gem5 JSONL to FST failed core={core}: {log}")
        fst_info(target)
        paths.append(target)
    (output / "manifest.txt").write_text(
        "".join(
            f"{core} fastsim-binary core{core}.fst\n"
            for core in range(cores)
        ),
        encoding="utf-8",
    )
    return paths


def _remove_gem5_raw_trace(raw_dir: Path) -> None:
    import shutil
    shutil.rmtree(raw_dir)


def _fst_paths(root: Path, cores: int) -> list[Path]:
    paths = [root / f"core{core}.fst" for core in range(cores)]
    if not all(path.is_file() for path in paths):
        raise FileNotFoundError(f"FST output is incomplete: {root}")
    return paths


def _case_dir(root: Path, cores: int, workload: str) -> Path:
    return root / f"c{cores:02d}" / workload


def _gem5_trace_dir(trace_root: Path, cores: int, workload: str) -> Path:
    return _case_dir(trace_root, cores, workload) / "gem5"


def _dr_capture_dir(trace_root: Path, cores: int, workload: str) -> Path:
    return _case_dir(trace_root, cores, workload) / "dr"


def _gem5_fst_dir(fst_root: Path, cores: int, workload: str) -> Path:
    return _case_dir(fst_root, cores, workload) / "gem5"


def _dr_fst_dir(fst_root: Path, cores: int, workload: str) -> Path:
    return _case_dir(fst_root, cores, workload) / "dr"


def _workload_binary_name(workload: MatrixWorkload) -> str:
    if workload.workload_dir == DEFAULT_WORKLOAD_DIR:
        return workload.name.removeprefix("W_")
    return workload.name


def _workload_bin(workload: MatrixWorkload, producer: str) -> Path:
    try:
        binary_dir = WORKLOAD_BINARY_PRODUCERS[producer]
    except KeyError as error:
        raise ValueError(f"unknown workload binary producer: {producer}") from error
    return (
        WORKLOAD_ROOT / workload.workload_dir / "bin" / binary_dir
        / _workload_binary_name(workload)
    )


def _single_dr_trace_dir(capture_root: Path) -> Path:
    candidates = sorted(
        path for path in capture_root.iterdir()
        if path.is_dir() and path.name.startswith("drmemtrace")
    )
    if len(candidates) != 1:
        raise RuntimeError(
            f"DR capture must contain one drmemtrace directory under {capture_root}: "
            f"{[path.name for path in candidates]}"
        )
    return candidates[0]


def _manifest_entries(manifest: Path) -> list[tuple[int, Path]]:
    entries: list[tuple[int, Path]] = []
    for line_number, line in enumerate(
        manifest.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3:
            raise ValueError(
                f"{manifest}:{line_number}: expected '<core> fastsim-binary <path>'"
            )
        core_text, format_name, path_text = parts
        if format_name != "fastsim-binary":
            raise ValueError(
                f"{manifest}:{line_number}: DR validation accepts only "
                f"fastsim-binary, got {format_name}"
            )
        path = Path(path_text)
        if not path.is_absolute():
            path = manifest.parent / path
        entries.append((int(core_text), path))
    return entries


def _validate_fst_manifest(manifest: Path, *, require_physical: bool) -> None:
    entries = sorted(_manifest_entries(manifest))
    if not entries:
        raise ValueError(f"FST manifest is empty: {manifest}")
    for expected_core, (core, path) in enumerate(entries):
        if core != expected_core:
            raise ValueError(
                f"{manifest}: core IDs must be dense from zero, got {core}"
            )
        info = fst_info(path)
        if info.core_id != core:
            raise ValueError(f"{path}: header core ID does not match manifest")
        if info.records == 0:
            raise ValueError(f"{path}: FST has no records")
        with path.open("rb") as source:
            raw = source.read(FST_HEADER.size)
        magic, version, header_size, record_size, *_ = FST_HEADER.unpack(raw)
        if (
            magic != FST_MAGIC
            or version != FST_VERSION
            or header_size != FST_HEADER.size
            or record_size != FST_RECORD.size
        ):
            raise ValueError(f"{path}: invalid FST v6 header")
        for ordinal, record in enumerate(iter_fst(path), start=1):
            flags = int(record[9])
            op_class = int(record[10])
            reserved = int(record[-1])
            is_memory = bool(flags & MEMORY_FLAGS)
            is_syscall = op_class == -1
            if op_class < 0 and not is_syscall:
                raise ValueError(f"{path}: record {ordinal}: invalid negative op_class")
            if is_syscall:
                if flags & MEMORY_FLAGS:
                    raise ValueError(
                        f"{path}: record {ordinal}: syscall carries memory flags"
                    )
                if int(record[8]) != 0:
                    raise ValueError(
                        f"{path}: record {ordinal}: syscall carries memory size"
                    )
                if flags & PHYSICAL_ADDRESS:
                    raise ValueError(
                        f"{path}: record {ordinal}: syscall carries physical address"
                    )
                if flags & VIRTUAL_PAGE_TOKEN:
                    raise ValueError(
                        f"{path}: record {ordinal}: syscall carries virtual-page token"
                    )
            if is_memory:
                if int(record[8]) == 0:
                    raise ValueError(f"{path}: record {ordinal}: memory size is zero")
                if int(record[1]) == 0:
                    raise ValueError(f"{path}: record {ordinal}: memory address is zero")
                if require_physical and not flags & PHYSICAL_ADDRESS:
                    raise ValueError(
                        f"{path}: record {ordinal}: memory lacks physical address"
                    )
                if (
                    not flags & VIRTUAL_PAGE_TOKEN
                    or (reserved & ~DESTINATION_CLASS_MARKER) == 0
                ):
                    raise ValueError(
                        f"{path}: record {ordinal}: memory lacks virtual-page token"
                    )


def _validate_strict_fst_manifest(manifest: Path) -> None:
    _validate_fst_manifest(manifest, require_physical=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_dr_address_provenance(directory: Path, cores: int) -> dict[str, Any]:
    """Validate the per-stream PA/VA/token evidence that FST v6 cannot encode."""
    path = directory / "address-provenance.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid DR address provenance: {path}") from error
    if payload.get("schema") != "fastsim-dr-address-provenance-v1":
        raise ValueError(f"{path}: unsupported provenance schema")
    entries = payload.get("cores")
    if not isinstance(entries, list) or len(entries) != cores:
        raise ValueError(f"{path}: expected exactly {cores} core provenance entries")
    mapping_count = 0
    for expected_core, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("core") != expected_core:
            raise ValueError(f"{path}: provenance cores must be dense from zero")
        # FST v6 carries only the token, so one address space per output core is
        # the only sound strict-mode contract until the sidecar is consumed by
        # FastSim itself.
        if not isinstance(entry.get("pid"), int):
            raise ValueError(f"{path}: core {expected_core} lacks a single PID")
        mappings = entry.get("mappings")
        if not isinstance(mappings, list):
            raise ValueError(f"{path}: core {expected_core} has invalid mappings")
        virtual_pages: set[int] = set()
        tokens: set[int] = set()
        for mapping in mappings:
            if not isinstance(mapping, dict):
                raise ValueError(f"{path}: core {expected_core} has invalid mapping")
            vpage = mapping.get("virtual_page")
            ppage = mapping.get("physical_page")
            token = mapping.get("token")
            if (
                not isinstance(vpage, int)
                or not isinstance(ppage, int)
                or not isinstance(token, int)
                or ppage <= 0
                or token <= 0
                or vpage in virtual_pages
                or token in tokens
            ):
                raise ValueError(
                    f"{path}: core {expected_core} has non-bijective PA/token mapping"
                )
            virtual_pages.add(vpage)
            tokens.add(token)
        mapping_count += len(mappings)
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "address_spaces": cores,
        "mappings": mapping_count,
    }


def _audit_gem5_roi(gem5_out: Path, cores: int) -> dict[str, Any]:
    path = gem5_out / "tao_trace" / "roi_boundaries.jsonl"
    if not path.is_file():
        raise RuntimeError(f"gem5 ROI boundaries are missing: {path}")
    counts = {core: {"begin": 0, "end": 0} for core in range(cores)}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        event = json.loads(line)
        core = int(event["core_id"])
        name = str(event["event"])
        if core not in counts or name not in {"begin", "end"}:
            raise RuntimeError(f"invalid gem5 ROI boundary: {event}")
        counts[core][name] += 1
    errors = [
        {"core": core, **value}
        for core, value in counts.items()
        if value["begin"] != 1 or value["end"] != 1
    ]
    if errors:
        raise RuntimeError(f"gem5 ROI boundary audit failed: {errors}")
    return {"path": str(path), "per_core": counts}


def _case_status(comparison: dict[str, Any]) -> str:
    status = str(comparison.get("status", "fail"))
    if status in {"pass", "fail", "unsupported", "error"}:
        return status
    return "fail"


def _summarize_cases(cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    unsupported: list[dict[str, Any]] = []
    for case in cases:
        status = str(case.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
        if status == "unsupported":
            unsupported.append({
                "workload": case.get("workload"),
                "reason_code": case.get("reason_code"),
                "pc": case.get("pc"),
                "reason": case.get("reason"),
            })
    return {
        "status_counts": counts,
        "unsupported_cases": unsupported,
    }


def _ensure_workloads_built(skip_build: bool, workloads: Sequence[MatrixWorkload]) -> None:
    if skip_build:
        return
    workload_dirs = sorted({workload.workload_dir for workload in workloads})
    for workload_dir in workload_dirs:
        subprocess.run(
            ["make", "all", "dynamoRIO"],
            cwd=WORKLOAD_ROOT / workload_dir,
            check=True,
        )
    if DEFAULT_WORKLOAD_DIR in workload_dirs:
        subprocess.run([sys.executable, str(WORKLOAD_AUDIT)], check=True)


def _matrix_context(options: MatrixActionOptions) -> tuple[int, int, list[MatrixWorkload]]:
    matrix = _load_matrix(options.matrix_path, workload_dir=options.workload_dir)
    cores = int(options.cores or matrix["default_cores"])
    seed = int(options.seed if options.seed is not None else matrix["default_seed"])
    workloads = _selected_workloads(matrix, options.workloads)
    if options.scale is not None:
        workloads = [
            MatrixWorkload(
                name=item.name,
                group=item.group,
                scale=options.scale,
                workload_dir=item.workload_dir,
                domain=item.domain,
                expected_profiles=item.expected_profiles,
            )
            for item in workloads
        ]
    return cores, seed, workloads


def _workload_scales(workloads: Sequence[MatrixWorkload]) -> dict[str, int]:
    return {item.name: item.scale for item in workloads}


def _workload_args(cores: int, scale: int, seed: int) -> list[str]:
    return [str(cores), str(scale), "1", str(seed)]


def collect_gem5_traces(
    *,
    options: MatrixActionOptions,
    environment: ValidationEnvironment | None = None,
) -> dict[str, Any]:
    environment = environment or load_validation_environment()
    cores, seed, workloads = _matrix_context(options)
    _ensure_workloads_built(options.skip_build, workloads)
    trace_root = _matrix_root(options.trace_root, options.matrix_path)
    report: dict[str, Any] = {
        "schema": "fastsim-dr-gem5-trace-collection-v1",
        "status": "running",
        "trace_root": str(trace_root),
        "cases": [],
    }
    for workload in workloads:
        output = _gem5_trace_dir(trace_root, cores, workload.name)
        case_report: dict[str, Any] = {"workload": workload.name, "path": str(output)}
        try:
            if output.exists() and not options.force:
                raise FileExistsError(f"gem5 trace exists: {output}; use --force")
            if output.exists():
                import shutil
                shutil.rmtree(output)
            output.mkdir(parents=True)
            log = output / "gem5.log"
            command = [
                str(environment.runtime.detailed_gem5), f"--outdir={output}",
                str(options.gem5_config), "--cmd",
                str(_workload_bin(workload, "gem5").resolve()),
                "--workload-args", *_workload_args(cores, workload.scale, seed),
                "--num-cores", str(cores), "--require-roi", "--ff-atomic",
                "--records-only",
            ]
            with log.open("w", encoding="utf-8") as handle:
                result = subprocess.run(
                    command, cwd=PROJECT_ROOT,
                    env=environment.runtime.runtime_environ(), stdout=handle,
                    stderr=subprocess.STDOUT, check=False,
                )
            if result.returncode != 0:
                raise RuntimeError(f"detailed gem5 failed rc={result.returncode}: {log}")
            roi = _audit_gem5_roi(output, cores)
            case_report.update({"status": "pass", "gem5_roi": roi})
        except Exception as error:
            case_report.update({
                "status": "error", "error_type": type(error).__name__,
                "error": str(error),
            })
        report["cases"].append(case_report)
        _write_json(trace_root / "gem5-trace-report.json", report)
    statuses = [case["status"] for case in report["cases"]]
    report["status"] = "pass" if statuses and all(s == "pass" for s in statuses) else "error"
    _write_json(trace_root / "gem5-trace-report.json", report)
    return report


def collect_dr_traces(
    *,
    options: MatrixActionOptions,
    environment: ValidationEnvironment | None = None,
) -> dict[str, Any]:
    environment = environment or load_validation_environment()
    cores, seed, workloads = _matrix_context(options)
    _ensure_workloads_built(options.skip_build, workloads)
    if options.skip_build:
        _div_capture_client(environment)
    else:
        build_div_capture_client(environment)
    trace_root = _matrix_root(options.trace_root, options.matrix_path)
    report: dict[str, Any] = {
        "schema": "fastsim-dr-raw-trace-collection-v1",
        "status": "running",
        "trace_root": str(trace_root),
        "cases": [],
    }
    for workload in workloads:
        output = _dr_capture_dir(trace_root, cores, workload.name)
        case_report: dict[str, Any] = {"workload": workload.name, "path": str(output)}
        try:
            if output.exists() and not options.force:
                raise FileExistsError(f"DR trace exists: {output}; use --force")
            if output.exists():
                import shutil
                shutil.rmtree(output)
            capture = capture_dr_trace(
                binary=_workload_bin(workload, "dr"),
                arguments=_workload_args(cores, workload.scale, seed),
                output_dir=output,
                environment=environment,
                use_sudo=options.dr_capture_sudo,
                build_client=False,
            )
            case_report.update({
                "status": "pass",
                "trace_dir": str(capture["trace_dir"]),
                "invariant_checker": str(capture["invariant_checker"]),
                "sudo": capture["sudo"],
            })
        except Exception as error:
            case_report.update({
                "status": "error", "error_type": type(error).__name__,
                "error": str(error),
            })
        report["cases"].append(case_report)
        _write_json(trace_root / "dr-trace-report.json", report)
    statuses = [case["status"] for case in report["cases"]]
    report["status"] = "pass" if statuses and all(s == "pass" for s in statuses) else "error"
    _write_json(trace_root / "dr-trace-report.json", report)
    return report


def convert_gem5_fsts(*, options: MatrixActionOptions) -> dict[str, Any]:
    cores, seed, workloads = _matrix_context(options)
    fastsim = _fastsim_binary(options.fastsim_binary)
    trace_root = _matrix_root(options.trace_root, options.matrix_path)
    fst_root = _matrix_root(options.fst_root, options.matrix_path)
    fst_root.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema": "fastsim-dr-gem5-fst-conversion-v1",
        "status": "running",
        "trace_root": str(trace_root),
        "fst_root": str(fst_root),
        "cases": [],
    }
    for workload in workloads:
        trace = _gem5_trace_dir(trace_root, cores, workload.name)
        output = _gem5_fst_dir(fst_root, cores, workload.name)
        case_report: dict[str, Any] = {"workload": workload.name, "path": str(output)}
        try:
            if output.exists() and not options.force:
                raise FileExistsError(f"gem5 FST exists: {output}; use --force")
            if output.exists():
                import shutil
                shutil.rmtree(output)
            raw_dir = trace / "tao_trace"
            paths = _convert_gem5_records(raw_dir, output, cores, fastsim)
            _validate_strict_fst_manifest(output / "manifest.txt")
            _remove_gem5_raw_trace(raw_dir)
            case_report.update({"status": "pass", "records": sum(fst_info(path).records for path in paths)})
        except Exception as error:
            case_report.update({
                "status": "error", "error_type": type(error).__name__,
                "error": str(error),
            })
        report["cases"].append(case_report)
        _write_json(fst_root / "gem5-fst-report.json", report)
    statuses = [case["status"] for case in report["cases"]]
    report["status"] = "pass" if statuses and all(s == "pass" for s in statuses) else "error"
    _write_json(fst_root / "gem5-fst-report.json", report)
    return report


def convert_dr_fsts(
    *,
    options: MatrixActionOptions,
    environment: ValidationEnvironment | None = None,
) -> dict[str, Any]:
    environment = environment or load_validation_environment()
    cores, seed, workloads = _matrix_context(options)
    trace_root = _matrix_root(options.trace_root, options.matrix_path)
    fst_root = _matrix_root(options.fst_root, options.matrix_path)
    fst_root.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema": "fastsim-dr-fst-conversion-v1",
        "status": "running",
        "trace_root": str(trace_root),
        "fst_root": str(fst_root),
        "cases": [],
    }
    for workload in workloads:
        trace_case_root = _dr_capture_dir(trace_root, cores, workload.name)
        output = _dr_fst_dir(fst_root, cores, workload.name)
        case_report: dict[str, Any] = {"workload": workload.name, "path": str(output)}
        try:
            if output.exists() and not options.force:
                raise FileExistsError(f"DR FST exists: {output}; use --force")
            if output.exists():
                import shutil
                shutil.rmtree(output)
            paths = list(convert_dr_trace(
                trace_dir=_single_dr_trace_dir(trace_case_root),
                output_dir=output,
                num_cores=cores,
                runtime=environment.runtime,
            ))
            _validate_strict_fst_manifest(output / "manifest.txt")
            case_report.update({
                "status": "pass",
                "records": sum(fst_info(path).records for path in paths),
            })
        except UnsupportedConversion as error:
            case_report.update({
                "status": "unsupported", "reason_code": error.reason_code,
                "pc": f"{error.pc:#x}", "reason": error.reason,
            })
        except Exception as error:
            case_report.update({
                "status": "error", "error_type": type(error).__name__,
                "error": str(error),
            })
        report["cases"].append(case_report)
        _write_json(fst_root / "dr-fst-report.json", report)
    statuses = [case["status"] for case in report["cases"]]
    report["status"] = (
        "error" if "error" in statuses
        else "pass" if statuses and all(s == "pass" for s in statuses)
        else "needs_work"
    )
    report.update(_summarize_cases(report["cases"]))
    _write_json(fst_root / "dr-fst-report.json", report)
    return report


def validate_dr_matrix(
    *,
    options: ValidationOptions,
    environment: ValidationEnvironment | None = None,
) -> dict[str, Any]:
    output_dir = options.output_dir
    if output_dir.exists():
        raise FileExistsError(f"validation output exists: {output_dir}")
    output_dir.mkdir(parents=True)
    matrix = _load_matrix(options.matrix_path, workload_dir=options.workload_dir)
    cores = int(options.cores or matrix["default_cores"])
    seed = int(options.seed if options.seed is not None else matrix["default_seed"])
    workloads = _selected_workloads(matrix, options.workloads)
    trace_root = _matrix_root(options.trace_root, options.matrix_path)
    fst_root = _matrix_root(options.fst_root, options.matrix_path)
    if options.scale is not None:
        workloads = [
            MatrixWorkload(
                name=item.name,
                group=item.group,
                scale=options.scale,
                workload_dir=item.workload_dir,
                domain=item.domain,
                expected_profiles=item.expected_profiles,
            )
            for item in workloads
        ]
    report: dict[str, Any] = {
        "schema": "fastsim-dr-readonly-validation-report-v2",
        "status": "running",
        "matrix": str(options.matrix_path),
        "git": _git_identity(),
        "parameters": {
            "cores": cores,
            "seed": seed,
            "workloads": [item.name for item in workloads],
            "workload_scales": _workload_scales(workloads),
        },
        "roots": {
            "trace_root": str(trace_root),
            "fst_root": str(fst_root),
        },
        "cases": [],
        "not_run": [],
    }
    for workload in workloads:
        gem5_dir = _gem5_fst_dir(fst_root, cores, workload.name)
        dr_dir = _dr_fst_dir(fst_root, cores, workload.name)
        case_report: dict[str, Any] = {
            "workload": workload.name,
            "group": workload.group,
            "gem5_fst": str(gem5_dir),
            "dr_fst": str(dr_dir),
        }
        try:
            gem5_fst = _fst_paths(gem5_dir, cores)
            dr_fst = _fst_paths(dr_dir, cores)
            _validate_strict_fst_manifest(gem5_dir / "manifest.txt")
            _validate_strict_fst_manifest(dr_dir / "manifest.txt")
            provenance = _validate_dr_address_provenance(dr_dir, cores)
            comparison = compare_fst_pairs(gem5_fst, dr_fst)
            comparison["status"] = _case_status(comparison)
            case_report.update({
                "status": comparison["status"],
                "address_provenance": provenance,
                "records": comparison.get("records", 0),
                "bytes_compared": comparison.get("bytes_compared", 0),
                "per_core": comparison.get("per_core", []),
                "domains": comparison.get("domains", {}),
            })
            _write_json(output_dir / f"{workload.name}.comparison.json", case_report)
        except Exception as error:
            case_report.update({
                "status": "error",
                "error_type": type(error).__name__,
                "error": str(error),
            })
            _write_json(output_dir / f"{workload.name}.comparison.json", case_report)
        report["cases"].append(case_report)
        _write_json(output_dir / "report.json", report)
    statuses = [str(case["status"]) for case in report["cases"]]
    report["status"] = (
        "error" if "error" in statuses
        else "pass" if statuses and all(value == "pass" for value in statuses)
        else "needs_work"
    )
    report.update(_summarize_cases(report["cases"]))
    _write_json(output_dir / "report.json", report)
    return report
