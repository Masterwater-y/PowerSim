"""The stdlib permits LLC bank count independent of memory-channel count."""
import configparser
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUBY = 'board.cache_hierarchy.ruby_system'


def target_ini():
    ini = configparser.ConfigParser(interpolation=None)
    ini.optionxform = str
    ini['board.clk_domain'] = {'clock': '333'}
    core = {k: '8' for k in ('fetchWidth', 'decodeWidth', 'renameWidth', 'dispatchWidth',
                             'issueWidth', 'wbWidth', 'commitWidth')}
    core.update(cpu_id='0', numROBEntries='192', LQEntries='32', SQEntries='114',
                numPhysIntRegs='256', numPhysFloatRegs='256', numPhysVecRegs='256',
                numPhysCCRegs='256', tsoStore='buffer', tsoStoreBufferSize='114', needsTSO='true')
    ini['board.processor.switch0.core'] = core
    ini['board.processor.switch0.core.instQueues'] = {'numEntries': '64'}
    for name in ('dtb', 'itb'):
        ini['board.processor.switch0.core.mmu.' + name] = {'size': '64'}
    ini[RUBY] = {'block_size_bytes': '64', 'number_of_virtual_networks': '5'}
    ini[RUBY + '.network'] = {'type': 'SimpleNetwork', 'endpoint_bandwidth': '1000', 'buffer_size': '0'}
    ini['lru'] = {'type': 'LRURP'}
    for family, count, kind, cache_name, size, assoc in (
        ('l1', 1, 'L0', 'Dcache', 32768, 8),
        ('l2', 1, 'L1', 'cache', 1048576, 8),
        ('l3', 8, 'L2', 'L2cache', 8388608, 16)):
        for i in range(count):
            controller = RUBY + f'.{family}_controllers{i}'
            ini[controller] = {'type': f'MESI_Three_Level_{kind}Cache_Controller',
                               'number_of_TBEs': '256', 'transitions_per_cycle': '32'}
            ini[controller + '.' + cache_name] = {'size': str(size), 'assoc': str(assoc), 'replacement_policy': 'lru'}
            if family == 'l1':
                ini[controller + '.Icache'] = dict(ini[controller + '.Dcache'])
    for i in range(2):
        ini[RUBY + f'.directory_controllers{i}'] = {'type': 'MESI_Three_Level_Directory_Controller'}
        controller = f'board.memory.mem_ctrl{i}'
        ini[controller] = {'mem_sched_policy': 'frfcfs', 'write_high_thresh_perc': '85', 'write_low_thresh_perc': '50'}
        ini[controller + '.dram'] = {
            'range': f'0:3221225472:{i}', 'type': 'DDR4_2400_8x8',
            'device_rowbuffer_size': '1024', 'devices_per_rank': '8',
            'burst_length': '8', 'device_bus_width': '8',
            'read_buffer_size': '64', 'write_buffer_size': '128',
            'page_policy': 'open_adaptive', 'addr_mapping': 'RoRaBaCoCh',
            'ranks_per_channel': '2', 'banks_per_rank': '16',
            'bank_groups_per_rank': '4', 'tREFI': '7800000', 'tRFC': '350000'}
    return ini


