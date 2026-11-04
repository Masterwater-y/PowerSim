"""Independent, fail-closed decoder for the functional FSTXLT1 companion.

This checks recorded functional evidence, never cache outcomes or timing.
Without an FST, footer populations remain declarations, not verified coverage.
"""
from collections import defaultdict
import mmap
import bisect
import gzip
from pathlib import Path
import struct
import subprocess
import zlib

HEADER = struct.Struct('<8sIIIIQ')
_U64_MAX = (1 << 64) - 1


class _GzipReader:
    """Read one gzip member incrementally, rejecting concatenation and junk."""
    def __init__(self, source):
        self.source = source
        self.decoder = zlib.decompressobj(31)
        self.buffer = bytearray()
        self.pending = b''
        self.finished = False

    def read(self, size):
        while len(self.buffer) < size and not self.finished:
            compressed = self.pending or self.source.read(65536)
            if not compressed:
                raise ValueError('truncated gzip stream or missing CRC/EOF')
            try:
                self.buffer.extend(self.decoder.decompress(compressed, 65536))
            except zlib.error as error:
                raise ValueError('invalid gzip stream/CRC: ' + str(error)) from error
            self.pending = self.decoder.unconsumed_tail
            if self.decoder.eof:
                if self.decoder.unused_data or self.source.read(1):
                    raise ValueError('trailing gzip member or compressed bytes')
                self.finished = True
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def exact(self, size):
        value = self.read(size)
        if len(value) != size:
            raise ValueError('truncated dynamic translation field/footer')
        return value

    def byte(self):
        return self.exact(1)[0]

    def varint(self):
        value = 0
        for index in range(10):
            byte = self.byte()
            if index == 9 and byte > 1:
                raise ValueError('unsigned LEB128 overflow beyond 64 bits')
            value |= (byte & 127) << (7 * index)
            if not byte & 128:
                return value
        raise ValueError('unsigned LEB128 exceeds ten bytes')


def _fnv(data, bits):
    value, prime = ((14695981039346656037, 1099511628211) if bits == 64
                    else (2166136261, 16777619))
    mask = (1 << bits) - 1
    for byte in data:
        value = ((value ^ byte) * prime) & mask
    return value


def _definition(reader, next_id, version):
    names = ('mapping_id', 'address_space_id', 'walk_root', 'virtual_base',
             'physical_base', 'pcid')
    result = dict(zip(names, (reader.varint() for _ in names)))
    result.update(zip(('page_bits', 'levels', 'flags'), reader.exact(3)))
    flags, bits, levels = result['flags'], result['page_bits'], result['levels']
    if result['mapping_id'] != next_id:
        raise ValueError('mapping definitions must use sequential IDs starting at one')
    if flags & ~7 or levels > 8 or result['pcid'] > 4095:
        raise ValueError('invalid mapping flags, levels or PCID')
    if flags & 1:
        if bits not in (12, 21, 22, 30):
            raise ValueError('invalid known leaf page size')
        if result['virtual_base'] & ((1 << bits) - 1) or result['physical_base'] & ((1 << bits) - 1):
            raise ValueError('unaligned mapping virtual/physical base')
    elif bits or flags & 2:
        raise ValueError('page size/path cannot be known without a known leaf')
    if flags & 2:
        if not levels or not result['walk_root']:
            raise ValueError('known PTE path requires nonzero root and levels')
    elif levels or result['walk_root']:
        raise ValueError('unknown PTE path requires zero root and levels')
    result['pte_addresses'] = [reader.varint() for _ in range(levels)]
    # Legacy non-PAE x86 has four-byte PTEs; the format does not encode mode.
    if any(address & 3 for address in result['pte_addresses']):
        raise ValueError('unaligned PTE address')
    result['kind'] = reader.byte() if version >= 2 else 0
    result['request_physical_base'] = reader.varint() if version >= 2 else 0
    result['request_virtual_base'] = reader.varint() if version >= 2 else 0
    kind, request = result['kind'], result['request_physical_base']
    if kind > 6 or (kind == 0 and (request or result['request_virtual_base'])):
        raise ValueError('invalid translation kind/request physical base')
    if kind and not flags & 1:
        if bits or levels or result['walk_root'] or flags or result['physical_base'] or result['virtual_base'] & 4095:
            raise ValueError('invalid nonpaged special mapping')
    return result


def _complete(mapping):
    return mapping['flags'] & 3 == 3 or (mapping['kind'] != 0 and mapping['flags'] == 0)


