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
    def run_generator(self, ini):
        with tempfile.TemporaryDirectory() as work:
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
