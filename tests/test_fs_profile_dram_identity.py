"""Source clock conversion and split-layout validation for profile identity."""
import configparser
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from audit_fs_profile_identity import target_cycles
from generate_fs_effective_target import dram_address_layout, RUBY_PREFIX

class DramProfileIdentityTest(unittest.TestCase):
    def test_cycle_conversion_uses_actual_period_and_rounds_up(self):
        self.assertEqual(target_cycles(14160, 333), 43)
        self.assertEqual(target_cycles(32000, 333), 97)
        self.assertEqual(target_cycles(1666, 333), 6)
        self.assertEqual(target_cycles(3332, 333), 11)
        self.assertEqual(target_cycles(10000, 333), 31)
        self.assertEqual(target_cycles(32000, 1000), 32)
        self.assertEqual(target_cycles(0, 333), 0)
        with self.assertRaises(ValueError): target_cycles(1, 0)

    def test_zero_padded_controller_interfaces_keep_actual_split_capacity(self):
        p = configparser.ConfigParser(interpolation=None)
        p[RUBY_PREFIX] = {'block_size_bytes': '64'}
        interfaces = []
        for i in range(16):
            name = f'board.memory.mem_ctrl{i:02d}.dram'; interfaces.append(name)
            bounds = '0:3221225472' if i < 8 else '4294967296:35433480192'
            p[name] = {'range': f'{bounds}:{i % 8}:64:128:256', 'addr_mapping': 'RoRaBaCoCh'}
        size, layout = dram_address_layout(p, interfaces)
        self.assertEqual(size, 32 * 2**30)
        self.assertEqual(layout['channels_per_range'], 8)
        p[interfaces[9]]['range'] = '4294967296:35433480192:1:64:128:512'
        with self.assertRaisesRegex(ValueError, 'interleave masks'):
            dram_address_layout(p, interfaces)

if __name__ == '__main__': unittest.main()
