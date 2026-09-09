"""Restore-only TaoTrace wrapper for x86_fs_kvm_boot_checkpoint.py.

minesim mainline note
---------------------
Promoted into the minesim FastSim mainline as the canonical gem5 TaoTrace
collection entry point (moved here from the fastsim-branch collection tree,
originally /data00/yinhaolang/TCSim/configs/gem5/). It is paired with the
QEMU-FST producer so both sources emit canonical FST v7 for cross-producer
comparison. For minesim we drive it with --tao-functional-user-only (instead
of --tao-functional-include-kernel) so the resulting FST is pure user-scope
and directly symmetric with the QEMU user-only FST. The ROI checkpoint is
scope-independent and shared between user-only and user-plus-kernel captures;
only this collection-time switch changes. CONFIG_ROOT resolves to this file's
directory, so the co-located x86_fs_kvm_boot_checkpoint.py is the main config.

This wrapper is invoked in place of the main FS checkpoint config when a
functional TaoTrace capture is desired at ROI start. It never modifies the
main config's byte contents, so the ROI checkpoint cache stays valid.

It hooks m5._simulate_module._fix_all_objects: the Simulator stdlib calls
this once, after the SimObject tree exists (root, cores, memories) but
before any C++ instantiation. We walk descendants(), find X86O3CPU /
DerivO3CPU cores, attach a TaoTrace ProbeListenerObject as a child param,
then delegate to the real _fix_all_objects so adoptOrphanParams picks up
our new node. The main config is executed via runpy.run_path with
run_name=__m5_main__ so its top-level guard triggers exactly as in a
direct gem5 invocation.
"""

import argparse
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import m5
from m5.objects import TaoTrace


CONFIG_ROOT = Path(__file__).resolve().parent
MAIN_CONFIG = CONFIG_ROOT / "x86_fs_kvm_boot_checkpoint.py"

# Linux x86-64 ABI argument counts for the portable, commonly observed
# syscall surface. Like drmemtrace -record_syscall, unlisted calls remain
# number-only instead of guessing how many stale argument registers are valid.
DEFAULT_SYSCALL_ARG_COUNTS = (
    "0:3,1:3,2:3,3:1,4:2,5:2,6:2,7:3,8:3,9:6,10:3,11:2,12:1,"
    "13:4,14:4,16:3,17:4,18:4,19:3,20:3,21:2,22:1,23:5,24:0,25:5,"
    "28:3,32:1,33:2,35:2,39:0,41:3,42:3,43:3,44:6,45:6,46:3,47:3,"
    "49:3,50:2,51:3,52:3,53:4,54:5,55:5,56:5,57:0,58:0,59:3,60:1,"
    "61:4,62:2,63:1,72:3,89:3,96:2,97:2,98:2,99:1,102:0,104:0,"
    "107:0,108:0,110:0,111:0,158:2,186:0,201:1,202:6,203:3,217:3,218:1,"
    "219:0,228:2,230:2,231:1,232:4,233:4,234:3,257:4,262:4,267:4,"
    "270:6,271:5,273:2,281:6,286:4,291:1,293:2,302:4,318:3,332:5,"
    "334:4,435:2"
)


def _units_to_bytes(text):
    text = str(text).strip()
    units = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "B": 1}
    for suffix, multiplier in units.items():
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * multiplier)
    return int(text)


def _normalize_protocol(name):
    mapping = {
        "mesi-three-level": "MESI_Three_Level",
        "mesi_three_level": "MESI_Three_Level",
        "MESI_Three_Level": "MESI_Three_Level",
    }
    return mapping.get(str(name), "MESI_Three_Level")


