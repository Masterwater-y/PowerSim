"""Actual-object import, typed resource units and fail-closed profile evidence."""
import configparser
import json
import runpy
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools/generate_spr_mechanism_profile.py'
CAPTURED = ROOT / 'tmp/spr-speed-c4-20261104/source/c04-872.marian_s/config.ini'


def fixture():
    c = configparser.ConfigParser()
    c['board.processor'] = {'type': 'SubSystem', 'cores': 'board.processor.switch0'}
    c['board.processor.switch0'] = {'type': 'SubSystem', 'core': 'board.processor.switch0.core'}
    c['board.processor.switch0.core'] = {
        'type': 'BaseO3CPU', 'cpu_id': '0', 'instQueues': 'exec mem data',
        'SQEntries': '112', 'tsoOrderedStoreLineWindow': '4', 'sprStoreBuffer': 'true',
        'sprEarlyStorePrepare': 'true', 'sprL1LoadBankConflicts': 'true',
        'decoupledFrontEnd': 'false', 'recvRespThrottling': 'false',
        'cacheLoadPorts': '3', 'cacheStorePorts': '2', 'mmu': 'mmu',
    }
    for name, n in [('exec', 128), ('mem', 72), ('data', 48), ('orphanIQ', 64)]:
        c[name] = {'type': 'IQUnit', 'numEntries': str(n)}
    # Native routing is filter/FU based; vector order is not an IQ role.
    memory_ops = ('FloatMemRead FloatMemWrite MemRead MemWrite SimdIndexedLoad SimdIndexedStore '
                  'SimdStrideSegmentedLoad SimdStrideSegmentedStore SimdStridedLoad SimdStridedStore '
                  'SimdUnitStrideFaultOnlyFirstLoad SimdUnitStrideLoad SimdUnitStrideMaskLoad '
                  'SimdUnitStrideMaskStore SimdUnitStrideSegmentedFaultOnlyFirstLoad '
                  'SimdUnitStrideSegmentedLoad SimdUnitStrideSegmentedStore SimdUnitStrideStore '
                  'SimdWholeRegisterLoad SimdWholeRegisterStore X86StoreAddress')
    for q in ['exec', 'mem', 'data']:
        c[q].update({'allowedOpClasses': '', 'excludedOpClasses': '', 'fuPool': 'pool'})
    c['exec']['excludedOpClasses'] = memory_ops + ' X86StoreData'
    c['mem']['allowedOpClasses'] = memory_ops
    c['data']['allowedOpClasses'] = 'X86StoreData'
    c['pool'] = {'type': 'FUPool', 'FUList': 'fu', 'branchPorts': '0 6', 'storeDataPorts': '4 9'}
    ops = ['IntAlu'] + memory_ops.split() + ['X86StoreData']
    c['fu'] = {'type': 'FUDesc', 'count': '1', 'opList': ' '.join('op' + str(i) for i in range(len(ops)))}
    for i, op in enumerate(ops):
        c['op' + str(i)] = {'type': 'OpDesc', 'opClass': op, 'opLat': '1', 'issueLat': '1', 'pipelined': 'true'}
    c['mmu'] = {'type': 'X86MMU', 'itb': 'itlb', 'dtb': 'dtlb'}
    for name, n, latency, typ in [('itlb', 256, 7, 'instruction'), ('dtlb', 96, 6, 'data')]:
        c[name] = {'type': 'X86TLB', 'size': str(n), 'next_level': 'stlb',
                   'next_level_hit_latency': str(latency), 'entry_type': typ}
    c['stlb'] = {'type': 'X86TLB', 'size': '2048', 'spr_shared_domains': 'true', 'entry_type': 'unified'}
    c['board.cache_hierarchy.ruby_system'] = {'type': 'RubySystem', 'children': 'l1 l2 llc dma'}
    r = 'board.cache_hierarchy.ruby_system.'
    c[r+'l1'] = {'type': 'MESI_Three_Level_L0Cache_Controller', 'sequencer': 'seq',
                  'Dcache': 'dcache', 'number_of_TBEs': '256', 'bufferToL1': 'up',
                  'prefetcher': 'pf1', 'enable_prefetch': 'true','response_latency':'2','request_latency':'2'}
    c[r+'l2'] = {'type': 'MESI_Three_Level_L1Cache_Controller', 'cache': 'l2cache',
                  'number_of_TBEs': '256', 'bufferFromL0': 'up', 'prefetcher': 'pf2', 'enable_prefetch': 'true','l1_request_latency':'2','l1_response_latency':'2'}
    c[r+'llc'] = {'type': 'MESI_Three_Level_L2Cache_Controller', 'number_of_TBEs': '256','to_l1_latency':'1'}
    c[r+'dma'] = {'type': 'DMASequencer', 'max_outstanding_requests': '64'}
    c['seq'] = {'type': 'RubySequencer', 'version': '0', 'max_outstanding_requests': '16',
                'max_store_prepare_outstanding': '16', 'dcache': 'dcache',
                'in_ports': 'board.processor.switch0.core.dcache_port'}
    c['dcache'] = {'type': 'RubyCache', 'fill_capacity': '16'}
    c['l2cache'] = {'type': 'RubyCache', 'fill_capacity': '48'}
    for name, ip in [('pf1', 'true'), ('pf2', 'false')]:
        c[name] = {'type': 'RubyPrefetcher', 'ip_stride': ip, 'cross_page': 'false',
                   'queue_dedup': 'true', 'num_streams': '4', 'unit_filter': '8',
                   'nonunit_filter': '8', 'train_misses': '4', 'num_startup_pfs': '1', 'page_shift': '12'}
    c[r+'llc']['L2cache']='llccache'
    c['llccache']={'type':'RubyCache','replacement_policy':'rp'}
    c['dcache']['replacement_policy']='rp';c['l2cache']['replacement_policy']='rp'
    c['rp']={'type':'LRURP'}
    c[r+'l1']['mandatory_queue_latency']='1';c[r+'l1']['mandatoryQueue']='mandatory'
    c['mandatory']={'type':'MessageBuffer','ordered':'false'}
    c['dcache']['dataAccessLatency']='3';c['seq']['clk_domain']='clock'
    c['clock']={'type':'SrcClockDomain','clock':'385'}
    for obj in ('board.processor.switch0.core',r+'l1',r+'l2',r+'llc'):
        c[obj]['clk_domain']='clock'
    return c


