#!/usr/bin/env python3
"""gem5 SE stats-only runner for FastSim microarchitecture sweeps.

This file is executed by gem5, not by the host Python interpreter.  It keeps
the existing v28 ROI/Atomic->O3 protocol while deliberately omitting TaoTrace.
Every exposed option below mutates an actual gem5 SimObject; the outer
collector validates the resulting config.ini before accepting a case.
"""

import argparse
import json
import os

import m5
from m5.objects import SimpleMemory, TAGE_SC_L_64KB, TaoTrace

from gem5.components.boards.simple_board import SimpleBoard
from gem5.components.cachehierarchies.ruby.mesi_three_level_cache_hierarchy import (
    MESIThreeLevelCacheHierarchy,
)
from gem5.components.memory.dram_interfaces.ddr4 import DDR4_2400_8x8
from gem5.components.memory.memory import ChanneledMemory
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_switchable_processor import (
    SimpleSwitchableProcessor,
)
from gem5.isas import ISA
from gem5.resources.resource import BinaryResource
from gem5.simulate.exit_event import ExitEvent
from gem5.simulate.simulator import Simulator


def parse_args():
    parser = argparse.ArgumentParser(
        description="FastSim uarch validation: gem5 SE CPI/PMU only"
    )
    parser.add_argument("--cmd", required=True)
    parser.add_argument("--workload-args", nargs="*", default=[])
    parser.add_argument("--num-cores", type=int, default=4)
    parser.add_argument("--clk", default="3GHz")
    parser.add_argument("--mem-size", default="4GiB")

    parser.add_argument("--fetch-width", type=int, default=8)
    parser.add_argument("--decode-width", type=int, default=8)
    parser.add_argument("--rename-width", type=int, default=8)
    parser.add_argument("--dispatch-width", type=int, default=8)
    parser.add_argument("--issue-width", type=int, default=8)
    parser.add_argument("--wb-width", type=int, default=8)
    parser.add_argument("--commit-width", type=int, default=8)
    parser.add_argument("--rob-entries", type=int, default=192)
    parser.add_argument("--iq-entries", type=int, default=64)
    parser.add_argument("--lq-entries", type=int, default=32)
    parser.add_argument("--sq-entries", type=int, default=32)
    parser.add_argument("--dtlb-entries", type=int, default=64)
    parser.add_argument("--phys-int-regs", type=int, default=256)
    parser.add_argument("--phys-float-regs", type=int, default=256)
    parser.add_argument("--phys-vec-regs", type=int, default=256)
    parser.add_argument("--phys-cc-regs", type=int, default=1280)
    parser.add_argument(
        "--branch-predictor",
        choices=("tournament", "tage64k"),
        default="tournament",
    )

    parser.add_argument("--l1i-size", default="32KiB")
    parser.add_argument("--l1d-size", default="32KiB")
    parser.add_argument("--l2-size", default="1MiB")
    parser.add_argument(
        "--l3-size",
        default="8MiB",
        help="Per-bank L3 capacity (total is size multiplied by banks)",
    )
    parser.add_argument("--l1i-assoc", type=int, default=8)
    parser.add_argument("--l1d-assoc", type=int, default=8)
    parser.add_argument("--l2-assoc", type=int, default=8)
    parser.add_argument("--l3-assoc", type=int, default=16)
    parser.add_argument("--num-l3-banks", type=int, default=8)
    parser.add_argument(
        "--mem-channels", type=int, choices=(1, 2, 4, 8), default=8
    )

    # Ruby Sequencer capacity and controller TBEs are separate resources.
    parser.add_argument("--sequencer-outstanding", type=int, default=16)
    parser.add_argument("--l1-tbes", type=int, default=256)
    parser.add_argument("--l2-tbes", type=int, default=256)
    parser.add_argument("--l3-tbes", type=int, default=256)
    parser.add_argument("--directory-tbes", type=int, default=256)
    parser.add_argument(
        "--functional-trace-subdir",
        default="",
        help=(
            "Enable a functional-only TaoTrace capture under this outdir-relative "
            "directory. Timing labels and cache/coherence oracle streams remain off."
        ),
    )
    return parser.parse_args()


def parse_size_bytes(value):
    text = value.strip()
    units = {
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "B": 1,
    }
    for suffix, multiplier in units.items():
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * multiplier)
    return int(text)


def configure_o3_cores(processor, args):
    """Mutate the switched-out O3 CPUs before SimObject instantiation."""
    for wrapped_core in processor._switchable_cores["switch"]:
        cpu = wrapped_core.get_simobject()
        cpu.fetchWidth = args.fetch_width
        cpu.decodeWidth = args.decode_width
        cpu.renameWidth = args.rename_width
        cpu.dispatchWidth = args.dispatch_width
        cpu.issueWidth = args.issue_width
        cpu.wbWidth = args.wb_width
        cpu.commitWidth = args.commit_width
        cpu.numROBEntries = args.rob_entries
        cpu.numPhysIntRegs = args.phys_int_regs
        cpu.numPhysFloatRegs = args.phys_float_regs
        cpu.numPhysVecRegs = args.phys_vec_regs
        cpu.numPhysCCRegs = args.phys_cc_regs
        if args.branch_predictor == "tage64k":
            cpu.branchPred.conditionalBranchPred = TAGE_SC_L_64KB()
        # gem5 v25 models one or more IQUnit objects as a VectorParam.  The
        # captured baseline has one queue, but update every configured queue
        # so the option remains effective if a future profile partitions it.
        for instruction_queue in cpu.instQueues:
            instruction_queue.numEntries = args.iq_entries
        cpu.LQEntries = args.lq_entries
        cpu.SQEntries = args.sq_entries
        cpu.mmu.dtb.size = args.dtlb_entries


