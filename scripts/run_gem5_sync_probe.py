#!/usr/bin/env python3
"""gem5 FS functional-sync diagnostic runner for a marked OpenMP
workload. This is an output/coverage diagnostic, not a CPI/PMU certification.
The supplied static binary drives ROI boundaries through its own markers.

Four-phase flow (per patches/tcsim-zstd-prefork-checkpoint.patch workflow and
the gem5 producer source):

  1. KVM fast boot + fast-forward of all workload initialisation.
  2. The workload reaches its checkpoint marker (after application and chunk
     initialisation). gem5_roi_checkpoint_once writes the address-mode
     checkpoint m5op (0x43) to the m5ops MMIO page; under KVM this traps as a
     KVM_EXIT_MMIO, is decoded by pseudo_inst::m5checkpoint, and raises
     exitSimLoop("checkpoint") -> ExitEvent.CHECKPOINT. The runner switches
     KVM -> O3 + Ruby MESI_Three_Level here. TaoTrace (functional_warmup=True)
     then records a CPL3+CPL0 warmup prefix with no cold start.
  3. The workload runs its bounded warmup under O3, then reaches the workbegin
     marker. gem5_roi_begin_once prints the serial line
     '[gem5-roi] marker=... operation=workbegin-serial ...'; the gem5 serial
     terminal recognises it and calls TaoTrace::traceMeasurementBegin(), which
     opens the measured functional window, and raises exitSimLoop("workbegin")
     -> ExitEvent.WORKBEGIN. The runner resets stats and continues.
  4. The fastest core reaching functional_user_target measured user UOPs closes
     collection on every participant (first-core-common-end) and drains, exiting
     via ExitEvent.MAX_INSTS. The runner finishes.

The instrumented binary opens /dev/gem5_bridge when present, else maps the
fixed m5ops physical page (0xFFFF0000) through /dev/mem, which the stock gem5
x86-ubuntu image exposes; no gem5_bridge kernel module is required.
"""

import argparse
import base64
import json
import os
import hashlib
import struct
import subprocess
from pathlib import Path

import m5
from m5.objects import TaoTrace

from gem5.components.boards.x86_board import X86Board
from gem5.components.cachehierarchies.ruby.mesi_three_level_cache_hierarchy import (
    MESIThreeLevelCacheHierarchy,
)
from gem5.components.memory.memory import ChanneledMemory
from gem5.components.memory.dram_interfaces.ddr4 import DDR4_2400_8x8
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_switchable_processor import (
    SimpleSwitchableProcessor,
)
from gem5.isas import ISA
from gem5.resources.resource import obtain_resource
from gem5.simulate.exit_event import ExitEvent
from gem5.simulate.simulator import Simulator


