#!/usr/bin/env python3
"""Check a Zstd ROI's executed source phase, independently of CPI/PMU scoring.

Counts physical retired user UOPs, not task-qualified macroinstructions. Owner,
common-end and native-PMU validation remain separate required gates.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess

import numpy as np
from build_fst_v7_formal_dataset import read_header

HEADER = struct.Struct('<8sIIIIQQ4Q')
DTYPE = np.dtype({'names': ['pc', 'flags', 'op'],
                  'formats': ['<u8', '<u2', '<i2'],
                  'offsets': [0, 50, 52], 'itemsize': 64})
SETUP_PREFIXES = ('RDG_genBuffer', 'BMK_', 'ZSTD_reset', 'ZSTD_compressBegin',
                  'ZSTD_CCtx_init', 'ZSTD_CCtx_reset', 'ZSTD_cwksp_')


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(8 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def symbolize(binary, pcs):
    result = subprocess.run(
        ['addr2line', '-a', '-f', '-i', '-C', '-e', str(binary)],
        input=''.join(f'0x{pc:x}\n' for pc in pcs), capture_output=True,
        text=True, check=True)
    symbols = {}
    current = None
    frame = []
    for line in result.stdout.splitlines():
        if line.startswith('0x'):
            if frame:
                raise ValueError('incomplete addr2line frame')
            current = int(line, 16)
            symbols[current] = []
        else:
            frame.append(line)
            if len(frame) == 2:
                symbols[current].append(dict(function=frame[0], source=frame[1]))
                frame = []
    if frame or set(symbols) != set(pcs):
        raise ValueError('incomplete addr2line population')
    return symbols


def summarize(rows, symbols):
    user = rows[((rows['flags'] & 1) != 0) & (rows['op'] >= -1)]
    pcs, counts = np.unique(user['pc'], return_counts=True)
    compression = setup = unknown = 0
    top = []
    for pc, count in zip(pcs, counts):
        frames = symbols[int(pc)]
        names = [f['function'] for f in frames]
        is_setup = any('memset' in name or name.startswith(SETUP_PREFIXES)
                       for name in names)
        is_compression = any('lib/compress/' in f['source'].replace('\\', '/')
                             for f in frames) and not is_setup
        compression += int(count) if is_compression else 0
        setup += int(count) if is_setup else 0
        unknown += int(count) if all(f['function'] == '??' for f in frames) else 0
        top.append(dict(pc=hex(int(pc)), uops=int(count), frames=frames))
    total = len(user)
    return dict(user_uops=total, compression_user_uops=compression,
                setup_user_uops=setup, unknown_symbol_user_uops=unknown,
                compression_fraction=compression / total if total else 0,
                setup_fraction=setup / total if total else 0,
                top_pc=sorted(top, key=lambda r: r['uops'], reverse=True)[:12])


def audit(trace, binary, cores, minimum, maximum, bins):
    identity = json.loads((trace.parent / 'sync-run-request.json').read_text())['binary_identity']
    actual = sha256(binary)
    if actual != identity['binary_sha256']:
        raise ValueError('captured binary identity mismatch')
    result = dict(schema='fastsim-roi-phase-v1', valid=True, errors=[], per_core=[],
                  scope='physical-retired-user-uops', oracle_used=False,
                  binary=str(binary), binary_sha256=actual,
                  validator_sha256=sha256(Path(__file__).resolve()),
                  policy=dict(min_compression_fraction=minimum, max_setup_fraction=maximum,
                              bins=bins, setup_function_prefixes=SETUP_PREFIXES,
                              applies_to='each core and each measurement bin'))
    for core in range(cores):
        metadata = json.loads((trace / f'thread-state-metadata-core{core}.json').read_text())
        boundary = json.loads((trace / f'functional-boundary-core{core}.json').read_text())
        source = (trace / metadata['source_file']).resolve()
        source.relative_to(trace.resolve())
        with source.open('rb') as handle:
            header = HEADER.unpack(handle.read(72))
        # FST v7 may append an embedded syscall table after its hot records.
        # Reuse the maintained framing validator, then map only the hot array.
        read_header(source)
        if (header[:5] != (b'FSTRC01\0', 7, 72, 64, core)
                or header[5] != metadata['source_record_count']
                or metadata['core_id'] != core or boundary['core_id'] != core
                or not boundary['measurement_closed']
                or boundary['total_records'] != header[5]):
            raise ValueError(f'core{core}: source/header/boundary identity mismatch')
        warm = boundary['warmup_records']
        measured = boundary['measurement_records']
        if warm <= 0 or measured <= 0 or warm + measured != header[5]:
            raise ValueError(f'core{core}: invalid warmup/stat record bounds')
        data = np.memmap(source, dtype=DTYPE, mode='r', offset=72, shape=(header[5],))
        symbols = symbolize(binary, [int(x) for x in np.unique(data['pc'])])
        row = dict(core=core, source=str(source), source_records=header[5],
                   source_sha256=sha256(source),
                   warmup=summarize(data[:warm], symbols),
                   measurement=summarize(data[warm:], symbols), bins=[])
        for index in range(bins):
            lo = warm + measured * index // bins
            hi = warm + measured * (index + 1) // bins
            row['bins'].append(dict(index=index, begin_ordinal=lo, end_ordinal=hi,
                                    **summarize(data[lo:hi], symbols)))
        for name, population in [('measurement', row['measurement'])] + [
                (f'bin{b["index"]}', b) for b in row['bins']]:
            if (population['user_uops'] == 0
                    or population['compression_fraction'] < minimum
                    or population['setup_fraction'] > maximum):
                result['errors'].append(f'core{core}:{name}: compression/setup phase threshold failed')
        result['per_core'].append(row)
        del data
    result['valid'] = not result['errors']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trace-dir', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--cores', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--min-compression-fraction', type=float, default=0.5)
    parser.add_argument('--max-setup-fraction', type=float, default=0.1)
    parser.add_argument('--bins', type=int, default=4)
    args = parser.parse_args()
    if (args.cores <= 0 or args.bins <= 0
            or not 0 <= args.min_compression_fraction <= 1
            or not 0 <= args.max_setup_fraction <= 1):
        parser.error('positive cores/bins and fractions in [0,1] required')
    try:
        report = audit(args.trace_dir.resolve(), args.binary.resolve(), args.cores,
                       args.min_compression_fraction, args.max_setup_fraction, args.bins)
    except (OSError, ValueError, KeyError, struct.error, subprocess.CalledProcessError) as error:
        report = dict(schema='fastsim-roi-phase-v1', valid=False, errors=[str(error)], per_core=[])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(dict(valid=report['valid'], errors=report['errors'])))
    return 0 if report['valid'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
