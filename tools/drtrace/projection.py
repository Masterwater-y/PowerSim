from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


FST_HEADER = struct.Struct("<8sIIIIQQQQQQ")
FST_RECORD = struct.Struct("<QQQQ4IHHhBB4BI")
FST_MAGIC = b"FSTRC01\0"
FST_VERSION = 6
FEATURE_DESTINATION_CLASSES = 1 << 2
LOAD = 1 << 1
STORE = 1 << 2
ATOMIC = 1 << 3
PHYSICAL_ADDRESS = 1 << 12
VIRTUAL_PAGE_TOKEN = 1 << 15
DESTINATION_CLASS_MARKER = 1 << 31
MEMORY_FLAGS = LOAD | STORE | ATOMIC
CACHE_LINE_SIZE = 64


@dataclass(frozen=True)
class FstInfo:
    core_id: int
    records: int
    features: int


def fst_info(path: Path) -> FstInfo:
    with path.open("rb") as source:
        raw = source.read(FST_HEADER.size)
    if len(raw) != FST_HEADER.size:
        raise ValueError(f"truncated FST header: {path}")
    magic, version, header_size, record_size, core, count, features, *_ = (
        FST_HEADER.unpack(raw)
    )
    if (
        magic != FST_MAGIC
        or version != FST_VERSION
        or header_size != FST_HEADER.size
        or record_size != FST_RECORD.size
    ):
        raise ValueError(f"invalid FastSim FST v6 header: {path}")
    if not features & FEATURE_DESTINATION_CLASSES:
        raise ValueError(f"FST lacks destination-class metadata: {path}")
    expected = FST_HEADER.size + count * FST_RECORD.size
    if path.stat().st_size != expected:
        raise ValueError(f"FST size/count mismatch: {path}")
    return FstInfo(core_id=core, records=count, features=features)


def iter_fst(path: Path) -> Iterator[tuple[int, ...]]:
    info = fst_info(path)
    with path.open("rb") as source:
        source.seek(FST_HEADER.size)
        for _ in range(info.records):
            raw = source.read(FST_RECORD.size)
            if len(raw) != FST_RECORD.size:
                raise ValueError(f"truncated FST record: {path}")
            yield FST_RECORD.unpack(raw)


FST_FIELD_NAMES = (
    "pc",
    "address",
    "target",
    "next_pc",
    "producer_dist0",
    "producer_dist1",
    "producer_dist2",
    "producer_dist3",
    "size",
    "flags",
    "op_class",
    "n_src",
    "n_dst",
    "producer_class0",
    "producer_class1",
    "producer_class2",
    "producer_class3",
    "reserved",
)
