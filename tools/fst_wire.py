"""Single Python description and strict reader for the canonical FST v7 ABI."""

import struct
from dataclasses import dataclass
from pathlib import Path

FST_HEADER = struct.Struct("<8sIIIIQQQQQQ")
FST_RECORD = struct.Struct("<QQQQ4IHHhBB4BI")
FST_SYSCALL_METADATA = struct.Struct("<QQQQ6QQQQIIIHBBQ")
FST_ASMAP_HEADER = struct.Struct("<8sIIIIQQQ")
FST_ASMAP_ENTRY = struct.Struct("<QQ")
FST_VMAP_HEADER = struct.Struct("<8sIIIIQQII")
FST_VMAP_ENTRY = struct.Struct("<IIQQQ")
FST_IFMAP_HEADER = struct.Struct("<8sIIIIQQII")
FST_IFMAP_ENTRY = struct.Struct("<QQQQ")
FST_IMAP_HEADER = struct.Struct("<8sIIIIQQII")
FST_IMAP_ENTRY = struct.Struct("<QQQQHBB4xQQQQ")

assert FST_HEADER.size == 72 and FST_RECORD.size == 64
assert FST_SYSCALL_METADATA.size == 128
assert FST_ASMAP_HEADER.size == 48 and FST_ASMAP_ENTRY.size == 16
assert FST_VMAP_HEADER.size == 48 and FST_VMAP_ENTRY.size == 32
assert FST_IFMAP_HEADER.size == 48 and FST_IFMAP_ENTRY.size == 32
assert FST_IMAP_HEADER.size == 48
assert FST_IMAP_ENTRY.size == 72

FST_MAGIC = b"FSTRC01\0"
FST_VERSION = 7
FEATURE_VIRTUAL_PAGE_TOKENS = 1 << 0
FEATURE_SYSCALL_MARKERS = 1 << 1
FEATURE_DESTINATION_CLASSES = 1 << 2
FEATURE_SYSCALL_METADATA = 1 << 3
FEATURE_PRIVILEGE_RECORDS = 1 << 4
KNOWN_FEATURES = (1 << 5) - 1
SYSCALL_METADATA_ROW_SIZE = 128

ASMAP_MAGIC = b"FSTASM1\0"
VMAP_MAGIC = b"FSTVMP1\0"
IMAP_MAGIC = b"FSTIMA1\0"
VMAP_PHYSICAL_VALID = 1 << 0
VIRTUAL_PAGE_BITS = 12


@dataclass(frozen=True)
class FstInfo:
    core_id: int
    records: int
    features: int
    metadata_offset: int
    syscall_metadata_count: int
    metadata_row_size: int
    linux_syscall_abi: int


@dataclass(frozen=True)
class AsmapEntry:
    record_ordinal: int
    address_space_id: int


@dataclass(frozen=True)
class Asmap:
    core_id: int
    source_record_count: int
    entries: tuple[AsmapEntry, ...]


@dataclass(frozen=True)
class VmapEntry:
    token: int
    flags: int
    first_record_ordinal: int
    virtual_page: int
    physical_page: int


@dataclass(frozen=True)
class Vmap:
    core_id: int
    source_record_count: int
    page_offset_bits: int
    entries: tuple[VmapEntry, ...]


