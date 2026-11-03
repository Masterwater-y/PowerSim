#!/usr/bin/env python3
"""Build isolated, marked SPEC-source LBM/Graph500 research inputs.

No installed SPEC sources or binaries are overwritten. LBM uses a documented
64^3 research grid; Graph500 retains its source algorithm and scale18 input.
"""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(8 << 20), b''):
            h.update(block)
    return h.hexdigest()


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('source anchor is not unique: ' + old[:80])
    return text.replace(old, new, 1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bounded-warmup',action='store_true',help='KVM serial initialization, O3 instruction-budget warmup; runner owns stat opening')
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    output = args.output.resolve()
    output.relative_to(ROOT)
    output.mkdir(parents=True, exist_ok=True)
    runtime = ROOT / 'tmp/thread-state-active-elapsed/runtime/libgomp.a'
    compiler = Path('/opt/gcc-11/bin/gcc')
    flags = ['-std=c17', '-D_GNU_SOURCE', '-O3', '-g', '-gdwarf-4', '-march=x86-64',
             '-fno-strict-aliasing', '-fcommon', '-fopenmp', '-DSPEC',
             '-DSPEC_OPENMP', '-DSPEC_LP64', '-DNDEBUG']
    env = dict(os.environ, LD_LIBRARY_PATH='/opt/gcc-11/lib64')
    commands, artifacts, source_hashes, patches = [], {}, {}, []
    helper = '''#include "gem5_roi_marker.h"
#include <time.h>
#include <errno.h>
static void fastsim_spec_checkpoint(const char *name, unsigned id) {
  if (getenv("GEM5_DISABLE_ROI_MARKER")) return;
  gem5_roi_checkpoint_once(name, id);
  /* KVM checkpoint exit is asynchronous; remain outside stat until switch. */
  struct timespec pause_time = {0, 5000000};
  while (nanosleep(&pause_time, &pause_time)) if (errno != EINTR) abort();
}
'''
    for workload, sources in (
        ('782.lbm_r', ['main.c', 'lbm.c']),
        ('854.graph500_s', ['apsp.c', 'graph500.c', 'graph_generator.c', 'make_graph.c',
                            'omp-csr.c', 'options.c', 'prng.c', 'splittable_mrg.c',
                            'utils.c', 'verify.c', 'xalloc.c', 'spec_getopt/getopt.c'])):
        source = ROOT / 'workloads/spec_cpu_2026/benchspec/CPU' / workload / 'src'
        dest = output / workload
        if dest.exists():
            raise ValueError('refuse to overwrite build directory: ' + str(dest))
        shutil.copytree(source, dest, ignore=shutil.ignore_patterns('*.o', '*.a'))
        changed = {}
        if workload == '782.lbm_r':
            old = (dest / 'config.h').read_text()
            new = replace_once(old, '#define SIZE_X (2*SIZE)\n#define SIZE_Y (2*SIZE)\n#define SIZE_Z (130)',
                               '#define SIZE_X (64)\n#define SIZE_Y (64)\n#define SIZE_Z (64)')
            changed['config.h'] = (old, new)
            old = (dest / 'main.c').read_text()
            new = replace_once(old, '\tMAIN_initialize( &param );',
                               '\tfastsim_spec_checkpoint("lbm-before-first-team", 782);\n\tMAIN_initialize( &param );')
            new = replace_once(new, '\tfor( t = 1; t <= param.nTimeSteps; t++ ) {',
                               '\tfor( t = 1; t <= param.nTimeSteps; t++ ) {\n\t\tif (t == 2) gem5_roi_begin_once("lbm-one-step-warmup-complete", 782);')
            if args.bounded_warmup:
                new=replace_once(old,'\tMAIN_initialize( &param );',
                    '\tint fastsim_threads=fastsim_serial_setup_begin();\n'
                    '\tMAIN_initialize( &param );\n'
                    '\tfastsim_serial_setup_checkpoint(fastsim_spec_checkpoint, "lbm-after-serial-init", 782, fastsim_threads);')
                changed['main.c']=(old,helper+'#include "fastsim_serial_setup_checkpoint.h"\n'+new)
            else:
                changed['main.c'] = (old, helper + new)
        else:
            old = (dest / 'graph500.c').read_text()
            new = replace_once(old, '  int64_t desired_nedge;',
                               '  int64_t desired_nedge;\n  fastsim_graph_threads = fastsim_serial_setup_begin();')
            new = replace_once(new, '  for (m = 0; m < NBFS; ++m) {',
                               '  fastsim_serial_setup_checkpoint(fastsim_spec_checkpoint,\n'
                               '      "graph500-after-serial-setup", 854, fastsim_graph_threads);\n'
                               '  for (m = 0; m < NBFS; ++m) {\n    if (m == 1) gem5_roi_begin_once("graph500-first-bfs-warmup-complete", 854);')
            if args.bounded_warmup:
                new=new.replace('    if (m == 1) gem5_roi_begin_once("graph500-first-bfs-warmup-complete", 854);','')
            changed['graph500.c'] = (old, helper +
                '#include "fastsim_serial_setup_checkpoint.h"\nstatic int fastsim_graph_threads;\n' + new)
        for filename, (old, new) in changed.items():
            (dest / filename).write_text(new)
            patches.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                               fromfile=workload+'/original/'+filename,
                                               tofile=workload+'/marked/'+filename))
        for path in source.rglob('*'):
            if path.is_file() and path.suffix in ('.c', '.h'):
                source_hashes[str(path.relative_to(ROOT))] = digest(path)
        objects = []
        for filename in sources:
            obj = dest / (filename.replace('/', '_') + '.o')
            command = [str(compiler), *flags]
            if workload == '782.lbm_r':
                command += ['-DSPEC_RATE']
            else:
                # SPEC's bundled getopt owns these globals; avoid collisions
                # with the libc getopt also pulled in by static libgomp.
                command += ['-D'+name+'=spec_'+name for name in
                            ('optind', 'opterr', 'optarg', 'optopt')]
            command += ['-I'+str(ROOT/'workloads/spec_roi_integration'), '-I'+str(dest),
                        '-I'+str(dest/'spec_restrict'), '-I'+str(dest/'spec_getopt'),
                        '-c', str(dest/filename), '-o', str(obj)]
            commands.append(command)
            subprocess.run(command, env=env, check=True)
            objects.append(str(obj))
        binary = dest / 'workload'
        command = [str(compiler), '-static', '-no-pie', '-o', str(binary),
                   *objects, str(runtime), '-pthread', '-ldl', '-lm']
        commands.append(command)
        subprocess.run(command, env=env, check=True)
        artifacts[workload] = dict(binary=str(binary), sha256=digest(binary))
    (output/'roi.patch').write_text(''.join(patches))
    (output/'build-manifest.json').write_text(json.dumps(dict(
        schema='fastsim-spec-two-workload-build-v1', commands=commands,
        compiler_sha256=digest(compiler), runtime=str(runtime), runtime_sha256=digest(runtime),
        source_sha256=source_hashes, artifacts=artifacts,
        marker_headers={name:digest(ROOT/'workloads/spec_roi_integration'/name)
                        for name in ('gem5_roi_marker.h','sidecar_roi.h',
                                     'fastsim_serial_setup_checkpoint.h')},
        lbm_geometry=[64,64,64], lbm_standard_rate_score=False,
        graph500_scale=18, graph500_edgefactor=16,
        bounded_warmup=args.bounded_warmup,
        stat_open_policy='core0-gem5-inst-stop-budget-excludes-nop-and-instruction-prefetch' if args.bounded_warmup else 'workload-marker',
        graph500_setup='KVM serial regions; quiescent main-only checkpoint; '+
                       ('identity re-emitted; bounded instruction warmup, host verifies BFS' if args.bounded_warmup else
                        'identity re-emitted; first four-thread BFS+verify O3 warmup')), indent=2)+'\n')
    print(json.dumps(artifacts, indent=2))


if __name__ == '__main__':
    main()
