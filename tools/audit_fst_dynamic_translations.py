#!/usr/bin/env python3
"""Audit a compressed dynamic-translation companion without timing/oracle input."""
import argparse
import json
import sys

from fst_dynamic_translations import read_sidecar


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sidecar', help='FSTXLT1 .fst.xlat.gz file')
    parser.add_argument('--fst', help='source FST; verifies row hashes, stream hash and coverage')
    parser.add_argument('--require-complete', action='store_true',
                        help='require FST-verified complete leaf and PTE-path coverage')
    args = parser.parse_args(argv)
    try:
        result = read_sidecar(args.sidecar, args.fst, require_complete=args.require_complete)
    except (ValueError, OSError) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}), file=sys.stderr)
        return 1
    # Keep reports bounded even for very large dictionaries/binding histories.
    print(json.dumps({'status': 'passed', 'header': result['header'], 'footer': result['footer'],
                      'fst_verified': result['fst_verified'], 'coverage': result['coverage']}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