def _physical(mapping, row):
    if not mapping['flags'] & 1 and not mapping['kind']:
        raise ValueError('binding has no known leaf')
    offset = row['fragment_virtual_address'] - mapping['virtual_base']
    if offset < 0 or offset + row['fragment_size'] > 1 << (mapping['page_bits'] or 12):
        raise ValueError('fragment exceeds its mapping leaf')
    if mapping['kind']:
        address = (mapping['request_physical_base'] + row['fragment_virtual_address']
                   - mapping['request_virtual_base'])
    else:
        address = mapping['physical_base'] + offset
    if address < 0 or address + row['fragment_size'] > 1 << 64:
        raise ValueError('fragment physical address overflows')
    return address


def _verify_fst(result, fst_path):
    path = Path(fst_path)
    footer = result['footer']
    with path.open('rb') as source:
        if path.stat().st_size < 72:
            raise ValueError('truncated FST header')
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
            magic, version, hsize, rsize, core, count, features = struct.unpack_from('<8sIIIIQQ', data)
            if (magic != b'FSTRC01\0' or version != 7 or hsize != 72 or rsize != 64
                    or core != result['header']['core_id'] or count != footer['record_count']
                    or not features & 512 or len(data) < 72 + 64 * count):
                raise ValueError('FST header/core/count/feature9 mismatch or truncated hot stream')
            origins = result['header']['version'] == 3
            source_asids = []
            token_map = {}
            if origins:
                if not features & (1 << 14):
                    raise ValueError('data translation origins require static UOP source policy')
                with gzip.open(str(path) + '.uops.gz', 'rb') as companion:
                    uops = companion.read(64)
                if (len(uops) != 64 or struct.unpack_from('<8sIIII', uops) !=
                        (b'FSTUOP1\0', 1, 64, result['header']['core_id'], 64) or
                        uops[24:56].hex() != result['header']['policy_sha256'] or any(uops[56:64])):
                    raise ValueError('data translation policy differs from static UOP source policy')
                asmap = Path(str(path) + '.asmap').read_bytes()
                if len(asmap) < 48:
                    raise ValueError('missing/truncated independent address-space map')
                magic_a, version_a, hs_a, es_a, core_a, count_a, entries_a, reserved_a = struct.unpack_from('<8sIIIIQQQ', asmap)
                if ((magic_a, version_a, hs_a, es_a, core_a, count_a, reserved_a) !=
                        (b'FSTASM1\0', 1, 48, 16, result['header']['core_id'], count, 0) or
                        not entries_a or len(asmap) != 48 + 16 * entries_a):
                    raise ValueError('invalid independent address-space map')
                source_asids = list(struct.iter_unpack('<QQ', asmap[48:]))
                for index, (ordinal_a, asid_a) in enumerate(source_asids):
                    if (not asid_a or ordinal_a >= count or (index == 0 and ordinal_a != 0) or
                            (index and (ordinal_a <= source_asids[index - 1][0] or asid_a == source_asids[index - 1][1]))):
                        raise ValueError('invalid independent address-space map entry')
                if features & 1:
                    vmap = Path(str(path) + '.vmap').read_bytes()
                    if len(vmap) < 48:
                        raise ValueError('missing/truncated declared virtual-page map')
                    magic_v, version_v, hs_v, es_v, core_v, count_v, entries_v, bits_v, reserved_v = struct.unpack_from('<8sIIIIQQII', vmap)
                    if (version_v not in (1, 2) or magic_v != (b'FSTVMP1\0' if version_v == 1 else b'FSTVMP2\0') or
                            (hs_v, es_v, core_v, count_v, bits_v, reserved_v) !=
                            (48, 32 if version_v == 1 else 168, result['header']['core_id'], count, 12, 0) or
                            not entries_v or len(vmap) != 48 + es_v * entries_v):
                        raise ValueError('invalid declared virtual-page map')
                    for index in range(entries_v):
                        token_v, flags_v, first_v, va_v, pa_v = struct.unpack_from('<IIQQQ', vmap, 48 + es_v * index)
                        if not token_v or token_v > 0x7fffffff or token_v in token_map or first_v >= count:
                            raise ValueError('invalid virtual-page token entry')
                        token_map[token_v] = (first_v, va_v, pa_v, bool(flags_v & 1))
            expected = footer['hot_stream_hash']
            if count <= 65536:
                if _fnv(data[72:72 + 64 * count], 64) != expected:
                    raise ValueError('FST full hot-stream hash mismatch')
            else:
                helper = Path(__file__).resolve().parents[1] / 'build/fastsim_fst_stream_hash'
                if not helper.is_file():
                    raise ValueError('FST full-stream hash unavailable: build/fastsim_fst_stream_hash missing')
                checked = subprocess.run([str(helper), str(path), str(count), str(expected)],
                                         capture_output=True, text=True)
                if checked.returncode:
                    raise ValueError('FST full hot-stream hash mismatch: ' + checked.stderr)
            groups = defaultdict(list)
            for row in result['bindings']:
                groups[row['record_ordinal']].append(row)
            definitions = {row['mapping_id']: row for row in result['definitions']}
            states = {}
            memories = unknown = 0
            context_ordinals = [item[0] for item in source_asids]
            for ordinal in range(count):
                raw = data[72 + 64 * ordinal:72 + 64 * (ordinal + 1)]
                size, flags = struct.unpack_from('<HH', raw, 48)
                rows = groups.get(ordinal, [])
                token = struct.unpack_from('<I', raw, 60)[0] & 0x7fffffff if flags & 32768 else 0
                if origins and token and not features & 1:
                    raise ValueError('nonzero hot token requires declared virtual-page map')
                if not size or not flags & 14:
                    if origins and token:
                        raise ValueError('nonzero token requires memory row')
                    if rows:
                        raise ValueError('binding attached to a non-memory FST record')
                    continue
                memories += 1
                address = struct.unpack_from('<Q', raw, 8)[0]
                expected_asid = source_asids[bisect.bisect_right(context_ordinals, ordinal) - 1][1] if origins else None
                if origins and token:
                    mapped_token = token_map.get(token)
                    if mapped_token is None:
                        raise ValueError('missing declared virtual-page token')
                    first_v, va_v, pa_v, physical_valid = mapped_token
                    namespace = source_asids[bisect.bisect_right(context_ordinals, first_v) - 1][1]
                    if first_v > ordinal or namespace != expected_asid or (physical_valid and pa_v != address >> 12):
                        raise ValueError('virtual-page token namespace/PA changed')
                if rows:
                    if origins and any(row['retirement_address_space_id'] != expected_asid for row in rows):
                        raise ValueError('binding retirement ASID disagrees with source context')
                    if any(row['hot_record_hash'] != _fnv(raw, 32) for row in rows):
                        raise ValueError('binding hot-record hash mismatch')
                    if any(row['token'] != token for row in rows):
                        raise ValueError('binding token disagrees with FST record')
                    if sum(row['fragment_size'] for row in rows) != size:
                        raise ValueError('binding fragment coverage does not match memory size')
                    for index, row in enumerate(rows):
                        if origins and token and row['fragment_virtual_address'] >> 12 != va_v:
                            raise ValueError('binding VA disagrees with virtual-page token')
                        mid = row['mapping_id']
                        if mid:
                            actual = _physical(definitions[mid], row)
                            if index == 0 and actual != address:
                                raise ValueError('binding physical address disagrees with FST')
                        if index and row['fragment_virtual_address'] != (rows[index - 1]['fragment_virtual_address']
                                                                        + rows[index - 1]['fragment_size']):
                            raise ValueError('binding fragments do not cover contiguous virtual bytes')
                    complete = all(row['mapping_id'] and _complete(definitions[row['mapping_id']])
                                   for row in rows)
                    if token:
                        # Only a full single fragment permits subsequent omission.
                        if len(rows) == 1:
                            row = rows[0]
                            states[token] = (row['mapping_id'], row['fragment_virtual_address'], address, expected_asid)
                        else:
                            states.pop(token, None)
                else:
                    if not token or token not in states:
                        raise ValueError('missing explicit binding/coverage for memory record')
                    mid, bound_va, bound_pa, bound_asid = states[token]
                    if origins and bound_asid != expected_asid:
                        raise ValueError('sparse token retirement context changed without binding')
                    if mid:
                        if address & ~4095 != bound_pa & ~4095 or (address & 4095) + size > 4096:
                            raise ValueError('sparse token binding no longer covers memory record')
                        inherited = {'fragment_virtual_address': bound_va + address - bound_pa,
                                     'fragment_size': size}
                        if not 0 <= inherited['fragment_virtual_address'] <= _U64_MAX:
                            raise ValueError('inherited binding virtual address overflow')
                        if _physical(definitions[mid], inherited) != address:
                            raise ValueError('inherited binding physical address mismatch')
                    elif origins:
                        inherited_va = bound_va + address - bound_pa
                        if (address >> 12 != bound_pa >> 12 or (address & 4095) + size > 4096 or
                                not 0 <= inherited_va <= _U64_MAX or (token and inherited_va >> 12 != va_v)):
                            raise ValueError('sparse unknown token outside virtual/physical page')
                    complete = bool(mid and _complete(definitions[mid]))
                unknown += not complete
            if memories != footer['memory_records'] or unknown != footer['unknown_records']:
                raise ValueError('footer memory/unknown record count disagrees with FST population')
    result['fst_verified'] = True
    result['coverage'] = {'memory_records': memories, 'known_records': memories - unknown,
                          'unknown_records': unknown}


