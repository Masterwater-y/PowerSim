"""Run real interval-model FST regressions: dependencies, fragments and EOF."""
import os
import pathlib
import re
import sqlite3
import struct
import subprocess
import tempfile
import unittest

from fixtures import make_fixture, add_dependencies, memory_at_first_record
from run_e2e_matrix import extract

ROOT = pathlib.Path(__file__).resolve().parents[2]
TREE = pathlib.Path(os.environ.get('TRACEBENCH_SNIPER_TREE',
    ROOT / 'tmp/sniper-interval-20261116/sniper-source'))


def run_fixture(manifest, window=32):
    out = manifest.parent / 'output'
    command = [str(TREE / 'run-sniper'), '--build-dir=build/fst-native',
        '-c', str(ROOT / 'tmp/e2e-throughput-20261116/campaign/sniper.cfg'),
        '-d', str(out), '-g', 'perf_model/core/type=interval',
        '-g', f'perf_model/core/interval_timer/window_size={window}',
        '-g', 'general/enable_icache_modeling=false',
        '-g', 'general/total_cores=2', '-g', 'traceinput/num_apps=2',
        '-g', f'traceinput/fst_manifest={manifest}', '--sim-end=last', '--trace-manual']
    p = subprocess.run(command, cwd=TREE, capture_output=True, text=True, timeout=60,
        env=dict(os.environ, SNIPER_SIM_LD_LIBRARY_PATH='/opt/gcc-11/lib64',
                 LD_LIBRARY_PATH='/opt/gcc-11/lib64'))
    (manifest.parent / 'run.log').write_text(p.stdout + p.stderr)
    if p.returncode:
        raise AssertionError(p.stdout[-4000:] + p.stderr)
    db = sqlite3.connect(out / 'simulation/sim.stats.sqlite3')
    q = "select n.objectname,n.metricname,v.core,v.value from 'values' v join names n using(nameid) where v.prefixid=(select prefixid from prefixes where prefixname='roi-end')"
    counters = {(o, m, c): v for o, m, c, v in db.execute(q)}
    names = set(db.execute('select objectname,metricname from names'))
    db.close()
    ends = {int(c): int(t) for c, t in re.findall(
        r'\[FST-ENDPOINT:(\d+)\] complete_macros=\d+ model_elapsed_fs=(\d+) stop=0', p.stdout)}
    if set(ends) != {0, 1}:
        raise AssertionError('incomplete model completion endpoints')
    return counters, names, ends


class SniperIntervalTest(unittest.TestCase):
    def fixture(self, **kw):
        root = pathlib.Path(tempfile.mkdtemp(prefix='sniper-interval-test-', dir=ROOT / 'tmp'))
        return make_fixture(root, **kw)

    def count(self, data, metric):
        counters, names, _ = data
        self.assertTrue(('interval_timer', metric) in names, 'missing interval metric: ' + metric)
        return [counters.get(('interval_timer', metric, c), 0) for c in range(2)]

    def test_short_tail_dispatches_all_records_without_extra_macro(self):
        manifest = self.fixture(macros=128, tail=True)
        data = run_fixture(manifest, window=512)
        self.assertEqual(self.count(data, 'fst_dispatched_macros'), [128, 128])
        self.assertEqual(self.count(data, 'fst_dispatched_uops'), [129, 129])
        self.assertTrue(all(t > 0 for t in data[2].values()))
        out = manifest.parent
        # The campaign reader must take counts from the interval model itself.
        (out / 'engine.log').write_text((out / 'run.log').read_text())
        measured = extract('sniper', out, 2, sniper_model='interval')
        self.assertEqual(measured['instructions'], [128, 128])
        self.assertEqual(measured['uops'], [129, 129])

    def test_recorded_dependency_chain_changes_interval_timing(self):
        independent = run_fixture(self.fixture(macros=512, tail=False, dependent=False))
        chain = run_fixture(self.fixture(macros=512, tail=False, dependent=True))
        self.assertGreater(min(chain[2].values()), max(independent[2].values()) * 2)
        self.assertEqual(self.count(chain, 'fst_dispatched_uops'), [512, 512])

    def test_extended_dependencies_enter_native_window(self):
        manifest = self.fixture(macros=32, tail=False)
        add_dependencies(manifest.parent, 15)
        data = run_fixture(manifest, window=512)
        self.assertEqual(self.count(data, 'fst_extra_declared'), [11, 11])
        self.assertGreaterEqual(min(self.count(data, 'fst_dependencies_added')), 15)
        self.assertEqual(self.count(data, 'fst_dispatched_uops'), [32, 32])

    def test_noaccess_and_serialization_are_preserved(self):
        manifest = self.fixture(macros=1, tail=False)
        memory_at_first_record(manifest.parent, role=1, serialize=True)
        data = run_fixture(manifest)
        self.assertEqual(self.count(data, 'fst_serial_declared'), [1, 1])
        self.assertEqual(self.count(data, 'fst_dispatched_uops'), [1, 1])
        self.assertEqual(self.count(data, 'uop_load'), [0, 0])

    def test_last_memory_operation_is_issued_and_completed(self):
        manifest = self.fixture(macros=1, tail=False)
        memory_at_first_record(manifest.parent, role=0)
        data = run_fixture(manifest)
        self.assertEqual(self.count(data, 'fst_dispatched_uops'), [1, 1])
        self.assertEqual(self.count(data, 'uop_load'), [1, 1])
        self.assertTrue(all(t > 1000000 for t in data[2].values()))

    def test_last_store_is_issued_and_completed(self):
        manifest = self.fixture(macros=1, tail=False)
        memory_at_first_record(manifest.parent, write=True)
        data = run_fixture(manifest)
        self.assertEqual(self.count(data, 'fst_dispatched_uops'), [1, 1])
        self.assertEqual(self.count(data, 'uop_store'), [1, 1])
        self.assertTrue(all(t > 1000000 for t in data[2].values()))

    def test_fu_pool_capacity_affects_independent_stream(self):
        times = []
        for op in [1, 2]:
            manifest = self.fixture(macros=512, tail=False, dependent=False)
            for f in manifest.parent.glob('core*.fst'):
                b = bytearray(f.read_bytes())
                for i in range(512):
                    struct.pack_into('<h', b, 72 + 64 * i + 52, op)
                f.write_bytes(b)
            data = run_fixture(manifest)
            times.append(max(data[2].values()))
        self.assertGreater(times[1], times[0])

    def test_eof_includes_nonpipelined_fu_pressure(self):
        manifest = self.fixture(macros=4, tail=False, dependent=False)
        for f in manifest.parent.glob('core*.fst'):
            b = bytearray(f.read_bytes())
            for i in range(4):
                struct.pack_into('<h', b, 72 + 64 * i + 52, 11)
            f.write_bytes(b)
        data = run_fixture(manifest, window=512)
        # 4 independent sqrt records, occupancy 24 cycles, 2 lanes:
        # at least 48 cycles under the configured aggregate resource bound.
        self.assertGreaterEqual(min(data[2].values()), 48 * 384615)


if __name__ == '__main__':
    unittest.main()
