#!/usr/bin/env python3
"""gem5 FS runner for marked OpenMP workloads or independent SPEC rate copies.
This is an output/coverage diagnostic, not a CPI/PMU certification.
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
import tarfile
from pathlib import PurePosixPath
import base64
import json
import os
import shlex
import hashlib
import struct
import subprocess
import importlib.util
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))

import m5
from m5.objects import TaoTrace, SimpleMemory

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
    p.add_argument("--input-archive", action="append", default=[],
                   help="tar of regular files/directories to extract in the guest cwd; "
                        "content is bound into staged checkpoint identity")
    p.add_argument("--omp-threads", "--participant-cores", dest="omp_threads", type=int, required=True,
                   help="participant cores; OpenMP thread count only in non-rate mode")
    p.add_argument("--rate-copies", type=int, default=0,
                   help="independent process copies; equals core count unless --native-workers is set")
    p.add_argument("--native-workers", type=int, default=0,
                   help="marked native tasks including active main; tasks may share participant CPUs")
    p.add_argument("--native-omp-num-threads", action="store_true",
                   help="explicit official speed thread-count environment for Graph500/XZ; does not change the runtime")
    p.add_argument("--native-warmup-after-identities", action="store_true",
                   help="arm native instruction-budget warmup only after every task identity is acknowledged")
    p.add_argument("--staging-size", choices=('512m', '1g', '2g'), default='512m',
                   help="guest tmpfs ceiling for official input staging; charged to guest RAM")
    p.add_argument("--prepare-workload-args", default='',
                   help="run this binary once under KVM to prepare official inputs before the staged checkpoint")
    p.add_argument("--rate-launcher", type=Path,
                   help="external marked rate-copy launcher (no OpenMP)")
    p.add_argument("--rate-private-cwd", action="store_true",
                   help="give each rate copy its own output directory and shared input links")
    p.add_argument("--housekeeping-cpu", type=int,
                   help="guest CPU for movable background tasks before starting rate copies")
    p.add_argument("--save-boot-checkpoint", type=Path,
                   help="save staged, pre-fork KVM state at the launcher's first checkpoint")
    p.add_argument("--restore-boot-checkpoint", type=Path,
                   help="restore a matching staged pre-fork KVM checkpoint")
    p.add_argument("--checkpoint-only", action="store_true",
                   help="stop after saving the actual boot checkpoint")
    p.add_argument("--checkpoint-phase", choices=('staged', 'compute-ready'), default='staged',
                   help="save before fork or after independent copies finish initialization")
    p.add_argument("--rate-serial-init", action="store_true",
                   help="initialize independent rate copies one at a time before shared release")
    p.add_argument("--extra-cores", type=int, default=0,
                   help="spare, untraced OS cores beyond worker cores; "
                        "num_cores = omp_threads + extra_cores. Reference used 0.")
    p.add_argument("--functional-user-target", type=int, default=10000000,
                   help="measured user UOPs; fastest core stops collection here")
    p.add_argument("--kernel-resource", default="x86-linux-kernel-5.4.49")
    p.add_argument("--disk-resource", default="x86-ubuntu-18.04-img")
    p.add_argument("--clk", default="3GHz")
    p.add_argument("--mem-size", default="3GiB")
    p.add_argument("--x86-large-memory", action="store_true",
                   help="opt in to split-range DDR4 for RAM above 3GiB; "
                        "smaller capacities keep the legacy components")
    p.add_argument("--mem-channels", type=int, default=2)
    p.add_argument("--ruby-functional-backing-store", action="store_true",
                   help="use Ruby's supported physical functional store; KVM maps "
                        "the same store, while DRAM/Ruby continue to model timing")
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
    p.add_argument("--skip-workload-validation",action="store_true",help="Set Graph500 SKIP_VALIDATION after separate host correctness preflight")
    p.add_argument("--warmup-insts",type=int,default=0,help="Open stat after this many gem5 instruction-stop counts on detailed core0 (excludes NOP and instruction prefetch); zero uses workload WORKBEGIN")
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
    p.add_argument("--functional-syscall-microops", action="store_true",
                   help="retain committed syscall micro-ops at the exact common cut (FST only)")
    p.add_argument("--trace-format", default="fst", choices=("jsonl", "fst"))
    p.add_argument("--functional-sync", action="store_true", help="enable committed semantic marker sidecar")
    p.add_argument("--thread-state", action="store_true",
                   help="enable diagnostic runtime wait and exact-build scheduler facts")
    p.add_argument("--scheduler-profile", type=Path,
                   help="explicit audited guest scheduler profile, required with --thread-state")
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


def validate_x86_memory_options(args):
    size = parse_size_bytes(args.mem_size)
    if size <= 0:
        raise ValueError('memory size must be positive')
    requested = getattr(args, 'x86_large_memory', False)
    if size > 3 * 2**30 and not requested:
        raise ValueError('X86 RAM above 3GiB requires --x86-large-memory')
    active = requested and size > 3 * 2**30
    if active and getattr(args, 'ruby_functional_backing_store', False):
        raise ValueError('large-memory DDR4 cannot use --ruby-functional-backing-store: '
                         'the current backing store covers only low RAM')
    return active


def select_x86_memory_components(active):
    if not active:
        return X86Board, ChanneledMemory
    from gem5.components.boards.x86_large_memory_board import X86LargeMemoryBoard
    from gem5.components.memory.x86_multirange_memory import X86MultiRangeDDR4Memory
    return X86LargeMemoryBoard, X86MultiRangeDDR4Memory


def x86_memory_identity(args):
    return dict(x86_large_memory=bool(getattr(args, 'x86_large_memory', False)),
                x86_large_memory_active=validate_x86_memory_options(args))


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
    if validate_x86_memory_options(args):
        profile['dram'].update(
            model=f"SplitRangeDDR4_2400_{args.mem_channels}ChannelsPerRange",
            num_channels=2 * args.mem_channels,
            channels_per_range=args.mem_channels,
            memory_layout='x86-low3g-high4g-v1',
            physical_ranges=[{'start': 0, 'size': 3 * 2**30},
                             {'start': 4 * 2**30,
                              'size': parse_size_bytes(args.mem_size) - 3 * 2**30}])
    path = os.path.join(os.path.dirname(trace_dir), "uarch_profile.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(profile, handle, indent=2, sort_keys=True)
    return path


def _b64_heredoc(host_path, guest_path):
    import gzip
    # m5 init writes the readfile script to the small root disk before our
    # tmpfs mount can run. Compress transport; decoded input stays unchanged.
    with open(host_path, "rb") as handle:
        payload = base64.b64encode(gzip.compress(handle.read(),mtime=0)).decode("ascii")
    chunks = "\n".join(payload[i:i + 76] for i in range(0, len(payload), 76))
    tag = "FASTSIM_BASE64_PAYLOAD_END"
    stage = shlex.quote(os.path.join(os.path.dirname(guest_path), ".fastsim-stage.b64"))
    packed = shlex.quote(os.path.join(os.path.dirname(guest_path), ".fastsim-stage.gz"))
    return (f"cat > {stage} <<'{tag}'\n{chunks}\n{tag}\n"
            f"base64 -d {stage} > {packed}\n"
            f"gzip -dc {packed} > {shlex.quote(guest_path)}\n"
            f"rm -f {stage} {packed}\n")


def validate_input_archive(path):
    """Reject members that can escape staging or replace the launched program."""
    seen = set()
    with tarfile.open(path, 'r:*') as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if (not member.name or name.is_absolute() or '..' in name.parts
                    or not name.parts or name.parts[0] in ('workload', 'rate-launcher')
                    or name.parts[0].startswith('.fastsim-')
                    or not (member.isfile() or member.isdir())
                    or str(name) in seen):
                raise ValueError('invalid staged input archive member: ' + member.name)
            seen.add(str(name))


def guest_housekeeping_script(cpu, proc_root="/proc", sys_root="/sys"):
    if type(cpu) is not int or cpu < 0:
        raise ValueError('invalid guest housekeeping CPU')
    proc = shlex.quote(str(proc_root))
    workqueue = shlex.quote(str(sys_root)+'/devices/virtual/workqueue/cpumask')
    return f'''command -v taskset >/dev/null
echo "[fastsim-housekeeping] cpu={cpu} setup-before-rate-copies"
if [ -w {workqueue} ]; then printf '%s\\n' '{1 << cpu:x}' > {workqueue}; fi
for task in {proc}/[0-9]*/task/[0-9]*; do
  [ -d "$task" ] || continue
  tid=${{task##*/}}
  comm=$(cat "$task/comm" 2>/dev/null || printf vanished)
  if taskset -pc {cpu} "$tid" >/dev/null 2>&1; then
    printf '[fastsim-housekeeping] pinned tid=%s comm=%s cpu={cpu}\\n' "$tid" "$comm"
  else
    printf '[fastsim-housekeeping] retained-kernel-affinity tid=%s comm=%s\\n' "$tid" "$comm"
  fi
done
'''


def boot_script(args):
    """Guest after-boot script run by m5 readfile.

    Decodes the instrumented binary and inputs under KVM, then execs the binary.
    The binary itself drives all ROI boundaries: its checkpoint marker switches
    KVM->O3, its workbegin serial marker opens the measured window. No raw m5
    pseudo-instructions and no artificial markers are used here.
    """
    # The stock image has little free root-disk space. A large input's base64
    # temporary and decoded copy must share this bounded RAM staging mount.
    staging_size = getattr(args, 'staging_size', '512m')
    if staging_size not in ('512m', '1g', '2g'):
        raise ValueError('unsupported guest staging size')
    lines = ["#!/bin/sh", "set -e", "mkdir -p /tmp/run",
             f"mount -t tmpfs -o size={staging_size} tmpfs /tmp/run", "cd /tmp/run"]
    if getattr(args, 'rate_copies', 0):
        lines.append('swapoff -a')
        lines.append('echo "[fastsim-rate] swap disabled"')
    lines.append(_b64_heredoc(args.workload_binary, "/tmp/run/workload"))
    lines.append("chmod +x /tmp/run/workload")
    for host_path in args.input_file:
        guest = "/tmp/run/" + os.path.basename(host_path)
        lines.append(_b64_heredoc(host_path, guest))
    for index, host_path in enumerate(getattr(args, 'input_archive', [])):
        validate_input_archive(host_path)
        guest = f"/tmp/run/.fastsim-input-{index}.tar"
        lines.append(_b64_heredoc(host_path, guest))
        lines.append(f"tar -xf {shlex.quote(guest)} -C /tmp/run")
        lines.append(f"rm -f {shlex.quote(guest)}")
    rate_copies = getattr(args, 'rate_copies', 0)
    if rate_copies:
        lines.append(_b64_heredoc(args.rate_launcher, "/tmp/run/rate-launcher"))
        lines.append("chmod +x /tmp/run/rate-launcher")
    preparation = getattr(args, 'prepare_workload_args', '')
    if preparation:
        lines.append('echo "[fastsim-fs] preparing official input under KVM"')
        lines.append('/tmp/run/workload ' + shlex.join(shlex.split(preparation)))
        lines.append('echo "[fastsim-fs] official input preparation complete"')
    env = ([] if rate_copies else [f"OMP_NUM_THREADS={args.omp_threads}",
                                  f"OMP_THREAD_LIMIT={args.omp_threads}"])
    if getattr(args, 'checkpoint_phase', 'staged') == 'compute-ready':
        env.append('FASTSIM_RATE_READY_CHECKPOINT=1')
    if getattr(args, 'rate_serial_init', False):
        env.append('FASTSIM_RATE_SERIAL_INIT=1')
    if getattr(args, 'native_omp_num_threads', False):
        env.append(f"OMP_NUM_THREADS={args.omp_threads}")
    if getattr(args, 'native_warmup_after_identities', False):
        env.append('FASTSIM_NATIVE_WARMUP_AFTER_IDENTITIES=1')
    if args.omp_proc_bind:
        env.append(f"OMP_PROC_BIND={args.omp_proc_bind}")
    if args.omp_places:
        env.append(f"OMP_PLACES={args.omp_places}")
    if args.omp_wait_policy:
        env.append(f"OMP_WAIT_POLICY={args.omp_wait_policy}")
    if args.skip_workload_validation:env.append("SKIP_VALIDATION=1")
    if args.warmup_steps is not None:
        env.append(f"GEM5_ROI_WARMUP_STEPS={args.warmup_steps}")
    env_str = " ".join(env)
    # stderr must reach the gem5 UART so the workbegin-serial marker is seen by
    # terminal.cc; do NOT redirect the workload's stderr to a file.
    lines.append('echo "[fastsim-fs] launching instrumented workload under KVM"')
    if getattr(args, 'housekeeping_cpu', None) is not None:
        lines.append(guest_housekeeping_script(args.housekeeping_cpu))
    if rate_copies:
        native_workers = getattr(args, 'native_workers', 0)
        count = args.omp_threads if native_workers else rate_copies
        selected = getattr(args, 'participant_core_ids', list(range(count)))
        if len(selected) < count:
            raise ValueError('participant CPU list is shorter than the rate worker count')
        cpus = ','.join(str(c) for c in selected[:count])
        native = f' --native-workers {native_workers}' if native_workers else ''
        private = ' --private-cwd' if getattr(args, 'rate_private_cwd', False) else ''
        prefix = env_str + ' ' if env_str else ''
        lines.append(f"{prefix}/tmp/run/rate-launcher --cpus {cpus}{native}{private} -- /tmp/run/workload {args.workload_args}")
    else:
        selected = getattr(args, 'participant_core_ids', None)
        affinity = ('taskset -c ' + ','.join(map(str, selected)) + ' ') if selected else ''
        lines.append(f"{env_str} {affinity}/tmp/run/workload {args.workload_args}")
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


def static_marker_identity(filename, symbol, abi, weak_only=False):
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
    matches = [row.split() for row in symbols if row.split()[-1:] == [symbol]]
    if len(matches) != 1 or matches[0][1] not in (("W",) if weak_only else ("W", "T")):
        raise ValueError(f"binary has no unique {'weak ' if weak_only else ''}{symbol} symbol")
    pc = int(matches[0][0], 16)
    marker_bytes = None
    for i in range(phcount):
        header = phoff + i * phsize
        kind, flags, offset, vaddr, _, filesz = struct.unpack_from("<IIQQQQ", data, header)
        if kind == 1 and flags & 1 and vaddr <= pc and pc + 2 <= vaddr + filesz:
            marker_bytes = data[offset + pc - vaddr:offset + pc - vaddr + 2]
    if marker_bytes != b"\x90\xc3":
        raise ValueError(f"{symbol} must be exactly nop;ret at its executable symbol PC")
    return dict(binary=str(path), binary_sha256=hashlib.sha256(data).hexdigest(),
                marker_pc=pc, marker_abi=abi)


def sync_binary_identity(filename):
    return static_marker_identity(filename, "fastsim_sync_marker", "fastsim-sync-marker-v1")


def thread_state_profile(profile_path, kernel_path, task_clock=False):
    """Re-prove the known kernel build; profile files never authorize offsets."""
    if profile_path is None:
        raise ValueError("--thread-state requires --scheduler-profile")
    helper_path = Path(__file__).resolve().parents[1] / "tools/generate_guest_scheduler_profile.py"
    spec = importlib.util.spec_from_file_location("fastsim_guest_scheduler_preflight", helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    profile_bytes = Path(profile_path).read_bytes()
    requested = json.loads(profile_bytes)
    actual = helper.generate(Path(kernel_path).resolve(), task_clock=task_clock)
    if requested.get("schema") != "fastsim-guest-scheduler-profile-v1":
        raise ValueError("unsupported scheduler profile schema")
    identity = actual["kernel"]
    if identity["sha256"] != helper.AUDITED_SHA256 or identity["build_id"] != helper.AUDITED_BUILD_ID:
        raise ValueError("guest kernel is not the known exact audited build")
    for field in ("sha256", "build_id", "machine", "elf_class", "little_endian"):
        if requested.get("kernel", {}).get(field) != identity[field]:
            raise ValueError(f"scheduler profile kernel identity mismatch: {field}")
    evidence = actual["machine_code_evidence"]
    if evidence.get("status") != "partial_exact_build_audit":
        raise ValueError("actual kernel failed exact machine-code audit")
    supplied = requested.get("machine_code_evidence", {})
    # Equality includes every proof's PC, expected bytes, and verified result;
    # generate() just checked these bytes against the selected kernel PT_LOADs.
    for field in ("status", "proofs", "members", "boundaries", "current_task_gs_displacement"):
        if supplied.get(field) != evidence[field]:
            raise ValueError(f"scheduler profile machine-code evidence mismatch: {field}")
    if task_clock and (supplied.get("task_clock_contract") != "task-lifecycle-basecpu-active-v1"
                       or supplied.get("counter_domain") != "gem5-basecpu-active-cycles-v1"):
        raise ValueError("task clock requires exact independent lifecycle/counter domain proofs")
    if not evidence["proofs"] or not all(p["bytes_verified"] for p in evidence["proofs"]):
        raise ValueError("scheduler machine-code proofs are missing or unverified")
    params = dict(scheduler_switch_pc=int(evidence["boundaries"]["switch"]["pc"], 16),
                  scheduler_wakeup_pc=int(evidence["boundaries"]["wakeup"]["pc"], 16),
                  scheduler_pid_offset=evidence["members"]["task_struct.pid"]["offset_bytes"],
                  scheduler_state_offset=evidence["members"]["task_struct.state"]["offset_bytes"],
                  scheduler_current_task_gs_offset=evidence["current_task_gs_displacement"],
                  scheduler_kernel_sha256=identity["sha256"],
                  scheduler_profile_id="sha256:" + hashlib.sha256(profile_bytes).hexdigest())
    if task_clock:
        params.update(scheduler_account_out_pc=int(evidence["boundaries"]["account_out"]["pc"],16),
                      scheduler_account_in_pc=int(evidence["boundaries"]["account_in"]["pc"],16))
    return params, dict(profile=str(Path(profile_path).resolve()),
                        kernel_identity=identity, profile_id=params["scheduler_profile_id"],
                        audit_status=evidence["status"], limitations=evidence["limitations"])


def validate_rate_options(args):
    ready_checkpoint = getattr(args, 'checkpoint_phase', 'staged') == 'compute-ready'
    serial_init = getattr(args, 'rate_serial_init', False)
    if ready_checkpoint or serial_init:
        if not args.rate_copies or getattr(args, 'native_workers', 0):
            raise ValueError('ready checkpoint and serial initialization require independent rate copies')
    native_workers = getattr(args, 'native_workers', 0)
    if getattr(args, 'native_warmup_after_identities', False) and (not native_workers or args.warmup_insts <= 0):
        raise ValueError('native warmup after identities requires native tasks and positive --warmup-insts')
    if native_workers < 0 or (native_workers and
            (args.rate_copies != 1 or not args.omp_threads <= native_workers <= 1024)):
        raise ValueError('native tasks require one process, coverage of participant CPUs and at most 1024 tasks')
    if getattr(args, 'native_omp_num_threads', False) and not native_workers:
        raise ValueError('native thread-count environment requires native tasks')
    housekeeping = getattr(args, 'housekeeping_cpu', None)
    if housekeeping is not None and (not 0 <= housekeeping < args.omp_threads + args.extra_cores):
        raise ValueError('housekeeping CPU must be a configured rate guest CPU')
    if args.rate_copies:
        if (not native_workers and args.rate_copies != args.omp_threads) or not args.rate_launcher:
            raise ValueError('rate copies require one participant core per copy and a launcher')
        if args.functional_sync or any((args.omp_proc_bind,args.omp_places,args.omp_wait_policy)):
            raise ValueError('rate copies cannot force OpenMP or use its synchronization protocol')
        if not args.checkpoint_only and args.warmup_insts <= 0:
            raise ValueError('rate capture requires a positive --warmup-insts budget')
    elif args.save_boot_checkpoint or args.restore_boot_checkpoint or args.checkpoint_only:
        raise ValueError('staged boot checkpoints require the rate-copy launcher')
    if args.checkpoint_only and not args.save_boot_checkpoint:
        raise ValueError('--checkpoint-only requires --save-boot-checkpoint')
    if args.save_boot_checkpoint and args.restore_boot_checkpoint:
        raise ValueError('cannot save and restore the same boot checkpoint in one run')


def main():
    args = parse_args()
    validate_rate_options(args)
    memory_identity = x86_memory_identity(args)
    large_memory_active = memory_identity['x86_large_memory_active']
    binary_path = Path(args.workload_binary).resolve()
    identity = dict(binary=str(binary_path),
                    binary_sha256=hashlib.sha256(binary_path.read_bytes()).hexdigest())
    if args.functional_sync:
        identity.update(sync_binary_identity(args.workload_binary))
    if args.scheduler_profile and not args.thread_state:
        raise ValueError("--scheduler-profile requires --thread-state")
    if args.thread_state:
        wait_identity = static_marker_identity(args.workload_binary, "fastsim_wait_marker",
                                               "fastsim-wait-marker-v1", weak_only=True)
        identity["thread_state_marker_pc"] = wait_identity["marker_pc"]
        identity["thread_state_marker_abi"] = wait_identity["marker_abi"]
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

    class ProbeCacheHierarchy(MESIThreeLevelCacheHierarchy):
        def incorporate_cache(self, board):
            super().incorporate_cache(board)
            if args.ruby_functional_backing_store:
                # Match configs/ruby/Ruby.py's access_backing_store mode.
                # The official data store must be populated by KVM itself;
                # enabling access_backing_store only after switching loses
                # the boot/application contents. DRAM remains in the system
                # address map for geometry, but cannot overlap the KVM map.
                self.ruby_system.access_backing_store = True
                self.ruby_system.phys_mem = SimpleMemory(
                    range=board.mem_ranges[0], in_addr_map=False, kvm_map=True)
                for controller in board.get_memory().get_memory_controllers():
                    controller.dram.kvm_map = False
                print("[fastsim-fs] Ruby functional backing store: KVM/data shared; "
                      "Ruby/DRAM timing retained")

    cache = ProbeCacheHierarchy(
        l1i_size=args.l1i_size, l1i_assoc=args.l1i_assoc,
        l1d_size=args.l1d_size, l1d_assoc=args.l1d_assoc,
        l2_size=args.l2_size, l2_assoc=args.l2_assoc,
        l3_size=args.l3_size, l3_assoc=args.l3_assoc,
        num_l3_banks=args.num_l3_banks,
    )
    board_class, memory_class = select_x86_memory_components(large_memory_active)
    memory_kwargs = dict(num_channels=args.mem_channels,
                         interleaving_size=64, size=args.mem_size)
    if not large_memory_active:
        memory_kwargs['dram_interface_class'] = DDR4_2400_8x8
    memory = memory_class(**memory_kwargs)
    board = board_class(clk_freq=args.clk, processor=processor,
                        memory=memory, cache_hierarchy=cache)

    kernel = obtain_resource(args.kernel_resource)
    thread_params = {}
    scheduler_identity = None
    if args.thread_state:
        thread_params, scheduler_identity = thread_state_profile(args.scheduler_profile,
                                                                  kernel.get_local_path())
        thread_params["thread_state_marker_pc"] = identity["thread_state_marker_pc"]
    disk = obtain_resource(args.disk_resource)
    from spec_rate_checkpoint import sha256, validate_receipt, write_receipt, RateCheckpointFlow
    checkpoint_identity = None
    if args.save_boot_checkpoint or args.restore_boot_checkpoint:
        hardware = {k:v for k,v in vars(args).items() if k in (
            'omp_threads','extra_cores','clk','mem_size','mem_channels','x86_large_memory',
            'ruby_functional_backing_store','l1i_size','l1d_size','l2_size','l3_size',
            'l1i_assoc','l1d_assoc','l2_assoc','l3_assoc','num_l3_banks',
            'rob_entries','iq_entries','lq_entries','sq_entries','dtlb_entries',
            'tso_store','tso_store_buffer_size','rate_copies','kvm_quantum_us')}
        checkpoint_identity=dict(hardware=hardware,workload_sha256=identity['binary_sha256'],
            guest_setup={'housekeeping_cpu':args.housekeeping_cpu},
            workload_args=args.workload_args,launcher_sha256=sha256(args.rate_launcher),
            input_sha256={Path(p).name:sha256(p) for p in args.input_file},
            gem5_sha256=sha256('/proc/self/exe'),runner_sha256=sha256(__file__),
            kernel_sha256=sha256(kernel.get_local_path()),disk_sha256=sha256(disk.get_local_path()),
            scheduler_profile_sha256=sha256(args.scheduler_profile) if args.scheduler_profile else None,
            readfile_sha256=hashlib.sha256(boot_script(args).encode()).hexdigest())
        checkpoint_identity['hardware'].update(memory_identity)
        checkpoint_identity['checkpoint_phase'] = args.checkpoint_phase
        checkpoint_identity['guest_setup']['rate_serial_init'] = args.rate_serial_init
        if args.native_workers:
            checkpoint_identity['hardware']['native_workers'] = args.native_workers
        if args.native_omp_num_threads:
            checkpoint_identity['guest_setup']['native_omp_num_threads'] = args.omp_threads
        if args.native_warmup_after_identities:
            checkpoint_identity['guest_setup']['native_warmup_after_identities'] = True
        if args.prepare_workload_args:
            checkpoint_identity['guest_setup']['prepare_workload_args'] = args.prepare_workload_args
        if args.input_archive:
            checkpoint_identity['input_archive_sha256'] = {
                str(index): sha256(path) for index, path in enumerate(args.input_archive)}
        if args.restore_boot_checkpoint:
            validate_receipt(args.restore_boot_checkpoint,
                json.loads((args.restore_boot_checkpoint/'fastsim-checkpoint.json').read_text()),
                checkpoint_identity)
        if args.save_boot_checkpoint and args.save_boot_checkpoint.exists():
            raise ValueError('refuse to overwrite an existing checkpoint')
    board.set_kernel_disk_workload(
        kernel=kernel, disk_image=disk,
        readfile_contents=boot_script(args), exit_on_work_items=True,
        checkpoint=args.restore_boot_checkpoint,
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
            **({"functional_syscall_microops": True}
               if args.functional_syscall_microops else {}),
            functional_user_target=args.functional_user_target,
            trace_format=args.trace_format,
            functional_sync_marker_pc=identity.get("marker_pc", 0),
            functional_sync_initial_context=args.sync_initial_context,
            **thread_params,
        )

    from roi_capture_policy import BoundedWarmup
    warmup_policy=BoundedWarmup(args.warmup_insts,
                              defer_until_identities=args.native_warmup_after_identities)
    state = {"switched": False}
    checkpoint_flow = RateCheckpointFlow(args.checkpoint_phase, bool(args.restore_boot_checkpoint))

    def milestone(name):
        import time
        with Path(m5.options.outdir, 'capture-phases.jsonl').open('a') as output:
            output.write(json.dumps(dict(phase=name, wall_time=time.time(),
                                         tick=int(m5.curTick())))+'\n')

    def on_checkpoint():
        if args.rate_copies:
            action = checkpoint_flow.checkpoint()
            if action == 'continue':
                milestone('staged-before-fork')
                return False
            if action == 'save':
                milestone(args.checkpoint_phase+'-checkpoint-reached')
                if args.save_boot_checkpoint:
                    print(f'[fastsim-fs] saving {args.checkpoint_phase} KVM checkpoint',flush=True)
                    simulator.save_checkpoint(args.save_boot_checkpoint)
                    write_receipt(args.save_boot_checkpoint,checkpoint_identity)
                    milestone('checkpoint-saved')
                    print('[fastsim-fs] staged boot checkpoint saved',flush=True)
                    if args.checkpoint_only:return True
                return False
        # Workload checkpoint marker: switch KVM -> O3 + Ruby so the warmup and
        # measured region run on the detailed core.
        if not state["switched"]:
            print("[fastsim-fs] CHECKPOINT marker: switch KVM -> O3+Ruby",flush=True)
            milestone('switch-begin')
            simulator.switch_processor()
            milestone('switch-complete')
            state["switched"] = True
            arm_at_switch = warmup_policy.checkpoint()
            if arm_at_switch and args.warmup_insts:
                traced[0].get_simobject().scheduleInstStopAnyThread(args.warmup_insts)
        elif args.native_warmup_after_identities:
            warmup_policy.identities_ready()
            milestone('native-identities-ready')
            milestone('warmup-armed')
            traced[0].get_simobject().scheduleInstStopAnyThread(args.warmup_insts)
            print('[fastsim-fs] native identities ready: warmup instruction budget armed',flush=True)
        else:
            print("[fastsim-fs] extra CHECKPOINT ignored")
        return False

    def on_workbegin():
        # Serial workbegin marker already fired traceMeasurementBegin() in the
        # producer; reset host stats and continue the measured region.
        if args.warmup_insts:raise RuntimeError("unexpected workload WORKBEGIN in instruction-budget mode")
        print("[fastsim-fs] WORKBEGIN serial marker: measurement window open",flush=True)
        m5.stats.reset()
        return False

    def on_max_insts():
        if warmup_policy.instruction_exit()=="open-measurement":
            traced[0].get_simobject().tao_trace.startFunctionalMeasurement()
            m5.stats.reset()
            milestone('measurement-open')
            print("[fastsim-fs] WARMUP instruction budget reached: measurement window open",flush=True)
            return False
        print("[fastsim-fs] functional_user_target reached: common-end, finish")
        milestone('common-end')
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
    execution = (f"process_copies={args.rate_copies} native_workers={args.native_workers} native_threading=preserved" if args.rate_copies
                 else f"omp_threads={args.omp_threads}")
    print(f"[fastsim-fs] cores={num_cores} traced={len(traced)} {execution} "
          f"extra={args.extra_cores} target={args.functional_user_target} "
          f"TSO={args.tso_store} SQ={effective_sq(args)} "
          f"L3={args.num_l3_banks}x{args.l3_size} fmt={args.trace_format}")
    Path(m5.options.outdir, "sync-run-request.json").write_text(json.dumps(
        dict(schema="fastsim-sync-pilot-request-v1", diagnostic=True,
             memory_mode=memory_identity,
             arguments={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
             binary_identity=identity, scheduler_identity=scheduler_identity), indent=2) + "\n")
    milestone('restore-start' if args.restore_boot_checkpoint else 'boot-start')
    simulator.run(max_ticks=args.max_ticks or None)
    Path(m5.options.outdir, "sync-run-result.json").write_text(json.dumps(
        dict(cause=simulator.get_last_exit_event_cause(),
             exit_code=simulator.get_last_exit_event_code(),
             tick=simulator.get_current_tick()), indent=2) + "\n")
    print("[fastsim-fs] simulation complete")


if __name__ == "__m5_main__":
    main()
