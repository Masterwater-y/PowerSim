#!/usr/bin/env python3
"""Copy a canonical x86 FST while removing gem5 syscall timing metadata.

This does not certify any other FST or companion fields as functional-only.
"""
import argparse
import hashlib
import json
import shutil
import struct
from pathlib import Path

from audit_fst_syscall_metadata import audit_file, HEADER, FIELDS, SYSCALL_BYTES

TIME_BITS = FIELDS['pre_timestamp'] | FIELDS['post_timestamp']


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(8 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def strip_file(source, target):
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise ValueError('source and destination must differ')
    audit_file(source)
    before = source.stat()
    with source.open('rb') as src:
        header = HEADER.unpack(src.read(HEADER.size))
        offset, count = header[7], header[8]
        created = False
        try:
            with target.open('xb+') as out:
                created = True
                src.seek(0)
                shutil.copyfileobj(src, out, 8 << 20)
                for index in range(count):
                    pos = offset + index * SYSCALL_BYTES
                    out.seek(pos + 88)
                    out.write(bytes(16))
                    out.seek(pos + 116)
                    raw = out.read(2)
                    if len(raw) != 2:
                        raise ValueError('short syscall metadata')
                    out.seek(pos + 116)
                    out.write(struct.pack('<H', struct.unpack('<H', raw)[0] & ~TIME_BITS))
            after = source.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError('source changed during copy')
            report = audit_file(target)
            if any(report['field_coverage'].get(k, 0) for k in ('pre_timestamp', 'post_timestamp')):
                raise ValueError('timestamp validity remains')
            return {'source': str(source.resolve()), 'output': str(target.resolve()),
                    'rows': count, 'source_sha256': sha256(source),
                    'output_sha256': sha256(target),
                    'scope': 'syscall timestamps only; other fields are not certified'}
        except BaseException:
            if created:
                target.unlink(missing_ok=True)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    print(json.dumps(strip_file(args.source, args.output), indent=2))


if __name__ == '__main__':
    main()
