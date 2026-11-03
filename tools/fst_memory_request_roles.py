"""Bound sparse functional request roles for independent population audits.

Rows are checked against their exact hot record. The FST consumer separately
validates the complete stream FNV hash; source identity binds both artifacts.
This helper consumes no native request, timing, or cache observation.
"""
import hashlib
from pathlib import Path
import struct

HEADER = struct.Struct('<8sIIIIQQQII')
ROW = struct.Struct('<QIIB7s')


def load_request_roles(source, data, core, count):
    source = Path(source)
    features = struct.unpack_from('<Q', data, 32)[0]
    if not features & 64:
        return {}, {}
    if (data[:8] != b'FSTRC01\0' or struct.unpack_from('<I', data, 8)[0] != 7
            or struct.unpack_from('<I', data, 20)[0] != core
            or struct.unpack_from('<Q', data, 24)[0] != count):
        raise ValueError('memory request role source header mismatch')
    companion = Path(str(source) + '.mreq')
    if not companion.is_file():
        raise ValueError('required memory request role companion missing')
    content = companion.read_bytes()
    if len(content) < HEADER.size:
        raise ValueError('truncated memory request role header')
    magic, version, size, source_core, flags, records, rows, _, row_size, reserved = HEADER.unpack_from(content)
    if (magic != b'FSTMREQ1' or version not in (1, 2) or size != HEADER.size
            or source_core != core or flags != 1 or records != count
            or rows > count or row_size != ROW.size or reserved
            or len(content) != HEADER.size + ROW.size * rows):
        raise ValueError('invalid memory request role header/version/population')
    roles = {}; previous = -1
    for offset in range(HEADER.size, len(content), ROW.size):
        ordinal, expected_hash, _, role, padding = ROW.unpack_from(content, offset)
        if (ordinal <= previous or ordinal >= count or role not in (1, 2)
                or role == 2 and version != 2 or padding != bytes(7)):
            raise ValueError('invalid memory request role row/order/version')
        record = data[72 + ordinal * 64:72 + (ordinal + 1) * 64]
        value = 2166136261
        for byte in record:
            value = ((value ^ byte) * 16777619) & 0xffffffff
        if value != expected_hash:
            raise ValueError('memory request role hot-record hash binding mismatch')
        memory_flags = struct.unpack_from('<H', record, 50)[0]
        if struct.unpack_from('<H', record, 48)[0] == 0 or memory_flags & 8:
            raise ValueError('memory request role attached to empty or atomic record')
        if role == 1 and (not memory_flags & 4 or memory_flags & 2):
            raise ValueError('NO_ACCESS memory request role requires a pure store')
        if role == 2 and (not memory_flags & 2 or memory_flags & 4):
            raise ValueError('software-prefetch role requires a read-only memory hint')
        roles[ordinal] = role; previous = ordinal
    return roles, {str(companion.resolve()): hashlib.sha256(content).hexdigest()}
