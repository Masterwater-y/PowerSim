#!/usr/bin/env python3
"""Independent content identity for functional FST inputs; never an ROI oracle."""
import hashlib
import json
import os
import struct
from pathlib import Path

POLICY = 'functional-record-window-v1'
SCHEMA = 'fastsim-functional-source-v1'
_BINARY = {'binary', 'fastsim-binary'}
_SLICE = {'binary-slice', 'fastsim-binary-slice'}
_WARMUP = {'binary-warmup-slice', 'fastsim-binary-warmup-slice'}
_STATE = {'binary-warmup-state-slice', 'fastsim-binary-warmup-state-slice'}
_CONTEXT = {'fastsim-binary-context-v1'}


def _uint(text, maximum=(1 << 64) - 1):
    if not text.isascii() or not text.isdecimal() or int(text) > maximum:
        raise ValueError('invalid unsigned manifest integer')
    return int(text)


def _artifact(path, role):
    path = Path(path)
    with path.open('rb') as source:
        before = os.fstat(source.fileno())
        hasher = hashlib.sha256()
        for chunk in iter(lambda: source.read(1 << 20), b''): hasher.update(chunk)
        digest = hasher.hexdigest()
        after = os.fstat(source.fileno())
        path_after = path.stat()
    key = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    if key(before) != key(after) or key(after) != key(path_after):
        raise ValueError('functional input changed while hashing: ' + str(path))
    return dict(role=role, path=str(path), size_bytes=after.st_size, sha256=digest)


def canonical_identity(value):
    """Path-free, ASCII canonical stream, shared byte-for-byte with C++."""
    fields = ('core_id', 'source_core_id', 'source_version', 'source_record_count',
              'slice_kind', 'skip_instructions', 'warmup_instructions', 'take_instructions',
              'score_begin_ordinal', 'score_end_ordinal_exclusive',
              'execution_begin_ordinal', 'execution_end_ordinal_exclusive')
    lines = [POLICY]
    for source in value['sources']:
        lines.append('core\t' + '\t'.join('-' if source[k] is None else str(source[k]) for k in fields))
        for artifact in source['artifacts']:
            lines.append('artifact\t%d\t%s\t%d\t%s' % (source['core_id'], artifact['role'], artifact['size_bytes'], artifact['sha256']))
    for artifact in value['artifacts']:
        lines.append('global\t%s\t%d\t%s' % (artifact['role'], artifact['size_bytes'], artifact['sha256']))
    return ('\n'.join(lines) + '\n').encode('ascii')