def _write_uarch_profile(profile_path, cli_args):
    freq_str = cli_args.clk.strip()
    for suffix in ("GHz", "Ghz"):
        if freq_str.endswith(suffix):
            freq_str = freq_str[: -len(suffix)]
            break
    data = {
        "schema_version": 2,
        "source": (
            "TCSim FS x86_fs_kvm_boot_checkpoint functional-only capture"
        ),
        "core": {
            "isa": "X86",
            "num_cores": cli_args.num_cores,
            "freq_ghz": float(freq_str),
            "rob_entries": cli_args.rob_entries,
        },
        "cache": {
            "l1d": {
                "size_b": _units_to_bytes(cli_args.l1d_size),
                "assoc": cli_args.l1d_assoc,
                "line_b": 64,
                "num_banks": 1,
                "bank_select_low_bit": 6,
                "policy": "lru",
            },
            "l1i": {
                "size_b": _units_to_bytes(cli_args.l1i_size),
                "assoc": cli_args.l1i_assoc,
                "line_b": 64,
                "num_banks": 1,
                "bank_select_low_bit": 6,
                "policy": "lru",
            },
            "l2": {
                "size_b": _units_to_bytes(cli_args.l2_size),
                "assoc": cli_args.l2_assoc,
                "line_b": 64,
                "num_banks": 1,
                "bank_select_low_bit": 6,
                "policy": "lru",
            },
            "l3": {
                "size_b": (
                    _units_to_bytes(cli_args.l3_size)
                    * cli_args.num_l3_banks
                ),
                "assoc": cli_args.l3_assoc,
                "line_b": 64,
                "num_banks": cli_args.num_l3_banks,
                "bank_select_low_bit": 6,
                "policy": "lru",
            },
        },
        "tlb": {
            "dtlb": {"entries": 64, "assoc": 64},
            "itlb": {"entries": 64, "assoc": 64},
            "stlb": None,
        },
        "page_walker": {
            "levels": 4,
            "page_size_bits": 12,
            "walk_attaches_to": "sequencer",
            "pwc_entries": 0,
        },
        "coherence": {"protocol": _normalize_protocol(cli_args.cache_hierarchy)},
        "dram": {
            "model": f"{cli_args.mem_channels}ChannelDDR4_2400",
            "size_b": _units_to_bytes(cli_args.mem_size),
            "num_channels": cli_args.mem_channels,
            "banks_per_channel": 16,
            "row_size_b": 8192,
            "burst_b": 64,
            "interleaving_size_b": 64,
            "queue_window": 256,
        },
    }
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    with profile_path.open("w") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return profile_path


def _parse_trace_args():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--tao-trace-dir", type=Path, required=True)
    parser.add_argument(
        "--tao-trace-format",
        choices=("jsonl", "fst"),
        default="jsonl",
    )
    parser.add_argument("--tao-measure-cpl", action="store_true")
    parser.add_argument("--tao-native-response-jsonl", action="store_true")
    parser.add_argument("--tao-native-anomaly-limit", type=int, default=32)
    parser.add_argument("--tao-wrong-path-oracle", action="store_true")
    parser.add_argument("--tao-no-records", action="store_true")
    parser.add_argument("--tao-functional-user-only", action="store_true")
    parser.add_argument(
        "--tao-functional-include-kernel", action="store_true"
    )
    parser.add_argument("--tao-functional-warmup", action="store_true")
    parser.add_argument("--tao-functional-user-target", type=int, default=0)
    parser.add_argument(
        "--tao-syscall-arg-counts",
        default=DEFAULT_SYSCALL_ARG_COUNTS,
        help=(
            "Comma-separated Linux x86-64 nr:argument-count map. Unlisted "
            "syscalls retain number-only metadata."
        ),
    )
    known, remaining = parser.parse_known_args()
    if known.tao_native_anomaly_limit < 0:
        parser.error("--tao-native-anomaly-limit must be >= 0")
    return known, remaining


def _peek_main_args(remaining):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--num-cores", type=int, default=1)
    parser.add_argument("--clk", default="3GHz")
    parser.add_argument("--rob-entries", type=int, default=192)
    parser.add_argument("--l1d-size", default="32KiB")
    parser.add_argument("--l1d-assoc", type=int, default=8)
    parser.add_argument("--l1i-size", default="32KiB")
    parser.add_argument("--l1i-assoc", type=int, default=8)
    parser.add_argument("--l2-size", default="1MiB")
    parser.add_argument("--l2-assoc", type=int, default=8)
    parser.add_argument("--l3-size", default="8MiB")
    parser.add_argument("--l3-assoc", type=int, default=16)
    parser.add_argument("--num-l3-banks", type=int, default=8)
    parser.add_argument("--mem-size", default="3GiB")
    parser.add_argument("--mem-channels", type=int, default=8)
    parser.add_argument("--cache-hierarchy", default="mesi-three-level")
    known, _ = parser.parse_known_args(remaining)
    return known


