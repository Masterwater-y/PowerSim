"""Native bypass conservation through the maintained native sideband auditor."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'tests')]
import test_p1_native_response_sideband as legacy
from audit_p1_native_response_sideband import audit_result
from audit_gem5_commit_gaps import load_native_facts

class NativePioAuditorTests(unittest.TestCase):
    def fixture(self, directory, summary=False):
        helper=legacy.NativeResponseSidebandTest()
        result=helper.make_result_summary(Path(directory)) if summary else helper.make_result_v7(Path(directory))
        file=result/'oracle'/('native-summary-core0.json' if summary else 'native-response-core0.jsonl')
        if summary:
            rows=json.loads(file.read_text())
            rows['source_sideband_schema']='taotrace-native-response-v8'
            rows['bypass_semantics']='ruby-port-device-route-acceptance-and-response'
            for key in ('native_bypass_accepted_fragments','native_bypass_response_fragments','native_bypass_protocol_errors','native_device_bypass_uops','native_mixed_bypass_uops'):
                rows[key]=0
            for pop in rows['native_ruby_pmu_by_scope'].values():
                pop.update(bypass_accepted_fragments=0,bypass_response_fragments=0,bypass_protocol_errors=0,device_bypass_uops=0,mixed_bypass_uops=0)
        else:
            rows=[json.loads(line) for line in file.read_text().splitlines()]
            rows[0]['schema']='taotrace-native-response-v8'
            rows[0]['bypass_semantics']='ruby-port-device-route-acceptance-and-response'
            for row in rows[1:-1]:
                row.update(native_bypass_accepted=0,native_bypass_responses=0,native_bypass_protocol_errors=0)
            # Existing terminal row becomes a literal conserved pure PIO.
            terminal=next(row for row in rows[1:-1] if row['native_terminal_no_ruby'])
            terminal.update(native_bypass_accepted=1,native_bypass_responses=1,native_terminal_reason_mask=512,native_issuance_closed=True)
        self.write(file,rows)
        return result,file,rows
    def write(self,file,rows):
        file.write_text(json.dumps(rows) if isinstance(rows,dict) else '\n'.join(map(json.dumps,rows))+'\n')
    def test_v8_pure_bypass_and_missing_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            result,file,rows=self.fixture(d)
            audited=audit_result(result)
            self.assertEqual(audited['aggregate']['terminal_reason_counts']['device_bypass_complete'],1)
            self.assertEqual(audited['aggregate']['native_bypass_response_fragments'],1)
            for field in ('native_bypass_accepted','native_bypass_responses','native_bypass_protocol_errors'):
                broken=copy.deepcopy(rows);del broken[1][field];self.write(file,broken)
                with self.assertRaises(ValueError):audit_result(result)
    def test_v8_summary_requires_bypass_population_conservation(self):
        with tempfile.TemporaryDirectory() as d:
            result,file,rows=self.fixture(d,True)
            self.assertEqual(audit_result(result)['aggregate']['native_bypass_response_fragments'],0)
            rows['native_bypass_accepted_fragments']=1;self.write(file,rows)
            with self.assertRaisesRegex(ValueError,'bypass'):audit_result(result)
    def test_v8_mixed_pending_and_invalid_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            result,file,rows=self.fixture(d)
            mixed=rows[1]
            mixed.update(native_bypass_accepted=1,native_bypass_responses=1)
            self.write(file,rows)
            core=audit_result(result)['aggregate']
            self.assertEqual(core['native_mixed_bypass_uops'],1)
            self.assertEqual(core['native_bypass_response_fragments'],2)
            for patch in ({'native_bypass_responses':2},
                          {'native_bypass_protocol_errors':1},
                          {'native_terminal_no_ruby':True,'native_terminal_reason_mask':512}):
                broken=copy.deepcopy(rows);broken[1].update(patch);self.write(file,broken)
                with self.assertRaises(ValueError):audit_result(result)

    def test_summary_and_sideband_conserve_same_pio_population(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as other:
            result,_,_=self.fixture(d)
            actual=audit_result(result)['per_core'][0]
            summary_result,file,summary=self.fixture(other,True)
            for field in summary:
                if field in actual:summary[field]=copy.deepcopy(actual[field])
            for population in summary['native_ruby_pmu_by_scope'].values():
                population['ruby_aliased_admission_fragments']=0
            summary['native_ruby_pmu_by_scope']['user']['ruby_aliased_admission_fragments']=1
            self.write(file,summary)
            audited=audit_result(summary_result)['per_core'][0]
            for field in ('native_bypass_accepted_fragments','native_bypass_response_fragments',
                          'native_device_bypass_uops','native_mixed_bypass_uops','terminal_reason_counts'):
                self.assertEqual(audited[field],actual[field])
            summary['terminal_reason_counts']['device_bypass_complete']=0
            self.write(file,summary)
            with self.assertRaisesRegex(ValueError,'bypass reason'):audit_result(summary_result)

    def test_legacy_absence_is_not_bypass_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            result=legacy.NativeResponseSidebandTest().make_result_v7(Path(d))
            self.assertFalse(audit_result(result)['aggregate']['bypass_evidence_available'])

    def test_commit_gap_reader_accepts_v8_and_waits_for_device(self):
        with tempfile.TemporaryDirectory() as d:
            _,file,rows=self.fixture(d)
            terminal=next(row for row in rows[1:-1] if row['native_terminal_no_ruby'])
            sequence=terminal['inst_seq_num']
            facts=load_native_facts(file,0,{sequence})
            self.assertEqual(facts[sequence]['native_bypass_responses'],1)
            terminal['native_bypass_responses']=0
            terminal['native_terminal_reason_mask']=0
            terminal['native_terminal_no_ruby']=False
            self.write(file,rows)
            with self.assertRaisesRegex(RuntimeError,'non-terminal|unresolved'):
                load_native_facts(file,0,{sequence})

    def test_old_reason_bits_seven_and_eight_decode(self):
        for bit,name in ((7,'store_overwrite_elided'),(8,'store_buffer_combined')):
            with tempfile.TemporaryDirectory() as d:
                result=legacy.NativeResponseSidebandTest().make_result_v7(Path(d))
                file=result/'oracle/native-response-core0.jsonl'
                rows=[json.loads(x) for x in file.read_text().splitlines()]
                terminal=next(row for row in rows[1:-1] if row['native_terminal_no_ruby'])
                terminal['native_terminal_reason_mask']=1<<bit;self.write(file,rows)
                self.assertEqual(audit_result(result)['aggregate']['terminal_reason_counts'].get(name),1)
if __name__=='__main__':unittest.main()
