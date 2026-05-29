#!/usr/bin/env python3

import argparse
import json
import os

import m5
from m5.objects import SimpleMemory, TaoTrace

from gem5.components.boards.simple_board import SimpleBoard
from gem5.components.cachehierarchies.ruby.mesi_three_level_cache_hierarchy import (
    MESIThreeLevelCacheHierarchy,
)
from gem5.components.memory import SingleChannelDDR4_2400
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.isas import ISA
from gem5.resources.resource import BinaryResource
from gem5.simulate.simulator import Simulator


def parse_args():
    parser = argparse.ArgumentParser(
        description="运行 mt_micro，并在每个 O3 核上挂载 TaoTrace。"
    )
    parser.add_argument("--cmd", required=True, help="待运行的静态 x86_64 二进制")
    parser.add_argument(
        "--workload-args",
        nargs="*",
        default=["4", "100000"],
        help="传给 workload 的参数",
    )
    parser.add_argument("--num-cores", type=int, default=4)
    parser.add_argument("--clk", default="3GHz")
    parser.add_argument("--mem-size", default="4GiB")
    parser.add_argument("--l1i-size", default="32KiB")
    parser.add_argument("--l1d-size", default="32KiB")
    parser.add_argument("--l2-size", default="256KiB")
    parser.add_argument("--l3-size", default="2MiB")
    parser.add_argument("--l1i-assoc", type=int, default=8)
    parser.add_argument("--l1d-assoc", type=int, default=8)
    parser.add_argument("--l2-assoc", type=int, default=8)
    parser.add_argument("--l3-assoc", type=int, default=16)
    parser.add_argument(
        "--num-l3-banks",
        type=int,
        default=4,
        help="MESI_Three_Level 的共享 L3 bank 数",
    )
    parser.add_argument(
        "--trace-subdir",
        default="tao_trace",
        help="相对 outdir 的 trace 子目录",
    )
    parser.add_argument(
        "--dtlb-entries", type=int, default=64, help="L1 dTLB entries"
    )
    parser.add_argument(
        "--dtlb-assoc", type=int, default=8, help="L1 dTLB assoc"
    )
    parser.add_argument(
        "--itlb-entries", type=int, default=64, help="L1 iTLB entries"
    )
    parser.add_argument(
        "--itlb-assoc", type=int, default=8, help="L1 iTLB assoc"
    )
    parser.add_argument(
        "--mshr-l1d", type=int, default=16, help="L1D MSHR entries"
    )
    parser.add_argument(
        "--mshr-l2", type=int, default=32, help="L2 MSHR entries"
    )
    parser.add_argument(
        "--mshr-l3", type=int, default=64, help="L3 MSHR entries"
    )
    parser.add_argument(
        "--page-size-bits", type=int, default=12, help="x86 4KiB page = 12"
    )
    parser.add_argument(
        "--walker-levels", type=int, default=4, help="x86_64 4-level"
    )
    # V9.6 ROI 闸门：默认关闭（V9.5 全程 emit 行为）；--require-roi 时启用
    #   always-update + ROI-only-emit。状态机仍在 ROI 外更新，emit 路径短路。
    parser.add_argument(
        "--require-roi", action="store_true",
        help="Enable V9.6 ROI gate: emit only between m5_work_begin/end "
             "(probe state machines remain always-update).",
    )
    return parser.parse_args()


def _parse_size_b(s: str) -> int:
    """支持 32KiB / 256KiB / 2MiB / 4MiB / 1GiB 等 gem5 风格容量字符串。"""
    s = s.strip()
    units = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "B": 1}
    for u, m in units.items():
        if s.endswith(u):
            return int(float(s[: -len(u)]) * m)
    return int(s)


