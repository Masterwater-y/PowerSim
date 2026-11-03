"""Run with gem5: real stdlib compatibility checks and optional KVM boot.

This checks wiring and guest memory visibility, not simulator accuracy.
"""
import argparse
import json
import os
from pathlib import Path
import re
import runpy

import m5
from m5.objects import HBMCtrl
from m5.params import AddrRange

from gem5.components.boards.x86_board import X86Board
from gem5.components.cachehierarchies.ruby.mesi_three_level_cache_hierarchy import (
    MESIThreeLevelCacheHierarchy,
)
from gem5.components.memory.dram_interfaces.ddr4 import DDR4_2400_8x8
from gem5.components.memory.hbm import HBM2Stack
from gem5.components.memory.memory import ChanneledMemory
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.isas import ISA
from gem5.resources.resource import obtain_resource


def expect_error(function, text):
    try:
        function()
    except ValueError as error:
        assert text in str(error), str(error)
    else:
        raise AssertionError('missing rejection: ' + text)


def check_component_contracts():
    from gem5.components.boards.x86_large_memory_board import X86LargeMemoryBoard
    from gem5.components.memory.x86_multirange_memory import X86MultiRangeDDR4Memory

    hbm = HBM2Stack(size='2GiB')
    hbm.set_memory_range([AddrRange(2 * 2**30)])
    assert len(hbm.get_memory_controllers()) == 8
    assert len(hbm.get_mem_interfaces()) == 16
    assert all(isinstance(ctrl, HBMCtrl) for ctrl in hbm.get_memory_controllers())
    assert all(int(ctrl.dram.range.intlvMatch) % 2 == 0 and
               int(ctrl.dram_2.range.intlvMatch) % 2 == 1
               for ctrl in hbm.get_memory_controllers())
    expect_error(lambda: X86LargeMemoryBoard('3GHz', None, hbm, None), 'DDR4')

    small = X86MultiRangeDDR4Memory(num_channels=8, interleaving_size=64, size='3GiB')
    original = list(small.get_memory_controllers())
    original[0].dram.read_buffer_size = 99
    small.set_memory_range([AddrRange(3 * 2**30)])
    assert all(a is b for a, b in zip(original, small.get_memory_controllers()))
    assert int(small.get_memory_controllers()[0].dram.read_buffer_size) == 99

    large = X86MultiRangeDDR4Memory(num_channels=8, interleaving_size=64, size='32GiB')
    original = list(large.get_memory_controllers())
    expect_error(lambda: large.set_memory_range([]), 'at least one')
    expect_error(lambda: large.set_memory_range([AddrRange(0)]), 'positive')
    expect_error(lambda: large.set_memory_range([
        AddrRange(0, size=32 * 2**30, intlvHighBit=6,
                  intlvBits=1, intlvMatch=0)]), 'interleaved')
    expect_error(lambda: large.set_memory_range([AddrRange(3 * 2**30)]), 'total')
    expect_error(lambda: large.set_memory_range([
        AddrRange(0, size=16 * 2**30), AddrRange(8 * 2**30, size=16 * 2**30)]), 'overlap')
    expect_error(lambda: large.set_memory_range([
        AddrRange(0, size=3 * 2**30 + 1),
        AddrRange(4 * 2**30, size=29 * 2**30 - 1)]), 'multiple')
    assert all(a is b for a, b in zip(original, large.get_memory_controllers()))
    large.set_memory_range([AddrRange(0, size=3 * 2**30),
                            AddrRange(4 * 2**30, size=29 * 2**30)])
    controllers = large.get_memory_controllers()
    assert len(controllers) == len(large.get_mem_ports()) == 16
    assert sum(ctrl.dram.range.size() for ctrl in controllers) == 32 * 2**30
    for index, ctrl in enumerate(controllers):
        assert int(ctrl.dram.range.start) == (0 if index < 8 else 4 * 2**30)
        assert int(ctrl.dram.range.intlvMatch) == index % 8
    row_striped = X86MultiRangeDDR4Memory(
        num_channels=8, interleaving_size=64, size='32GiB', addr_mapping='RoRaBaChCo')
    before = list(row_striped.get_memory_controllers())
    # Row-buffer striping uses 8KiB * 8 = 64KiB periods, not 64B * 8.
    expect_error(lambda: row_striped.set_memory_range([
        AddrRange(0, size=3 * 2**30 + 512),
        AddrRange(4 * 2**30, size=29 * 2**30 - 512)]), 'multiple')
    assert all(a is b for a, b in zip(before, row_striped.get_memory_controllers()))
    row_striped.set_memory_range([AddrRange(0, size=3 * 2**30),
                                 AddrRange(4 * 2**30, size=29 * 2**30)])
    assert set(row_striped.get_memory_controllers()[0].dram.range.masks) == {
        1 << 13, 1 << 14, 1 << 15}
    print('X86_LARGE_MEMORY_COMPONENT_CONTRACTS_OK', flush=True)