def native_spr_ini(core_count=4):
    ini = target_ini()
    base = 'board.processor.switch0.core'
    core = dict(ini[base])
    for field in ('tsoStore', 'tsoStoreBufferSize'):
        core.pop(field)
    core.update(type='BaseO3CPU', SQEntries='112', sprStoreBuffer='true',
                sprEarlyStorePrepare='true', tsoOrderedStoreLineWindow='4')
    for section in list(ini.sections()):
        if section.startswith(base):
            ini.remove_section(section)
    for i in range(core_count):
        root = f'board.processor.switch{i}.core'
        ini[root] = dict(core, cpu_id=str(i), instQueues=' '.join(
            root + '.' + name for name in ('executionIQ', 'memoryIQ', 'storeDataIQ')))
        for name, size, allowed, excluded in (
                ('executionIQ', 128, '', 'MemRead X86StoreAddress X86StoreData'),
                ('memoryIQ', 72, 'MemRead X86StoreAddress', ''),
                ('storeDataIQ', 48, 'X86StoreData', '')):
            ini[root + '.' + name] = dict(type='IQUnit', numEntries=str(size),
                fuPool=root + '.sprFUPool', allowedOpClasses=allowed,
                excludedOpClasses=excluded, numThreads='1', smtIQPolicy='Partitioned',
                smtIQThreshold='100')
        ini[root + '.sprFUPool'] = dict(type='FUPool', FUList=root + '.sprFUPool.FU0',
                                       branchPorts='0', storeDataPorts='0')
        ini[root + '.sprFUPool.FU0'] = dict(type='FUDesc', count='1',
                                         opList=root + '.sprFUPool.FU0.op0')
        ini[root + '.sprFUPool.FU0.op0'] = dict(type='OpDesc', opClass='IntAlu',
                                              opLat='1', issueLat='1', pipelined='true')
        for name in ('dtb', 'itb'):
            ini[root + '.mmu.' + name] = {'size': '64'}
        if i:
            for family in ('l1', 'l2'):
                old = RUBY + f'.{family}_controllers0'
                for section in list(ini.sections()):
                    if section == old or section.startswith(old + '.'):
                        ini[section.replace(old, RUBY + f'.{family}_controllers{i}')] = dict(ini[section])
    return ini


