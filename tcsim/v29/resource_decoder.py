"""gem5-compatible cache/DRAM physical-resource decoder.

The implementation mirrors ``AddrRange::contains/removeIntlvBits/getOffset``
and ``DRAMInterface::decodePacket``.  Unsupported or underspecified mappings
fail closed; v29 never falls back to fabricated modulo defaults.
"""
from __future__ import annotations

import configparser
import copy
import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .contracts import RESOURCE_DECODER_SCHEMA_VERSION


def _int(value: Any) -> int:
    if isinstance(value, int):
        return value
    return int(str(value).strip(), 0)


def _power_of_two(value: int, name: str) -> int:
    value = int(value)
    if value <= 0 or value & (value - 1):
        raise ValueError(f"{name} must be a positive power of two, got {value}")
    return value


def _ceil_power_of_two(value: int, name: str) -> int:
    value = int(value)
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return 1 << (value - 1).bit_length()


def _lsb_index(value: int) -> int:
    if value <= 0:
        raise ValueError("interleave mask must be nonzero")
    return (int(value) & -int(value)).bit_length() - 1


def _popcount(value: int) -> int:
    # ``int.bit_count`` is unavailable in the system Python used by some
    # collection hosts.  The decoder must remain usable before tensor-cache
    # construction selects the newer training venv.
    return bin(int(value) & ((1 << 64) - 1)).count("1")


@dataclass(frozen=True)
class AddrRangeSpec:
    start: int
    end: int
    intlv_match: int = 0
    masks: Tuple[int, ...] = ()

    @classmethod
    def parse(cls, value: str) -> "AddrRangeSpec":
        parts = [part.strip() for part in str(value).split(":")]
        if len(parts) == 2:
            return cls(_int(parts[0]), _int(parts[1]))
        if len(parts) < 4:
            raise ValueError(f"unsupported gem5 AddrRange encoding {value!r}")
        return cls(
            _int(parts[0]),
            _int(parts[1]),
            _int(parts[2]),
            tuple(_int(part) for part in parts[3:]),
        )

    @property
    def interleaved(self) -> bool:
        return bool(self.masks)

    @property
    def size(self) -> int:
        """Mirror gem5 ``AddrRange::size`` for one interleaved stripe."""
        span = int(self.end) - int(self.start)
        if span <= 0:
            raise ValueError(f"invalid gem5 address range [{self.start},{self.end})")
        stripes = 1 << len(self.masks)
        if span % stripes:
            raise ValueError(
                f"address-range span {span} is not divisible by {stripes} stripes"
            )
        return span // stripes

    @property
    def granularity(self) -> int:
        if not self.masks:
            return self.end - self.start
        combined = 0
        for mask in self.masks:
            combined |= int(mask)
        return 1 << _lsb_index(combined)

    def selector(self, address: int) -> int:
        selected = 0
        for index, mask in enumerate(self.masks):
            selected |= (_popcount(int(address) & int(mask)) & 1) << index
        return selected

    def contains(self, address: int) -> bool:
        address = int(address)
        return (
            self.start <= address < self.end
            and (not self.masks or self.selector(address) == self.intlv_match)
        )

    def remove_intlv_bits(self, address: int) -> int:
        address = int(address)
        if not self.masks:
            return address
        for removed, bit in enumerate(sorted(_lsb_index(mask) for mask in self.masks)):
            shifted_bit = bit - removed
            low_mask = (1 << shifted_bit) - 1
            address = ((address >> (shifted_bit + 1)) << shifted_bit) | (address & low_mask)
        return address

    def get_offset(self, address: int) -> int:
        if not self.contains(address):
            raise ValueError(f"address {address:#x} is not in controller range {self}")
        return self.remove_intlv_bits(address) - self.remove_intlv_bits(self.start)


@dataclass(frozen=True)
class DramControllerSpec:
    channel: int
    range: AddrRangeSpec