class StatsOnlyMESIThreeLevel(MESIThreeLevelCacheHierarchy):
    """MESI hierarchy with explicit Sequencer/TBE settings and SE backing."""

    def __init__(self, *, sweep_args, **kwargs):
        self._sweep_args = sweep_args
        super().__init__(**kwargs)

    def incorporate_cache(self, board):
        super().incorporate_cache(board)
        args = self._sweep_args

        self.ruby_system.access_backing_store = True
        mem_ranges = board.get_mem_ranges()
        self.ruby_system.phys_mem = SimpleMemory(
            range=mem_ranges[0], in_addr_map=False
        )

        for controller in self._l1_controllers:
            controller.number_of_TBEs = args.l1_tbes
            controller.sequencer.max_outstanding_requests = (
                args.sequencer_outstanding
            )
            # Preserve the source-aligned workaround used by the v28 corpus.
            controller.bufferToL1.ordered = False
            controller.bufferFromL1.ordered = False
        for controller in self._l2_controllers:
            controller.number_of_TBEs = args.l2_tbes
        for controller in self._l3_controllers:
            controller.number_of_TBEs = args.l3_tbes
        for controller in self._directory_controllers:
            controller.number_of_TBEs = args.directory_tbes


def write_requested_profile(args):
    profile = {
        "schema": "fastsim-gem5-uarch-request-v1",
        "trace_enabled": bool(args.functional_trace_subdir),
        "trace_mode": (
            "functional-only" if args.functional_trace_subdir else "disabled"
        ),
        "core": {
            "num_cores": args.num_cores,
            "clock": args.clk,
            "fetch_width": args.fetch_width,
            "decode_width": args.decode_width,
            "rename_width": args.rename_width,
            "dispatch_width": args.dispatch_width,
            "issue_width": args.issue_width,
            "wb_width": args.wb_width,
            "commit_width": args.commit_width,
            "rob_entries": args.rob_entries,
            "iq_entries": args.iq_entries,
            "lq_entries": args.lq_entries,
            "sq_entries": args.sq_entries,
            "dtlb_entries": args.dtlb_entries,
        },
        "cache": {
            "l1i": {"size": args.l1i_size, "assoc": args.l1i_assoc},
            "l1d": {"size": args.l1d_size, "assoc": args.l1d_assoc},
            "l2": {"size": args.l2_size, "assoc": args.l2_assoc},
            "llc": {
                "per_bank_size": args.l3_size,
                "total_size_bytes": parse_size_bytes(args.l3_size)
                * args.num_l3_banks,
                "assoc": args.l3_assoc,
                "banks": args.num_l3_banks,
            },
        },
        "ruby": {
            "sequencer_outstanding": args.sequencer_outstanding,
            "l1_tbes": args.l1_tbes,
            "l2_tbes": args.l2_tbes,
            "l3_tbes": args.l3_tbes,
            "directory_tbes": args.directory_tbes,
        },
        "dram": {
            "type": "DDR4_2400_8x8",
            "channels": args.mem_channels,
            "size": args.mem_size,
        },
    }
    path = os.path.join(m5.options.outdir, "requested_uarch.json")
    os.makedirs(m5.options.outdir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as output:
        json.dump(profile, output, indent=2, sort_keys=True)


def write_tao_uarch_profile(args):
    """Write the schema-v2 profile required by the existing TaoTrace probe."""
    profile = {
        "schema_version": 2,
        "source": "FastSim run_uarch_stats_se.py functional-only capture",
        "core": {
            "isa": "X86",
            "num_cores": args.num_cores,
            "freq_ghz": float(args.clk.removesuffix("GHz")),
        },
        "cache": {
            "l1d": {
                "size_b": parse_size_bytes(args.l1d_size),
                "assoc": args.l1d_assoc,
                "line_b": 64,
                "num_banks": 1,
                "bank_select_low_bit": 6,
                "policy": "lru",
            },
            "l1i": {
                "size_b": parse_size_bytes(args.l1i_size),
                "assoc": args.l1i_assoc,
                "line_b": 64,
                "num_banks": 1,
                "bank_select_low_bit": 6,
                "policy": "lru",
            },
            "l2": {
                "size_b": parse_size_bytes(args.l2_size),
                "assoc": args.l2_assoc,
                "line_b": 64,
                "num_banks": 1,
                "bank_select_low_bit": 6,
                "policy": "lru",
            },
            "l3": {
                "size_b": parse_size_bytes(args.l3_size) * args.num_l3_banks,
                "assoc": args.l3_assoc,
                "line_b": 64,
                "num_banks": args.num_l3_banks,
                "bank_select_low_bit": 6,
                "policy": "lru",
            },
        },
        "tlb": {
            "dtlb": {"entries": args.dtlb_entries, "assoc": args.dtlb_entries},
            "itlb": {"entries": 64, "assoc": 64},
            "stlb": None,
        },
        "page_walker": {
            "levels": 4,
            "page_size_bits": 12,
            "walk_attaches_to": "sequencer",
            "pwc_entries": 0,
        },
        "mshr": {
            "l1d_entries": args.sequencer_outstanding,
            "l2_entries": args.l2_tbes,
            "l3_entries": args.l3_tbes,
        },
        "coherence": {"protocol": "MESI_Three_Level"},
        "dram": {
            "model": f"{args.mem_channels}ChannelDDR4_2400",
            "size_b": parse_size_bytes(args.mem_size),
            "num_channels": args.mem_channels,
            "banks_per_channel": 16,
            "row_size_b": 8192,
            "burst_b": 64,
            "interleaving_size_b": 64,
            "queue_window": 256,
        },
    }
    path = os.path.join(m5.options.outdir, "uarch_profile.json")
    with open(path, "w", encoding="utf-8") as output:
        json.dump(profile, output, indent=2, sort_keys=True)
    return path


def main():
    args = parse_args()
    if args.num_l3_banks <= 0 or (
        args.num_l3_banks & (args.num_l3_banks - 1)
    ):
        raise ValueError("--num-l3-banks must be a power of two")

    processor = SimpleSwitchableProcessor(
        starting_core_type=CPUTypes.ATOMIC,
        switch_core_type=CPUTypes.O3,
        num_cores=args.num_cores,
        isa=ISA.X86,
    )
    configure_o3_cores(processor, args)

    cache_hierarchy = StatsOnlyMESIThreeLevel(
        sweep_args=args,
        l1i_size=args.l1i_size,
        l1i_assoc=args.l1i_assoc,
        l1d_size=args.l1d_size,
        l1d_assoc=args.l1d_assoc,
        l2_size=args.l2_size,
        l2_assoc=args.l2_assoc,
        l3_size=args.l3_size,
        l3_assoc=args.l3_assoc,
        num_l3_banks=args.num_l3_banks,
    )
    board = SimpleBoard(
        clk_freq=args.clk,
        processor=processor,
        memory=ChanneledMemory(
            dram_interface_class=DDR4_2400_8x8,
            num_channels=args.mem_channels,
            interleaving_size=64,
            size=args.mem_size,
        ),
        cache_hierarchy=cache_hierarchy,
    )
    board.set_se_binary_workload(
        binary=BinaryResource(local_path=args.cmd),
        arguments=args.workload_args,
    )
    write_requested_profile(args)

    if args.functional_trace_subdir:
        trace_dir = os.path.join(m5.options.outdir, args.functional_trace_subdir)
        profile_path = write_tao_uarch_profile(args)
        for wrapped_core in processor._switchable_cores["switch"]:
            cpu = wrapped_core.get_simobject()
            cpu.tao_trace = TaoTrace(
                manager=cpu,
                output_dir=trace_dir,
                uarch_profile_path=profile_path,
                emit_micro=True,
                emit_micro_labels=False,
                emit_macro=False,
                emit_mem_events=False,
                require_roi=True,
            )

    state = {"switched": False, "workends": 0}

    def switch_at_first_workbegin():
        if not state["switched"]:
            print("[fastsim-uarch] first WORKBEGIN: Atomic -> O3+Ruby")
            simulator.switch_processor()
            # Switched O3 counters start at zero, but Ruby/DRAM objects were
            # already alive during Atomic initialization. Reset every global
            # statistic at the exact ROI boundary while retaining warm state.
            m5.stats.reset()
            state["switched"] = True
        return False

    def finish_at_last_workend():
        state["workends"] += 1
        done = state["workends"] >= args.num_cores
        print(
            "[fastsim-uarch] WORKEND "
            f"{state['workends']}/{args.num_cores} "
            f"{'finish' if done else 'continue'}"
        )
        return done

    simulator = Simulator(
        board=board,
        on_exit_event={
            ExitEvent.WORKBEGIN: switch_at_first_workbegin,
            ExitEvent.WORKEND: finish_at_last_workend,
        },
    )
    print(
        "[fastsim-uarch] "
        f"mode={'functional-trace+stats' if args.functional_trace_subdir else 'stats-only'} "
        f"cmd={args.cmd} cores={args.num_cores} "
        f"width={args.fetch_width}/{args.issue_width}/{args.commit_width} "
        f"ROB={args.rob_entries} IQ={args.iq_entries} "
        f"L3={args.num_l3_banks}x{args.l3_size} "
        f"DRAM={args.mem_channels}ch"
    )
    simulator.run()


if __name__ == "__m5_main__":
    main()