class EffectiveTargetTopologyTest(unittest.TestCase):
    def split_range_ini(self):
        ini = target_ini()
        for i in range(2, 4):
            ini[RUBY + f'.directory_controllers{i}'] = dict(
                ini[RUBY + '.directory_controllers0'])
            ini[f'board.memory.mem_ctrl{i}'] = dict(ini['board.memory.mem_ctrl0'])
            ini[f'board.memory.mem_ctrl{i}.dram'] = dict(ini['board.memory.mem_ctrl0.dram'])
        for i in range(4):
            bounds = '0:3221225472' if i < 2 else '4294967296:35433480192'
            ini[f'board.memory.mem_ctrl{i}.dram']['range'] = f'{bounds}:{i % 2}:64'
        return ini

    def test_split_x86_ram_keeps_hole_out_of_capacity_and_channels_per_range(self):
        run, profile = self.run_generator(self.split_range_ini())
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(profile['dram']['size_b'], 32 * 2**30)
        self.assertEqual(profile['dram']['num_channels'], 4)
        self.assertEqual(profile['dram']['channels_per_range'], 2)
        self.assertEqual(profile['dram']['memory_layout'], 'x86-low3g-high4g-v1')
        self.assertEqual(profile['dram']['physical_ranges'], [
            {'start': 0, 'size': 3 * 2**30},
            {'start': 4 * 2**30, 'size': 29 * 2**30}])

    def test_split_x86_ram_rejects_non_cacheline_interleaving(self):
        ini = self.split_range_ini()
        for i in range(4):
            section = ini[f'board.memory.mem_ctrl{i}.dram']
            section['range'] = section['range'].rsplit(':', 1)[0] + ':128'
        run, _ = self.run_generator(ini)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('interleave masks', run.stderr)

    def test_split_x86_ram_rejects_other_address_mapping(self):
        ini = self.split_range_ini()
        ini['board.memory.mem_ctrl0.dram']['addr_mapping'] = 'RoRaBaChCo'
        run, _ = self.run_generator(ini)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('RoRaBaCoCh', run.stderr)

    def test_split_x86_ram_rejects_duplicate_channel_match_within_high_range(self):
        ini = self.split_range_ini()
        ini['board.memory.mem_ctrl3.dram']['range'] = '4294967296:35433480192:0:64'
        run, _ = self.run_generator(ini)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('dense channel IDs', run.stderr)

    def test_split_x86_ram_rejects_unequal_controller_counts(self):
        ini = self.split_range_ini()
        ini.remove_section('board.memory.mem_ctrl3')
        ini.remove_section('board.memory.mem_ctrl3.dram')
        ini.remove_section(RUBY + '.directory_controllers3')
        run, _ = self.run_generator(ini)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('equal channel counts', run.stderr)

    def test_singleton_core_and_private_cache_names_without_zero_suffix(self):
        ini = target_ini()
        for old in list(ini.sections()):
            if (any(old.startswith(RUBY + f'.l3_controllers{i}') for i in range(1,8))
                    or old.startswith(RUBY + '.directory_controllers1')
                    or old.startswith('board.memory.mem_ctrl1')):
                ini.remove_section(old)
        for old in list(ini.sections()):
            new = old.replace('board.processor.switch0.', 'board.processor.switch.')
            new = new.replace('.l1_controllers0', '.l1_controllers')
            new = new.replace('.l2_controllers0', '.l2_controllers')
            new = new.replace('.l3_controllers0', '.l3_controllers')
            new = new.replace('.directory_controllers0', '.directory_controllers')
            new = new.replace('board.memory.mem_ctrl0', 'board.memory.mem_ctrl')
            if new != old:
                ini[new] = dict(ini[old])
                ini.remove_section(old)
        run, profile = self.run_generator(ini)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(profile['core']['num_cores'], 1)
        self.assertEqual(profile['cache']['l1d']['size_b'], 32768)
        self.assertEqual(profile['cache']['l2']['size_b'], 1048576)
        self.assertEqual(profile['cache']['l3']['num_banks'], 1)
        self.assertEqual(profile['dram']['num_channels'], 1)

    def test_mixed_singleton_and_indexed_duplicate_core_identity_is_rejected(self):
        ini = target_ini()
        ini['board.processor.switch.core'] = dict(ini['board.processor.switch0.core'])
        run, _ = self.run_generator(ini)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('core IDs must be dense', run.stderr)

    def run_generator(self, ini, include_target=False, mutate=None):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as work:
            work = Path(work)
            with (work / 'config.ini').open('w') as f:
                ini.write(f)
            for header in ('uarch_profile.hh', 'lru_banked.hh'):
                (work / header).write_text('/* LRU-only fixture oracle model */\n')
            run = subprocess.run([sys.executable, str(ROOT / 'tools/generate_fs_effective_target.py'),
                '--config', str(work / 'config.ini'), '--uarch-profile', str(work / 'tao_trace/uarch_profile.json'),
                '--effective-target', str(work / 'target.json'), '--taotrace-shared-root', str(work)],
                capture_output=True, text=True)
            profile_path = work / 'tao_trace/uarch_profile.json'
            profile = json.loads(profile_path.read_text()) if run.returncode == 0 else None
            if include_target:
                target = json.loads((work / 'target.json').read_text()) if run.returncode == 0 else None
                return run, profile, target
            if mutate and run.returncode == 0:
                target = json.loads((work / 'target.json').read_text())
                mutate(target, profile)
                target['source']['uarch_profile_semantic_sha256'] = hashlib.sha256(
                    json.dumps(profile, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
                (work / 'effective-target.json').write_text(json.dumps(target))
                profile_path.write_text(json.dumps(profile))
                sys.path.insert(0, str(ROOT / 'tools'))
                import validate_fs_oracle_identity
                return run, validate_fs_oracle_identity.validate_result_identity(work)
            return run, profile

    def test_native_spr_four_cores_preserve_split_queues_and_store_identity(self):
        run, profile, target = self.run_generator(native_spr_ini(), include_target=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(profile['core']['num_cores'], 4)
        pipeline = target['core']['pipeline']
        queues = pipeline['instructionQueues']
        self.assertEqual(queues['layout'], 'split')
        self.assertEqual(queues['aggregateEntries'], 248)
        self.assertFalse(queues['aggregateIsUnifiedCapacity'])
        self.assertNotIn('numIQEntries', pipeline)
        self.assertEqual([q['numEntries'] for q in queues['queues']], [128, 72, 48])
        self.assertTrue(queues['sharedFUPool'])
        self.assertEqual(pipeline['storeModel'], 'native-spr')
        self.assertTrue(pipeline['sprStoreBuffer'])
        self.assertEqual(pipeline['tsoOrderedStoreLineWindow'], 4)
        self.assertEqual(profile['core']['store_model'], 'native-spr')
        self.assertNotIn('tso_store', profile['core'])
        self.assertNotIn('tsoStore', pipeline)

    def test_native_spr_rejects_missing_routing_and_nonshared_pool(self):
        for defect in ('routing', 'pool'):
            with self.subTest(defect=defect):
                ini = native_spr_ini(1)
                queue = ini['board.processor.switch0.core.memoryIQ']
                if defect == 'routing':
                    del queue['allowedOpClasses']
                else:
                    queue['fuPool'] = 'unknown.pool'
                run, _ = self.run_generator(ini)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn('instruction queue', run.stderr)

    def test_native_spr_rejects_incomplete_or_mixed_store_mechanisms(self):
        for defect in ('missing', 'mixed'):
            with self.subTest(defect=defect):
                ini = native_spr_ini(1)
                core = ini['board.processor.switch0.core']
                if defect == 'missing':
                    del core['sprEarlyStorePrepare']
                else:
                    core['tsoStore'] = 'buffer'
                run, _ = self.run_generator(ini)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn('native SPR store', run.stderr)

    def test_mixed_timing_and_o3_topology_fails_explicitly(self):
        ini = native_spr_ini(1)
        ini['board.processor.switch1.core'] = {'type': 'BaseTimingSimpleCPU', 'cpu_id': '1'}
        run, _ = self.run_generator(ini)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('unsupported CPU topology', run.stderr)

    def test_native_identity_accepts_derived_sidecars_without_legacy_request(self):
        run, report = self.run_generator(native_spr_ini(1), mutate=lambda target, profile: None)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(report['valid'], report)

    def test_split_queue_routing_and_shared_fu_identity_are_homogeneous(self):
        for defect in ('capacity', 'routing', 'latency'):
            with self.subTest(defect=defect):
                ini = native_spr_ini(4)
                root = 'board.processor.switch3.core'
                if defect == 'capacity':
                    ini[root + '.executionIQ']['numEntries'] = '129'
                elif defect == 'routing':
                    ini[root + '.executionIQ']['excludedOpClasses'] = 'MemRead'
                else:
                    ini[root + '.sprFUPool.FU0.op0']['opLat'] = '3'
                run, _ = self.run_generator(ini)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn('non-homogeneous instruction queue identity', run.stderr)

    def test_native_identity_rejects_forged_matching_sidecars(self):
        for defect in ('store', 'queue', 'count', 'missing-store', 'scalar-iq'):
            with self.subTest(defect=defect):
                def mutate(target, profile):
                    if defect == 'store':
                        target['core']['pipeline']['sprStoreBuffer'] = False
                        profile['core']['spr_store_buffer'] = False
                    elif defect == 'queue':
                        target['core']['pipeline']['instructionQueues']['queues'][0]['numEntries'] = 129
                    elif defect == 'count':
                        target['core']['count'] = profile['core']['num_cores'] = 2
                    elif defect == 'missing-store':
                        del target['core']['pipeline']['sprStoreBuffer']
                    else:
                        target['core']['pipeline']['numIQEntries'] = 248
                run, report = self.run_generator(native_spr_ini(1), mutate=mutate)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertFalse(report['valid'])
                self.assertTrue(any(m['field'].startswith('config.core.')
                                    for m in report['mismatches']), report)

    def test_eight_llc_banks_two_memory_channels_keep_distinct_identity(self):
        run, profile = self.run_generator(target_ini())
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(profile['cache']['l3']['size_b'], 67108864)
        self.assertEqual(profile['cache']['l3']['num_banks'], 8)
        self.assertEqual(profile['dram']['num_channels'], 2)
        self.assertEqual(profile['dram']['size_b'], 3221225472)

    def test_directory_memory_channel_mismatch_remains_rejected(self):
        ini = target_ini()
        ini.remove_section(RUBY + '.directory_controllers1')
        run, _ = self.run_generator(ini)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('memory channel and directory controller counts differ', run.stderr)


if __name__ == '__main__':
    unittest.main()
