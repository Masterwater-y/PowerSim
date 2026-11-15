"""Native parameter gate: numeric mapping only, never mechanism certification."""
import configparser
import importlib.util
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('spr_fill_audit',ROOT/'tools/audit_spr_fill_mapping.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
class MappingTest(unittest.TestCase):
 def fixture(self):
  c=configparser.ConfigParser(interpolation=None);root='board.cache_hierarchy.ruby_system.'
  for i in range(2):
   l1=root+f'l1_controllers{i}';l2=root+f'l2_controllers{i}'
   c[l1]={'type':'MESI_Three_Level_L0Cache_Controller','number_of_TBEs':'256'}
   c[l1+'.Dcache']={'type':'RubyCache','fill_capacity':'16'}
   c[l1+'.sequencer']={'type':'RubySequencer','is_cpu_sequencer':'true','max_outstanding_requests':'16','max_store_prepare_outstanding':'16'}
   c[l2]={'type':'MESI_Three_Level_L1Cache_Controller','number_of_TBEs':'256'}
   c[l2+'.cache']={'type':'RubyCache','fill_capacity':'48'}
  c[root+'l3_controllers0']={'type':'MESI_Three_Level_L2Cache_Controller','number_of_TBEs':'256'}
  d={'configuration':{'cores':2,'spr_load_fill_feedback':False,'spr_l1d_fill_entries':16,'spr_l2_fill_entries':48,'l1d_mshrs':256,'l2_mshrs':256,'llc_mshrs':256,'ruby_sequencer_max_outstanding':16},'functional_source_identity':{'status':'complete','participants':[0,1]}}
  return c,d
 def test_pass_off_and_proxy_is_not_equivalence(self):
  c,d=self.fixture();r=mod.audit(c,d)
  self.assertEqual(r['status'],'pass');self.assertFalse(r['feature_enabled']);self.assertFalse(r['mechanism_equivalence_certified'])
  self.assertEqual(r['resources']['l1_controller_tbes']['values'],[256,256]);self.assertEqual(r['resources']['l1_controller_tbes']['relation'],'capacity_proxy_only')
 def test_missing_fails_closed(self):
  c,d=self.fixture();del c['board.cache_hierarchy.ruby_system.l1_controllers1.Dcache']['fill_capacity']
  r=mod.audit(c,d);self.assertEqual(r['status'],'unscorable');self.assertTrue(any('fill_capacity' in x for x in r['issues']))
 def test_heterogeneous_fails(self):
  c,d=self.fixture();c['board.cache_hierarchy.ruby_system.l2_controllers1.cache']['fill_capacity']='32'
  r=mod.audit(c,d);self.assertEqual(r['status'],'unscorable');self.assertTrue(any('heterogeneous' in x for x in r['issues']))
 def test_zero_means_unlimited(self):
  c,d=self.fixture();c['board.cache_hierarchy.ruby_system.l1_controllers0.Dcache']['fill_capacity']='0'
  r=mod.audit(c,d);self.assertEqual(r['status'],'unscorable');self.assertTrue(any('unlimited' in x for x in r['issues']))
 def test_fastsim_mismatch_and_missing_participants(self):
  c,d=self.fixture();d['configuration']['spr_l1d_fill_entries']=8
  self.assertEqual(mod.audit(c,d)['status'],'mismatch')
  d['functional_source_identity']['participants']=[];self.assertEqual(mod.audit(c,d)['status'],'unscorable')
 def test_missing_prepare_is_unscorable_and_no_false_l2_sequencer(self):
  c,d=self.fixture();del c['board.cache_hierarchy.ruby_system.l1_controllers0.sequencer']['max_store_prepare_outstanding']
  r=mod.audit(c,d);self.assertEqual(r['status'],'unscorable');self.assertNotIn('l2_sequencer',r['resources'])
 def test_source_binding_refuses_wrong_case_or_missing_core(self):
  _,d=self.fixture();d['functional_source_identity']['sources']=[
    {'core_id':i,'artifacts':[{'role':'fst','path':f'/cases/flac/tao_trace/core{i}.fst'}]} for i in range(2)]
  self.assertTrue(mod.source_paths_bind(Path('/cases/flac/config.ini'),d))
  self.assertFalse(mod.source_paths_bind(Path('/cases/xz/config.ini'),d))
  d['functional_source_identity']['sources'].pop()
  self.assertFalse(mod.source_paths_bind(Path('/cases/flac/config.ini'),d))
if __name__=='__main__':unittest.main()