def parse_args():
    p = argparse.ArgumentParser(description="FastSim gem5 FS OpenMP synchronization diagnostic")
    p.add_argument("--workload-binary", required=True,
                   help="instrumented *_base.gem5-x86-linux binary")
    p.add_argument("--workload-args", default="",
                   help="argv passed to the workload (shell-quoted)")
    p.add_argument("--input-file", action="append", default=[],
                   help="host path of an input file to stage next to the "
                        "workload in the guest cwd (repeatable)")
    p.add_argument("--omp-threads", type=int, required=True,
                   help="OMP_NUM_THREADS for the workload")
    p.add_argument("--extra-cores", type=int, default=0,
                   help="spare, untraced OS cores beyond worker cores; "
                        "num_cores = omp_threads + extra_cores. Reference used 0.")
    p.add_argument("--functional-user-target", type=int, default=10000000,
                   help="measured user UOPs; fastest core stops collection here")
    p.add_argument("--kernel-resource", default="x86-linux-kernel-5.4.49")
    p.add_argument("--disk-resource", default="x86-ubuntu-18.04-img")
    p.add_argument("--clk", default="3GHz")
    p.add_argument("--mem-size", default="3GiB")
    p.add_argument("--mem-channels", type=int, default=2)
    p.add_argument("--l1i-size", default="32KiB")
    p.add_argument("--l1d-size", default="32KiB")
    p.add_argument("--l2-size", default="1MiB")
    p.add_argument("--l3-size", default="8MiB")
    p.add_argument("--l1i-assoc", type=int, default=8)
    p.add_argument("--l1d-assoc", type=int, default=8)
    p.add_argument("--l2-assoc", type=int, default=8)
    p.add_argument("--l3-assoc", type=int, default=16)
    p.add_argument("--num-l3-banks", type=int, default=8)
    p.add_argument("--rob-entries", type=int, default=192)
    p.add_argument("--iq-entries", type=int, default=64)
    p.add_argument("--lq-entries", type=int, default=32)
    p.add_argument("--sq-entries", type=int, default=32)
    p.add_argument("--dtlb-entries", type=int, default=64)
    p.add_argument("--tso-store", choices=("serial", "buffer"), default="buffer")
    p.add_argument("--tso-store-buffer-size", type=int, default=114)
    p.add_argument("--warmup-steps", type=int, default=None,
                   help="GEM5_ROI_WARMUP_STEPS for the workload (optional)")
    p.add_argument("--omp-proc-bind", default="",
                   help="OMP_PROC_BIND for the workload. Empty (default) omits "
                        "it so the guest scheduler is free to spread threads "
                        "across cores. On gem5's fake-CPU topology, "
                        "'close'+PLACES=cores can collapse every thread onto "
                        "core 0; leave empty unless the topology is trusted.")
    p.add_argument("--omp-places", default="",
                   help="OMP_PLACES for the workload. Empty (default) omits it.")
    p.add_argument("--omp-wait-policy", choices=("", "ACTIVE", "PASSIVE"), default="",
                   help="explicit diagnostic runtime wait policy; empty preserves default")
    p.add_argument("--trace-subdir", default="tao_trace")
    p.add_argument("--trace-format", default="fst", choices=("jsonl", "fst"))
    p.add_argument("--functional-sync", action="store_true", help="enable committed semantic marker sidecar")
    p.add_argument("--sync-initial-context", choices=("empty", "unknown"), default="unknown")
    p.add_argument("--max-ticks", type=int, default=0, help="optional simulator safety limit")
    p.add_argument("--kvm-quantum-us", type=float, default=1000,
                   help="KVM synchronization quantum; lower for very short ROI probes")
    return p.parse_args()


def effective_sq(args):
    return args.tso_store_buffer_size if args.tso_store == "buffer" else args.sq_entries


def parse_size_bytes(value):
    text = value.strip()
    for suffix, mult in (("KiB", 1024), ("MiB", 1024**2), ("GiB", 1024**3), ("B", 1)):
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * mult)
    return int(text)


