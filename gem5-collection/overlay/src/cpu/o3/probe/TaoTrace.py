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
    # V9.6: ROI 闸门。True 时启用 always-update + ROI-only-emit：probe 内部
    #   状态机（line_states_ / LRU / TLB / walker / MSHR / branch_history /
    #   last_writer / MacroAccum）始终更新；emit*/write* 路径仅在
    #   m5_work_begin..m5_work_end 区间内放行。从而 ROI 第一条 µop 看到的
    #   微架构与 probe 视图均已预热，无冷启动；启动期 / 同步段 / scheduler
    #   µop 不进入 records.micro / labels.micro。False（默认）退化为 V9.5
    #   全程 emit 行为，向后兼容现有 µbench。
    require_roi = Param.Bool(False,
        "Enable always-update + ROI-only-emit gate. probe state machines "
        "are always updated; emit paths are gated by m5_work_begin/end. "
        "False = legacy V9.5 behavior (always emit).")
    measure_cpl = Param.Bool(False,
        "Measure mutually exclusive user, syscall, page-fault, IRQ, idle, "
        "scheduler, and unknown CPL0 cycles plus dual-scope PMU counters "
        "into oracle outputs without changing functional trace records.")
    emit_native_response_jsonl = Param.Bool(False,
        "Emit the full per-memory-UOP native Ruby response JSONL. Disabled "
        "by default because native PMU populations are aggregated online "
        "into one bounded summary JSON per core.")
    native_response_anomaly_limit = Param.Unsigned(32,
        "Maximum native Ruby lifecycle/hierarchy anomaly examples retained "
        "per core in the online summary. Counts remain exact after this "
        "bounded diagnostic sample is full.")
    emit_wrong_path_oracle = Param.Bool(False,
        "Emit oracle/wrong_path.jsonl with measured-window squash episodes "
        "and per-dynamic-instruction pipeline-stage observations. This is "
        "an offline attribution oracle and is never part of FST input.")
    functional_user_only = Param.Bool(False,
        "Emit only CPL3 functional records and replace each FS syscall "
        "macroop with one op_class=-1 marker carrying sysnum in address.")
    functional_include_kernel = Param.Bool(False,
        "Emit both CPL3 and CPL0 functional records. CPL0 OpClass N is "
        "encoded in FST v7 as -(N+2); syscall macroops are still replaced "
        "by one user-scoped op_class=-1 marker.")
    functional_warmup = Param.Bool(False,
        "Capture CPL3 records before traceMeasurementBegin as a functional "
        "warmup prefix. The functional user target then counts only records "
        "after the measurement boundary.")
    functional_user_target = Param.Unsigned(0,
        "When non-zero, the first attached TaoTrace core to retire this many "
        "CPL3 architectural macro instructions closes every participant at "
        "one common gem5 tick. Requires exactly one functional trace mode "
        "and emit_micro.")
    syscall_arg_counts = Param.String("",
        "Comma-separated Linux x86-64 syscall argument-count map, for "
        "example '0:3,1:3,9:6,202:6'. Only listed syscalls expose raw ABI "
        "arguments in FST v7, matching drmemtrace -record_syscall validity "
        "semantics. Empty preserves number-only capture.")
    # 记录流物理格式开关（只影响 records.micro 这一路指令流）：
    #   "jsonl" = 逐条 JSON 文本（默认，向后兼容；配合 tools/fst-convert 转 FST）
    #   "fst"   = 直接写 FastSim FST schema-7 二进制；64B hot records 后附
    #             每 syscall 一个 128B sparse metadata row，
    #             省去 ~10x 文本中间文件和离线转换 pass。fst 模式下不再输出
    #             labels.micro（下游 FST 交付物不含 per-micro tick），进一步省盘。
    #   mem_events / macro 等其它流不受本开关影响，仍由各自开关控制。
    trace_format = Param.String("jsonl",
        "Physical format for the records.micro instruction stream: "
        "'jsonl' (per-line JSON, default) or 'fst' (in-probe FastSim FST "
        "schema-7 binary; skips labels.micro). Other streams unaffected.")
