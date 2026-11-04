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
        'commitToIEWDelay':'1', 'SQEntries': '112', 'tsoOrderedStoreLineWindow': '4', 'sprStoreBuffer': 'true',
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
    c[r+'l1']['transitions_per_cycle']='32'
    c['mandatory']={'type':'MessageBuffer','ordered':'false'}
    c['dcache']['dataAccessLatency']='3';c['seq']['clk_domain']='clock'
    c['clock']={'type':'SrcClockDomain','clock':'385'}
    for obj in ('board.processor.switch0.core',r+'l1',r+'l2',r+'llc'):
        c[obj]['clk_domain']='clock'
    return c


def integer_fixture():
    c=fixture();cpu=c['board.processor.switch0.core']
    cpu['resultForwarding']='forward';c['forward']={'type':'ResultForwarding','paths':''}
    cpu['executionPorts']='shared';cpu['intALU']='Null'
    c['shared']={'type':'ExecutionPorts','ports':' '.join('port'+str(i) for i in range(12))}
    for port in range(12):
        c['port'+str(port)]={'type':'ExecutionPort','number':str(port)}
    alus=[]
    for port in (0,1,5,6,11):
        name='alu'+str(port);alus.append(name)
        c[name]={'type':'IntegerALU','family':'1','port':'port'+str(port),
                 'issueInterval':'1','completionDelay':'0','prfDelay':'0'}
    cpu['intALUs']=' '.join(alus)
    for key,family in [('intSHFT',2),('intSlow',3),('intBM',4),('intBranch',5)]:
        cpu[key]=key;c[key]={'type':'IntExecutionResource','family':str(family),
                           'issueInterval':'1','completionDelay':'0','prfDelay':'0'}
    # Native FUPool construction sums FUDesc.count; each selected pool needs12.
    for index in range(1,12):
        c['fu'+str(index)]=dict(c['fu'])
    c['pool']['FUList']=' '.join(['fu']+['fu'+str(i) for i in range(1,12)])
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

    def test_commit_to_iew_delay_is_typed_and_sourced(self):
        for delay in (0,1,3):
            c=fixture();c['board.processor.switch0.core']['commitToIEWDelay']=str(delay)
            p=self.success(cfg=c)
            value=p['per_core'][0]['store_commit_to_iew_delay']
            self.assertEqual(value['value'],delay)
            self.assertEqual(value['unit'],'core_cycles')
            self.assertEqual(value['source_keys'],['board.processor.switch0.core.commitToIEWDelay'])
            self.assertEqual(p['flat_config']['core.store_commit_to_iew_delay'],delay)
        c=fixture();del c['board.processor.switch0.core']['commitToIEWDelay']
        self.rejection(c,'commitToIEWDelay')
        c=fixture();c['board.processor.switch0.core']['commitToIEWDelay']='-1'
        self.rejection(c,'commitToIEWDelay')

    def test_l0_transition_budget_is_imported_positive_and_sourced(self):
        for budget in (1,7,32):
            c=fixture();obj='board.cache_hierarchy.ruby_system.l1'
            c[obj]['transitions_per_cycle']=str(budget)
            p=self.success(cfg=c)
            self.assertIn('l0_transitions_per_cycle',list(p['per_core'][0]))
            value=p['per_core'][0]['l0_transitions_per_cycle']
            self.assertEqual(value['value'],budget)
            self.assertEqual(value['unit'],'successful_transitions_per_controller_cycle')
            self.assertEqual(value['source_keys'],[obj+'.transitions_per_cycle'])
            self.assertEqual(p['flat_config']['spr.memory.l0_transitions_per_cycle'],budget)
        c=fixture();del c[obj]['transitions_per_cycle'];self.rejection(c,'transitions_per_cycle')
        c=fixture();c[obj]['transitions_per_cycle']='0';self.rejection(c,'transitions_per_cycle')

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

    def test_integer_flat_gate_requires_exact_resources_and_empty_paths(self):
        c=integer_fixture();cpu=c['board.processor.switch0.core']
        self.assertEqual(self.success(cfg=c)['flat_config'].get('spr.profile.integer_nominal_shape_verified'),'true')
        c['intSHFT']['family']='3';c['intSlow']['family']='2'
        self.assertEqual(self.success(cfg=c)['flat_config'].get('spr.profile.integer_nominal_shape_verified'),'false')
        c['intSHFT']['family']='2';c['intSlow']['family']='3'
        c['intSlow']['prfDelay']='1'
        self.assertEqual(self.success(cfg=c)['flat_config'].get('spr.profile.integer_nominal_shape_verified'),'false')
        c['intSlow']['prfDelay']='0';c['forward']['paths']='rule'
        c['rule']={'type':'ResultPathRule','producerFamily':'Any','producerPort':'-1','resultKind':'Any',
                   'consumerFamily':'Any','consumerPort':'-1','sourceIndex':'-1','delay':'0'}
        p=self.success(cfg=c)
        self.assertEqual(p['flat_config'].get('spr.profile.integer_nominal_shape_verified'),'false')
        self.assertEqual(p['per_core'][0]['integer_execution']['result_paths']['status'],'unsupported')
        del cpu['resultForwarding']
        self.assertEqual(self.success(cfg=c)['flat_config'].get('spr.profile.integer_nominal_shape_verified'),'false')

    def test_integer_gate_requires_connected_shared_port_graph_and_native_fu_slots(self):
        def rejected_or_unverified(config):
            result,p=self.invoke(cfg=config)
            if result.returncode:
                self.assertIsNone(p)
            else:
                self.assertEqual(p['flat_config']['spr.profile.integer_nominal_shape_verified'],'false')
        cpu='board.processor.switch0.core'
        cases=('missing_binding','null_binding','dangling_binding','missing_vector','null_member',
               'missing_number','duplicate_reference','duplicate_number','out_of_range_number',
               'disconnected_alu','legacy_conflict','pool11','pool13')
        for case in cases:
            with self.subTest(case=case):
                c=integer_fixture()
                if case=='missing_binding':del c[cpu]['executionPorts']
                elif case=='null_binding':c[cpu]['executionPorts']='Null'
                elif case=='dangling_binding':c[cpu]['executionPorts']='absent'
                elif case=='missing_vector':del c['shared']['ports']
                elif case=='null_member':c['shared']['ports']=c['shared']['ports'].replace('port4 ','Null ')
                elif case=='missing_number':c['shared']['ports']=c['shared']['ports'].replace('port4 ','')
                elif case=='duplicate_reference':c['shared']['ports']=c['shared']['ports'].replace('port4 ','port5 ')
                elif case=='duplicate_number':c['port4']['number']='5'
                elif case=='out_of_range_number':c['port4']['number']='12'
                elif case=='disconnected_alu':
                    c['otherPort0']=dict(c['port0']);c['alu0']['port']='otherPort0'
                elif case=='legacy_conflict':
                    c['legacy']=dict(c['intSHFT']);c['legacy']['family']='1';c[cpu]['intALU']='legacy'
                elif case=='pool11':c['fu']['count']='0'
                elif case=='pool13':c['fu']['count']='2'
                rejected_or_unverified(c)
        p=self.success(cfg=integer_fixture())
        self.assertEqual(p['flat_config']['spr.profile.integer_nominal_shape_verified'],'true')
        graph=p['per_core'][0]['integer_execution']['execution_ports']
        self.assertEqual(graph['object'],'shared')
        self.assertEqual(graph['native_fu_slots'],12)
        self.assertEqual([(port['number'],port['object']) for port in graph['ports']],
                         [(i,'port'+str(i)) for i in range(12)])
        self.assertIn(cpu+'.executionPorts',graph['source_keys'])
        self.assertIn('fu.count',graph['source_keys'])
        # Native table placement uses each port's number, and numFU ignores
        # disabled descriptors; serialized vector order/list length is not shape.
        c=integer_fixture();c['shared']['ports']=' '.join(reversed(c['shared']['ports'].split()))
        c['disabled']=dict(c['fu']);c['disabled']['count']='0';c['pool']['FUList']+=' disabled'
        self.assertEqual(self.success(cfg=c)['flat_config']['spr.profile.integer_nominal_shape_verified'],'true')

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
    def test_integer_cross_core_shared_coordinator_or_ports_fail_closed(self):
        for shared in ('fu_pool','execution_ports','port_objects'):
            with self.subTest(shared=shared):
                c=configparser.ConfigParser();c.read(CAPTURED)
                first='board.processor.switch0.core';second='board.processor.switch1.core'
                if shared=='fu_pool':
                    pool=c[c[first]['instQueues'].split()[0]]['fuPool']
                    for iq in c[second]['instQueues'].split():c[iq]['fuPool']=pool
                else:
                    ports=c[first]['executionPorts']
                    if shared=='execution_ports':c[second]['executionPorts']=ports
                    else:c[c[second]['executionPorts']]['ports']=c[ports]['ports']
                    selected={int(c[port]['number']):port for port in c[ports]['ports'].split()}
                    for alu in c[second]['intALUs'].split():
                        number=int(c[c[alu]['port']]['number']);c[alu]['port']=selected[number]
                self.rejection(c,'cross-core shared integer')

    @unittest.skipUnless(CAPTURED.exists(), 'actual saved Marian configuration unavailable')
    def test_heterogeneous_integer_latency_resource_fails_closed(self):
        c=configparser.ConfigParser();c.read(CAPTURED)
        c['board.processor.switch1.core.intSlow']['prfDelay']='1'
        self.rejection(c,'heterogeneous CPU')

    @unittest.skipUnless(CAPTURED.exists(), 'actual saved Marian configuration unavailable')
    def test_captured_integer_result_paths_are_disabled_by_target(self):
        p=self.success(captured=True)
        self.assertEqual(p['flat_config']['spr.profile.integer_nominal_shape_verified'],'true')
        for core in p['per_core']:
            integer=core['integer_execution']
            self.assertEqual(integer['result_paths']['status'],'disabled_by_target')
            self.assertEqual(integer['result_paths']['objects'],[])
            self.assertTrue(integer['nominal_shape_verified'])
            graph=integer['execution_ports']
            self.assertEqual(graph['native_fu_slots'],12)
            self.assertEqual([port['number'] for port in graph['ports']],list(range(12)))
            selected={port['number']:port['object'] for port in graph['ports']}
            self.assertTrue(all(resource['port_object']==selected[resource['port']]
                                for resource in integer['resources'] if resource['family']==1))
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
