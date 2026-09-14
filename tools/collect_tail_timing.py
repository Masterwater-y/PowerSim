#!/usr/bin/env python3
"""Recollect bounded oracle timing from a frozen case's exact gem5 command.

Does not modify checkpoints, original traces, or FastSim configuration. Short
targets are diagnostic only: functional prefix/ROI identity must be checked
before comparing these labels with a frozen full-length result.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import time


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', required=True, type=Path)
    parser.add_argument('--case', required=True)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--user-uops', type=int, default=100000)
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--sidecar-python', default='/data00/yinhaolang/infer/.venv/bin/python')
    parser.add_argument('--diagnostic-gem5', type=Path,
                        help='alternate instrumented binary; requires separate target-equivalence validation')
    parser.add_argument('--trace-format', choices=['jsonl', 'fst'], default='jsonl')
    parser.add_argument('--debug-start-tick', type=int)
    parser.add_argument('--debug-end-tick', type=int)
    parser.add_argument('--debug-memory-controller', action='store_true',
                        help='also record MemCtrl request type, queues and response-ready time')
    parser.add_argument('--wrong-path-oracle', action='store_true',
                        help='record squashed instructions and their completed data addresses')
    parser.add_argument('--committed-mem-events', action='store_true',
                        help='enable TaoTrace committed memory-event JSONL for boundary-state extraction')
    args = parser.parse_args()
    if args.user_uops <= 0 or args.timeout <= 0:
        parser.error('target and timeout must be positive')
    if args.debug_memory_controller and args.debug_start_tick is None:
        parser.error('debug-memory-controller requires debug tick bounds')
    if (args.debug_start_tick is None) != (args.debug_end_tick is None) or \
            (args.debug_start_tick is not None and not 0 <= args.debug_start_tick < args.debug_end_tick):
        parser.error('debug ticks must specify a positive half-open interval')
    case = json.loads(args.inventory.read_text())[args.case]
    first = Path(case['manifest']).read_text().splitlines()[0].split()
    original = Path(first[2]).parent.parent
    request = json.loads((original / 'request.json').read_text())
    line = next(line for line in (original / 'run.log').read_text().splitlines()
                if line.startswith('command line: '))
    command = shlex.split(line[len('command line: '):])
    original_command_binary = str(Path(command[0]).resolve())
    original_binary_sha = request['gem5']['binary_sha256']
    observed_command_binary_sha = sha256(command[0])
    if args.diagnostic_gem5:
        command[0] = str(args.diagnostic_gem5.resolve())
        binary_sha = sha256(command[0])
    else:
        binary_sha = observed_command_binary_sha
        if binary_sha != original_binary_sha:
            raise ValueError(
                'gem5 binary changed: use --diagnostic-gem5 for an '
                'explicitly non-baseline diagnostic replay')
    checkpoint = Path(command[command.index('--checkpoint-dir') + 1])
    if not (checkpoint / 'm5.cpt').is_file():
        raise ValueError('original ROI checkpoint unavailable: ' + str(checkpoint))
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)
    trace = output / 'trace'
    script_index = command.index('-d') + 2
    source_script = Path(command[script_index]).resolve()
    if args.committed_mem_events:
        wrapper_text = source_script.read_text()
        disabled = 'emit_mem_events=False,'
        if wrapper_text.count(disabled) != 1:
            raise ValueError(
                'cannot safely enable committed mem_events in wrapper: '
                f'expected one {disabled!r}')
        config_root = 'CONFIG_ROOT = Path(__file__).resolve().parent'
        if wrapper_text.count(config_root) != 1:
            raise ValueError(
                'cannot safely relocate TaoTrace wrapper: CONFIG_ROOT anchor changed')
        wrapper_text = wrapper_text.replace(
            config_root, f'CONFIG_ROOT = Path({str(source_script.parent)!r})')
        wrapper_text = wrapper_text.replace(
            disabled, 'emit_mem_events=True,')
        generated_script = output / 'x86_fs_kvm_boot_checkpoint_tao_mem_events.py'
        generated_script.write_text(wrapper_text)
        command[script_index] = str(generated_script)
    replacements = {
        '-d': str(output / 'gem5'),
        '--stats-outfile': str(output / 'gem5' / 'stats.txt'),
        '--tao-trace-dir': str(trace),
        '--tao-trace-format': args.trace_format,
        '--roi-user-records-per-core': str(args.user_uops),
        '--tao-functional-user-target': str(args.user_uops),
    }
    for key, value in replacements.items():
        command[command.index(key) + 1] = value
    if '--tao-native-response-jsonl' not in command:
        command.append('--tao-native-response-jsonl')
    if args.wrong_path_oracle and '--tao-wrong-path-oracle' not in command:
        command.append('--tao-wrong-path-oracle')
    if args.debug_start_tick is not None:
        command[1:1] = [
            '--debug-flags=LSQUnit,ProtocolTrace,DRAM' +
            (',MemCtrl' if args.debug_memory_controller else ''),
            '--debug-start=' + str(args.debug_start_tick),
            '--debug-end=' + str(args.debug_end_tick),
            '--debug-file=timing-debug.log',
        ]
    # Keep the original O3 restore, warmup, hardware and CPL settings. Never
    # execute a reconstructed shell string (paths/arguments are an argv list).
    script = Path(command[script_index])
    provenance = {
        'schema': 'fastsim-tail-timing-collection-v1',
        'case': args.case, 'original': str(original), 'argv': command,
        'gem5_sha256': binary_sha, 'config_script_sha256': sha256(script),
        'original_gem5_sha256': original_binary_sha,
        'original_command_binary': original_command_binary,
        'observed_command_binary_sha256': observed_command_binary_sha,
        'same_gem5_binary': binary_sha == original_binary_sha,
        'trace_format': args.trace_format,
        'base_config_script_sha256': sha256(request['gem5']['config']),
        'original_base_config_script_sha256': request['gem5']['config_sha256'],
        'checkpoint_metadata_sha256': sha256(checkpoint / 'm5.cpt'),
        'inventory_sha256': sha256(args.inventory),
        'diagnostic_user_uops': args.user_uops,
        'original_user_uops': request['sampling']['roi_insts'],
        'full_case_replacement': False,
        'prefix_and_roi_identity': 'not_yet_verified',
        'timeout_seconds': args.timeout,
        'wrong_path_oracle': args.wrong_path_oracle,
        'committed_mem_events': args.committed_mem_events,
        'source_config_script': str(source_script),
        'source_config_script_sha256': sha256(source_script),
    }
    if provenance['base_config_script_sha256'] != provenance['original_base_config_script_sha256']:
        provenance['config_provenance_warning'] = 'base script differs; verify effective config'
    write_json(output / 'collection.json', provenance)
    if not args.execute:
        print(output / 'collection.json')
        return
    started = time.monotonic()
    env = dict(os.environ)
    env.pop('PYTHONHOME', None)
    generator = Path(__file__).resolve().parent / 'generate_fs_effective_target.py'
    env['FASTSIM_EFFECTIVE_TARGET_GENERATOR'] = str(generator)
    env['FASTSIM_EFFECTIVE_TARGET_PYTHON'] = args.sidecar_python
    provenance['sidecar_generator_sha256'] = sha256(generator)
    provenance['sidecar_python'] = args.sidecar_python
    write_json(output / 'collection.json', provenance)
    with (output / 'run.log').open('w') as log:
        try:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                    env=env, timeout=args.timeout)
            status = {'returncode': result.returncode, 'timed_out': False}
        except subprocess.TimeoutExpired:
            status = {'returncode': None, 'timed_out': True}
    status['wall_seconds'] = time.monotonic() - started
    write_json(output / 'completion.json', status)
    print(json.dumps(status))
    if status['returncode'] != 0:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
