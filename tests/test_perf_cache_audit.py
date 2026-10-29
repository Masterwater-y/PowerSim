"""Current-source native adapter: no legacy fallback, no promoted missing hooks."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from test_perf_retirement_validation import retirement_fixture
from audit_perf_retirement import audit_perf_retirement
import perf_cache_contract as contract
SPEC=importlib.util.find_spec('audit_perf_cache')
adapter=__import__('audit_perf_cache') if SPEC else None

KINDS=('data_read','rfo','code_read','software_prefetch','hardware_prefetch_l1',
       'hardware_prefetch_l2','hardware_prefetch_l3','hardware_prefetch','writeback','unknown')
OUTCOMES=('hit','tag_miss','permission_upgrade','merged_miss','remote_supply')


def fixture(trace):
    _,ret,_=retirement_fixture(trace)
    scopes={}
    for name,r in ret['scopes'].items():
        scopes[name]=dict(retired_macro_instructions=r['retired_macro_instructions'],
                         retirement_events={f:r[f] for f,b in contract.load_dictionary()['events'].items() if b['domain']=='retirement'},
                         retirement_unknown={f:0 for f,b in contract.load_dictionary()['events'].items() if b['domain']=='retirement'},
                         request_events={f:0 for f,b in contract.load_dictionary()['events'].items() if b['domain']=='request'},
                         event_capabilities={f:{c:False for c in b['required_capabilities']} for f,b in contract.load_dictionary()['events'].items()},
                         request_primitives={level:{kind:dict.fromkeys(OUTCOMES,0) for kind in KINDS} for level in ('private_l2','llc')},
                         private_l2_outgoing_writebacks=0)
    row={k:deepcopy(ret[k]) for k in ('core_id','source_file','source_record_count','measurement_begin_ordinal',
                                    'measurement_end_ordinal_exclusive','common_cut_id','common_start_tick','common_end_tick','task_ids')}
    row.update(schema='gem5-perf-cache-observations-v2',oracle_only=True,fst_input=False,
               evidence_source='native-o3-ruby-observers',scopes=scopes,
               observer=dict(schema='native-cache-request-observer-v1',unknown_requests=0,overflow=0))
    side=trace/'oracle/perf-cache-core0.json';side.write_text(json.dumps(row));return side,row


class PerfCacheAuditTest(unittest.TestCase):
    def setUp(self):self.assertIsNotNone(adapter,'independent v2 native sidecar adapter is missing')

    def test_old_source_rejected_without_new_request_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);retirement_fixture(trace)
            with self.assertRaisesRegex(ValueError,'sidecar|participant'):
                adapter.audit_perf_cache(trace)

    def test_native_primitives_are_recounted_and_missing_caps_stay_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);path,row=fixture(trace)
            for name in ('physical','user','workload_task'):
                scope=row['scopes'][name]
                scope['request_primitives']['private_l2']['data_read']['tag_miss']=1
                scope['request_primitives']['llc']['data_read']['remote_supply']=1
                scope['request_events']['perf_l2_accesses']=1
                scope['request_events']['perf_l2_demand_misses']=1
                scope['request_events']['perf_llc_accesses_core']=1
            path.write_text(json.dumps(row))
            audit=adapter.audit_perf_cache(trace)
            self.assertTrue(audit['valid'])
            m=audit['scopes']['user'];self.assertEqual(m['events']['perf_l2_demand_misses']['diagnostic_value'],1)
            self.assertIsNone(m['events']['perf_l2_demand_misses']['value'])
            self.assertEqual(m['events']['perf_llc_misses_core']['diagnostic_value'],0)

    def test_changed_native_count_or_current_fst_invalidates_source(self):
        for failure in ('count','population','scope_sum','boundary','missing_participant','bad_source'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                trace=Path(directory);path,row=fixture(trace)
                if failure=='count':row['scopes']['physical']['request_events']['perf_l2_accesses']=99
                if failure=='population':row['scopes']['physical']['retired_macro_instructions']+=1
                if failure=='scope_sum':row['scopes']['kernel']['retirement_events']['perf_retired_loads']=1
                if failure=='boundary':row['measurement_end_ordinal_exclusive']-=1
                if failure=='missing_participant':row['core_id']=1
                if failure=='bad_source':row['evidence_source']='taotrace-path-class-v3'
                path.write_text(json.dumps(row))
                with self.assertRaises(ValueError):adapter.audit_perf_cache(trace)

    def test_unknown_primitives_cannot_hide_behind_zero_unknown_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);path,row=fixture(trace)
            for scope in ('physical','user','workload_task'):
                r=row['scopes'][scope]
                r['request_primitives']['private_l2']['unknown']['tag_miss']=1
                for field,binding in contract.load_dictionary()['events'].items():
                    if binding['domain']=='request':r['event_capabilities'][field]={k:True for k in binding['required_capabilities']}
            path.write_text(json.dumps(row))
            with self.assertRaisesRegex(ValueError,'unknown|coverage'):
                adapter.audit_perf_cache(trace)

    def test_native_missing_unknown_and_unclassified_evidence_cannot_claim_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);path,row=fixture(trace)
            row['scopes']['physical']['event_capabilities']['perf_l1d_load_misses']['member_service_facts']=True
            row['scopes']['physical']['retirement_unknown']['perf_l1d_load_misses']=1
            path.write_text(json.dumps(row))
            with self.assertRaisesRegex(ValueError,'unknown|coverage'):
                adapter.audit_perf_cache(trace)

    def test_native_sidecar_mutation_after_core_scan_invalidates_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);path,row=fixture(trace)
            original=adapter.load_capture_evidence
            def mutate_after_scan(source):
                evidence=original(source)
                row['scopes']['physical']['request_events']['perf_l2_demand_misses']=99
                path.write_text(json.dumps(row))
                return evidence
            with patch.object(adapter,'load_capture_evidence',side_effect=mutate_after_scan):
                with self.assertRaisesRegex(ValueError,'source changed|sidecar changed'):
                    adapter.audit_perf_cache(trace)


if __name__=='__main__':unittest.main()