@dataclass(frozen=True)
class DramGeometry:
    addr_mapping: str
    burst_size_b: int
    row_buffer_size_b: int
    bursts_per_row_buffer: int
    banks_per_rank: int
    ranks_per_channel: int
    rows_per_bank: int
    assigned_capacity_per_channel_b: int
    device_capacity_per_channel_b: int
    controllers: Tuple[DramControllerSpec, ...]

    @property
    def num_channels(self) -> int:
        return len(self.controllers)


@dataclass(frozen=True)
class DecodedResources:
    physical_line: int
    l1_set: int
    l2_set: int
    llc_set: int
    llc_bank: int
    dram_channel: int
    dram_rank: int
    dram_bank: int
    dram_row: int
    dram_column: int


class Gem5AddressDecoder:
    """Decode the exact resource tuple used by the collected gem5 config."""

    def __init__(self, profile: Mapping[str, Any], geometry: DramGeometry) -> None:
        self.profile = copy.deepcopy(dict(profile))
        self.geometry = geometry
        self.line_size_b = _power_of_two(
            int(self.profile["cache"]["l1d"]["line_b"]), "cache line size"
        )
        if self.line_size_b != geometry.burst_size_b:
            raise ValueError(
                "v29 currently requires one cache line per DRAM burst: "
                f"line={self.line_size_b}, burst={geometry.burst_size_b}"
            )
        self.line_bits = int(math.log2(self.line_size_b))
        self.l1_sets = self._sets("l1d", per_bank=False)
        self.l2_sets = self._sets("l2", per_bank=False)
        l3 = self.profile["cache"]["l3"]
        self.llc_banks = _power_of_two(int(l3["num_banks"]), "L3 banks")
        self.llc_bank_low_bit = int(l3.get("bank_select_low_bit", self.line_bits))
        if self.llc_bank_low_bit != self.line_bits:
            raise ValueError(
                "unsupported MESI_Three_Level L3 bank-select bit: "
                f"{self.llc_bank_low_bit}; expected line bit {self.line_bits}"
            )
        self.llc_sets = self._sets("l3", per_bank=True)
        self._controller_by_match = {
            spec.range.intlv_match: spec for spec in geometry.controllers
        }
        self._validate()

    def _sets(self, level: str, *, per_bank: bool) -> int:
        cfg = self.profile["cache"][level]
        size = int(cfg["size_b"])
        assoc = int(cfg["assoc"])
        banks = int(cfg.get("num_banks", 1)) if per_bank else 1
        denom = assoc * self.line_size_b * banks
        if size <= 0 or size % denom:
            raise ValueError(f"invalid {level} size/assoc/line/bank geometry")
        return _power_of_two(size // denom, f"{level} sets per bank")

    def _validate(self) -> None:
        g = self.geometry
        if g.addr_mapping not in {"RoRaBaCoCh", "RoRaBaChCo"}:
            raise ValueError(
                f"unsupported DRAM addr_mapping={g.addr_mapping!r}; "
                "v29 fails closed instead of approximating"
            )
        for name, value in (
            ("burst size", g.burst_size_b),
            ("row buffer size", g.row_buffer_size_b),
            ("bursts per row", g.bursts_per_row_buffer),
            ("banks per rank", g.banks_per_rank),
            ("ranks per channel", g.ranks_per_channel),
        ):
            _power_of_two(value, name)
        matches = sorted(spec.range.intlv_match for spec in g.controllers)
        if matches != list(range(g.num_channels)):
            raise ValueError(f"DRAM channel matches must be 0..N-1, got {matches}")
        common_masks = {spec.range.masks for spec in g.controllers}
        common_bounds = {(spec.range.start, spec.range.end) for spec in g.controllers}
        if len(common_masks) != 1 or len(common_bounds) != 1:
            raise ValueError("all DRAM controllers must share one interleave geometry")
        masks = next(iter(common_masks))
        if len(masks) != int(math.log2(g.num_channels)):
            raise ValueError(
                f"channel mask count {len(masks)} != log2(channels={g.num_channels})"
            )
        for spec in g.controllers:
            if spec.range.granularity % g.burst_size_b:
                raise ValueError("DRAM interleave granularity is not burst-aligned")
            if spec.range.size != g.assigned_capacity_per_channel_b:
                raise ValueError("DRAM controllers have inconsistent assigned capacity")
            if spec.range.interleaved and g.addr_mapping == "RoRaBaChCo":
                if spec.range.granularity != g.row_buffer_size_b:
                    raise ValueError(
                        "RoRaBaChCo requires channel interleaving at row-buffer size"
                    )
            if spec.range.interleaved and g.addr_mapping == "RoRaBaCoCh":
                if not g.burst_size_b <= spec.range.granularity <= g.row_buffer_size_b:
                    raise ValueError(
                        "RoRaBaCoCh channel stripe must be between burst and row-buffer size"
                    )
        expected_rows = g.assigned_capacity_per_channel_b // (
            g.row_buffer_size_b * g.banks_per_rank * g.ranks_per_channel
        )
        if expected_rows <= 0 or expected_rows != g.rows_per_bank:
            raise ValueError(
                f"invalid gem5 rows-per-bank geometry {g.rows_per_bank} != {expected_rows}"
            )

    @classmethod
    def from_config(
        cls, profile: Mapping[str, Any], config_path: str,
    ) -> "Gem5AddressDecoder":
        if not os.path.isfile(config_path):
            raise FileNotFoundError(config_path)
        parser = configparser.RawConfigParser(interpolation=None, strict=False)
        parser.read(config_path)
        sections = [
            section for section in parser.sections()
            if parser.has_option(section, "addr_mapping")
            and parser.has_option(section, "banks_per_rank")
            and parser.has_option(section, "range")
        ]
        if not sections:
            raise ValueError(f"no DRAMInterface sections in {config_path}")

        geometries = []
        controllers = []
        for section in sorted(sections):
            range_spec = AddrRangeSpec.parse(parser.get(section, "range"))
            devices_per_rank = parser.getint(section, "devices_per_rank")
            burst_length = parser.getint(section, "burst_length")
            device_bus_width = parser.getint(section, "device_bus_width")
            burst_size = devices_per_rank * burst_length * device_bus_width // 8
            device_rowbuffer = parser.getint(section, "device_rowbuffer_size")
            row_buffer = devices_per_rank * device_rowbuffer
            banks = parser.getint(section, "banks_per_rank")
            ranks = parser.getint(section, "ranks_per_channel")
            device_size = parser.getint(section, "device_size")
            assigned_capacity = _ceil_power_of_two(
                range_spec.size, "assigned DRAM controller capacity",
            )
            device_capacity = device_size * devices_per_rank * ranks
            # gem5 DRAMInterface does *not* derive rowsPerBank from the DRAM
            # chip geometry.  It rounds AbstractMemory::size() up to a power
            # of two and divides that assigned controller capacity by the row,
            # bank and rank geometry (dram_interface.cc).  The old v29 code
            # used device_size and therefore wrote incorrect provenance.
            rows = assigned_capacity // (row_buffer * banks * ranks)
            geometry_key = (
                parser.get(section, "addr_mapping"), burst_size, row_buffer,
                row_buffer // burst_size, banks, ranks, rows,
                assigned_capacity, device_capacity,
            )
            geometries.append(geometry_key)
            controllers.append(DramControllerSpec(
                channel=range_spec.intlv_match,
                range=range_spec,
            ))
        if len(set(geometries)) != 1:
            raise ValueError("heterogeneous DRAM controller geometry is unsupported")
        (
            mapping, burst, row_buffer, bursts_per_row, banks, ranks, rows,
            assigned_capacity, device_capacity,
        ) = geometries[0]
        geometry = DramGeometry(
            addr_mapping=str(mapping),
            burst_size_b=int(burst),
            row_buffer_size_b=int(row_buffer),
            bursts_per_row_buffer=int(bursts_per_row),
            banks_per_rank=int(banks),
            ranks_per_channel=int(ranks),
            rows_per_bank=int(rows),
            assigned_capacity_per_channel_b=int(assigned_capacity),
            device_capacity_per_channel_b=int(device_capacity),
            controllers=tuple(sorted(controllers, key=lambda item: item.channel)),
        )
        return cls(profile, geometry)

    def _controller(self, address: int) -> DramControllerSpec:
        matches = [spec for spec in self.geometry.controllers if spec.range.contains(address)]
        if len(matches) != 1:
            raise ValueError(
                f"physical address {address:#x} maps to {len(matches)} DRAM controllers"
            )
        return matches[0]

    def decode_dram(self, address: int) -> Tuple[int, int, int, int, int]:
        spec = self._controller(address)
        g = self.geometry
        ctrl_addr = spec.range.get_offset(address)
        burst_addr = ctrl_addr // g.burst_size_b
        column = burst_addr % g.bursts_per_row_buffer
        value = burst_addr // g.bursts_per_row_buffer
        bank = value % g.banks_per_rank
        value //= g.banks_per_rank
        rank = value % g.ranks_per_channel
        value //= g.ranks_per_channel
        row = value % g.rows_per_bank
        return spec.channel, rank, bank, row, column

    def decode(self, address: int) -> DecodedResources:
        address = int(address)
        if address < 0:
            raise ValueError("physical address must be non-negative")
        line = address // self.line_size_b
        llc_bank = (address >> self.llc_bank_low_bit) & (self.llc_banks - 1)
        llc_set = (address >> (self.line_bits + int(math.log2(self.llc_banks)))) % self.llc_sets
        channel, rank, bank, row, column = self.decode_dram(address)
        return DecodedResources(
            physical_line=line,
            l1_set=line % self.l1_sets,
            l2_set=line % self.l2_sets,
            llc_set=llc_set,
            llc_bank=llc_bank,
            dram_channel=channel,
            dram_rank=rank,
            dram_bank=bank,
            dram_row=row,
            dram_column=column,
        )

    def metadata(self) -> Dict[str, Any]:
        geometry = asdict(self.geometry)
        geometry["controllers"] = [
            {
                "channel": spec.channel,
                "range": asdict(spec.range),
            }
            for spec in self.geometry.controllers
        ]
        return {
            "schema": RESOURCE_DECODER_SCHEMA_VERSION,
            "line_size_b": self.line_size_b,
            "l1_sets": self.l1_sets,
            "l2_sets": self.l2_sets,
            "llc_sets_per_bank": self.llc_sets,
            "llc_banks": self.llc_banks,
            "llc_bank_select_low_bit": self.llc_bank_low_bit,
            "dram": geometry,
        }

    def provenance_hash(self) -> str:
        blob = json.dumps(self.metadata(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def enriched_profile(self) -> Dict[str, Any]:
        profile = copy.deepcopy(self.profile)
        dram = profile.setdefault("dram", {})
        dram.pop("banks_per_channel", None)
        dram.update({
            "addr_mapping": self.geometry.addr_mapping,
            "num_channels": self.geometry.num_channels,
            "banks_per_rank": self.geometry.banks_per_rank,
            "ranks_per_channel": self.geometry.ranks_per_channel,
            "rows_per_bank": self.geometry.rows_per_bank,
            "burst_size_b": self.geometry.burst_size_b,
            "row_buffer_size_b": self.geometry.row_buffer_size_b,
            "bursts_per_row_buffer": self.geometry.bursts_per_row_buffer,
        })
        profile["resource_decoder"] = self.metadata()
        profile["resource_decoder_hash"] = self.provenance_hash()
        return profile


def trace_config_path(trace_dir: str) -> str:
    return os.path.join(os.path.dirname(trace_dir.rstrip("/")), "config.ini")


def build_decoder_for_trace(
    trace_dir: str, profile: Mapping[str, Any],
) -> Gem5AddressDecoder:
    return Gem5AddressDecoder.from_config(profile, trace_config_path(trace_dir))
