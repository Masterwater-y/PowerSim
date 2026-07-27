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
from gem5.components.memory.dram_interfaces.ddr4 import DDR4_2400_8x8
from gem5.components.memory.memory import ChanneledMemory
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.components.processors.simple_switchable_processor import (
    SimpleSwitchableProcessor,
)
from gem5.isas import ISA
from gem5.resources.resource import BinaryResource
from gem5.simulate.exit_event import ExitEvent
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
    parser.add_argument("--l2-size", default="1MiB")
    parser.add_argument(
        "--l3-size",
        default="8MiB",
        help="per-bank shared L3 capacity (total = size * --num-l3-banks)",
    )
    parser.add_argument("--l1i-assoc", type=int, default=8)
    parser.add_argument("--l1d-assoc", type=int, default=8)
    parser.add_argument("--l2-assoc", type=int, default=8)
    parser.add_argument("--l3-assoc", type=int, default=16)
    parser.add_argument(
        "--num-l3-banks",
        type=int,
        default=8,
        help="MESI_Three_Level 的共享 L3 bank 数",
    )
    parser.add_argument(
        "--mem-channels",
        type=int,
        choices=(1, 2, 4, 8),
        default=8,
        help="DDR4-2400 channels; must be a power of two",
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
    parser.add_argument(
        "--ff-atomic", action="store_true",
        help="Fast-forward init phase with AtomicSimpleCPU + atomic_noncaching, "
             "switch to O3+Ruby on first m5_work_begin. Requires --require-roi "
             "to be meaningful (TaoTrace probes are attached to the O3 cores).",
    )
    parser.add_argument(
        "--no-tao-trace", action="store_true",
        help="Do not attach TaoTrace probes. Intended for fast stats-only "
             "CPI pilots; raw dataset collection must leave this disabled.",
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

    L3 是 banked：gem5 的 args.l3_size 是每个 bank 的容量，profile 中
    size_b 记录所有 bank 的总容量。每核私有 L1D/L1I/L2，num_banks=1。
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
        # V10.3 C 字段：DRAM 简化派生用配置（packer 离线读取）。
        # 每个 DDR4-2400 x64 channel 有 16 banks、8KiB row、64B burst；
        # channels 必须与下面实际创建的 ChanneledMemory 完全一致。
        #   args.mem_size 经 _parse_size_b 统一字节口径。所有派生（bank_id /
        #   bank_freq_W256 / row_freq_W256）均由 pack_to_parquet 离线计算，
        #   ref_sim / gem5 oracle 不需要任何 DRAM 状态机；推理与训练同源。
        "dram": {
            "model": f"{int(args.mem_channels)}ChannelDDR4_2400",
            "size_b": _parse_size_b(args.mem_size),
            "num_channels": int(args.mem_channels),
            "banks_per_channel": 16,
            "row_size_b": 8192,
            "burst_b": 64,
            "interleaving_size_b": 64,
            "queue_window": 256,
        },
    }
    with open(profile_path, "w") as f:
        json.dump(profile, f, indent=2)
    print(f"[run_mt_mvp] uarch_profile written: {profile_path}")
    return profile_path


def main():
    args = parse_args()

    if args.ff_atomic:
        # Fast-forward: AtomicSimpleCPU 跑 init/启动；首次 m5_work_begin 时切到
        # O3+Ruby。stdlib 在 starting_core=ATOMIC + Ruby 时强制 mem_mode=
        # atomic_noncaching，Atomic 阶段直接走 backing store，不污染 Ruby 状态。
        # 进 ROI 后 Ruby cache 是 cold，但 hot loop 跑 thousands of rounds，
        # 前 1-2 轮就 warm，steady-state PMU 几乎不变。
        processor = SimpleSwitchableProcessor(
            starting_core_type=CPUTypes.ATOMIC,
            switch_core_type=CPUTypes.O3,
            num_cores=args.num_cores,
            isa=ISA.X86,
        )
    else:
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

    trace_dir = os.path.join(m5.options.outdir, args.trace_subdir)
    # V9.5: 仿真前生成 uarch_profile.json，TaoTrace 与 ref_sim 共用同一份
    #   (schema v2，禁止 hardcode；不支持的微架构 fail-fast)。
    profile_path = write_uarch_profile(args, m5.options.outdir)
    # fast-forward 模式下 TaoTrace 必须挂在 O3 cores (switched-out group "switch")
    # 上；processor.get_cores() 此时返回 Atomic 启动组，Atomic CPU 上挂 TaoTrace
    # 没用（probe 是 O3 专属）。pre-switch 这些 O3 SimObject 已经存在于 SimObject
    # 树中，挂载 probe 不需要它们 active。
    if args.ff_atomic:
        tao_target_cores = processor._switchable_cores["switch"]
    else:
        tao_target_cores = processor.get_cores()
    if not args.no_tao_trace:
        for i, core in enumerate(tao_target_cores):
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
            # 关闭 L0<->L1 (stdlib 语义为 L1<->L2) MessageBuffer 的 strict-FIFO。
            # ordered=True 在 8 核高争用 workload (ads_ctr) 下，会因 L0
            # recycle()→delayHead() 路径出现 arrival_time<last_arrival_time
            # 的 1 cycle 越位，触发 MessageBuffer.cc:261 panic。L0 协议用
            # block_on="addr" 已保证同地址串行化，跨地址消息不需要全局
            # arrival 单调；放宽后 dequeue 仍按 arrival_time 最小堆顺序，
            # 不影响协议正确性。stdlib 的 l1_cache.py 把这两个 buffer 写死
            # ordered=True 且会被 embed 进 gem5.opt，故在配置脚本里运行时 patch。
            for l1 in self._l1_controllers:
                l1.bufferToL1.ordered = False
                l1.bufferFromL1.ordered = False

    cache_hierarchy.__class__ = _MESIThreeLevelWithBacking

    # 验证路径要求 trace / stats 走同一口径：
    # - TaoTrace 默认全程 emit（除非显式 --require-roi）
    # - gem5 stdlib 默认把 WORKBEGIN 解释成 reset stats、WORKEND 解释成 dump
    #   stats，会导致 stats 只覆盖 ROI、trace 覆盖全程，instruction 口径不一致。
    # 这里显式把 WORKBEGIN/WORKEND 改成 no-op + continue，让 stats 保持全程累计。
    # fast-forward 模式：第一次 WORKBEGIN 时 switch Atomic→O3，后续 WORKBEGIN
    # （多个 worker 各发一次）走 no-op。require-roi 模式下前 N-1 个 WORKEND
    # 继续仿真，让完成核执行 m5_quiesce；第 N 个 WORKEND 直接结束仿真，
    # 因而不会进入 pthread_join/futex/cleanup 尾部。
    ff_state = {"switched": False, "workends": 0}

    def _ignore_work_marker():
        return False

    def _maybe_switch_then_continue():
        if args.ff_atomic and not ff_state["switched"]:
            print("[run_mt_mvp] first WORKBEGIN -> switch Atomic -> O3+Ruby")
            simulator.switch_processor()
            ff_state["switched"] = True
        return False

    def _finish_on_last_workend():
        if not args.require_roi:
            return False
        ff_state["workends"] += 1
        done = ff_state["workends"] >= args.num_cores
        print(
            f"[run_mt_mvp] WORKEND {ff_state['workends']}/{args.num_cores} "
            f"{'-> finish' if done else '-> continue/quiesce'}"
        )
        return done

    simulator = Simulator(
        board=board,
        on_exit_event={
            ExitEvent.WORKBEGIN: _maybe_switch_then_continue,
            ExitEvent.WORKEND: _finish_on_last_workend,
        },
    )

    print(
        f"[run_mt_mvp] cmd={args.cmd} args={args.workload_args} "
        f"cores={args.num_cores} l2={args.l2_size} "
        f"l3={args.num_l3_banks}x{args.l3_size} "
        f"dram={args.mem_channels}ch-DDR4-2400 "
        f"tao_trace={'off' if args.no_tao_trace else trace_dir} "
        f"access_backing_store=True"
    )
    simulator.run()


if __name__ == "__m5_main__":
    main()
