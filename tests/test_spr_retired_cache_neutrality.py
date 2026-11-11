"""Paired observers cannot pass with a mismatched start or hidden core delta."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import compare_spr_retired_cache_neutrality as checker


class NeutralityTests(unittest.TestCase):
    def fixture(self,directory):
        sources=[Path(directory)/name for name in ('before','after')]
        scope=dict(retired_macro_instructions=20,retired_uops=30,
                   events=dict(retired_loads=dict(count=10,unknown=0),
                               retired_stores=dict(count=1,unknown=0)))
        report=dict(input_identity="same",scopes=dict(workload_task=scope),
                    per_core=[dict(core_id=0,scopes=dict(workload_task=scope))],
                    evidence_gaps=[])
        old=copy.deepcopy(report);new=copy.deepcopy(report)
        new['membership_policy']=checker.SPR_MISS_POLICY
        self.references=[old,new]
        for source in sources:
            oracle=source/'tao_trace/oracle';oracle.mkdir(parents=True)
            (source/'spr-capture-preparation.json').write_text(json.dumps(dict(target_identity_valid=True)))
            (source/'effective-target.json').write_text(json.dumps(dict(source=str(source),cores=1)))
            (source/'sync-run-request.json').write_text(json.dumps(dict(staged_checkpoint=dict(
                restored=True,receipt_sha256='same-checkpoint',identity=dict(input='same')))))
            (source/'stats.txt').write_text('simTicks 100\nsimInsts 30\nsystem.cpu.numCycles 40\n')
            native=dict(committed_memory_uops=11,native_admission_fragments=11)
            (oracle/'native-summary-core0.json').write_text(json.dumps(native))
            raw=dict(source_record_count=30,measurement_begin_ordinal=0,
                     measurement_end_ordinal_exclusive=30,common_start_tick=0,
                     common_end_tick=100,common_cut_id='same',task_ids=[7],
                     scopes=dict(workload_task=dict(retirement_events={},retirement_unknown={},
                         request_events={},request_primitives={})))
            (oracle/'perf-cache-core0.json').write_text(json.dumps(raw))
        return sources

    def compare(self,sources):
        with patch.object(checker,'audit_retired_cache',side_effect=self.references):
            return checker.compare(*sources)

    def test_same_pair_passes_and_per_core_population_shift_fails(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            sources=self.fixture(directory)
            self.assertTrue(self.compare(sources)['passed'])
            self.references[1]['per_core'][0]['scopes']['workload_task']['retired_uops']+=1
            result=self.compare(sources)
            self.assertFalse(result['passed'])
            self.assertFalse(result['same_retired_population'])

    def test_missing_staged_start_is_unverified(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            sources=self.fixture(directory)
            (sources[1]/'sync-run-request.json').write_text('{}')
            self.assertFalse(self.compare(sources)['passed'])

    def test_changed_native_counter_fails(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            sources=self.fixture(directory)
            (sources[1]/'tao_trace/oracle/native-summary-core0.json').write_text(
                '{"committed_memory_uops":11,"native_admission_fragments":12}')
            self.assertFalse(self.compare(sources)['passed'])


if __name__=='__main__':
    unittest.main()