def read_sidecar(path, fst_path=None, *, require_complete=False, policy_sha256=None):
    """Decode one gzip companion and optionally bind every row/population to FST.

    Returns JSON-compatible header, definitions, bindings and footer objects.
    `fst_verified` is false and `coverage` is None without the source FST.
    `require_complete` requires independently verified full leaf AND path coverage.
    """
    definitions, bindings = [], []
    # Token identity survives mutable sparse-state changes and unknown rows.
    # Unknown mappings do not establish an ASID or virtual/physical identity.
    token_identities = {}
    with Path(path).open('rb') as source:
        reader = _GzipReader(source)
        magic, version, size, core, flags, reserved = HEADER.unpack(reader.exact(HEADER.size))
        if magic != b'FSTXLT1\0' or version not in (1, 2, 3) or size != (64 if version == 3 else HEADER.size) or flags != 1 or reserved:
            raise ValueError('invalid dynamic translation header/version/flags')
        declared_policy = reader.exact(32).hex() if version == 3 else None
        if version == 3 and (declared_policy == '00' * 32 or
                             (policy_sha256 is not None and policy_sha256 != declared_policy)):
            raise ValueError('data translation origin policy mismatch/undeclared')
        previous = 0
        while True:
            tag = reader.byte()
            if tag == 1:
                definitions.append(_definition(reader, len(definitions) + 1, version))
            elif tag == 2:
                delta = reader.varint()
                ordinal = previous + delta
                if ordinal > _U64_MAX:
                    raise ValueError('binding ordinal overflow')
                row = {'record_ordinal': ordinal,
                       'hot_record_hash': struct.unpack('<I', reader.exact(4))[0]}
                for name in ('token', 'fragment_virtual_address', 'fragment_size', 'mapping_id'):
                    row[name] = reader.varint()
                if version == 3:
                    row['retirement_address_space_id'] = reader.varint()
                if row['mapping_id'] > len(definitions) or row['token'] > 0x7fffffff:
                    raise ValueError('binding references undefined mapping or invalid token')
                if not row['fragment_size'] or row['fragment_size'] > 65535:
                    raise ValueError('invalid binding fragment size')
                if row['fragment_virtual_address'] + row['fragment_size'] > 1 << 64:
                    raise ValueError('binding virtual address overflows')
                if row['mapping_id']:
                    mapping = definitions[row['mapping_id'] - 1]
                    physical = _physical(mapping, row)
                    if row['token']:
                        identity = (row['retirement_address_space_id'] if version == 3 else mapping['address_space_id'],
                                    row['fragment_virtual_address'] >> 12, physical >> 12)
                        if token_identities.setdefault(row['token'], identity) != identity:
                            raise ValueError('nonzero token identity changed ASID or 4KiB virtual/physical page')
                bindings.append(row)
                previous = ordinal
            elif tag == 255:
                names = ('record_count', 'memory_records', 'definition_count', 'binding_count', 'unknown_records')
                footer = dict(zip(names, (reader.varint() for _ in names)))
                footer['hot_stream_hash'] = struct.unpack('<Q', reader.exact(8))[0]
                if reader.read(1):
                    raise ValueError('trailing bytes after dynamic translation footer')
                break
            else:
                raise ValueError('invalid dynamic translation tag or missing footer')
    if (footer['definition_count'] != len(definitions) or footer['binding_count'] != len(bindings)
            or footer['unknown_records'] > footer['memory_records']
            or footer['memory_records'] > footer['record_count']
            or any(row['record_ordinal'] >= footer['record_count'] for row in bindings)):
        raise ValueError('dynamic translation footer count/population mismatch')
    explicit_records = {row['record_ordinal'] for row in bindings}
    explicit_unknown = {row['record_ordinal'] for row in bindings
                        if not row['mapping_id'] or not _complete(definitions[row['mapping_id'] - 1])}
    if (len(explicit_records) > footer['memory_records']
            or len(explicit_unknown) > footer['unknown_records']):
        raise ValueError('footer understates explicit binding/unknown population count')
    result = {'header': {'version': version, 'header_size': size, 'core_id': core, 'flags': flags, 'policy_sha256': declared_policy},
              'definitions': definitions, 'bindings': bindings, 'footer': footer,
              'fst_verified': False, 'coverage': None}
    if fst_path is not None:
        _verify_fst(result, fst_path)
    if require_complete:
        if not result['fst_verified']:
            raise ValueError('complete coverage requires the source FST')
        if footer['unknown_records']:
            raise ValueError('incomplete translation evidence: unknown leaf/PTE path records')
    return result
