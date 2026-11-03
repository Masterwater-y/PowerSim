#!/usr/bin/env python3
"""Statistics-only wrapper for deterministic recapture with a frozen runner.

Invoke with gem5: --runner ORIGINAL --reference-source FROZEN -- ORIGINAL_ARGS.
The original runner executes with its actual __file__ and unchanged checkpoint
validation. This wrapper is additional instrumentation, recorded separately.
"""
import argparse
import json
from pathlib import Path
import runpy
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from common_end_stats import reference_window, require, sha256, watch_actual_common_end


def main():
    import m5
    from m5.objects import Root

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runner', type=Path, required=True)
    parser.add_argument('--reference-source', type=Path, required=True)
    parser.add_argument('runner_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    original_args = args.runner_args[1:] if args.runner_args[:1] == ['--'] else args.runner_args
    require('--restore-boot-checkpoint' in original_args, 'frozen-checkpoint recapture required')
    require('--save-boot-checkpoint' not in original_args, 'wrapper does not create checkpoints')
    reference_start, reference_end, cores = reference_window(args.reference_source)
    account = json.loads((args.reference_source / 'oracle/time-accounting-core0.json').read_text())
    period = account['clock_period_ticks']
    runner = args.runner.resolve()
    checkpoint = Path(original_args[original_args.index('--restore-boot-checkpoint') + 1])
    identity = json.loads((checkpoint / 'fastsim-checkpoint.json').read_text())['identity']
    require(sha256(runner) == identity['runner_sha256'], 'runner differs from checkpoint identity')
    require(sha256('/proc/self/exe') == identity['gem5_sha256'], 'producer differs from checkpoint identity')
    outdir = Path(m5.options.outdir)
    require(outdir.resolve() != args.reference_source.resolve(), 'cannot overwrite reference source')
    metadata = dict(schema='fastsim-common-end-stats-instrumentation-v1',
                    runner=str(runner), runner_sha256=sha256(runner),
                    wrapper=str(Path(__file__).resolve()), wrapper_sha256=sha256(__file__),
                    helper_sha256=sha256(Path(__file__).resolve().parents[1] / 'tools/common_end_stats.py'),
                    producer_sha256=identity['gem5_sha256'], checkpoint=str(checkpoint),
                    reference_source=str(args.reference_source.resolve()),
                    mode='actual-common-end-queue0-close-marker',
                    reference_common_start_tick=reference_start,
                    reference_common_end_tick=reference_end, participant_cores=cores,
                    clock_period_ticks=period)
    armed = False
    watcher = None
    original_reset = m5.stats.reset

    def reset_and_schedule():
        nonlocal armed, watcher
        now = int(m5.curTick())
        require(not armed, 'unexpected stats reset after snapshot registration')
        original_reset()
        # instantiate/switch can reset at tick0. Arm only at the frozen
        # runner's explicit ROI reset, after all detailed CPUs are active.
        objects = list(Root.getInstance().descendants())
        detailed = [obj for obj in objects if obj.type == 'BaseO3CPU']
        if not detailed or any(obj.switchedOut() for obj in detailed):
            return
        require(len(detailed) == cores, 'detailed core population differs')
        inactive_kvm = []
        for obj in objects:
            if int(obj.eventq_index) != 0:
                require(obj.type == 'X86KvmCPU' and obj.switchedOut(),
                        'nonzero event queue has an active object: ' + obj.path())
                inactive_kvm.append(obj.path())
        for cpu in detailed:
            require(int(cpu.clk_domain.clock[0].getValue()) == period,
                    'detailed clock differs from reference')
        armed = True
        metadata.update(common_start_tick=now, reset_tick=now,
                        sim_quantum_ticks=int(Root.getInstance().sim_quantum),
                        inactive_kvm=inactive_kvm, detailed_queue0_verified=True)
        def dumped(tick, polls):
            metadata.update(common_end_tick=tick, polls=polls)
            (outdir / 'common-end-stats-schedule.json').write_text(json.dumps(metadata, indent=2) + '\n')
            print(f'[common-end-stats] dumped actual common-end tick={tick}', flush=True)
        watcher = watch_actual_common_end(m5, outdir / 'tao_trace', period, cores, dumped)
        (outdir / 'common-end-stats-schedule.json').write_text(json.dumps(metadata, indent=2) + '\n')
        print(f'[common-end-stats] watching actual common-end reset={now} period={period}', flush=True)

    m5.stats.reset = reset_and_schedule
    previous_argv = sys.argv
    try:
        sys.argv = [str(runner), *original_args]
        sys.path.insert(0, str(runner.parent))
        runpy.run_path(str(runner), run_name='__m5_main__')
        require(armed and watcher['dumped'], 'runner never produced an actual common-end snapshot')
    finally:
        m5.stats.reset = original_reset
        sys.argv = previous_argv


if __name__ == '__m5_main__':
    main()