def write_uarch_profile(args, trace_dir):
    profile = {
        "schema_version": 2,
        "source": "FastSim run_gem5_sync_probe.py full-system diagnostic capture",
        "core": {
            "isa": "X86", "num_cores": args.omp_threads + args.extra_cores,
            "freq_ghz": float(args.clk.removesuffix("GHz")),
            "tso_store": args.tso_store,
            "tso_store_buffer_size": args.tso_store_buffer_size,
            "tso_store_buffer_active": args.tso_store == "buffer",
            "sq_entries": effective_sq(args),
        },
        "cache": {
            "l1d": {"size_b": parse_size_bytes(args.l1d_size), "assoc": args.l1d_assoc,
                    "line_b": 64, "num_banks": 1, "bank_select_low_bit": 6, "policy": "lru"},
            "l1i": {"size_b": parse_size_bytes(args.l1i_size), "assoc": args.l1i_assoc,
                    "line_b": 64, "num_banks": 1, "bank_select_low_bit": 6, "policy": "lru"},
            "l2": {"size_b": parse_size_bytes(args.l2_size), "assoc": args.l2_assoc,
                   "line_b": 64, "num_banks": 1, "bank_select_low_bit": 6, "policy": "lru"},
            "l3": {"size_b": parse_size_bytes(args.l3_size) * args.num_l3_banks,
                   "assoc": args.l3_assoc, "line_b": 64, "num_banks": args.num_l3_banks,
                   "bank_select_low_bit": 6, "policy": "lru"},
        },
        "tlb": {"dtlb": {"entries": args.dtlb_entries, "assoc": args.dtlb_entries},
                "itlb": {"entries": 64, "assoc": 64}, "stlb": None},
        "page_walker": {"levels": 4, "page_size_bits": 12,
                        "walk_attaches_to": "sequencer", "pwc_entries": 0},
        "mshr": {"l1d_entries": 16, "l2_entries": 256, "l3_entries": 256},
        "coherence": {"protocol": "MESI_Three_Level"},
        "dram": {"model": f"{args.mem_channels}ChannelDDR4_2400",
                 "size_b": parse_size_bytes(args.mem_size),
                 "num_channels": args.mem_channels, "banks_per_channel": 16,
                 "row_size_b": 8192, "burst_b": 64, "interleaving_size_b": 64,
                 "queue_window": 256},
    }
    path = os.path.join(os.path.dirname(trace_dir), "uarch_profile.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(profile, handle, indent=2, sort_keys=True)
    return path


def _b64_heredoc(host_path, guest_path):
    with open(host_path, "rb") as handle:
        payload = base64.b64encode(handle.read()).decode("ascii")
    chunks = "\n".join(payload[i:i + 76] for i in range(0, len(payload), 76))
    tag = "EOF_" + os.path.basename(guest_path).replace(".", "_").replace("-", "_")
    return (f"cat > /tmp/stage.b64 <<'{tag}'\n{chunks}\n{tag}\n"
            f"base64 -d /tmp/stage.b64 > {guest_path}\n"
            f"rm -f /tmp/stage.b64\n")


def boot_script(args):
    """Guest after-boot script run by m5 readfile.

    Decodes the instrumented binary and inputs under KVM, then execs the binary.
    The binary itself drives all ROI boundaries: its checkpoint marker switches
    KVM->O3, its workbegin serial marker opens the measured window. No raw m5
    pseudo-instructions and no artificial markers are used here.
    """
    lines = ["#!/bin/sh", "set -e", "mkdir -p /tmp/run", "cd /tmp/run"]
    lines.append(_b64_heredoc(args.workload_binary, "/tmp/run/workload"))
    lines.append("chmod +x /tmp/run/workload")
    for host_path in args.input_file:
        guest = "/tmp/run/" + os.path.basename(host_path)
        lines.append(_b64_heredoc(host_path, guest))
    env = [f"OMP_NUM_THREADS={args.omp_threads}",
           f"OMP_THREAD_LIMIT={args.omp_threads}"]
    if args.omp_proc_bind:
        env.append(f"OMP_PROC_BIND={args.omp_proc_bind}")
    if args.omp_places:
        env.append(f"OMP_PLACES={args.omp_places}")
    if args.omp_wait_policy:
        env.append(f"OMP_WAIT_POLICY={args.omp_wait_policy}")
    if args.warmup_steps is not None:
        env.append(f"GEM5_ROI_WARMUP_STEPS={args.warmup_steps}")
    env_str = " ".join(env)
    # stderr must reach the gem5 UART so the workbegin-serial marker is seen by
    # terminal.cc; do NOT redirect the workload's stderr to a file.
    lines.append('echo "[fastsim-fs] launching instrumented workload under KVM"')
    lines.append(f"{env_str} /tmp/run/workload {args.workload_args}")
    lines.append('echo "[fastsim-fs] workload returned $?"')
    lines.append("m5 exit")
    return "\n".join(lines) + "\n"


def configure_o3(processor, args):
    for wrapped in processor._switchable_cores["switch"]:
        cpu = wrapped.get_simobject()
        cpu.fetchWidth = cpu.decodeWidth = cpu.renameWidth = 8
        cpu.dispatchWidth = cpu.issueWidth = cpu.wbWidth = cpu.commitWidth = 8
        cpu.numROBEntries = args.rob_entries
        for iq in cpu.instQueues:
            iq.numEntries = args.iq_entries
        cpu.LQEntries = args.lq_entries
        if hasattr(cpu, "tsoStore"):
            cpu.tsoStore = args.tso_store
            cpu.tsoStoreBufferSize = args.tso_store_buffer_size
        elif args.tso_store == "buffer":
            raise ValueError("gem5 binary lacks BaseO3CPU.tsoStore")
        if args.tso_store == "buffer":
            # buffer TSO store mode requires needsTSO=true and SQEntries ==
            # tsoStoreBufferSize (lsq_unit.cc fatal_if).
            cpu.needsTSO = True
        cpu.SQEntries = effective_sq(args)
        cpu.mmu.dtb.size = args.dtlb_entries


def sync_binary_identity(filename):
    path = Path(filename).resolve()
    data = path.read_bytes()
    if data[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<HH", data, 16) != (2, 62):
        raise ValueError("sync pilot requires a static non-PIE ELF64 x86-64 executable")
    phoff = struct.unpack_from("<Q", data, 32)[0]
    phsize, phcount = struct.unpack_from("<HH", data, 54)
    if any(struct.unpack_from("<I", data, phoff + i * phsize)[0] in (2, 3) for i in range(phcount)):
        raise ValueError("sync pilot binary must have no dynamic loader or dynamic segment")
    symbols = subprocess.run(["nm", "--defined-only", str(path)], check=True,
                             capture_output=True, text=True).stdout.splitlines()
    matches = [row.split() for row in symbols if row.split()[-1:] == ["fastsim_sync_marker"]]
    if len(matches) != 1 or matches[0][1] not in ("W", "T"):
        raise ValueError("binary has no unique functional sync marker symbol")
    return dict(binary=str(path), binary_sha256=hashlib.sha256(data).hexdigest(),
                marker_pc=int(matches[0][0], 16), marker_abi="fastsim-sync-marker-v1")


def main():
    args = parse_args()
    binary_path = Path(args.workload_binary).resolve()
    identity = dict(binary=str(binary_path),
                    binary_sha256=hashlib.sha256(binary_path.read_bytes()).hexdigest())
    if args.functional_sync:
        identity.update(sync_binary_identity(args.workload_binary))
    num_cores = args.omp_threads + args.extra_cores

    if args.kvm_quantum_us <= 0:
        raise ValueError("KVM quantum must be positive")

    class ProbeProcessor(SimpleSwitchableProcessor):
        def _pre_instantiate(self, root):
            super()._pre_instantiate(root)
            root.sim_quantum = m5.ticks.fromSeconds(args.kvm_quantum_us * 1e-6)

    processor = ProbeProcessor(
        starting_core_type=CPUTypes.KVM, switch_core_type=CPUTypes.O3,
        num_cores=num_cores, isa=ISA.X86,
    )
    configure_o3(processor, args)

    cache = MESIThreeLevelCacheHierarchy(
        l1i_size=args.l1i_size, l1i_assoc=args.l1i_assoc,
        l1d_size=args.l1d_size, l1d_assoc=args.l1d_assoc,
        l2_size=args.l2_size, l2_assoc=args.l2_assoc,
        l3_size=args.l3_size, l3_assoc=args.l3_assoc,
        num_l3_banks=args.num_l3_banks,
    )
    memory = ChanneledMemory(
        dram_interface_class=DDR4_2400_8x8, num_channels=args.mem_channels,
        interleaving_size=64, size=args.mem_size,
    )
    board = X86Board(clk_freq=args.clk, processor=processor,
                     memory=memory, cache_hierarchy=cache)

    kernel = obtain_resource(args.kernel_resource)
    disk = obtain_resource(args.disk_resource)
    board.set_kernel_disk_workload(
        kernel=kernel, disk_image=disk,
        readfile_contents=boot_script(args), exit_on_work_items=True,
    )

    trace_dir = os.path.join(m5.options.outdir, args.trace_subdir)
    os.makedirs(trace_dir, exist_ok=True)
    profile_path = write_uarch_profile(args, trace_dir)

    # Attach TaoTrace to the declared participant cores. Any participant
    # reaching the threshold closes every stream at the common end. functional_warmup gates the measured window to the serial workbegin
    # marker; functional_user_target closes collection at the common end.
    traced = processor._switchable_cores["switch"][: args.omp_threads]
    for wrapped in traced:
        cpu = wrapped.get_simobject()
        cpu.tao_trace = TaoTrace(
            manager=cpu, output_dir=trace_dir, uarch_profile_path=profile_path,
            emit_micro=True, emit_macro=False, emit_mem_events=False,
            # functional_warmup + process-wide traceMeasurementBegin already
            # define capture/reset. Legacy require_roi gates each CPU's own
            # WORKBEGIN and would silently drop OpenMP workers on other CPUs.
            require_roi=False, measure_cpl=True,
            functional_include_kernel=True, functional_warmup=True,
            functional_user_target=args.functional_user_target,
            trace_format=args.trace_format,
            functional_sync_marker_pc=identity.get("marker_pc", 0),
            functional_sync_initial_context=args.sync_initial_context,
        )

    state = {"switched": False}

    def on_checkpoint():
        # Workload checkpoint marker: switch KVM -> O3 + Ruby so the warmup and
        # measured region run on the detailed core.
        if not state["switched"]:
            print("[fastsim-fs] CHECKPOINT marker: switch KVM -> O3+Ruby")
            simulator.switch_processor()
            state["switched"] = True
        else:
            print("[fastsim-fs] extra CHECKPOINT ignored")
        return False

    def on_workbegin():
        # Serial workbegin marker already fired traceMeasurementBegin() in the
        # producer; reset host stats and continue the measured region.
        print("[fastsim-fs] WORKBEGIN serial marker: measurement window open")
        m5.stats.reset()
        return False

    def on_max_insts():
        print("[fastsim-fs] functional_user_target reached: common-end, finish")
        return True

    def on_exit():
        # Reaching the trailing `m5 exit` means the workload finished before the
        # target: short diagnostic only, not a completed common-end formal case.
        print("[fastsim-fs] guest exit before target: finish")
        return True

    simulator = Simulator(
        board=board,
        on_exit_event={
            ExitEvent.CHECKPOINT: on_checkpoint,
            ExitEvent.WORKBEGIN: on_workbegin,
            ExitEvent.MAX_INSTS: on_max_insts,
            ExitEvent.EXIT: on_exit,
        },
    )
    print(f"[fastsim-fs] cores={num_cores} traced={len(traced)} omp={args.omp_threads} "
          f"extra={args.extra_cores} target={args.functional_user_target} "
          f"TSO={args.tso_store} SQ={effective_sq(args)} "
          f"L3={args.num_l3_banks}x{args.l3_size} fmt={args.trace_format}")
    Path(m5.options.outdir, "sync-run-request.json").write_text(json.dumps(
        dict(schema="fastsim-sync-pilot-request-v1", diagnostic=True,
             arguments=vars(args), binary_identity=identity), indent=2) + "\n")
    simulator.run(max_ticks=args.max_ticks or None)
    Path(m5.options.outdir, "sync-run-result.json").write_text(json.dumps(
        dict(cause=simulator.get_last_exit_event_cause(),
             exit_code=simulator.get_last_exit_event_code(),
             tick=simulator.get_current_tick()), indent=2) + "\n")
    print("[fastsim-fs] simulation complete")


if __name__ == "__m5_main__":
    main()