def collect_source_identity(manifest_path, thread_state_plan=''):
    manifest_path = Path(manifest_path)
    result = dict(schema=SCHEMA, identity_policy=POLICY, status='complete',
                  roi_edges=False, macro_boundary_complete=False, participants=[], sources=[], artifacts=[])
    for line in manifest_path.read_text().splitlines():
        tokens = line.split('#', 1)[0].split()
        if not tokens: continue
        if len(tokens) < 3: raise ValueError('invalid manifest entry')
        core = _uint(tokens[0], (1 << 32) - 1)
        fmt, path_text = tokens[1:3]
        path = Path(path_text)
        if not path.is_absolute(): path = manifest_path.parent / path
        if fmt not in _BINARY | _SLICE | _WARMUP | _STATE | _CONTEXT:
            raise ValueError('functional identity currently requires a binary FST source')
        with path.open('rb') as stream: header = stream.read(72)
        if len(header) != 72: raise ValueError('truncated FST header')
        magic, version, header_size, record_size, source_core, count, flags, *_ = struct.unpack('<8sIIIIQQ4Q', header)
        if magic != b'FSTRC01\0' or version not in range(2, 8) or header_size != 72 or record_size != (40 if version == 2 else 64):
            raise ValueError('invalid FST header')
        source = dict(core_id=core, source_core_id=core, source_version=version,
                      source_record_count=count, slice_kind='full', skip_instructions=0,
                      warmup_instructions=0, take_instructions=0,
                      score_begin_ordinal=0, score_end_ordinal_exclusive=count,
                      execution_begin_ordinal=0, execution_end_ordinal_exclusive=count, artifacts=[])
        if fmt in _BINARY:
            if len(tokens) not in (3, 4): raise ValueError('unexpected manifest fields')
            if len(tokens) == 4: source['source_core_id'] = _uint(tokens[3], (1 << 32) - 1)
        else:
            if len(tokens) < 6: raise ValueError('missing slice fields')
            source['source_core_id'] = _uint(tokens[3], (1 << 32) - 1)
            prefix, take = map(_uint, tokens[4:6])
            if not take: raise ValueError('zero slice size')
            source['take_instructions'] = take
            if fmt in _SLICE:
                if len(tokens) != 6 or not prefix: raise ValueError('invalid instruction slice')
                source.update(slice_kind='slice', skip_instructions=prefix,
                              score_begin_ordinal=None, score_end_ordinal_exclusive=None,
                              execution_begin_ordinal=None, execution_end_ordinal_exclusive=None)
            else:
                source.update(slice_kind='warmup', warmup_instructions=prefix,
                              score_begin_ordinal=None, score_end_ordinal_exclusive=None,
                              execution_end_ordinal_exclusive=None)
                expected = 9 if fmt in _STATE | _CONTEXT else 8
                if len(tokens) != 6:
                    if len(tokens) != expected: raise ValueError('invalid exact record slice')
                    warm, score = map(_uint, tokens[6:8])
                    if not score or (not warm and prefix) or warm > count or score > count - warm:
                        raise ValueError('invalid record boundaries')
                    execution = _uint(tokens[8]) if fmt in _CONTEXT else score
                    if execution < score or execution > count - warm:
                        raise ValueError('invalid execution boundaries')
                    if fmt in _CONTEXT and (prefix > warm or take > score):
                        raise ValueError('invalid instruction/record counts')
                    source.update(score_begin_ordinal=warm, score_end_ordinal_exclusive=warm + score,
                                  execution_end_ordinal_exclusive=warm + execution)
                    if fmt in _STATE:
                        state_path = Path(tokens[8])
                        if not state_path.is_absolute(): state_path = manifest_path.parent / state_path
                        source['artifacts'].append(_artifact(state_path, 'boundary_memory_state'))
                elif fmt in _STATE | _CONTEXT:
                    raise ValueError('exact record bounds required')
        if source['source_core_id'] != source_core: raise ValueError('source core does not match FST header')
        if path.stat().st_size < 72 + count * record_size: raise ValueError('truncated FST records')
        source['artifacts'].append(_artifact(path, 'fst'))
        if version == 7:
            for extension in ('deps', 'vmap', 'asmap', 'imap', 'ifmap', 'mreq', 'fsem'):
                companion = Path(str(path) + '.' + extension)
                if companion.exists(): source['artifacts'].append(_artifact(companion, extension))
                elif (extension == 'deps' and flags & 32) or (extension == 'mreq' and flags & 64) or (extension == 'fsem' and flags & 128): raise ValueError('required dependency companion missing')
        source['artifacts'].sort(key=lambda item: item['role'])
        result['sources'].append(source)
    result['sources'].sort(key=lambda item: item['core_id'])
    result['participants'] = [source['core_id'] for source in result['sources']]
    if not result['participants'] or result['participants'] != list(range(len(result['participants']))):
        raise ValueError('manifest cores must be dense starting at zero')
    if thread_state_plan: result['artifacts'].append(_artifact(thread_state_plan, 'thread_state_plan'))
    if any(source['score_begin_ordinal'] is None for source in result['sources']): result['status'] = 'incomplete'
    result['source_identity'] = 'sha256:' + hashlib.sha256(canonical_identity(result)).hexdigest()
    return result


def validate_identity(actual, expected):
    """Validate both claimed digests and full path-independent semantic metadata."""
    for value in (actual, expected):
        if value.get('schema') != SCHEMA or value.get('identity_policy') != POLICY:
            raise ValueError('unsupported functional identity')
        digest = 'sha256:' + hashlib.sha256(canonical_identity(value)).hexdigest()
        if digest != value.get('source_identity'): raise ValueError('functional identity digest mismatch')
    def semantic(value):
        if isinstance(value, dict): return {k: semantic(v) for k, v in value.items() if k != 'path'}
        if isinstance(value, list): return [semantic(v) for v in value]
        return value
    if semantic(actual) != semantic(expected): raise ValueError('functional source identity differs')
    return True


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('manifest'); parser.add_argument('--thread-state-plan', default='')
    args = parser.parse_args()
    print(json.dumps(collect_source_identity(args.manifest, args.thread_state_plan), indent=2))
