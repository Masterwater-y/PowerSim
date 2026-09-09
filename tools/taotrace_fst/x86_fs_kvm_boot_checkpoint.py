#!/usr/bin/env python3
"""Create or restore topology-specific x86 FS boot checkpoints.

minesim mainline note
---------------------
Promoted into the minesim FastSim mainline as the canonical gem5 full-system
board/checkpoint config for TaoTrace collection (moved here from the
fastsim-branch collection tree, originally
/data00/yinhaolang/TCSim/configs/gem5/). Invoked indirectly through the
co-located x86_fs_kvm_boot_checkpoint_tao.py wrapper. Only gem5 stdlib is
imported, so the local gem5_taotrace build satisfies it unchanged.

The create path boots Ubuntu with KVM and saves a checkpoint when the gem5
systemd workload emits its "after boot" hypercall.  The restore path rebuilds
the same board topology and resumes the checkpoint with a selectable CPU
model.
"""

import argparse
import json
import os
import platform
import sys
from datetime import datetime, timezone
from math import log2
from pathlib import Path

import m5
from _m5 import core as m5_core
from m5.objects import (
    CowDiskImage,
    IdeDisk,
    RangeAddrMapper,
    RawDiskImage,
    X86E820Entry,
)
from m5.params import AddrRange
from m5.util.convert import toMemorySize

from gem5.coherence_protocol import CoherenceProtocol
from gem5.components.boards.x86_board import X86Board
from gem5.components.cachehierarchies.classic.private_l1_private_l2_cache_hierarchy import (
    PrivateL1PrivateL2CacheHierarchy,
)
from gem5.components.memory.dram_interfaces.ddr4 import DDR4_2400_8x8
from gem5.components.memory.memory import ChanneledMemory
from gem5.components.processors.cpu_types import (
    CPUTypes,
    get_cpu_type_from_str,
)
from gem5.components.processors.simple_switchable_processor import (
    SimpleSwitchableProcessor,
)
from gem5.isas import ISA
from gem5.resources.resource import (
    DiskImageResource,
    KernelResource,
    obtain_resource,
)
from gem5.simulate.exit_event import ExitEvent
from gem5.simulate.simulator import Simulator
from gem5.utils.override import overrides
from gem5.utils.requires import requires


SUPPORTED_CORE_COUNTS = (4, 8, 16, 32)
METADATA_SCHEMA = "tcsim-gem5-x86-fs-boot-checkpoint-v3"
DEFAULT_KERNEL_ID = "x86-linux-kernel-6.8.0-52-generic"
DEFAULT_KERNEL_VERSION = "1.0.0"
DEFAULT_DISK_IMAGE_ID = "x86-ubuntu-24.04-img"
DEFAULT_DISK_IMAGE_VERSION = "4.0.0"
DEFAULT_DISK_ROOT_PARTITION = "1"
DEFAULT_KERNEL_ARGS = (
    "earlyprintk=ttyS0",
    "console=ttyS0",
    "lpj=7999923",
    "root=/dev/sda2",
    # gem5 O3 does not model the full MCE bank MSR set (e.g. IA32_MC1_CTL2
    # at 0x421). Linux mce_intel_feature_init's periodic poll will #GP on
    # first tick after restore and Kernel panics with "MCA architectural
    # violation". Disable MCE polling entirely.
    "mce=off",
    "nomce",
    # Restore switches CPU model; ASLR/KPTI/speculation mitigations either
    # trip on unimplemented MSRs (0x48/0x49/0xc0000104...) or make address
    # comparisons non-deterministic across KVM->O3 handoff.
    "nokaslr",
    "mitigations=off",
    "nopti",
    # gem5 Atomic/Timing/O3 all trip on MONITOR/MWAIT: AtomicSimpleCPU asserts
    # in BaseCPU::mwaitAtomic, and the timing model has no full mwait wakeup
    # path. Force the kernel cpuidle to plain HLT/loop so restore doesn't
    # execute MWAIT the first thing it does.
    "idle=poll",
)


def fail(message):
    print(f"[fs-ckpt][ERROR] {message}", file=sys.stderr)
    raise SystemExit(2)


