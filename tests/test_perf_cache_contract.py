"""Hardware-bound event definitions and independently gated cache PMU reports."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
SPEC = importlib.util.find_spec('perf_cache_contract')
contract = __import__('perf_cache_contract') if SPEC else None

BINDINGS = {
    'perf_retired_loads': ('MEM_INST_RETIRED.ALL_LOADS', 0xd0, 0x81),
    'perf_retired_stores': ('MEM_INST_RETIRED.ALL_STORES', 0xd0, 0x82),
    'perf_l1d_load_hits': ('MEM_LOAD_RETIRED.L1_HIT', 0xd1, 0x01),
    'perf_l1d_load_misses': ('MEM_LOAD_RETIRED.L1_MISS', 0xd1, 0x08),
    'perf_l1d_load_fb_hits': ('MEM_LOAD_RETIRED.FB_HIT', 0xd1, 0x40),
    'perf_l2_accesses': ('L2_RQSTS.REFERENCES', 0x24, 0xff),
    'perf_l2_demand_misses': ('L2_RQSTS.ALL_DEMAND_MISS', 0x24, 0x27),
    'perf_l2_writebacks': ('L2_TRANS.L2_WB', 0x23, 0x40),
    'perf_llc_accesses_core': ('LONGEST_LAT_CACHE.REFERENCE', 0x2e, 0x4f),
    'perf_llc_misses_core': ('LONGEST_LAT_CACHE.MISS', 0x2e, 0x41),
}


def metric(producer='fastsim', *, complete=True):
    dictionary = contract.load_dictionary()
    events = {}
    for name, binding in dictionary['events'].items():
        events[name] = dict(event=binding['event'], umask=binding['umask'],
                            unit=binding['unit'], domain=binding['domain'],
                            diagnostic_value=1, value=1 if complete else None,
                            coverage={key: complete for key in binding['required_capabilities']},
                            hardware_validated=False)
    return dict(schema=producer+'-perf-cache-v2', contract_id=dictionary['contract_id'],
                hardware_target=deepcopy(dictionary['hardware_target']),
                evidence_source={'fastsim': 'model-self-generated-events',
                                 'gem5': 'native-o3-ruby-observers',
                                 'hardware': 'perf-event-open-raw'}[producer],
                oracle_only=producer=='gem5', fst_input=False,
                measurement=dict(scope='user', window_id='roi-0', core_ids=[0], task_ids=[101],
                                 context_policy='event-edge-context-v1'),
                retired_macro_instructions=100, events=events)


class PerfCacheContractTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(contract, 'versioned three-way cache PMU contract is missing')

    def test_exact_ten_bindings_and_speculation_domains(self):
        d = contract.load_dictionary()
        self.assertEqual(set(d['events']), set(BINDINGS))
        for name, (event, code, mask) in BINDINGS.items():
            row=d['events'][name]
            self.assertEqual((row['hardware_event'], row['event'], row['umask']), (event, code, mask))
            retired=name.startswith('perf_retired') or name.startswith('perf_l1d')
            self.assertEqual(row['domain'], 'retirement' if retired else 'request')
            self.assertEqual(row['speculative'], not retired)
        self.assertNotEqual(d['events']['perf_l2_accesses']['required_capabilities'],
                            d['events']['perf_l2_demand_misses']['required_capabilities'])

    def test_dictionary_cannot_relax_units_or_required_population(self):
        import tempfile
        for field in ('unit','required_capabilities','event_table_sha256'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                d=contract.load_dictionary()
                if field=='event_table_sha256':d['hardware_target'][field]='0'*64
                elif field=='unit':d['events']['perf_l1d_load_misses'][field]='cache-request'
                else:d['events']['perf_l2_demand_misses'][field]=['data_reads']
                p=Path(directory)/'dictionary.json';p.write_text(json.dumps(d))
                with self.assertRaises(ValueError):contract.load_dictionary(p)

    def test_aggregate_case_specific_ids_but_never_mix_scopes_or_references(self):
        a=contract.compare_metrics(metric(),metric('gem5'))
        f=metric();g=metric('gem5')
        for m in (f,g):
            m['measurement'].update(window_id='another-case',task_ids=[202])
        b=contract.compare_metrics(f,g)
        self.assertEqual(contract.aggregate_accuracy([a,b])['events']['perf_l2_demand_misses']['scored_cases'],2)
        other=deepcopy(b);other['measurement']['scope']='kernel'
        with self.assertRaises(ValueError):contract.aggregate_accuracy([a,other])
        other=deepcopy(b);other['reference_producer']='hardware'
        with self.assertRaises(ValueError):contract.aggregate_accuracy([a,other])

    def test_retired_events_cannot_exceed_their_macro_population(self):
        m=metric();m['retired_macro_instructions']=0
        with self.assertRaisesRegex(ValueError,'macro|population'):
            contract.validate_metric(m,'fastsim')

    def test_missing_capability_is_unavailable_even_when_count_is_zero(self):
        m=metric(); name='perf_l2_demand_misses'; row=m['events'][name]
        row['coverage']['speculative_data']=False
        row.update(diagnostic_value=0,value=None)
        out=contract.validate_metric(m,'fastsim')['events'][name]
        self.assertFalse(out['available'])
        self.assertIn('speculative_data',out['missing_capabilities'])
        row['value']=0
        with self.assertRaisesRegex(ValueError,'unavailable|coverage'):
            contract.validate_metric(m,'fastsim')

    def test_missing_source_does_not_poison_independent_retired_count(self):
        f=metric(); g=metric('gem5')
        g['events']['perf_l1d_load_misses']['coverage']['member_service_facts']=False
        g['events']['perf_l1d_load_misses']['value']=None
        compared=contract.compare_metrics(f,g)
        self.assertEqual(compared['events']['perf_retired_loads']['status'],'scored')
        self.assertEqual(compared['events']['perf_l1d_load_misses']['status'],'unavailable')
        self.assertEqual(compared['hardware_equivalence'],'unverified')

    def test_no_legacy_or_path_class_reference_fallback(self):
        with self.assertRaisesRegex(ValueError,'namespace|schema'):
            contract.validate_metric({'perf_l2_demand_misses':10},'fastsim')
        g=metric('gem5');g['evidence_source']='taotrace-path-class-v3'
        with self.assertRaisesRegex(ValueError,'source'):
            contract.compare_metrics(metric(),g)
        g=metric('gem5');g['oracle_only']=False
        with self.assertRaisesRegex(ValueError,'oracle'):
            contract.compare_metrics(metric(),g)

    def test_counter_identity_scope_and_window_must_match(self):
        for change in ('encoding','unit','scope','window','cores','tasks','context','target','boolean_count'):
            with self.subTest(change=change):
                f=metric();g=metric('gem5')
                if change=='encoding':g['events']['perf_l2_demand_misses']['umask']=0x21
                if change=='unit':g['events']['perf_l1d_load_misses']['unit']='cache-line'
                if change=='scope':g['measurement']['scope']='kernel'
                if change=='window':g['measurement']['window_id']='different'
                if change=='cores':g['measurement']['core_ids']=[0,0]
                if change=='tasks':g['measurement']['task_ids']=[102]
                if change=='context':g['measurement']['context_policy']='origin-owner-for-all-events'
                if change=='target':g['hardware_target']['model']=85
                if change=='boolean_count':g['events']['perf_l2_demand_misses']['value']=True
                with self.assertRaises(ValueError):contract.compare_metrics(f,g)

    def test_raw_and_rate_errors_are_not_pooled_and_max_ape_is_kept(self):
        f=metric();g=metric('gem5');f['retired_macro_instructions']=200
        name='perf_l2_demand_misses';f['events'][name].update(value=3,diagnostic_value=3)
        a=contract.compare_metrics(f,g)
        b=contract.compare_metrics(metric(),metric('gem5'))
        out=contract.aggregate_accuracy([a,b])['events'][name]
        self.assertEqual(out['raw']['mape_percent'],100)
        self.assertEqual(out['raw']['max_ape_percent'],200)
        self.assertEqual(out['raw']['wape_percent'],100)
        self.assertEqual(out['per_1k_inst']['mape_percent'],25)
        self.assertEqual(out['per_1k_inst']['max_ape_percent'],50)

    def test_nonzero_over_zero_is_not_a_finite_ape_or_missing_oracle(self):
        f=metric();g=metric('gem5');name='perf_llc_misses_core'
        g['events'][name].update(value=0,diagnostic_value=0)
        out=contract.aggregate_accuracy([contract.compare_metrics(f,g)])['events'][name]['raw']
        self.assertEqual(out['zero_reference_nonzero_prediction_cases'],1)
        self.assertEqual(out['finite_ape_cases'],0)
        self.assertIsNone(out['mape_percent'])
        self.assertEqual(out['absolute_count_error'],1)

    def test_hardware_reference_requires_nonmultiplexed_raw_event_evidence(self):
        h=metric('hardware')
        with self.assertRaisesRegex(ValueError,'running|perf|hardware'):
            contract.compare_metrics(metric(),h)
        h['perf_read']=dict(time_enabled=100,time_running=100,scaled=False)
        result=contract.compare_metrics(metric(),h)
        self.assertEqual(result['reference_producer'],'hardware')
        self.assertEqual(result['hardware_equivalence'],'unverified')
        h['perf_read']['time_running']=99
        with self.assertRaisesRegex(ValueError,'multiplex|running'):
            contract.compare_metrics(metric(),h)


if __name__=='__main__':unittest.main()
