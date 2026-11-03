import configparser
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import audit_fs_profile_identity as audit

class IndexedSectionIdentityTest(unittest.TestCase):
    def find(self,names,prefix='board.processor.switch',suffix='.core'):
        ini=configparser.ConfigParser()
        for name in names:ini.add_section(name)
        return audit.indexed_section(ini,prefix,0,suffix)
    def test_padded_and_unpadded_cpu_names(self):
        for name in ['board.processor.switch0.core','board.processor.switch00.core']:
            self.assertEqual(self.find([name,name+'.mmu']),name)
    def test_numeric_identity_excludes_other_cores(self):
        self.assertEqual(self.find(['board.processor.switch10.core','board.processor.switch00.core']), 'board.processor.switch00.core')
    def test_missing_or_ambiguous_zero_fails_closed(self):
        for names in [[],['board.processor.switch1.core'],['board.processor.switch0.core','board.processor.switch00.core']]:
            with self.assertRaises(ValueError):self.find(names)
    def test_cache_controller_padding(self):
        self.assertEqual(self.find(['ruby.l1_controllers00','ruby.l1_controllers01','ruby.l1_controllers00.Dcache'], 'ruby.l1_controllers',''),'ruby.l1_controllers00')
if __name__=='__main__':unittest.main()
