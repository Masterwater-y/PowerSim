"""Explicit DDR4 multi-range memory; legacy ChanneledMemory/HBM stay intact."""
from math import log2

from m5.objects import MemCtrl
from m5.params import AddrRange

from .dram_interfaces.ddr4 import DDR4_2400_8x8
from .memory import ChanneledMemory


class X86MultiRangeDDR4Memory(ChanneledMemory):
    """One set of DDR4 channels per RAM range, configured before board wiring.

    ``num_channels`` is channels per range, not total controllers. A single
    range delegates to the unchanged base implementation and retains the
    original controller objects and any parameters set on them by callers.
    """

    def __init__(self, num_channels, interleaving_size, size, addr_mapping=None):
        channels = int(num_channels)
        if channels <= 0 or channels & (channels - 1):
            raise ValueError('DDR4 channel count must be a positive power of two')
        self._x86_ranges = []
        super().__init__(DDR4_2400_8x8, channels, interleaving_size,
                         size=size, addr_mapping=addr_mapping)

    def set_memory_range(self, ranges):
        ranges = list(ranges)
        if not ranges:
            raise ValueError('DDR4 requires at least one RAM range')
        if any(r.intlvBits for r in ranges):
            raise ValueError('RAM input ranges must not be interleaved')
        if any(r.size() <= 0 for r in ranges):
            raise ValueError('RAM ranges must have positive size')
        if sum(r.size() for r in ranges) != self._size:
            raise ValueError('RAM range total must equal configured memory size')
        ordered = sorted(ranges, key=lambda r: int(r.start))
        if any(int(a.end) > int(b.start) for a, b in zip(ordered, ordered[1:])):
            raise ValueError('RAM ranges must not overlap')
        granularity = (1 << self._interleave_low_bit()) * self._num_channels
        if any(int(r.start) % granularity or r.size() % granularity for r in ranges):
            raise ValueError('RAM range starts and sizes must be a multiple of '
                             'the address mapping stripe period')
        if len(ranges) == 1:
            self._x86_ranges = ranges
            super().set_memory_range(ranges)
            return
        # Do not replace the base class's controller factory: HBM overrides
        # that factory and is intentionally outside this independent class.
        dram = [self._dram_class(addr_mapping=self._addr_mapping)
                for _ in range(len(ranges) * self._num_channels)]
        controllers = [MemCtrl(dram=interface) for interface in dram]
        self._x86_ranges = ranges
        self._dram = dram
        self.mem_ctrl = controllers
        self._interleave_addresses()

    def _interleave_low_bit(self):
        if self._addr_mapping == 'RoRaBaChCo':
            rowbuffer = (self._dram_class.device_rowbuffer_size.value *
                         self._dram_class.devices_per_rank.value)
            return int(log2(rowbuffer))
        elif self._addr_mapping in ('RoRaBaCoCh', 'RoCoRaBaCh'):
            return int(log2(self._intlv_size))
        else:
            raise ValueError('unsupported DDR4 address mapping: ' + self._addr_mapping)

    def _interleave_addresses(self):
        if len(self._x86_ranges) <= 1:
            return super()._interleave_addresses()
        low_bit = self._interleave_low_bit()
        bits = int(log2(self._num_channels))
        for segment, address_range in enumerate(self._x86_ranges):
            for channel in range(self._num_channels):
                controller = self.mem_ctrl[segment * self._num_channels + channel]
                controller.dram.range = AddrRange(
                    start=address_range.start, size=address_range.size(),
                    intlvHighBit=low_bit + bits - 1, xorHighBit=0,
                    intlvBits=bits, intlvMatch=channel)

    def get_uninterleaved_range(self):
        return list(self._x86_ranges)
