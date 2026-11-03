"""The stdlib permits LLC bank count independent of memory-channel count."""
import configparser
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

    def run_generator(self, ini):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as work:
            work = Path(work)
            with (work / 'config.ini').open('w') as f:
                ini.write(f)
            for header in ('uarch_profile.hh', 'lru_banked.hh'):
                (work / header).write_text('/* LRU-only fixture oracle model */\n')
            run = subprocess.run([sys.executable, str(ROOT / 'tools/generate_fs_effective_target.py'),
                '--config', str(work / 'config.ini'), '--uarch-profile', str(work / 'profile.json'),
                '--effective-target', str(work / 'target.json'), '--taotrace-shared-root', str(work)],
                capture_output=True, text=True)
            profile = json.loads((work / 'profile.json').read_text()) if run.returncode == 0 else None
            return run, profile

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
