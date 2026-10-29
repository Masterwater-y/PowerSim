#!/usr/bin/env python3
"""Audit measured functional NO_ACCESS adjacency, without native timing/cache input.

This does not certify the FST full-stream hash or compare CPU store roles to
Ruby line transitions. Use the maintained native validator for native truth.
"""
import argparse
from collections import Counter
import hashlib
import json
import mmap
from pathlib import Path
import struct

HEADER = struct.Struct('<8sIIIIQQQII')
ROW = struct.Struct('<QIIB7s')


def record_hash(record):
    value = 2166136261
    for byte in record:
        value = ((value ^ byte) * 16777619) & 0xffffffff
    return value


def audit_trace(path, core, begin, end):
    path = Path(path)
    companion = Path(str(path) + '.mreq')
    roles = companion.read_bytes()
    if len(roles) < HEADER.size:
        raise ValueError('truncated request-role header')
    magic, version, size, source_core, flags, records, rows, _, row_size, reserved = HEADER.unpack_from(roles)
    if (magic != b'FSTMREQ1' or version not in (1, 2) or size != HEADER.size
            or source_core != core or flags != 1 or rows > records
            or row_size != ROW.size or reserved or len(roles) != size + rows * row_size):
        raise ValueError('invalid request-role header/population')
    if not 0 <= begin < end <= records:
        raise ValueError('invalid measured record bounds')
    counts, pcs, micro_pcs = Counter(), Counter(), Counter()
    with path.open('rb') as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            # FST may carry cold metadata after the declared hot-record array.
            if (len(data) < 72 + records * 64 or data[:8] != b'FSTRC01\0'
                    or struct.unpack_from('<I', data, 8)[0] != 7
                    or struct.unpack_from('<II', data, 12) != (72, 64)
                    or struct.unpack_from('<I', data, 20)[0] != core
                    or struct.unpack_from('<Q', data, 24)[0] != records
                    or not struct.unpack_from('<Q', data, 32)[0] & 64):
                raise ValueError('FST identity/population/feature mismatch')
            previous = -1
            special_ordinals = set()
            for offset in range(size, len(roles), ROW.size):
                ordinal, _, _, role, padding = ROW.unpack_from(roles, offset)
                if (not previous < ordinal < records or role not in (1, 2)
                        or role == 2 and version != 2 or padding != bytes(7)):
                    raise ValueError('invalid request-role row/order')
                previous = ordinal
                special_ordinals.add(ordinal)
            for offset in range(size, len(roles), ROW.size):
                ordinal, expected, micro_pc, role, _ = ROW.unpack_from(roles, offset)
                if not begin <= ordinal < end:
                    continue
                record = data[72 + ordinal * 64:72 + (ordinal + 1) * 64]
                if record_hash(record) != expected:
                    raise ValueError('request-role record hash mismatch')
                width, memory_flags = struct.unpack_from('<HH', record, 48)
                if (not width or memory_flags & 8 or
                        role == 1 and (not memory_flags & 4 or memory_flags & 2) or
                        role == 2 and (not memory_flags & 2 or memory_flags & 4)):
                    raise ValueError('request-role record semantics mismatch')
                counts['bound_role_rows'] += 1
                if role != 1:
                    continue
                counts['noaccess'] += 1
                pc, address = struct.unpack_from('<QQ', record)
                pcs[hex(pc)] += 1
                micro_pcs[str(micro_pc)] += 1
                if ordinal + 2 >= end:
                    counts['boundary'] += 1
                    continue
                following = [data[72 + (ordinal + i) * 64:72 + (ordinal + i + 1) * 64]
                             for i in (1, 2)]
                identities = [struct.unpack_from('<QQ', r) for r in following]
                shapes = [struct.unpack_from('<HH', r, 48) for r in following]
                if (all(p == pc for p, _ in identities)
                        and ordinal + 1 not in special_ordinals
                        and ordinal + 2 not in special_ordinals
                        and all(w == 8 and f & 4 and not f & (2 | 8) for w, f in shapes)
                        and identities[0][1] + 8 == identities[1][1] == address):
                    counts['cda_two_store_same_pc'] += 1
                    counts['same_line'] += identities[0][1] // 64 == address // 64
                else:
                    counts['other'] += 1
    return dict(source=str(path.resolve()), core=core, measured_begin=begin, measured_end=end,
                companion_sha256=hashlib.sha256(roles).hexdigest(), counts=dict(counts),
                pcs=dict(pcs), micro_pcs=dict(micro_pcs), full_stream_hash_verified=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    results = []
    for line in args.manifest.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        fields = line.split()
        if len(fields) != 8 or fields[1] != 'binary-warmup-slice':
            raise ValueError('audit requires binary-warmup-slice with exact record bounds')
        source = Path(fields[2])
        if not source.is_absolute():
            source = args.manifest.parent / source
        begin, take = int(fields[6]), int(fields[7])
        results.append(audit_trace(source, int(fields[3]), begin, begin + take))
    if not results or len({r['core'] for r in results}) != len(results):
        raise ValueError('empty or duplicate-core manifest')
    args.output.write_text(json.dumps(dict(
        schema='fastsim-store-chain-functional-audit-v1',
        role_transition_comparison='unavailable-different-event-domains',
        full_stream_hash_verified=False, cores=results), indent=2) + '\n')


if __name__ == '__main__':
    main()