class SprMechanismProfileTest(unittest.TestCase):
    def invoke(self, cfg=None, *, ids=None, captured=False, capabilities=None):
        with tempfile.TemporaryDirectory(prefix='spr-profile-', dir=ROOT/'tmp') as d:
            d = Path(d)
            path = CAPTURED if captured else d/'config.ini'
            if not captured:
                with path.open('w') as out:
                    (cfg or fixture()).write(out)
            command = [sys.executable, str(TOOL), '--config', str(path), '--output', str(d/'profile.json')]
            if ids is not None:
                command += ['--participant-ids', ids]
            if capabilities is not None:
                (d/'caps.json').write_text(json.dumps(capabilities))
                command += ['--source-capabilities', str(d/'caps.json')]
            result = subprocess.run(command, capture_output=True, text=True)
            data = json.loads((d/'profile.json').read_text()) if (d/'profile.json').exists() else None
            return result, data

    def success(self, **kwargs):
        result, data = self.invoke(**kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return data

    def rejection(self, cfg, message, **kwargs):
        result, data = self.invoke(cfg, **kwargs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(data)
        self.assertIn(message, result.stderr)

    def test_actual_referenced_iqs_and_cpu_sequencer_not_orphans_or_dma(self):
        p = self.success()['per_core'][0]
        self.assertEqual([q['entries']['value'] for q in p['iq']], [128, 72, 48])
        self.assertEqual(p['limits']['ordinary_requests']['value'], 16)
        self.assertEqual(p['limits']['prepare_requests']['value'], 16)
        self.assertEqual(p['limits']['l1_data_fills']['value'], 16)
        self.assertEqual(p['limits']['l2_data_fills']['value'], 48)
        self.assertEqual(p['limits']['l1_tbes']['value'], 256)
        self.assertEqual(p['limits']['shared_stores']['unit'], 'store_entries')
        self.assertEqual(p['limits']['distinct_store_lines']['unit'], 'cache_lines')
        self.assertEqual(p['limits']['offload_width']['value'], 4)
        self.assertEqual(p['load_bank']['banks'], 16)
        self.assertEqual(p['load_bank']['ports_per_bank'], 2)
        self.assertEqual(p['cache_ports'], {'load': 3, 'store': 2})

    def test_protocol_edges_have_individual_native_parameter_provenance(self):
        c=fixture();r='board.cache_hierarchy.ruby_system.';c[r+'l1']['response_latency']='2'
        c[r+'l2']['l1_request_latency']='2';c[r+'l2']['l1_response_latency']='2'
        c[r+'llc']['to_l1_latency']='1'
        p=self.success(cfg=c);edges=p['per_core'][0]['protocol_edges']
        self.assertEqual([edges[k]['value'] for k in ['l1_response_latency','l2_request_latency','l2_response_latency','llc_to_l2_latency']],[2,2,2,1])
        self.assertIn('board.cache_hierarchy.ruby_system.l1.response_latency',edges['l1_response_latency']['source_keys'])
        self.assertIn('clock.clock',edges['l1_response_latency']['source_keys'])
        self.assertEqual(edges['l1_response_latency']['source_clock_period_ticks'],385)
        self.assertEqual(p['flat_config']['spr.memory.llc_to_l2_latency'],1)

    def test_native_cache_replacement_follows_target_rather_than_generic_defaults(self):
        p=self.success()
        self.assertEqual([p['flat_config']['cache.'+key+'.replacement'] for key in ('l1d','l2','llc')],['lru']*3)
        self.assertEqual(p['per_core'][0]['cache_replacement']['l1d']['source_keys'],['dcache.replacement_policy','rp.type'])
        c=fixture();c['rp']['type']='TreePLRURP'
        self.rejection(c,'native replacement policy')

    def test_mandatory_entry_service_and_order_are_independent_target_fields(self):
        p=self.success();entry=p['per_core'][0]['mandatory_entry']
        self.assertEqual(entry['mandatory_queue_latency']['value'],1)
        self.assertEqual(entry['data_access_latency']['value'],3)
        self.assertFalse(entry['ordered'])
        self.assertEqual(entry['effective_service_cycles'],3)
        self.assertEqual(p['flat_config']['spr.memory.l1_data_access_latency'],3)
        self.assertEqual(p['flat_config']['spr.memory.mandatory_queue_ordered'],'false')
        c=fixture();c['mandatory']['ordered']='true';changed=self.success(cfg=c)
        self.assertTrue(changed['per_core'][0]['mandatory_entry']['ordered'])
        c=fixture();c['seq_clock']={'type':'SrcClockDomain','clock':'1000'};c['seq']['clk_domain']='seq_clock'
        self.rejection(c,'clock-domain conversion')

    def test_protocol_cycle_units_reject_unmodeled_clock_phase_conversion(self):
        c=fixture();c['ruby_clock']={'type':'SrcClockDomain','clock':'1000'}
        c['board.cache_hierarchy.ruby_system.l1']['clk_domain']='ruby_clock'
        self.rejection(c,'clock-domain conversion')

    def test_iq_role_follows_filters_when_reference_vector_reordered(self):
        c = fixture(); c['board.processor.switch0.core']['instQueues'] = 'mem data exec'
        p = self.success(cfg=c)['per_core'][0]
        self.assertEqual([(q['role'], q['object'], q['entries']['value']) for q in p['iq']],
                         [('execution', 'exec', 128), ('memory', 'mem', 72), ('store_data', 'data', 48)])
        self.assertEqual(p['iq'][1]['routing']['allowed_opclasses'][2], 'MemRead')
        self.assertEqual(p['iq'][1]['routing']['fu_pool'], 'pool')
        self.assertIn('mem.allowedOpClasses', p['iq'][1]['routing']['source_keys'])
        self.assertIn('pool.FUList', p['iq'][1]['routing']['source_keys'])

    def test_changed_and_ambiguous_iq_filters_fail_closed(self):
        for key, value in [('allowedOpClasses', 'MemRead'), ('excludedOpClasses', 'MemWrite')]:
            c = fixture(); c['mem'][key] = value
            self.rejection(c, 'IQ routing')
        c = fixture(); c['data']['allowedOpClasses'] = c['mem']['allowedOpClasses']
        self.rejection(c, 'IQ routing')

    def test_missing_or_incapable_fu_pool_fails_closed(self):
        c = fixture(); c['mem']['fuPool'] = 'missing'
        self.rejection(c, 'missing object')
        c = fixture(); c['fu']['opList'] = 'op0'
        self.rejection(c, 'FU capability')

    def test_dormant_flags_and_missing_capabilities_are_explicit(self):
        p = self.success()['per_core'][0]
        self.assertEqual(p['mechanisms']['ftq']['status'], 'disabled_by_target')
        self.assertEqual(p['mechanisms']['read_response_throttle']['status'], 'disabled_by_target')
        self.assertEqual(p['mechanisms']['macro_fusion']['status'], 'missing_functional_input')
        self.assertEqual(p['mechanisms']['store_buffer']['status'], 'unsupported')
        self.assertFalse(p['runtime_qualified'])

    def test_prefetch_and_stlb_follow_actual_reference(self):
        p = self.success()['per_core'][0]
        self.assertTrue(p['prefetch']['l1']['ip_stride'])
        self.assertFalse(p['prefetch']['l2']['ip_stride'])
        self.assertEqual(p['translation']['stlb']['entries']['value'], 2048)
        self.assertEqual(p['translation']['itlb']['next_level'], 'stlb')
        self.assertEqual(p['translation']['dtlb']['next_level'], 'stlb')

    def test_identity_and_proposed_config_never_enable_runtime(self):
        p = self.success()
        self.assertEqual(p['schema'], 'spr-mechanism-profile-v1')
        self.assertEqual(len(p['identity']['config_sha256']), 64)
        self.assertEqual(p['flat_config']['spr.profile.enabled'], 'false')
        self.assertEqual(p['flat_config']['spr.profile.ordinary_requests'], 16)
        self.assertTrue(p['per_core'][0]['limits']['offload_width']['source_keys'])
        self.assertTrue(p['identity']['source_rules'])
        self.assertIn('shared_store_budget', p['identity']['source_rules'])
        self.assertIn('distinct_store_line_window', p['identity']['source_rules'])

    def test_static_uop_policy_binds_child_sources_and_stays_disabled(self):
        p=self.success()
        digest=p['identity']['static_uop_policy_sha256']
        self.assertEqual(len(digest),64)
        self.assertEqual(p['flat_config']['spr.profile.static_uop_policy_sha256'],digest)
        self.assertFalse(p['per_core'][0]['runtime_qualified'])

    def test_policy_covers_media_zero_idiom_decoder_and_operand_sources(self):
        native=ROOT/'gem5-spr'
        policy=runpy.run_path(str(native/'src/cpu/o3/probe/generate_static_uop_policy.py'))
        files=policy['policy_files'](native)
        self.assertIn('src/arch/x86/isa/microops/mediaop.isa',files)
        self.assertIn('src/arch/x86/isa/microops/ldstop.isa',files)
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            copy=Path(directory)
            for relative in files:
                target=copy/relative;target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(native/relative,target)
            before=policy['policy_digest'](copy)
            media=copy/'src/arch/x86/isa/microops/mediaop.isa'
            media.write_text(media.read_text()+'\n// decoder semantics test mutation\n')
            self.assertNotEqual(before,policy['policy_digest'](copy))

    def test_declared_functional_facts_do_not_claim_runtime_support(self):
        p = self.success(capabilities={'macro_fusion_facts': True})['per_core'][0]
        self.assertEqual(p['mechanisms']['macro_fusion']['status'], 'unsupported')

    def test_unknown_stlb_domain_is_not_assumed_unified(self):
        c = fixture(); c['stlb']['entry_type'] = 'instruction'
        self.rejection(c, 'STLB')

    def test_source_identity_includes_importer_and_functional_declaration(self):
        p = self.success(capabilities={'macro_fusion_facts': True})
        self.assertIn('generator_sha256', p['identity'])
        self.assertEqual(len(p['identity']['generator_sha256']), 64)
        self.assertEqual(len(p['identity']['source_capabilities_sha256']), 64)
        self.assertTrue(p['per_core'][0]['mechanisms']['ftq']['source_keys'])

    def test_dangling_iq_rejected(self):
        c = fixture(); c['board.processor.switch0.core']['instQueues'] = 'exec missing data'
        self.rejection(c, 'missing object')

    def test_unknown_iq_type_rejected(self):
        c = fixture(); c['mem']['type'] = 'UnrecognizedIQ'
        self.rejection(c, 'expected IQUnit')

    def test_zero_capacity_rejected(self):
        c = fixture(); c['dcache']['fill_capacity'] = '0'
        self.rejection(c, 'positive')

    def test_unknown_participant_rejected(self):
        self.rejection(fixture(), 'participant', ids='7')

    def test_cpu_is_not_inferred_from_unknown_sequencer_version(self):
        c = fixture(); c['seq']['in_ports'] = 'unknown.core.dcache_port'
        self.rejection(c, 'CPU port')

    def test_dangling_tlb_and_split_stlb_rejected(self):
        c = fixture(); c['dtlb']['next_level'] = 'missing'
        self.rejection(c, 'missing object')
        c = fixture(); c['stlb2'] = dict(c['stlb']); c['dtlb']['next_level'] = 'stlb2'
        self.rejection(c, 'shared STLB')

    @unittest.skipUnless(CAPTURED.exists(), 'actual saved Marian configuration unavailable')
    def test_captured_marian_iq_reference_reordering_preserves_native_roles(self):
        c = configparser.ConfigParser(); c.read(CAPTURED)
        cpu = c['board.processor.switch0.core']
        queues = cpu['instQueues'].split()
        cpu['instQueues'] = ' '.join([queues[1], queues[0], queues[2]])
        p = self.success(cfg=c)['per_core'][0]
        self.assertEqual([(q['role'], q['entries']['value']) for q in p['iq']],
                         [('execution', 128), ('memory', 72), ('store_data', 48)])

    @unittest.skipUnless(CAPTURED.exists(), 'actual saved Marian configuration unavailable')
    def test_heterogeneous_fu_policy_fails_closed(self):
        c = configparser.ConfigParser(); c.read(CAPTURED)
        pool = c[c['board.processor.switch1.core.memoryIQ']['fuPool']]
        fu = pool['FUList'].split()[0]
        op = c[fu]['opList'].split()[0]
        c[op]['opLat'] = '99'
        self.rejection(c, 'heterogeneous CPU')

    @unittest.skipUnless(CAPTURED.exists(), 'actual saved Marian configuration unavailable')
    def test_heterogeneous_cpu_iq_capacity_fails_closed(self):
        c = configparser.ConfigParser(); c.read(CAPTURED)
        c['board.processor.switch1.core.memoryIQ']['numEntries'] = '73'
        self.rejection(c, 'heterogeneous CPU')

    @unittest.skipUnless(CAPTURED.exists(), 'actual saved Marian configuration unavailable')
    def test_heterogeneous_integer_latency_resource_fails_closed(self):
        c=configparser.ConfigParser();c.read(CAPTURED)
        c['board.processor.switch1.core.intSlow']['prfDelay']='1'
        self.rejection(c,'heterogeneous CPU')

    @unittest.skipUnless(CAPTURED.exists(), 'actual saved Marian configuration unavailable')
    def test_captured_integer_result_paths_are_disabled_by_target(self):
        p=self.success(captured=True)
        for core in p['per_core']:
            integer=core['integer_execution']
            self.assertEqual(integer['result_paths']['status'],'disabled_by_target')
            self.assertEqual(integer['result_paths']['objects'],[])
            self.assertTrue(integer['nominal_shape_verified'])
            self.assertEqual(sorted(r['port'] for r in integer['resources'] if r['family']==1),[0,1,5,6,11])
            self.assertTrue(all(r['issue_interval']==1 and r['completion_delay']==0 and r['prf_delay']==0 for r in integer['resources']))

    @unittest.skipUnless(CAPTURED.exists(), 'actual saved Marian configuration unavailable')
    def test_captured_marian_all_participants_and_core_local_stlb(self):
        p = self.success(captured=True)
        self.assertEqual([c['core_id'] for c in p['per_core']], [0, 1, 2, 3])
        self.assertEqual(len({c['translation']['stlb']['object'] for c in p['per_core']}), 4)
        self.assertTrue(all(c['iq'][1]['entries']['value'] == 72 for c in p['per_core']))
        self.assertEqual(p['selection']['policy'], 'processor-switch-o3-children')


if __name__ == '__main__':
    unittest.main()
