from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .dynamorio import capture_dr_trace, convert_dr_trace
from .environment import load_validation_environment
from .validation import (
    FST_ROOT,
    GEM5_CONFIG,
    TRACE_ROOT,
    VALIDATION_MATRIX_PATH,
    MatrixActionOptions,
    ValidationOptions,
    collect_dr_traces,
    collect_gem5_traces,
    convert_dr_fsts,
    convert_gem5_fsts,
    _matrix_root,
    validate_dr_matrix,
)
from .replay_validation import (
    DEFAULT_CONFIG as DEFAULT_STATS_CONFIG,
    DEFAULT_DR_CONFIG as DEFAULT_DR_STATS_CONFIG,
    ReplayValidationOptions,
    simulate_replay_matrix,
    validate_replay_matrix,
)


def _matrix_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--matrix", type=Path, default=VALIDATION_MATRIX_PATH)
    parser.add_argument("--cores", type=int)
    parser.add_argument("--scale", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--workload", action="append", default=[])
    parser.add_argument("--workload-dir")
    parser.add_argument("--trace-root", type=Path, default=TRACE_ROOT)
    parser.add_argument("--fst-root", type=Path, default=FST_ROOT)


def _tool_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--gem5-config", type=Path, default=GEM5_CONFIG)
    parser.add_argument("--fastsim", type=Path)
    parser.add_argument("--gem5-root", type=Path)
    parser.add_argument("--converter", type=Path)
    parser.add_argument("--detailed-gem5", type=Path)
    parser.add_argument("--dynamorio-root", type=Path)


def _action_options(args: argparse.Namespace) -> MatrixActionOptions:
    return MatrixActionOptions(
        matrix_path=args.matrix,
        cores=args.cores,
        scale=args.scale,
        seed=args.seed,
        workloads=tuple(args.workload),
        trace_root=args.trace_root,
        fst_root=args.fst_root,
        force=getattr(args, "force", False),
        skip_build=getattr(args, "skip_build", False),
        gem5_config=getattr(args, "gem5_config", GEM5_CONFIG),
        fastsim_binary=getattr(args, "fastsim", None),
        dr_capture_sudo=getattr(args, "sudo", False),
        workload_dir=getattr(args, "workload_dir", None),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FastSim DR-to-FST tools")
    actions = parser.add_subparsers(dest="action", required=True)

    capture = actions.add_parser("capture")
    capture.add_argument("--binary", type=Path, required=True)
    capture.add_argument("--output-dir", type=Path, required=True)
    capture.add_argument(
        "--sudo", action="store_true",
        help="run the DR capture process through non-interactive sudo -n",
    )
    capture.add_argument(
        "workload_args", nargs=argparse.REMAINDER,
        help="workload arguments after --; first argument must be core count",
    )
    convert = actions.add_parser("convert")
    convert.add_argument("--trace-dir", type=Path, required=True)
    convert.add_argument("--output-dir", type=Path, required=True)
    convert.add_argument("--num-cores", type=int, required=True)

    collect_gem5 = actions.add_parser("collect-gem5-trace")
    _matrix_args(collect_gem5)
    _tool_args(collect_gem5)
    collect_gem5.add_argument("--force", action="store_true")
    collect_gem5.add_argument("--skip-build", action="store_true")

    collect_dr = actions.add_parser("collect-dr-trace")
    _matrix_args(collect_dr)
    _tool_args(collect_dr)
    collect_dr.add_argument("--force", action="store_true")
    collect_dr.add_argument("--skip-build", action="store_true")
    collect_dr.add_argument(
        "--sudo", action="store_true",
        help="run every DR capture process through non-interactive sudo -n",
    )

    convert_gem5 = actions.add_parser("convert-gem5-fst")
    _matrix_args(convert_gem5)
    _tool_args(convert_gem5)
    convert_gem5.add_argument("--force", action="store_true")

    convert_dr = actions.add_parser("convert-dr-fst")
    _matrix_args(convert_dr)
    _tool_args(convert_dr)
    convert_dr.add_argument("--force", action="store_true")

    simulate = actions.add_parser("simulate-replay")
    _matrix_args(simulate)
    simulate.add_argument("--config", type=Path)
    simulate.add_argument("--dr-config", type=Path)
    simulate.add_argument("--fastsim", type=Path)
    simulate.add_argument("--force", action="store_true")

    validate = actions.add_parser("validate")
    validate.add_argument("--out", type=Path, required=True)
    validate.add_argument("--matrix", type=Path, default=VALIDATION_MATRIX_PATH)
    validate.add_argument("--cores", type=int)
    validate.add_argument("--scale", type=int)
    validate.add_argument("--seed", type=int)
    validate.add_argument("--workload", action="append", default=[])
    validate.add_argument("--workload-dir")
    validate.add_argument("--trace-root", type=Path, default=TRACE_ROOT)
    validate.add_argument("--fst-root", type=Path, default=FST_ROOT)

    validate_replay = actions.add_parser("validate-replay")
    validate_replay.add_argument("--fst-root", type=Path, default=FST_ROOT)
    validate_replay.add_argument("--out", type=Path, required=True)
    validate_replay.add_argument("--matrix", type=Path, default=VALIDATION_MATRIX_PATH)
    validate_replay.add_argument("--cores", type=int)
    validate_replay.add_argument("--config", type=Path)
    validate_replay.add_argument("--dr-config", type=Path)
    validate_replay.add_argument("--fastsim", type=Path)
    validate_replay.add_argument("--workload", action="append", default=[])
    validate_replay.add_argument("--resume", action="store_true")
    return parser


def _environment(args: argparse.Namespace):
    return load_validation_environment(
        gem5_root=getattr(args, "gem5_root", None),
        converter=getattr(args, "converter", None),
        detailed_gem5=getattr(args, "detailed_gem5", None),
        dynamorio_root=getattr(args, "dynamorio_root", None),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.action == "capture":
        workload_args = list(args.workload_args)
        if workload_args[:1] == ["--"]:
            workload_args.pop(0)
        result = capture_dr_trace(
            binary=args.binary,
            arguments=workload_args,
            output_dir=args.output_dir,
            environment=load_validation_environment(),
            use_sudo=args.sudo,
        )
        print(result["trace_dir"])
        return 0
    if args.action == "convert":
        paths = convert_dr_trace(
            trace_dir=args.trace_dir,
            output_dir=args.output_dir,
            num_cores=args.num_cores,
        )
        for path in paths:
            print(path)
        return 0
    try:
        if args.action == "collect-gem5-trace":
            report = collect_gem5_traces(options=_action_options(args), environment=_environment(args))
            print(_matrix_root(args.trace_root, args.matrix).resolve() / "gem5-trace-report.json")
        elif args.action == "collect-dr-trace":
            report = collect_dr_traces(options=_action_options(args), environment=_environment(args))
            print(_matrix_root(args.trace_root, args.matrix).resolve() / "dr-trace-report.json")
        elif args.action == "convert-gem5-fst":
            report = convert_gem5_fsts(options=_action_options(args))
            print(_matrix_root(args.fst_root, args.matrix).resolve() / "gem5-fst-report.json")
        elif args.action == "convert-dr-fst":
            report = convert_dr_fsts(options=_action_options(args), environment=_environment(args))
            print(_matrix_root(args.fst_root, args.matrix).resolve() / "dr-fst-report.json")
        elif args.action == "simulate-replay":
            report = simulate_replay_matrix(
                ReplayValidationOptions(
                    fst_root=args.fst_root,
                    output_dir=args.fst_root,
                    matrix_path=args.matrix,
                    cores=args.cores,
                    config_path=args.config,
                    dr_config_path=args.dr_config,
                    fastsim_binary=args.fastsim,
                    workloads=tuple(args.workload),
                    resume=not args.force,
                )
            )
            print(_matrix_root(args.fst_root, args.matrix).resolve() / "replay-report.json")
        elif args.action == "validate":
            report = validate_dr_matrix(
                options=ValidationOptions(
                    output_dir=args.out,
                    matrix_path=args.matrix,
                    cores=args.cores,
                    scale=args.scale,
                    seed=args.seed,
                    workloads=tuple(args.workload),
                    workload_dir=args.workload_dir,
                    trace_root=args.trace_root,
                    fst_root=args.fst_root,
                )
            )
            print(args.out.resolve() / "report.json")
        elif args.action == "validate-replay":
            report = validate_replay_matrix(
                ReplayValidationOptions(
                    fst_root=args.fst_root,
                    output_dir=args.out,
                    matrix_path=args.matrix,
                    cores=args.cores,
                    config_path=args.config,
                    dr_config_path=args.dr_config,
                    fastsim_binary=args.fastsim,
                    workloads=tuple(args.workload),
                    resume=args.resume,
                )
            )
            print(args.out.resolve() / "report.json")
        else:
            raise ValueError(f"unknown action: {args.action}")
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"{args.action} failed: {error}", file=sys.stderr)
        return 2
    return {"pass": 0, "diagnostic_only": 0, "needs_work": 1, "error": 2}.get(
        str(report["status"]), 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
