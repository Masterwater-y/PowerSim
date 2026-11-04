"""Configuration-only integration check, run with gem5.opt (never simulates).

gem5.opt --outdir=<fresh-dir> tests/test_spr_speed_capture.py --mem-size 32GiB
"""
import argparse
import json
from pathlib import Path
import sys


def main():
    import m5
    from gem5.components.cachehierarchies.ruby.mesi_three_level_cache_hierarchy import SPRCacheHierarchy
    from gem5.components.processors.cpu_types import CPUTypes
    from gem5.components.processors.simple_switchable_processor import SimpleSwitchableProcessor
    from gem5.isas import ISA
    from gem5.resources.resource import DiskImageResource, KernelResource
    from gem5.simulate.simulator import Simulator
    from gem5.components.memory.dram_interfaces.ddr5 import DDR5_4400_4x8
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "gem5-spr/configs/fastsim"))
    from collection import parse_args, validate_options, native_profile
    from configlarge_memory import make_memory

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mem-size", choices=("2GiB", "32GiB"), required=True)
    parser.add_argument("--kernel", default="/root/.cache/gem5/x86-linux-kernel-5.4.49-1.0.0")
    parser.add_argument("--disk", default="/root/.cache/gem5/x86-ubuntu-18.04-img-1.0.0")
    requested = parser.parse_args()
    args = parse_args(["--fastsim-root", str(root), "--workload-binary", "/unused",
                       "--participant-cores", "4", "--mem-size", requested.mem_size,
                       "--x86-large-memory"])
    validate_options(args)
    board_class, memory = make_memory(args)
    processor = SimpleSwitchableProcessor(starting_core_type=CPUTypes.KVM,
        switch_core_type=CPUTypes.O3, num_cores=4, isa=ISA.X86)
    cache = SPRCacheHierarchy(l3_size="12MiB", l3_assoc=12, num_l3_banks=8)
    board = board_class(clk_freq="2.6GHz", processor=processor,
                        memory=memory, cache_hierarchy=cache)
    board.set_kernel_disk_workload(kernel=KernelResource(local_path=requested.kernel),
        disk_image=DiskImageResource(local_path=requested.disk, root_partition="1"),
        readfile_contents="#!/bin/sh\nexit 0\n")
    simulator = Simulator(board=board)
    simulator._instantiate()  # Emit config.ini and wire native Ruby; no run().
    controllers = memory.get_memory_controllers()
    expected_ranges = ([(0, 3 * 2**30), (4 * 2**30, 29 * 2**30)]
                       if requested.mem_size == "32GiB" else [(0, 2 * 2**30)])
    assert [(int(r.start), r.size()) for r in memory.get_uninterleaved_range()] == expected_ranges
    assert len(controllers) == len(expected_ranges) * 16
    # gem5's SimObject type is inherited DRAMInterface, so check the concrete
    # Python DRAM class and native timing parameters rather than .type.
    assert all(isinstance(ctrl.dram, DDR5_4400_4x8) for ctrl in controllers)
    dirs = cache._directory_controllers
    assert len(dirs) == len(controllers)
    assert sorted(str(d.addr_ranges[0]) for d in dirs) == sorted(str(c.dram.range) for c in controllers)
    cpu = processor._switchable_cores["switch"][0].get_simobject()
    profile = native_profile(cpu, cache, memory, 4)
    assert profile["dram"]["num_channels"] == len(controllers)
    assert profile["core"]["freq_ghz"] == 2.6
    assert profile["cache"]["l3"]["size_b"] == 100663296
    assert int(cpu.numROBEntries) == 512
    assert [int(iq.numEntries) for iq in cpu.instQueues] == [128, 72, 48]
    if requested.mem_size == "32GiB":
        assert any(int(entry.addr) == 4 * 2**30 and str(entry.size) == str(29 * 2**30)
                   and int(entry.range_type) == 1 for entry in board.workload.e820_table.entries)
    result = dict(status="passed", scope="configuration-only; no guest boot or capture",
                  memory=requested.mem_size, controllers=len(controllers),
                  directories=len(dirs), channels_per_range=16,
                  physical_ranges=expected_ranges, profile=profile)
    (Path(m5.options.outdir) / "memory-smoke.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "profile"}))


if __name__ in ("__m5_main__", "__main__"):
    main()
