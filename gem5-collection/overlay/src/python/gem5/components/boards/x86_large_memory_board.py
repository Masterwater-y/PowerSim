"""Opt-in X86 RAM above the PCI hole, without changing the legacy Board."""
from m5.objects import X86E820Entry
from m5.params import AddrRange

from ..memory.x86_multirange_memory import X86MultiRangeDDR4Memory
from .x86_board import X86Board


class X86LargeMemoryBoard(X86Board):
    """DDR4-only split-range Board. Select explicitly for RAM above 3GiB."""

    def __init__(self, clk_freq, processor, memory, cache_hierarchy):
        if not isinstance(memory, X86MultiRangeDDR4Memory):
            raise ValueError('X86LargeMemoryBoard requires X86MultiRangeDDR4Memory; '
                             'HBM and other memory implementations are unsupported')
        super().__init__(clk_freq, processor, memory, cache_hierarchy)

    def _setup_memory_ranges(self):
        memory = self.get_memory()
        size = memory.get_size()
        if size <= 3 * 2**30:
            return super()._setup_memory_ranges()
        ranges = [AddrRange(0, size=3 * 2**30),
                  AddrRange(4 * 2**30, size=size - 3 * 2**30)]
        memory.set_memory_range(ranges)
        self.mem_ranges = ranges + [AddrRange(0xC0000000, size=0x100000)]

    def _setup_io_devices(self):
        super()._setup_io_devices()
        # The base method retains all original low-memory/firmware entries.
        # Its first entry describes low RAM; only add the extra data ranges.
        extra_ranges = self.get_memory().get_uninterleaved_range()[1:]
        if not extra_ranges:
            return
        # A vector with already-parented old entries is not adopted again by
        # SimObject assignment; mixing in new entries leaves orphan children.
        # Preserve the base values in fresh objects before assigning a vector.
        entries = [X86E820Entry(addr=entry.addr, size=entry.size,
                                range_type=entry.range_type)
                   for entry in self.workload.e820_table.entries]
        for address_range in extra_ranges:
            entries.append(X86E820Entry(addr=address_range.start,
                                       size=f'{address_range.size()}B', range_type=1))
        self.workload.e820_table.entries = entries