def _install_instantiate_hook(
    trace_dir,
    profile_path,
    trace_format,
    measure_cpl,
    emit_native_response_jsonl,
    native_response_anomaly_limit,
    emit_wrong_path_oracle,
    emit_records,
    functional_user_only,
    functional_include_kernel,
    functional_warmup,
    functional_user_target,
    syscall_arg_counts,
):
    state = {"attached": False}
    sim_module = m5._simulate_module
    original = sim_module._fix_all_objects
    original_dump_configs = sim_module._dump_configs

    def patched_fix_all_objects(root, *args, **kwargs):
        if not state["attached"]:
            cores = []
            for descendant in root.descendants():
                cls = descendant.__class__.__name__
                if cls in ("X86O3CPU", "O3CPU", "DerivO3CPU"):
                    cores.append(descendant)
            for simobject in cores:
                simobject.tao_trace = TaoTrace(
                    output_dir=str(trace_dir),
                    uarch_profile_path=str(profile_path),
                    emit_micro=emit_records,
                    emit_macro=False,
                    emit_mem_events=False,
                    require_roi=False,
                    trace_format=trace_format,
                    measure_cpl=measure_cpl,
                    emit_native_response_jsonl=emit_native_response_jsonl,
                    native_response_anomaly_limit=(
                        native_response_anomaly_limit
                    ),
                    emit_wrong_path_oracle=emit_wrong_path_oracle,
                    functional_user_only=functional_user_only,
                    functional_include_kernel=functional_include_kernel,
                    functional_warmup=functional_warmup,
                    functional_user_target=functional_user_target,
                    syscall_arg_counts=syscall_arg_counts,
                )
            state["attached"] = True
            print(
                f"[tao-wrap] TaoTrace attached cores={len(cores)} "
                f"output_dir={trace_dir} format={trace_format} "
                f"measure_cpl={measure_cpl} "
                f"wrong_path_oracle={emit_wrong_path_oracle} "
                f"emit_records={emit_records}",
                flush=True,
            )
        return original(root, *args, **kwargs)

    sim_module._fix_all_objects = patched_fix_all_objects

    def patched_dump_configs(
        root,
        outdir=None,
        ini_config=None,
        json_config=None,
        dot_config=None,
    ):
        result = original_dump_configs(
            root, outdir, ini_config, json_config, dot_config
        )
        if state.get("profile_generated"):
            return result
        from m5 import options

        actual_outdir = Path(outdir or options.outdir).resolve()
        config_name = ini_config
        if config_name is None:
            config_name = options.dump_config
        if not config_name:
            raise RuntimeError(
                "TaoTrace P0 requires gem5 config.ini generation"
            )
        generator = os.environ.get("FASTSIM_EFFECTIVE_TARGET_GENERATOR")
        if not generator:
            raise RuntimeError(
                "FASTSIM_EFFECTIVE_TARGET_GENERATOR must name the canonical "
                "final-config sidecar generator"
            )
        python = os.environ.get(
            "FASTSIM_EFFECTIVE_TARGET_PYTHON", "/usr/bin/python3"
        )
        command = [
            python,
            generator,
            "--config",
            str(actual_outdir / config_name),
            "--uarch-profile",
            str(profile_path),
            "--effective-target",
            str(actual_outdir / "effective-target.json"),
        ]
        event_dictionary = os.environ.get("FASTSIM_EVENT_DICTIONARY")
        if event_dictionary:
            command.extend(["--event-dictionary", event_dictionary])
        if os.environ.get("TAOGEN_SHARED"):
            command.extend(
                ["--taotrace-shared-root", os.environ["TAOGEN_SHARED"]]
            )
        subprocess.run(command, check=True)
        state["profile_generated"] = True
        print(
            "[tao-wrap] generated uarch profile and effective target from "
            f"{actual_outdir / config_name}",
            flush=True,
        )
        return result

    sim_module._dump_configs = patched_dump_configs


trace_args, remaining = _parse_trace_args()
main_args = _peek_main_args(remaining)
trace_dir = trace_args.tao_trace_dir.resolve()
trace_dir.mkdir(parents=True, exist_ok=True)
syscall_arg_counts = {
    str(int(item.split(":", 1)[0])): int(item.split(":", 1)[1])
    for item in trace_args.tao_syscall_arg_counts.split(",")
    if item
}
(trace_dir / "syscall_capture.json").write_text(
    json.dumps(
        {
            "schema": "taotrace-syscall-capture-v1",
            "producer": "gem5-taotrace",
            "abi": "linux-x86_64",
            "fst_version": 7,
            "argument_counts": syscall_arg_counts,
            "timestamp_unit": "microseconds",
            "timestamp_origin": "gem5-simulation-start",
            "thread_id": "unavailable; validity bit remains clear",
            "return_association": "cr3+rsp+return-pc",
        },
        indent=2,
        sort_keys=True,
    )
    + "\n"
)
# The path is attached before C++ instantiation, but its content is generated
# only after gem5 has emitted the final config.ini. patched_dump_configs runs
# between _fix_all_objects and _create_cpp_objects, so TaoTrace never consumes
# wrapper defaults or a post-hoc profile.
profile_path = trace_dir / "uarch_profile.json"
_install_instantiate_hook(
    trace_dir,
    profile_path,
    trace_args.tao_trace_format,
    trace_args.tao_measure_cpl,
    trace_args.tao_native_response_jsonl,
    trace_args.tao_native_anomaly_limit,
    trace_args.tao_wrong_path_oracle,
    not trace_args.tao_no_records,
    trace_args.tao_functional_user_only,
    trace_args.tao_functional_include_kernel,
    trace_args.tao_functional_warmup,
    trace_args.tao_functional_user_target,
    trace_args.tao_syscall_arg_counts,
)
sys.argv = [str(MAIN_CONFIG)] + remaining
runpy.run_path(str(MAIN_CONFIG), run_name="__m5_main__")