def fst_info(path: Path) -> FstInfo:
    with path.open("rb") as source:
        raw = source.read(FST_HEADER.size)
    if len(raw) != FST_HEADER.size:
        raise ValueError(f"truncated FST header: {path}")
    values = FST_HEADER.unpack(raw)
    magic, version, header_size, record_size, core_id = values[:5]
    records, features, metadata_offset, metadata_count, metadata_size, abi = values[5:]
    if (magic != FST_MAGIC or version != FST_VERSION
            or header_size != FST_HEADER.size or record_size != FST_RECORD.size):
        raise ValueError(f"invalid canonical FST v7 header: {path}")
    if features & ~KNOWN_FEATURES:
        raise ValueError(f"FST has unknown feature bits: {path}")
    if not features & FEATURE_DESTINATION_CLASSES:
        raise ValueError(f"FST lacks destination-class metadata: {path}")
    records_end = FST_HEADER.size + records * FST_RECORD.size
    if features & FEATURE_SYSCALL_METADATA:
        if (not features & FEATURE_SYSCALL_MARKERS
                or metadata_offset != records_end
                or metadata_size != SYSCALL_METADATA_ROW_SIZE
                or metadata_count == 0):
            raise ValueError(f"invalid syscall metadata table: {path}")
        expected_size = records_end + metadata_count * metadata_size
    else:
        if (features & FEATURE_SYSCALL_MARKERS
                or metadata_offset or metadata_count or metadata_size):
            raise ValueError(f"unexpected metadata fields: {path}")
        expected_size = records_end
    if path.stat().st_size != expected_size:
        raise ValueError(f"FST size/count mismatch: {path}")
    return FstInfo(core_id, records, features, metadata_offset, metadata_count,
                   metadata_size, abi)


def read_asmap(path: Path) -> Asmap:
    raw = path.read_bytes()
    if len(raw) < FST_ASMAP_HEADER.size:
        raise ValueError(f"truncated FST asmap: {path}")
    magic, version, header_size, entry_size, core_id, records, count, reserved = (
        FST_ASMAP_HEADER.unpack_from(raw))
    if (magic != ASMAP_MAGIC or version != 1 or header_size != FST_ASMAP_HEADER.size
            or entry_size != FST_ASMAP_ENTRY.size or reserved != 0
            or len(raw) != FST_ASMAP_HEADER.size + count * FST_ASMAP_ENTRY.size):
        raise ValueError(f"invalid FST asmap: {path}")
    entries: list[AsmapEntry] = []
    previous_ordinal = -1
    previous_id = 0
    for index in range(count):
        ordinal, address_space_id = FST_ASMAP_ENTRY.unpack_from(
            raw, FST_ASMAP_HEADER.size + index * FST_ASMAP_ENTRY.size)
        if (address_space_id == 0 or (index == 0 and ordinal != 0)
                or (index and (ordinal <= previous_ordinal or address_space_id == previous_id))
                or (records and ordinal >= records)):
            raise ValueError(f"invalid FST asmap entry: {path}")
        entries.append(AsmapEntry(ordinal, address_space_id))
        previous_ordinal, previous_id = ordinal, address_space_id
    return Asmap(core_id, records, tuple(entries))


def read_vmap(path: Path) -> Vmap:
    raw = path.read_bytes()
    if len(raw) < FST_VMAP_HEADER.size:
        raise ValueError(f"truncated FST vmap: {path}")
    magic, version, header_size, entry_size, core_id, records, count, page_bits, reserved = (
        FST_VMAP_HEADER.unpack_from(raw))
    if (magic != VMAP_MAGIC or version != 1 or header_size != FST_VMAP_HEADER.size
            or entry_size != FST_VMAP_ENTRY.size or page_bits != VIRTUAL_PAGE_BITS
            or reserved != 0
            or len(raw) != FST_VMAP_HEADER.size + count * FST_VMAP_ENTRY.size):
        raise ValueError(f"invalid FST vmap: {path}")
    entries: list[VmapEntry] = []
    seen: set[int] = set()
    previous_ordinal = -1
    for index in range(count):
        token, flags, ordinal, virtual_page, physical_page = FST_VMAP_ENTRY.unpack_from(
            raw, FST_VMAP_HEADER.size + index * FST_VMAP_ENTRY.size)
        if (token == 0 or token >= 1 << 31 or token in seen or flags != VMAP_PHYSICAL_VALID
                or (index and ordinal <= previous_ordinal)
                or (records and ordinal >= records)):
            raise ValueError(f"invalid QEMU-FST vmap entry: {path}")
        seen.add(token)
        entries.append(VmapEntry(token, flags, ordinal, virtual_page, physical_page))
        previous_ordinal = ordinal
    return Vmap(core_id, records, page_bits, tuple(entries))