class HoleAwareChanneledMemory(ChanneledMemory):
    """Eight-channel DRAM behind a guest-physical x86 PCI/MMIO hole.

    The DRAM controllers keep one contiguous internal range. RangeAddrMapper
    objects expose the low and high guest-physical windows and remap the high
    window down over the internal representation of the PCI/MMIO hole. This
    preserves one controller per channel instead of duplicating controllers
    for the two guest-visible ranges.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._guest_ranges = []
        self._channel_guest_ranges = []
        self._range_mappers = []

    def _channel_range(self, base_range, channel):
        if self._addr_mapping == "RoRaBaChCo":
            rowbuffer_size = (
                self._dram_class.device_rowbuffer_size.value
                * self._dram_class.devices_per_rank.value
            )
            intlv_low_bit = int(log2(rowbuffer_size))
        elif self._addr_mapping in ("RoRaBaCoCh", "RoCoRaBaCh"):
            intlv_low_bit = int(log2(self._intlv_size))
        else:
            raise ValueError(
                "unsupported DRAM address mapping for x86 hole remapping: "
                f"{self._addr_mapping}"
            )
        intlv_bits = int(log2(self._num_channels))
        return AddrRange(
            start=base_range.start,
            size=base_range.size(),
            intlvHighBit=intlv_low_bit + intlv_bits - 1,
            xorHighBit=0,
            intlvBits=intlv_bits,
            intlvMatch=channel,
        )

    @overrides(ChanneledMemory)
    def set_memory_range(self, ranges):
        if sum(mem_range.size() for mem_range in ranges) != self._size:
            raise ValueError(
                "guest-visible memory ranges do not add up to the configured "
                f"memory size {self._size}"
            )
        if len(ranges) == 1:
            super().set_memory_range(ranges)
            self._guest_ranges = list(ranges)
            self._channel_guest_ranges = [
                [controller.dram.range] for controller in self.mem_ctrl
            ]
            self._range_mappers = []
            return
        if len(ranges) != 2:
            raise ValueError("x86 hole-aware memory expects one or two ranges")

        low_range, high_range = ranges
        if (
            int(low_range.start) != 0
            or int(high_range.start) != toMemorySize("4GiB")
        ):
            raise ValueError(
                "x86 split memory must start at 0 and 4GiB respectively"
            )

        # The memory controllers model one contiguous 0..size range. The
        # guest's high window is translated onto the internal bytes which
        # would otherwise sit in the 3-4GiB PCI/MMIO hole.
        internal_low = AddrRange(start=0, size=low_range.size())
        internal_high = AddrRange(
            start=low_range.size(), size=high_range.size()
        )
        super().set_memory_range([AddrRange(start=0, size=self._size)])

        self._guest_ranges = list(ranges)
        self._channel_guest_ranges = []
        mappers = []
        for channel, controller in enumerate(self.mem_ctrl):
            guest_channel_ranges = [
                self._channel_range(low_range, channel),
                self._channel_range(high_range, channel),
            ]
            internal_channel_ranges = [
                self._channel_range(internal_low, channel),
                self._channel_range(internal_high, channel),
            ]
            mapper = RangeAddrMapper(
                original_ranges=guest_channel_ranges,
                remapped_ranges=internal_channel_ranges,
            )
            mapper.mem_side_port = controller.port
            mappers.append(mapper)
            self._channel_guest_ranges.append(guest_channel_ranges)
        self.range_mappers = mappers
        self._range_mappers = mappers

    @overrides(ChanneledMemory)
    def get_mem_ports(self):
        if not self._range_mappers:
            return super().get_mem_ports()
        # MESIThreeLevelHoleAware expands each directory's addr_ranges to both
        # entries before instantiation. The first range is used here to create
        # one directory/port per physical channel.
        return [
            (ranges[0], mapper.cpu_side_port)
            for ranges, mapper in zip(
                self._channel_guest_ranges, self._range_mappers
            )
        ]

    @overrides(ChanneledMemory)
    def get_uninterleaved_range(self):
        return list(self._guest_ranges)

    def get_channel_guest_ranges(self):
        return [list(ranges) for ranges in self._channel_guest_ranges]


class HoleAwareX86Board(X86Board):
    """X86Board with low/high RAM windows around the 3-4GiB PCI hole."""

    @overrides(X86Board)
    def _setup_memory_ranges(self):
        memory = self.get_memory()
        memory_size = memory.get_size()
        low_limit = toMemorySize("3GiB")
        if memory_size <= low_limit:
            data_ranges = [AddrRange(start=0, size=memory_size)]
        else:
            data_ranges = [
                AddrRange(start=0, size=low_limit),
                AddrRange(
                    start=toMemorySize("4GiB"),
                    size=memory_size - low_limit,
                ),
            ]
        memory.set_memory_range(data_ranges)
        self.mem_ranges = data_ranges + [
            AddrRange(0xC0000000, size=0x100000)
        ]

    @overrides(X86Board)
    def _setup_io_devices(self):
        super()._setup_io_devices()
        ram_ranges = self.get_memory().get_uninterleaved_range()
        if len(ram_ranges) == 1:
            return

        entries = [
            X86E820Entry(addr=0, size="639KiB", range_type=1),
            X86E820Entry(addr=0x9FC00, size="385KiB", range_type=2),
            X86E820Entry(
                addr=0x100000,
                size=f"{ram_ranges[0].size() - 0x100000:d}B",
                range_type=1,
            ),
            X86E820Entry(
                addr=ram_ranges[1].start,
                size=f"{ram_ranges[1].size():d}B",
                range_type=1,
            ),
            X86E820Entry(addr=0xFFFF0000, size="64KiB", range_type=2),
        ]
        self.workload.e820_table.entries = entries


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Use KVM to create, or another CPU model to restore, an x86 "
            "full-system OS boot checkpoint."
        )
    )
    parser.add_argument(
        "--action",
        choices=("download", "prepare", "create", "create-roi", "restore"),
        required=True,
    )
    parser.add_argument(
        "--num-cores",
        type=int,
        choices=SUPPORTED_CORE_COUNTS,
        default=4,
    )
    parser.add_argument("--checkpoint-dir", type=Path)
    parser.add_argument(
        "--roi-checkpoint-dir",
        type=Path,
        help=(
            "create-roi only: output checkpoint directory saved when the "
            "KVM guest reaches the workload's source-level WORKBEGIN marker."
        ),
    )
    parser.add_argument(
        "--restore-cpu-type",
        choices=("atomic", "timing", "o3", "kvm"),
        default="atomic",
        help="CPU model used after restoring a checkpoint.",
    )
    parser.add_argument(
        "--roi-target-cpu-type",
        choices=("timing", "o3"),
        default="o3",
        help=(
            "create-roi only: detailed CPU SimObjects included switched-out "
            "in the ROI checkpoint so the restore topology exactly matches."
        ),
    )
    parser.add_argument(
        "--cache-hierarchy",
        choices=("classic", "mesi-three-level"),
        default="mesi-three-level",
        help=(
            "Board topology stored in the checkpoint. Restore must use the "
            "same value."
        ),
    )
    parser.add_argument("--clk", default="3GHz")
    parser.add_argument(
        "--workload-id",
        default="x86-ubuntu-24.04-boot-with-systemd",
    )
    parser.add_argument("--workload-version", default="5.0.0")
    parser.add_argument("--resource-dir", type=Path)
    parser.add_argument("--kernel-id", default=DEFAULT_KERNEL_ID)
    parser.add_argument("--kernel-version", default=DEFAULT_KERNEL_VERSION)
    parser.add_argument("--disk-image-id", default=DEFAULT_DISK_IMAGE_ID)
    parser.add_argument(
        "--disk-image-version", default=DEFAULT_DISK_IMAGE_VERSION
    )
    parser.add_argument(
        "--disk-root-partition", default=DEFAULT_DISK_ROOT_PARTITION
    )
    parser.add_argument("--mem-size", default="3GiB")
    parser.add_argument(
        "--create-cpu-type",
        choices=("kvm", "atomic"),
        default="kvm",
        help="CPU model used during checkpoint creation. KVM (~20s) is "
        "strongly preferred; atomic (~30-90 min) is a fallback for hosts "
        "without KVM. Memory >3GiB only works with KVM on a patched binary "
        "and remains experimental; 3GiB is the tested default.",
    )
    parser.add_argument(
        "--kvm-perf",
        action="store_true",
        help=(
            "Enable KVM perf counters. They are unnecessary for boot "
            "checkpoint creation and are disabled by default."
        ),
    )
    parser.add_argument("--l1i-size", default="32KiB")
    parser.add_argument("--l1d-size", default="32KiB")
    parser.add_argument("--l2-size", default="1MiB")
    parser.add_argument("--l3-size", default="8MiB")
    parser.add_argument(
        "--rob-entries",
        type=int,
        default=192,
        help="O3 reorder-buffer entries for every detailed core.",
    )
    parser.add_argument("--l1i-assoc", type=int, default=8)
    parser.add_argument("--l1d-assoc", type=int, default=8)
    parser.add_argument("--l2-assoc", type=int, default=8)
    parser.add_argument("--l3-assoc", type=int, default=16)
    parser.add_argument("--num-l3-banks", type=int, default=8)
    parser.add_argument("--mem-channels", type=int, choices=(1, 2, 4, 8), default=8)
    # --- restore-only workload-run options -----------------------------------
    parser.add_argument(
        "--aux-disk",
        type=Path,
        default=None,
        help=(
            "Path to a raw ext4/ext2 disk image mounted as the second guest "
            "disk (/dev/sdb for gem5 x86 IDE). Used for both create (KVM "
            "mounts + fires m5 checkpoint) and restore (workload run) actions."
        ),
    )
    parser.add_argument(
        "--readfile",
        type=Path,
        default=None,
        help=(
            "Host-side shell script served to `gem5-bridge readfile` "
            "(guest after_boot.sh runs it). Used for both create (install "
            "readfile: rescan+mount+m5 checkpoint) and restore (run readfile: "
            "cp+workbegin+exec workload) actions."
        ),
    )
    parser.add_argument(
        "--switch-on-workbegin",
        action="store_true",
        help=(
            "Restore-only: start phased execution when guest emits WORKBEGIN. "
            "After the optional Atomic fast-forward, switch to the requested "
            "O3/timing CPU, optionally warm detailed state, then reset stats "
            "for ROI collection. If unset, the restore CPU type is used from "
            "the start of the restore."
        ),
    )
    parser.add_argument(
        "--resume-at-roi",
        action="store_true",
        help=(
            "Restore a source-level warmup checkpoint and switch "
            "Atomic->O3/timing immediately after checkpoint instantiation."
        ),
    )
    parser.add_argument(
        "--wait-for-roi-workbegin",
        action="store_true",
        help=(
            "Restore-only: after resuming a source checkpoint and switching "
            "to O3/timing, execute source-defined warmup until the workload "
            "emits WORKBEGIN, then reset stats and start ROI collection."
        ),
    )
    parser.add_argument(
        "--max-insts-per-core",
        type=int,
        default=0,
        help=(
            "Restore-only ROI sample length: stop once any detailed core "
            "reaches this many instructions after the final stats reset "
            "(0 = no limit)."
        ),
    )
    parser.add_argument(
        "--roi-user-records-per-core",
        type=int,
        default=0,
        help=(
            "Restore-only user-only trace length. TaoTrace exits only after "
            "every detailed core has emitted at least this many functional "
            "records (0 = disabled)."
        ),
    )
    parser.add_argument(
        "--roi-safety-max-insts-per-core",
        type=int,
        default=0,
        help=(
            "Any-core total-instruction hard stop used while collecting a "
            "per-core user-record target. Reaching it is a failed sample."
        ),
    )
    parser.add_argument(
        "--roi-stop-policy",
        choices=("any-core", "all-core"),
        default="any-core",
        help=(
            "Restore-only ROI stop condition. any-core stops when the first "
            "core reaches the target; all-core waits until every simulated "
            "core has reached at least the target."
        ),
    )
    parser.add_argument(
        "--atomic-fast-forward-insts",
        type=int,
        default=0,
        help=(
            "Restore-only: after guest WORKBEGIN, remain on Atomic for this "
            "many any-core instructions before switching to the detailed CPU. "
            "This phase skips workload/runtime cold startup and does not warm "
            "Ruby caches (0 = switch immediately)."
        ),
    )
    parser.add_argument(
        "--detailed-warmup-insts",
        type=int,
        default=0,
        help=(
            "Restore-only: after switching to O3/timing, run this many "
            "any-core instructions to warm Ruby caches and detailed CPU "
            "state, then reset stats and begin the ROI sample (0 = no "
            "detailed warmup)."
        ),
    )
    parser.add_argument(
        "--stats-outfile",
        type=Path,
        default=None,
        help=(
            "Restore-only: if set, gem5 stats.txt is copied here after "
            "WORKEND or MAX_INSTS."
        ),
    )
    return parser.parse_args()


def checkpoint_profile(args):
    memory_size = toMemorySize(args.mem_size)
    low_limit = toMemorySize("3GiB")
    if memory_size <= low_limit:
        guest_ranges = [
            {"start": 0, "size": memory_size},
        ]
        remap = None
    else:
        guest_ranges = [
            {"start": 0, "size": low_limit},
            {
                "start": toMemorySize("4GiB"),
                "size": memory_size - low_limit,
            },
        ]
        remap = {
            "guest_high_start": toMemorySize("4GiB"),
            "controller_high_start": low_limit,
        }
    common = {
        "schema": METADATA_SCHEMA,
        "isa": "x86",
        "full_system": True,
        "num_cores": args.num_cores,
        "clk": args.clk,
        "memory_size": args.mem_size,
        "guest_physical_ranges": guest_ranges,
        "pci_mmio_hole": {
            "start": toMemorySize("3GiB"),
            "size": toMemorySize("1GiB"),
        },
        "controller_address_remap": remap,
        "kvm_backing_store_guest_ranges": guest_ranges,
        "cache_hierarchy": args.cache_hierarchy,
        "workload_id": args.workload_id,
        "workload_version": args.workload_version,
        "workload_resources": {
            "kernel_id": args.kernel_id,
            "kernel_version": args.kernel_version,
            "disk_image_id": args.disk_image_id,
            "disk_image_version": args.disk_image_version,
            "disk_root_partition": args.disk_root_partition,
            "kernel_args": list(DEFAULT_KERNEL_ARGS),
        },
    }
    # Existing baseline checkpoints predate this option but already contain
    # gem5's 192-entry default.  Keep them reusable; non-default ROB geometry
    # is part of the checkpoint topology and must receive its own identity.
    if args.rob_entries != 192:
        common["o3_rob_entries"] = args.rob_entries
    if args.cache_hierarchy == "classic":
        common["cache"] = {
            "l1i_size": args.l1i_size,
            "l1d_size": args.l1d_size,
            "l2_size": args.l2_size,
        }
        common["memory"] = {
            "type": "DDR4_2400_8x8",
            "channels": args.mem_channels,
            "interleaving_size": 64,
        }
    else:
        common["cache"] = {
            "protocol": "MESI_Three_Level",
            "l1i_size": args.l1i_size,
            "l1d_size": args.l1d_size,
            "l2_size": args.l2_size,
            "l3_size_per_bank": args.l3_size,
            "l1i_assoc": args.l1i_assoc,
            "l1d_assoc": args.l1d_assoc,
            "l2_assoc": args.l2_assoc,
            "l3_assoc": args.l3_assoc,
            "num_l3_banks": args.num_l3_banks,
        }
        common["memory"] = {
            "type": "DDR4_2400_8x8",
            "channels": args.mem_channels,
            "interleaving_size": 64,
        }
    return common


def validate_checkpoint_for_restore(checkpoint_dir, expected_profile):
    if not checkpoint_dir.is_dir():
        fail(f"checkpoint directory does not exist: {checkpoint_dir}")
    if not (checkpoint_dir / "m5.cpt").is_file():
        fail(f"checkpoint is incomplete (missing m5.cpt): {checkpoint_dir}")

    metadata_path = checkpoint_dir / "metadata.json"
    if not metadata_path.is_file():
        fail(
            f"checkpoint metadata is missing: {metadata_path}; refusing an "
            "unchecked restore"
        )
    with metadata_path.open() as metadata_file:
        metadata = json.load(metadata_file)
    actual_profile = metadata.get("profile")
    if actual_profile != expected_profile:
        fail(
            "checkpoint/configuration mismatch:\n"
            f"checkpoint profile={json.dumps(actual_profile, sort_keys=True)}\n"
            f"requested profile={json.dumps(expected_profile, sort_keys=True)}"
        )


def validate_kvm_device():
    if not os.path.exists("/dev/kvm"):
        fail(
            "/dev/kvm does not exist; expose the host device to the container "
            "(for example, --device=/dev/kvm)"
        )
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        fail("the current user cannot read and write /dev/kvm")


def make_cache_and_memory(args):
    memory = HoleAwareChanneledMemory(
        dram_interface_class=DDR4_2400_8x8,
        num_channels=args.mem_channels,
        interleaving_size=64,
        size=args.mem_size,
    )
    if args.cache_hierarchy == "classic":
        return (
            PrivateL1PrivateL2CacheHierarchy(
                l1d_size=args.l1d_size,
                l1i_size=args.l1i_size,
                l2_size=args.l2_size,
            ),
            memory,
        )

    requires(coherence_protocol_required=CoherenceProtocol.MESI_THREE_LEVEL)
    from gem5.components.cachehierarchies.ruby.mesi_three_level_cache_hierarchy import (
        MESIThreeLevelCacheHierarchy,
    )

    class MESIThreeLevelHoleAware(MESIThreeLevelCacheHierarchy):
        @overrides(MESIThreeLevelCacheHierarchy)
        def incorporate_cache(self, board):
            super().incorporate_cache(board)
            channel_ranges = board.get_memory().get_channel_guest_ranges()
            if len(channel_ranges) != len(self._directory_controllers):
                raise RuntimeError(
                    "Ruby directory count does not match DRAM channel count"
                )
            for directory, ranges in zip(
                self._directory_controllers, channel_ranges
            ):
                directory.addr_ranges = ranges
                directory.directory.addr_ranges = ranges

    cache = MESIThreeLevelHoleAware(
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
    return cache, memory


def disable_kvm_perf(processor, enabled):
    for cores in processor._switchable_cores.values():
        for core in cores:
            simobject = core.get_simobject()
            if hasattr(simobject, "usePerf"):
                simobject.usePerf = enabled


def configure_o3_rob(processor, entries):
    if entries <= 0:
        fail("--rob-entries must be positive")
    configured = 0
    for cores in processor._switchable_cores.values():
        for core in cores:
            simobject = core.get_simobject()
            if hasattr(simobject, "numROBEntries"):
                simobject.numROBEntries = entries
                configured += 1
    if configured == 0 and entries != 192:
        fail("non-default --rob-entries requested but no O3 cores were found")
    if configured:
        print(
            f"[fs-ckpt] configured O3 ROB entries={entries} "
            f"cores={configured}"
        )


def write_metadata(
    checkpoint_dir, args, profile, creation_cpu_type, extra=None
):
    metadata = {
        "schema": METADATA_SCHEMA,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "gem5_version": m5_core.gem5Version,
        "gem5_executable": str(Path("/proc/self/exe").resolve()),
        "config_script": str(Path(sys.argv[0]).resolve()),
        "host": {
            "machine": platform.machine(),
            "system": platform.system(),
            "release": platform.release(),
        },
        "creation_cpu_type": creation_cpu_type,
        "kvm_perf": bool(args.kvm_perf),
        "tick": int(m5.curTick()),
        "profile": profile,
    }
    if extra:
        metadata.update(extra)
    metadata_path = checkpoint_dir / "metadata.json"
    with metadata_path.open("w") as metadata_file:
        json.dump(metadata, metadata_file, indent=2, sort_keys=True)
        metadata_file.write("\n")
    print(f"[fs-ckpt] metadata written: {metadata_path}")


def local_resource_paths(args):
    if args.resource_dir is None:
        fail("--resource-dir is required for offline workload use")
    resource_dir = args.resource_dir.resolve()
    return (
        resource_dir / f"{args.kernel_id}-{args.kernel_version}",
        resource_dir / f"{args.disk_image_id}-{args.disk_image_version}",
    )


def validate_local_resources(args):
    kernel_path, disk_image_path = local_resource_paths(args)
    missing = [
        str(path)
        for path in (kernel_path, disk_image_path)
        if not path.is_file() or path.stat().st_size == 0
    ]
    if missing:
        fail(
            "local Ubuntu workload is incomplete; run the shell script's "
            f"download action first; missing={missing}"
        )
    return kernel_path, disk_image_path


def set_local_workload(board, args, readfile=None):
    """Bind the downloaded workload without resource API or MD5 work."""
    kernel_path, disk_image_path = validate_local_resources(args)
    board.set_kernel_disk_workload(
        kernel=KernelResource(
            local_path=str(kernel_path),
            id=args.kernel_id,
            resource_version=args.kernel_version,
            architecture=ISA.X86,
        ),
        disk_image=DiskImageResource(
            local_path=str(disk_image_path),
            id=args.disk_image_id,
            resource_version=args.disk_image_version,
            root_partition=args.disk_root_partition,
        ),
        # These are the exact additional_params of workload
        # x86-ubuntu-24.04-boot-with-systemd 5.0.0.
        kernel_args=list(DEFAULT_KERNEL_ARGS),
        readfile=str(readfile) if readfile is not None else None,
        checkpoint=(
            args.checkpoint_dir
            if args.action == "restore"
            else None
        ),
    )


def attach_aux_disk(board, aux_disk_path):
    """Attach a second raw disk to the IDE bus so guest sees /dev/sdb."""
    if not aux_disk_path.is_file() or aux_disk_path.stat().st_size == 0:
        fail(f"--aux-disk not a valid file: {aux_disk_path}")
    aux = IdeDisk()
    aux.driveID = "device1"
    aux.image = CowDiskImage(
        child=RawDiskImage(read_only=True), read_only=False
    )
    aux.image.child.image_file = str(aux_disk_path)
    existing = list(board.pc.south_bridge.ide.disks)
    if len(existing) != 1:
        fail(
            "expected exactly one IDE disk attached before adding aux disk; "
            f"found {len(existing)}"
        )
    # Give the aux disk a hard parent path in the SimObject tree before the
    # VectorParam reassignment, otherwise gem5 raises "orphan IdeDisk" during
    # instantiate. The board is the natural owner (mirrors how the primary IDE
    # disk is owned via ide.disks).
    board.aux_ide_disk = aux
    board.pc.south_bridge.ide.disks = existing + [aux]
    print(f"[fs-ckpt] aux disk attached as /dev/sdb: {aux_disk_path}")


def main():
    args = parse_args()
    if args.roi_user_records_per_core > 0:
        if args.action != "restore":
            fail("--roi-user-records-per-core is restore-only")
        if args.roi_safety_max_insts_per_core <= 0:
            fail(
                "--roi-user-records-per-core requires a positive "
                "--roi-safety-max-insts-per-core"
            )
        if args.max_insts_per_core > 0:
            fail(
                "--roi-user-records-per-core and --max-insts-per-core are "
                "mutually exclusive"
            )
    if args.action in ("create", "restore") and args.checkpoint_dir is None:
        fail("--checkpoint-dir is required for create/restore")
    if args.action == "create-roi" and args.roi_checkpoint_dir is None:
        fail("--roi-checkpoint-dir is required for create-roi")
    if args.checkpoint_dir is not None:
        args.checkpoint_dir = args.checkpoint_dir.resolve()
    if args.roi_checkpoint_dir is not None:
        args.roi_checkpoint_dir = args.roi_checkpoint_dir.resolve()
    profile = checkpoint_profile(args)
    switch_on_workbegin = False
    resume_at_roi = False
    wait_for_roi_workbegin = False
    switch_to_detailed = False

    if args.action == "prepare":
        kernel_path, disk_image_path = validate_local_resources(args)
        print(
            "[fs-ckpt] local workload ready (offline, no MD5): "
            f"kernel={kernel_path} disk={disk_image_path}"
        )
        return
    elif args.action == "download":
        requires(isa_required=ISA.X86)
        current_cpu_type = CPUTypes.ATOMIC
    elif args.action == "create":
        if args.checkpoint_dir.exists():
            fail(
                f"refusing to overwrite existing path: {args.checkpoint_dir}"
            )
        memory_size = toMemorySize(args.mem_size)
        if args.create_cpu_type == "kvm":
            validate_kvm_device()
            requires(isa_required=ISA.X86, kvm_required=True)
            current_cpu_type = CPUTypes.KVM
        else:
            requires(isa_required=ISA.X86)
            current_cpu_type = CPUTypes.ATOMIC
            if memory_size > toMemorySize("3GiB"):
                fail(
                    "Atomic CPU checkpoint creation is only supported with "
                    "--mem-size <= 3GiB; the >3GiB PCI hole remapping relies "
                    "on KVM-specific backing-store logic"
                )
    elif args.action == "create-roi":
        if args.roi_checkpoint_dir.exists():
            fail(
                "refusing to overwrite existing ROI checkpoint path: "
                f"{args.roi_checkpoint_dir}"
            )
        validate_kvm_device()
        requires(isa_required=ISA.X86, kvm_required=True)
        current_cpu_type = CPUTypes.KVM
        starting_cpu_type = CPUTypes.KVM
        switch_cpu_type = get_cpu_type_from_str(args.roi_target_cpu_type)
    else:
        validate_checkpoint_for_restore(args.checkpoint_dir, profile)
        current_cpu_type = get_cpu_type_from_str(args.restore_cpu_type)
        if current_cpu_type == CPUTypes.KVM:
            validate_kvm_device()
        # switch-on-workbegin: start restore with a fast CPU (KVM ideally, but
        # Ruby cache warmup during checkpoint restore is incompatible with
        # KVM's tick-zero clock reset on this gem5 build -- so we fall back to
        # Atomic as the "warm-up" CPU) and hand off to the requested
        # restore-cpu-type on WORKBEGIN.
        switch_on_workbegin = bool(args.switch_on_workbegin)
        resume_at_roi = bool(args.resume_at_roi)
        wait_for_roi_workbegin = bool(args.wait_for_roi_workbegin)
        if wait_for_roi_workbegin and not resume_at_roi:
            fail("--wait-for-roi-workbegin requires --resume-at-roi")
        if switch_on_workbegin and resume_at_roi:
            fail("--switch-on-workbegin and --resume-at-roi are exclusive")
        if switch_on_workbegin or resume_at_roi:
            if current_cpu_type == CPUTypes.KVM:
                fail(
                    "phased execution requires a timing/O3 target CPU, not kvm"
                )
            starting_cpu_type = CPUTypes.ATOMIC
            switch_cpu_type = current_cpu_type
            switch_to_detailed = True
            requires(isa_required=ISA.X86)
        else:
            starting_cpu_type = current_cpu_type
            switch_cpu_type = current_cpu_type
            requires(
                isa_required=ISA.X86,
                kvm_required=(current_cpu_type == CPUTypes.KVM),
            )

    if args.action in ("download", "create"):
        starting_cpu_type = current_cpu_type
        switch_cpu_type = current_cpu_type
        switch_on_workbegin = False
        switch_to_detailed = False
    elif args.action == "create-roi":
        switch_on_workbegin = False
        switch_to_detailed = False

    cache_hierarchy, memory = make_cache_and_memory(args)
    processor = SimpleSwitchableProcessor(
        starting_core_type=starting_cpu_type,
        switch_core_type=switch_cpu_type,
        isa=ISA.X86,
        num_cores=args.num_cores,
    )
    configure_o3_rob(processor, args.rob_entries)
    disable_kvm_perf(processor, args.kvm_perf)

    board = HoleAwareX86Board(
        clk_freq=args.clk,
        processor=processor,
        memory=memory,
        cache_hierarchy=cache_hierarchy,
    )

    if args.action == "download":
        workload = obtain_resource(
            args.workload_id,
            resource_directory=(
                str(args.resource_dir.resolve()) if args.resource_dir else None
            ),
            resource_version=args.workload_version,
        )
        board.set_workload(workload)
        print(
            f"[fs-ckpt] workload ready: {args.workload_id} "
            f"version={args.workload_version} resource_dir={args.resource_dir}"
        )
        return

    restore_readfile = (
        args.readfile
        if args.action in ("restore", "create", "create-roi")
        else None
    )
    if restore_readfile is not None:
        restore_readfile = restore_readfile.resolve()
        if not restore_readfile.is_file():
            fail(f"--readfile not found: {restore_readfile}")
    set_local_workload(board, args, readfile=restore_readfile)
    board.init_param = 1 if args.action in ("restore", "create-roi") else 0
    print(
        f"[fs-ckpt] guest init_param={board.init_param} "
        f"({'restore' if args.action == 'restore' else 'create'})"
    )

    if args.action in ("restore", "create", "create-roi") \
            and args.aux_disk is not None:
        attach_aux_disk(board, args.aux_disk.resolve())

    # AbstractBoard initializes its memory ranges from set_workload(), so this
    # must run after set_local_workload(). KVM bypasses Ruby and maps
    # PhysicalMemory backing stores directly. The eight DRAM controllers keep
    # a contiguous internal 4 GiB backing store, while the guest sees the x86
    # low/high layout. The accompanying KvmVM extension maps those same bytes
    # into two KVM memslots: [0, 3 GiB) and [4, 5 GiB).
    if starting_cpu_type == CPUTypes.KVM:
        if not hasattr(processor.kvm_vm, "memoryRanges"):
            fail(
                "gem5 binary lacks KvmVM.memoryRanges; rebuild the patched "
                "X86_MESI_Three_Level binary before creating checkpoints"
            )
        guest_ranges = memory.get_uninterleaved_range()
        if not guest_ranges:
            fail("KVM guest memory ranges were not initialized by the board")
        guest_size = sum(mem_range.size() for mem_range in guest_ranges)
        if guest_size != memory.get_size():
            fail(
                "KVM guest memory ranges total "
                f"{guest_size} bytes, expected {memory.get_size()} bytes"
            )
        processor.kvm_vm.memoryRanges = guest_ranges

    if args.action == "create":

        # gem5-bridge checkpoint invokes m5_checkpoint (legacy m5 op) which
        # surfaces as ExitEvent.CHECKPOINT via the classic exit event path
        # (hypercall_num=0), NOT as hypercall 7. So we wire the save-and-exit
        # behavior into on_exit_event[ExitEvent.CHECKPOINT] below. Nothing to
        # do at hypercall 2 (after_boot start) -- just let boot continue.
        _saved = {"done": False}

        def _on_checkpoint():
            while True:
                if _saved["done"]:
                    print("[fs-ckpt] duplicate checkpoint ignored")
                    yield False
                    continue
                print(
                    "[fs-ckpt] ExitEvent.CHECKPOINT fired: saving "
                    f"{args.num_cores}-core checkpoint to "
                    f"{args.checkpoint_dir}"
                )
                args.checkpoint_dir.parent.mkdir(parents=True, exist_ok=True)
                m5.checkpoint(str(args.checkpoint_dir))
                # write_metadata mirrors the pre-create-hook version.
                metadata = {
                    "schema": METADATA_SCHEMA,
                    "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    "gem5_version": m5_core.gem5Version,
                    "gem5_executable": str(Path("/proc/self/exe").resolve()),
                    "config_script": str(Path(sys.argv[0]).resolve()),
                    "host": {
                        "machine": platform.machine(),
                        "system": platform.system(),
                        "release": platform.release(),
                    },
                    "creation_cpu_type": args.create_cpu_type,
                    "kvm_perf": bool(args.kvm_perf),
                    "tick": int(m5.curTick()),
                    "profile": profile,
                }
                (args.checkpoint_dir / "metadata.json").write_text(
                    json.dumps(metadata, indent=2, sort_keys=True) + "\n"
                )
                print(f"[fs-ckpt] checkpoint complete: {args.checkpoint_dir}")
                _saved["done"] = True
                yield True  # exit simulator

    if args.action == "create-roi":
        _roi_saved = {"done": False}

        def _on_roi_checkpoint():
            while True:
                if _roi_saved["done"]:
                    print("[fs-ckpt] duplicate ROI checkpoint marker ignored")
                    yield False
                    continue
                print(
                    "[fs-ckpt] source ROI checkpoint marker reached under KVM: "
                    "saving "
                    f"ROI checkpoint to {args.roi_checkpoint_dir}"
                )
                args.roi_checkpoint_dir.parent.mkdir(
                    parents=True, exist_ok=True
                )
                m5.checkpoint(str(args.roi_checkpoint_dir))
                write_metadata(
                    args.roi_checkpoint_dir,
                    args,
                    profile,
                    "kvm-roi",
                    {
                        "checkpoint_kind": "roi",
                        "prepare_origin": "fresh-kvm-boot",
                        "roi_target_cpu_type": args.roi_target_cpu_type,
                    },
                )
                _roi_saved["done"] = True
                yield True

    simulator_kwargs = {}
    if args.action == "create":
        simulator_kwargs["on_exit_event"] = {
            ExitEvent.CHECKPOINT: _on_checkpoint(),
        }
    if args.action == "create-roi":
        simulator_kwargs["on_exit_event"] = {
            ExitEvent.CHECKPOINT: _on_roi_checkpoint(),
        }
    if args.action == "restore":
        run_state = {
            "workbegin_seen": False,
            "phase": "pre_workbegin",
            "phase_target_insts": 0,
            "phase_core_baselines": [],
            "detailed_cpu_active": False,
            "roi_start_tick": None,
            "roi_start_insts": None,
            "wait_for_roi_workbegin": wait_for_roi_workbegin,
            "roi_complete": False,
        }

        def _copy_stats(reason):
            if args.stats_outfile is None:
                return
            outdir = Path(m5.options.outdir)
            src = outdir / "stats.txt"
            if not src.is_file():
                m5.stats.dump()
            if src.is_file():
                args.stats_outfile.parent.mkdir(parents=True, exist_ok=True)
                args.stats_outfile.write_bytes(src.read_bytes())
                print(f"[fs-ckpt] stats.txt copied to {args.stats_outfile} "
                      f"(reason={reason})")

        def _core_instruction_counts():
            return [
                int(core.get_total_instructions())
                for core in processor.get_cores()
            ]

        def _schedule_max_insts(insts, phase):
            if insts <= 0:
                return
            run_state["phase_target_insts"] = insts
            run_state["phase_core_baselines"] = _core_instruction_counts()
            for core in processor.get_cores():
                core._set_inst_stop_any_thread(insts, True)
            print(
                f"[fs-ckpt] phase={phase} scheduled any-core MAX_INSTS="
                f"{insts} baselines={run_state['phase_core_baselines']}"
            )

        def _phase_instruction_deltas():
            baselines = run_state["phase_core_baselines"]
            current = _core_instruction_counts()
            if len(baselines) != len(current):
                raise RuntimeError(
                    "active core count changed inside an instruction phase: "
                    f"baselines={len(baselines)} current={len(current)}"
                )
            return [
                max(0, count - baseline)
                for count, baseline in zip(current, baselines)
            ]

        def _start_roi():
            run_state["phase"] = "roi"
            run_state["roi_start_tick"] = int(m5.curTick())
            try:
                run_state["roi_start_insts"] = int(
                    board.get_processor().get_total_instructions()
                )
            except Exception:
                run_state["roi_start_insts"] = None
            m5.stats.reset()
            print(
                f"[fs-ckpt] ROI collection started tick={m5.curTick()} "
                f"after stats reset"
            )
            roi_total_limit = (
                args.roi_safety_max_insts_per_core
                if args.roi_user_records_per_core > 0
                else args.max_insts_per_core
            )
            _schedule_max_insts(roi_total_limit, "roi")

        def _start_detailed_warmup():
            if switch_to_detailed and not run_state["detailed_cpu_active"]:
                print(
                    f"[fs-ckpt] switching CPU: {starting_cpu_type.value} "
                    f"-> {switch_cpu_type.value}"
                )
                processor.switch()
                run_state["detailed_cpu_active"] = True
            if run_state["wait_for_roi_workbegin"]:
                run_state["phase"] = "source_warmup"
                m5.stats.reset()
                print(
                    f"[fs-ckpt] source-defined detailed warmup started "
                    f"tick={m5.curTick()} cpu={switch_cpu_type.value} "
                    f"cache={args.cache_hierarchy}; waiting for WORKBEGIN"
                )
                return
            if args.detailed_warmup_insts > 0:
                run_state["phase"] = "detailed_warmup"
                m5.stats.reset()
                print(
                    f"[fs-ckpt] detailed warmup started tick={m5.curTick()} "
                    f"cpu={switch_cpu_type.value} cache={args.cache_hierarchy}"
                )
                _schedule_max_insts(
                    args.detailed_warmup_insts, "detailed_warmup"
                )
            else:
                _start_roi()

        def _on_workbegin():
            # A source marker deliberately has two transports: its serial
            # line is visible under KVM and its address-mode m5op is visible
            # after restore.  Keep this generator alive so the second event
            # is handled here as a duplicate instead of falling through to
            # gem5's default WORKBEGIN handler, which resets ROI stats.
            while True:
                print(f"[fs-ckpt] WORKBEGIN tick={m5.curTick()}")
                if run_state["phase"] == "source_warmup":
                    run_state["workbegin_seen"] = True
                    _start_roi()
                elif run_state["workbegin_seen"]:
                    print("[fs-ckpt] duplicate WORKBEGIN ignored")
                else:
                    run_state["workbegin_seen"] = True
                    if args.atomic_fast_forward_insts > 0:
                        run_state["phase"] = "atomic_fast_forward"
                        m5.stats.reset()
                        print(
                            "[fs-ckpt] atomic fast-forward started "
                            f"tick={m5.curTick()}"
                        )
                        _schedule_max_insts(
                            args.atomic_fast_forward_insts,
                            "atomic_fast_forward",
                        )
                    else:
                        _start_detailed_warmup()
                yield False

        def _on_workend():
            print(
                f"[fs-ckpt] WORKEND tick={m5.curTick()} "
                f"phase={run_state['phase']}"
            )
            m5.stats.dump()
            _copy_stats("workend")
            yield True  # exit simulation

        def _on_max_insts():
            while True:
                phase = run_state["phase"]
                target = run_state["phase_target_insts"]
                deltas = _phase_instruction_deltas()
                exit_code = simulator.get_last_exit_event_code()
                print(
                    f"[fs-ckpt] MAX_INSTS reached at tick={m5.curTick()} "
                    f"phase={phase} target={target} deltas={deltas} "
                    f"exit_code={exit_code}"
                )
                if (
                    phase == "roi"
                    and args.roi_user_records_per_core > 0
                    and exit_code == 86
                ):
                    m5.stats.dump()
                    _copy_stats("roi_user_records")
                    run_state["roi_complete"] = True
                    yield True
                    continue
                if phase == "roi" and args.roi_stop_policy == "all-core":
                    target_reached = (
                        bool(deltas) and min(deltas) >= target
                    )
                else:
                    target_reached = max(deltas, default=0) >= target
                if target > 0 and not target_reached:
                    print(
                        f"[fs-ckpt] stale MAX_INSTS ignored in phase={phase}"
                    )
                    yield False
                    continue
                if phase == "atomic_fast_forward":
                    _start_detailed_warmup()
                    yield False
                elif phase == "detailed_warmup":
                    _start_roi()
                    yield False
                elif phase == "roi":
                    m5.stats.dump()
                    if args.roi_user_records_per_core > 0:
                        _copy_stats("roi_safety_max_insts")
                        print(
                            "[fs-ckpt] ERROR: user-record ROI hit the "
                            "any-core total-instruction safety limit"
                        )
                    else:
                        _copy_stats("roi_max_insts")
                        run_state["roi_complete"] = True
                    yield True
                elif phase == "source_warmup":
                    print(
                        "[fs-ckpt] stale MAX_INSTS ignored while waiting "
                        "for source WORKBEGIN"
                    )
                    yield False
                else:
                    raise RuntimeError(
                        f"unexpected MAX_INSTS event in phase {phase!r}"
                    )

        simulator_kwargs["on_exit_event"] = {
            ExitEvent.WORKBEGIN: _on_workbegin(),
            ExitEvent.WORKEND: _on_workend(),
            ExitEvent.MAX_INSTS: _on_max_insts(),
        }

    simulator = Simulator(board=board, full_system=True, **simulator_kwargs)
    print(
        f"[fs-ckpt] action={args.action} cores={args.num_cores} "
        f"cpu={starting_cpu_type.value}"
        + (f"->{switch_cpu_type.value}" if (
            switch_to_detailed or args.action == "create-roi"
        )
           else "")
        + f" topology={args.cache_hierarchy}"
    )
    if args.action == "restore" and resume_at_roi:
        simulator._instantiate()
        run_state["workbegin_seen"] = True
        print(
            f"[fs-ckpt] resuming source-level ROI checkpoint at "
            f"tick={m5.curTick()}"
        )
        _start_detailed_warmup()
    simulator.run()
    if args.action == "restore" and (args.max_insts_per_core > 0 or \
            args.roi_user_records_per_core > 0) \
            and not run_state["roi_complete"]:
        fail(
            "simulation exited before the requested ROI instruction target: "
            f"phase={run_state['phase']} "
            f"total_target={args.max_insts_per_core} "
            f"user_record_target={args.roi_user_records_per_core} "
            f"safety_target={args.roi_safety_max_insts_per_core} "
            f"policy={args.roi_stop_policy}"
        )
    if args.action == "restore" and args.stats_outfile is not None \
            and not args.stats_outfile.is_file():
        # Fallback if we exited on WORKEND before the stat copy path ran, or
        # on some other terminal cause (e.g. AfterBootScript hypercall_num=3).
        _copy_stats("post_run")
    print(
        f"[fs-ckpt] exit tick={simulator.get_current_tick()} "
        f"cause={simulator.get_last_exit_event_cause()}"
    )


if __name__ == "__m5_main__":
    main()
