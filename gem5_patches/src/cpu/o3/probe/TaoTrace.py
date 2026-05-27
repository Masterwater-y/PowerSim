# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause
#
# TaoTrace: per-instruction JSONL probe for the multi-core MVP.
# Drop this file into gem5/src/cpu/o3/probe/ alongside tao_trace.{hh,cc}
# and add the SConscript hooks listed in
# single_core_mvp/doc/02_gem5_probe_adaptation.md.

from m5.objects.Probe import ProbeListenerObject
from m5.params import Param, Bool, String


class TaoTrace(ProbeListenerObject):
    type = "TaoTrace"
    cxx_class = "gem5::o3::TaoTrace"
    cxx_header = "cpu/o3/probe/tao_trace.hh"

    output_dir = Param.String("tao_trace",
        "Output directory for per-CPU JSONL traces "
        "(see doc/01_dataset_io_spec.md).")
    # 微架构配置文件 (schema v2)；空字符串触发自动发现：
    #   1) <output_dir>/../uarch_profile.json
    #   2) <output_dir>/uarch_profile.json
    # 找不到 → fail-fast。所有 cache/TLB/walker/MSHR 容量均来自此文件。
    uarch_profile_path = Param.String("",
        "Path to uarch_profile.json (schema v2). Empty = auto-discover near "
        "output_dir; fail-fast if not found.")
    # V9 micro-grain：默认开启 micro 输出 (records.micro / labels.micro)；
    # macro 输出（records / labels / diag / sched / mem_events）按需打开。
    emit_micro = Param.Bool(True,
        "Emit per-micro records.micro.jsonl + labels.micro.jsonl "
        "(V9 training input aligned with atomic_func_trace).")
    emit_macro = Param.Bool(False,
        "Emit legacy macro-op-grain outputs (records / labels / diag / "
        "sched). Default off to save disk; turn on for V1-V8 "
        "ref_simulator verification or macro-op-grain debugging. "
        "NOTE: mem_events.jsonl 已从此开关拆出，由 emit_mem_events 单独控制。")
    # V9.5: cache 事件流（cacheline 粒度，与 macro-op/micro-op 指令粒度
    #   正交）。oracle ↔ ref_sim 的 17/17 bit-exact 校验、5 张 PMU 表都
    #   依赖该文件，因此默认开启。
    emit_mem_events = Param.Bool(True,
        "Emit cacheline-grain mem_events.jsonl (data + ifetch). Required "
        "by ref_sim replay and 17/17 bit-exact pipeline; orthogonal to "
        "emit_macro / emit_micro instruction-grain switches.")
