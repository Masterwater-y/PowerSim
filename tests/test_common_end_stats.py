"""A late final dump must never be accepted as the frozen-window reference."""
import sys
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import common_end_stats as stats


def block(end, elapsed, extra=''):
    return ('---------- Begin Simulation Statistics ----------\n'
            f'finalTick {end}\nsimTicks {elapsed}\n{extra}'
            '---------- End Simulation Statistics   ----------\n')


class CommonEndStatsTest(unittest.TestCase):
    def test_selects_exact_dump_instead_of_later_final_dump(self):
        raw = block(5000, 4000, 'dram.readBursts 17\n') + block(6000, 5000)
        self.assertIn('dram.readBursts 17', stats.select_snapshot(raw, 1000, 5000))
        self.assertNotIn('finalTick 6000', stats.select_snapshot(raw, 1000, 5000))

    def test_late_duplicate_wrong_reset_and_truncated_dumps_rejected(self):
        for raw in (block(6000, 5000), block(5000, 4000) * 2,
                    block(5000, 3999), block(5000, 4000).split('---------- End')[0]):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                stats.select_snapshot(raw, 1000, 5000)

    def test_compensated_schedule_and_too_late_registration(self):
        self.assertEqual(stats.schedule_tick(1000, 5000, 1000), 4000)
        self.assertEqual(stats.schedule_tick(1000, 5000, 0), 5000)
        for start, end, quantum in ((4500, 5000, 1000), (1000, 1000, 0),
                                    (1000, 5000, -1)):
            with self.assertRaises(ValueError):
                stats.schedule_tick(start, end, quantum)

    def test_real_o3_type_and_successful_target_exit_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.ini'
            config.write_text('[cpu]\ntype=BaseO3CPU\neventq_index=0\n'
                              '[kvm]\ntype=X86KvmCPU\neventq_index=1\n')
            stats.validate_detailed_queues(config, 1)
            config.write_text('[cpu]\ntype=BaseO3CPU\neventq_index=1\n')
            with self.assertRaises(ValueError):
                stats.validate_detailed_queues(config, 1)
        stats.validate_target_exit(dict(cause='a thread reached the max instruction count',
                                        exit_code=86, tick=6000), 5000)
        for result in (dict(cause='a thread reached the max instruction count', exit_code=0, tick=6000),
                       dict(cause='simulate() limit reached', exit_code=86, tick=6000)):
            with self.assertRaises(ValueError):
                stats.validate_target_exit(result, 5000)

    def test_population_mismatch_and_extra_core_rejected(self):
        ref = {0: dict(common_end_tick=5000, measurement_closed=True,
                       measurement_records=7, measurement_instructions=3)}
        stats.compare_boundaries(ref, ref)
        for changed in ({0: dict(ref[0], measurement_records=8)},
                        {0: dict(ref[0], common_end_tick=5001)},
                        {0: dict(ref[0], measurement_closed=False)},
                        {0: ref[0], 1: ref[0]}, {}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                stats.compare_boundaries(ref, changed)

    def test_dynamic_watcher_dumps_on_cpu_edge_after_unaligned_reset(self):
        root = Path(__file__).resolve().parents[1]
        binary = root / 'tmp/dynamic-translation-consumer-20261029/frozen/gem5.opt'
        if not binary.exists():
            self.skipTest('frozen gem5 producer not available')
        with tempfile.TemporaryDirectory(dir=root / 'tmp') as directory:
            directory = Path(directory)
            probe = directory / 'probe.py'
            probe.write_text('''import m5, m5.event, json, sys
from pathlib import Path
from m5.objects import Root, System, SrcClockDomain, VoltageDomain
sys.path.insert(0, ''' + repr(str(root / 'tools')) + ''')
from common_end_stats import watch_actual_common_end
system = System()
system.clk_domain = SrcClockDomain(clock='1GHz', voltage_domain=VoltageDomain())
root = Root(full_system=False, system=system, sim_quantum=1000)
m5.instantiate()
m5.simulate(1)
m5.stats.reset()
trace = Path(m5.options.outdir) / 'tao_trace'
(trace / 'oracle').mkdir(parents=True)
def close():
    (trace / 'oracle/cpi-core0.json').write_text('{"core_id":0}')
def dumped(tick, polls):
    (Path(m5.options.outdir) / 'observed.json').write_text(json.dumps(dict(tick=tick, polls=polls)))
marker = m5.event.create(close, priority=50)
m5.event.getEventQueue(0).schedule(marker, 30)
state = watch_actual_common_end(m5, trace, 10, 1, dumped)
m5.simulate(100)
assert state['dumped']
''')
            env = dict(os.environ, LD_LIBRARY_PATH='/root/.pyenv/versions/3.11.14/lib:/opt/gcc-11/lib64')
            result = subprocess.run([str(binary), '--outdir=' + str(directory / 'output'), str(probe)],
                                    env=env, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            observed = json.loads((directory / 'output/observed.json').read_text())
            self.assertEqual(observed, dict(tick=30, polls=3))
            self.assertIn('finalTick', stats.select_snapshot(
                (directory / 'output/stats.txt').read_text(), 1, 30))

    def test_actual_window_checks_event86_separately_from_process_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            oracle = source / 'tao_trace/oracle'
            oracle.mkdir(parents=True)
            (oracle / 'time-accounting-core0.json').write_text(json.dumps(dict(
                common_start_tick=1000, common_end_tick=5000, common_cut_complete=True)))
            (source / 'tao_trace/functional-boundary-core0.json').write_text(json.dumps(dict(
                measurement_closed=True, common_end_tick=5000, participant_count=1,
                measurement_policy='first-core-target-common-end-v1')))
            (source / 'tao_trace/core0.fst').write_bytes(b'records')
            (source / 'config.ini').write_text('[cpu]\ntype=BaseO3CPU\neventq_index=0\n')
            metadata = dict(mode='actual-common-end-queue0-close-marker', detailed_queue0_verified=True,
                            reset_tick=1000, common_start_tick=1000, common_end_tick=5000,
                            participant_cores=1, clock_period_ticks=10)
            (source / 'common-end-stats-schedule.json').write_text(json.dumps(metadata))
            (source / 'stats.txt').write_text(block(5000, 4000) + block(6000, 5000))
            target_exit = dict(cause='a thread reached the max instruction count', exit_code=86, tick=6000)
            result_path = source / 'sync-run-result.json'
            result_path.write_text(json.dumps(target_exit))
            receipt = dict(returncode=0, guard_reason=None, measurement_open=True)
            (source / 'capture-receipt.json').write_text(json.dumps(receipt))
            observed = stats.audit_actual_window(source)
            self.assertTrue(observed['same_window_valid'])
            self.assertEqual(observed['frozen_reference_match'], 'not-tested')
            for wrong in (dict(target_exit, exit_code=0), dict(target_exit, cause='guest exit')):
                result_path.write_text(json.dumps(wrong))
                with self.assertRaises(ValueError):
                    stats.audit_actual_window(source)
            result_path.write_text(json.dumps(target_exit))
            (source / 'common-end-stats-schedule.json').write_text(json.dumps(dict(metadata, reset_tick=999)))
            with self.assertRaises(ValueError):
                stats.audit_actual_window(source)

    def test_audit_requires_identical_fst_and_frozen_oracle_window(self):
        # A matching total count cannot hide different functional content.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference, source = root / 'reference', root / 'recapture'
            boundary = dict(common_end_tick=5000, measurement_closed=True,
                            measurement_records=7, measurement_instructions=3)
            account = dict(common_start_tick=1000, common_end_tick=5000,
                           common_cut_complete=True)
            for path in (reference, source):
                (path / 'tao_trace/oracle').mkdir(parents=True)
                (path / 'tao_trace/functional-boundary-core0.json').write_text(json.dumps(boundary))
                (path / 'tao_trace/oracle/time-accounting-core0.json').write_text(json.dumps(account))
                (path / 'tao_trace/core0.fst').write_bytes(b'functional-records')
            (reference / 'oracle').symlink_to('tao_trace/oracle', target_is_directory=True)
            (source / 'config.ini').write_text('[root]\nsim_quantum=1000\n')
            (source / 'common-end-stats-schedule.json').write_text(json.dumps(dict(
                common_start_tick=1000, common_end_tick=5000, reset_tick=1000,
                sim_quantum_ticks=1000, sched_event_when=4000)))
            (source / 'sync-run-result.json').write_text(json.dumps(dict(
                cause='a thread reached the max instruction count', exit_code=86, tick=6000)))
            (source / 'stats.txt').write_text(block(5000, 4000) + block(6000, 5000))
            (source / 'tao_trace/core0.fst').write_bytes(b'other-records')
            with self.assertRaisesRegex(ValueError, 'FST content differs'):
                stats.audit(reference, source)
            self.assertFalse((source / 'stats-common-end.txt').exists())
            (source / 'tao_trace/core0.fst').write_bytes(b'functional-records')
            observed = source / 'tao_trace/oracle/time-accounting-core0.json'
            observed.write_text(json.dumps(dict(account, common_start_tick=1001)))
            with self.assertRaisesRegex(ValueError, 'oracle common window differs'):
                stats.audit(reference, source)
            observed.write_text(json.dumps(account))
            # Both sides missing the same last boundary must not silently
            # reduce the population declared by their accounts and FSTs.
            for path in (reference, source):
                (path / 'tao_trace/oracle/time-accounting-core1.json').write_text(json.dumps(account))
                (path / 'tao_trace/core1.fst').write_bytes(b'core1')
            with self.assertRaisesRegex(ValueError, 'functional boundary core set differs'):
                stats.audit(reference, source)
            for path in (reference, source):
                (path / 'tao_trace/functional-boundary-core1.json').write_text(json.dumps(boundary))
            result = stats.audit(reference, source)
            self.assertTrue(result['valid'])
            self.assertEqual(result['sim_ticks'], 4000)


if __name__ == '__main__':
    unittest.main()