GUEST_REPORT = '''#!/bin/sh
swapoff -a
echo FASTSIM_MEMORY_REPORT_BEGIN
free -m
grep -E 'MemTotal|MemFree|MemAvailable|SwapTotal' /proc/meminfo
dmesg | grep -iE 'BIOS-e820|Memory: [0-9]' | head -20
cat /proc/iomem
echo FASTSIM_MEMORY_REPORT_END
sleep 2
m5 exit
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mem-size', default='3GiB')
    parser.add_argument('--mem-channels', type=int, default=8)
    parser.add_argument('--num-cpus', type=int, default=8)
    parser.add_argument('--x86-large-memory', action='store_true')
    parser.add_argument('--ruby-functional-backing-store', action='store_true')
    parser.add_argument('--boot', action='store_true')
    parser.add_argument('--legacy-only', action='store_true',
                        help='compare a frozen pre-feature binary using only original components')
    args = parser.parse_args()
    root_path = Path(__file__).resolve().parents[2]
    runner = runpy.run_path(str(root_path / 'scripts/run_gem5_sync_probe.py'))
    assert 'validate_x86_memory_options' in runner, 'missing explicit memory policy'
    active = runner['validate_x86_memory_options'](args)
    board_class, memory_class = runner['select_x86_memory_components'](active)
    if not active:
        assert board_class is X86Board and memory_class is ChanneledMemory
    if args.legacy_only:
        assert not active, '--legacy-only cannot validate the new component path'
    else:
        check_component_contracts()
    kwargs = dict(num_channels=args.mem_channels, interleaving_size=64, size=args.mem_size)
    if not active:
        kwargs['dram_interface_class'] = DDR4_2400_8x8
    memory = memory_class(**kwargs)
    processor = SimpleProcessor(cpu_type=CPUTypes.KVM, isa=ISA.X86, num_cores=args.num_cpus)
    cache = MESIThreeLevelCacheHierarchy(
        l1i_size='32KiB', l1i_assoc=8, l1d_size='32KiB', l1d_assoc=8,
        l2_size='1MiB', l2_assoc=8, l3_size='8MiB', l3_assoc=16, num_l3_banks=8)
    board = board_class(clk_freq='3GHz', processor=processor,
                        memory=memory, cache_hierarchy=cache)
    board.set_kernel_disk_workload(
        kernel=obtain_resource('x86-linux-kernel-5.4.49'),
        disk_image=obtain_resource('x86-ubuntu-18.04-img'),
        readfile_contents=GUEST_REPORT, exit_on_work_items=True)
    root = board._pre_instantiate(full_system=True)
    root.sim_quantum = m5.ticks.fromSeconds(100e-6)
    m5.instantiate()
    ranges = [{'start': int(r.start), 'size': int(r.size())}
              for r in board.get_mem_ranges()]
    size = runner['parse_size_bytes'](args.mem_size)
    expected = ([{'start': 0, 'size': 3 * 2**30},
                 {'start': 4 * 2**30, 'size': size - 3 * 2**30}]
                if active else [{'start': 0, 'size': size}])
    assert ranges == expected, (ranges, expected)
    count = args.mem_channels * (2 if active else 1)
    assert len(memory.get_memory_controllers()) == count
    assert len(cache._directory_controllers) == count
    e820 = [{'start': int(e.addr), 'size': int(e.size), 'type': int(e.range_type)}
            for e in board.workload.e820_table.entries]
    assert len(e820) == (5 if active else 4), e820
    if active:
        assert e820[-1] == dict(start=4 * 2**30, size=size - 3 * 2**30, type=1)
    result = dict(valid=True, scope='stdlib contracts/wiring, not accuracy',
                  source_override=os.environ.get('M5_OVERRIDE_PY_SOURCE', '').lower() in ('true', 'yes'),
                  requested_large_memory=args.x86_large_memory, active_large_memory=active,
                  data_ranges=ranges, memory_controllers=count, ruby_directories=count,
                  e820=e820, hbm_legacy_contract=None if args.legacy_only else True)
    if args.boot:
        event = m5.simulate(m5.ticks.fromSeconds(120.0))
        result['exit_cause'] = event.getCause()
        assert result['exit_cause'] == 'm5_exit instruction encountered', result
        uart = (Path(m5.options.outdir) / 'board.pc.com_1.device').read_text()
        assert 'FASTSIM_MEMORY_REPORT_END' in uart
        memtotal = re.search(r'MemTotal:\s*(\d+) kB', uart)
        swap = re.search(r'SwapTotal:\s*(\d+) kB', uart)
        assert memtotal and int(memtotal.group(1)) > size // 2048
        assert swap and int(swap.group(1)) == 0
        result['guest_memtotal_kb'] = int(memtotal.group(1))
        result['guest_swap_total_kb'] = int(swap.group(1))
        if active:
            assert '100000000-83fffffff : System RAM' in uart if size == 32 * 2**30 else True
        assert not any(text in uart for text in ('Kernel panic', 'Out of memory:'))
        result['guest_report_complete'] = True
    (Path(m5.options.outdir) / 'large-memory-validation.json').write_text(
        json.dumps(result, indent=2) + '\n')
    print('X86_LARGE_MEMORY_VALIDATION_OK ' + json.dumps(result), flush=True)


if __name__ == '__m5_main__':
    main()