def write_uarch_profile(args, out_dir: str) -> str:
    """生成 schema v2 的 uarch_profile.json 并写入 outdir/uarch_profile.json。

    L3 是 banked：args.l3_size 是总容量，需在 profile 里 size_b=l3_size，
    num_banks=args.num_l3_banks。每核私有 L1D/L1I/L2，num_banks=1。
    """
    os.makedirs(out_dir, exist_ok=True)
    profile_path = os.path.join(out_dir, "uarch_profile.json")
    profile = {
        "schema_version": 2,
        "source": "run_mt_mvp.py(pre-sim)",
        "core": {
            "isa": "X86",
            "num_cores": int(args.num_cores),
            "freq_ghz": float(args.clk.replace("GHz", "")),
        },
        "cache": {
            "l1d": {
                "size_b": _parse_size_b(args.l1d_size),
                "assoc": int(args.l1d_assoc),
                "line_b": 64,
                "num_banks": 1,
                "bank_select_low_bit": 6,
                "policy": "lru",
            },
            "l1i": {
                "size_b": _parse_size_b(args.l1i_size),
                "assoc": int(args.l1i_assoc),
                "line_b": 64,
                "num_banks": 1,
                "bank_select_low_bit": 6,
                "policy": "lru",
            },
            "l2": {
                "size_b": _parse_size_b(args.l2_size),
                "assoc": int(args.l2_assoc),
                "line_b": 64,
                "num_banks": 1,
                "bank_select_low_bit": 6,
                "policy": "lru",
            },
            "l3": {
                # gem5 stdlib MESIThreeLevelCacheHierarchy(l3_size=...)
                # 的 l3_size 语义是【每 bank】容量（ruby
                # l3_controllers0..N-1 各自 size=l3_size），所以总容量需要
                # ×num_l3_banks。schema v2 的 cache.l3.size_b 约定是
                # **总容量**（与 lru_banked.hh::lines_per_bank() 协议一致），
                # 故此处显式相乘，避免 oracle LLC 缩水 N 倍。
                "size_b": _parse_size_b(args.l3_size) * int(args.num_l3_banks),
                "assoc": int(args.l3_assoc),
                "line_b": 64,
                "num_banks": int(args.num_l3_banks),
                "bank_select_low_bit": 6,
                "policy": "lru",
            },
        },
        "tlb": {
            "dtlb": {"entries": int(args.dtlb_entries),
                     "assoc": int(args.dtlb_assoc)},
            "itlb": {"entries": int(args.itlb_entries),
                     "assoc": int(args.itlb_assoc)},
            "stlb": None,
        },
        "page_walker": {
            "levels": int(args.walker_levels),
            "page_size_bits": int(args.page_size_bits),
            "walk_attaches_to": "sequencer",
            "pwc_entries": 0,
        },
        "mshr": {
            "l1d_entries": int(args.mshr_l1d),
            "l2_entries": int(args.mshr_l2),
            "l3_entries": int(args.mshr_l3),
        },
        "coherence": {"protocol": "MESI_Three_Level"},
    }
    with open(profile_path, "w") as f:
        json.dump(profile, f, indent=2)
    print(f"[run_mt_mvp] uarch_profile written: {profile_path}")
    return profile_path


def main():
    args = parse_args()

    processor = SimpleProcessor(
        cpu_type=CPUTypes.O3,
        isa=ISA.X86,
        num_cores=args.num_cores,
    )

    cache_hierarchy = MESIThreeLevelCacheHierarchy(
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
        memory=SingleChannelDDR4_2400(size=args.mem_size),
        cache_hierarchy=cache_hierarchy,
    )

    board.set_se_binary_workload(
        binary=BinaryResource(local_path=args.cmd),
        arguments=args.workload_args,
    )

    trace_dir = os.path.join(m5.options.outdir, args.trace_subdir)
    # V9.5: 仿真前生成 uarch_profile.json，TaoTrace 与 ref_sim 共用同一份
    #   (schema v2，禁止 hardcode；不支持的微架构 fail-fast)。
    profile_path = write_uarch_profile(args, m5.options.outdir)
    for i, core in enumerate(processor.get_cores()):
        core.core.tao_trace = TaoTrace(
            manager=core.core,
            output_dir=trace_dir,
            uarch_profile_path=profile_path,
            require_roi=args.require_roi,
        )

    # 开 RubySystem.access_backing_store：让 directory 持有完整 backing store。
    # SE 模式 startup 阶段（glibc init / set_robust_list 等 syscall）会触发
    # functional read，关闭 backing 时若该地址尚未被 timing 路径放进任何 cache
    # 就会 fatal（RubyPort.cc:463）；开启后 functional read 直接走 backing，
    # 不依赖 cache/timing 状态，根除 fatal。
    # 不影响 timing 路径产生的 stats / mem_events / labels 数据，对 V1-V8 已对齐
    # 的 ref_simulator 完全透明。MESI_Three_Level 默认 False；MESI_Two_Level
    # 默认 True（gem5 stdlib），故此 flag 是 SE+Ruby 多核标准 workaround。
    # 用 subclass 重写 incorporate_cache：父类创建 ruby_system 后立刻设置 flag，
    # 同时挂上 phys_mem (SimpleMemory, in_addr_map=False) 作为 backing store。
    # 这样 Simulator._instantiate → _create_cpp_objects 时能读到 True。
    class _MESIThreeLevelWithBacking(MESIThreeLevelCacheHierarchy):
        def incorporate_cache(self, _board):
            super().incorporate_cache(_board)
            self.ruby_system.access_backing_store = True
            mem_ranges = _board.get_mem_ranges()
            self.ruby_system.phys_mem = SimpleMemory(
                range=mem_ranges[0], in_addr_map=False
            )

    cache_hierarchy.__class__ = _MESIThreeLevelWithBacking

    simulator = Simulator(board=board)

    print(
        f"[run_mt_mvp] cmd={args.cmd} args={args.workload_args} "
        f"cores={args.num_cores} trace_dir={trace_dir} "
        f"access_backing_store=True"
    )
    simulator.run()


if __name__ == "__m5_main__":
    main()
