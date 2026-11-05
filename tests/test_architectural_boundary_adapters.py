"""Synthetic audit/source-policy/copy refusal tests, not native qualification."""
import json
import subprocess
from pathlib import Path
import runpy
import struct
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
NAMES=('decoder','frontend','allocation','static','fusion','operands','pins','normalization')
class Adapters(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  sys.path.insert(0,str(ROOT/'tools'))
  try:cls.module=runpy.run_path(str(ROOT/'tools/prepare_spr_capture.py'))
  finally:sys.path.pop(0)
  cls.policy=runpy.run_path(str(ROOT/'gem5-spr/src/cpu/o3/probe/generate_static_uop_policy.py'))
  cls.digest=cls.policy['policy_digest'](ROOT/'gem5-spr').hex()
 def fixture(self):
  a=dict(files_declared=1,structurally_valid_files=1,core_v2_files=1,controls_v2_files=1,ready_files=1,
   known_epoch_files=1,hot=2,accepted=1,accepted_pf=0,accepted_irq=1,accepted_other=0,terminal_complete=1,
   terminal_partial=0,terminal_unsupported=0,suppressed_events=0,pending=0,invocation_sealed=1,entry_bound=1,
   member_rows=2,sealed_members=2,boundary_ops=0,boundary_writes=0,boundary_commands=0,event_rows=1,epoch_rows=1,
   na_rom_rows=2,core_unknown_applicable_rows=0,reasons=[0]*16,policy_identities=[self.digest])
  a['controls']=dict(hot=2,sealed_retired=2,total_writes=0,retained_writes=0,boundary_writes=0,unbound_writes=0,
   total_commands=0,retained_commands=0,boundary_commands=0,unbound_commands=0,retained_groups=2,boundary_groups=1,
   unbound_groups=0,unsupported_groups=0,suppressed_ops=0,pending_owners=0,invocation_sealed_boundary=1,
   boundary_ops=0,event_rows=1,reasons=[0]*16)
  core=dict(files_declared=1,wire_version_1_files=0,wire_version_2_files=1,records=2,
   known_records={n:0 if n in ('decoder','allocation','fusion') else 2 for n in NAMES},
   not_applicable_records={n:2 if n in ('decoder','allocation','fusion') else 0 for n in NAMES},
   na_rom_rows=2,boundary_rows=2,unknown_applicable_rows=0,normalization_status_counts=[0,0,2,0],
   architectural_registers_by_core={'0':[39,48,1,2,1,0,6,4096]},policy_identities=[self.digest])
  return dict(records=2,architectural_boundary_bundle=a,core_functional_facts=core,static_uop_facts=dict(policy_identities=[self.digest]))
 def qualify(self,a):return self.module['architectural_bundle_qualification'](a,1,ROOT/'gem5-spr',True)
 def test_ready_is_functional_only(self):
  a=self.fixture();r=self.qualify(a);self.assertTrue(r['architectural_boundary_bridge_qualified'])
  for field in ('physical_state_complete','initial_tlb_residency_known','walker_state_input_complete','runtime_qualified','full_spr_profile_qualified'):self.assertFalse(r[field])
  r=self.module['core_functional_qualification'](a,1,ROOT/'gem5-spr',True,True)
  self.assertTrue(r['strict_core_facts_complete']);self.assertFalse(any(r['unknown_records'].values()))
 def test_false_ready_populations_rejected(self):
  changes=(('accepted',2),('core_v2_files',0),('known_epoch_files',0),('core_unknown_applicable_rows',1),('epoch_rows',0),('boundary_ops',1),('policy_identities',['0'*64]))
  for key,value in changes:
   with self.subTest(key=key):
    a=self.fixture();a['architectural_boundary_bundle'][key]=value
    with self.assertRaises(ValueError):self.qualify(a)
  for key in ('sealed_retired','retained_groups'):
   a=self.fixture();a['architectural_boundary_bundle']['controls'][key]=1
   with self.assertRaises(ValueError):self.qualify(a)
 def test_unknown_does_not_disappear_into_na(self):
  a=self.fixture();c=a['core_functional_facts'];c['known_records']['operands']=0;c['not_applicable_records']['operands']=2
  with self.assertRaises(ValueError):self.module['core_functional_qualification'](a,1,ROOT/'gem5-spr',True,True)
 def test_missing_request_and_v1_retag_rejected(self):
  a=self.fixture()
  with self.assertRaises(ValueError):self.module['architectural_bundle_qualification'](a,1,None,False)
  with self.assertRaises(ValueError):self.module['core_functional_qualification'](a,1,ROOT/'gem5-spr',True,False)
 def test_source_policy_covers_actual_hooks(self):
  files=set(self.policy['policy_files'](ROOT/'gem5-spr'))
  for name in ('src/sim/faults.hh','src/arch/x86/faults.cc','src/arch/x86/pcstate.hh','src/cpu/o3/commit.cc','src/cpu/o3/cpu.cc','src/cpu/o3/frontend_fetch.cc','src/cpu/o3/frontend_record.cc','src/cpu/o3/fetch.cc','src/arch/x86/isa/microops/seqop.isa','src/mem/functional_architectural_boundaries.hh','taogen/shared/fst_architectural_boundaries.hh','taogen/shared/fst_mmu_controls_v2.hh','configs/fastsim/fs_capture.py'):
   self.assertIn(name,files);self.assertTrue((ROOT/'gem5-spr'/name).is_file())
 def test_converter_refuses_boundary_only_bit_before_publication(self):
  sys.path.insert(0,str(ROOT/'tools'))
  try:module=runpy.run_path(str(ROOT/'tools/build_fst_v7_formal_dataset.py'))
  finally:sys.path.pop(0)
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as temp:
   source=Path(temp)/'source.fst';out=Path(temp)/'out.fst'
   source.write_bytes(struct.pack('<8sIIIIQQ4Q',b'FSTRC01\0',7,72,64,0,0,1<<18,0,0,0,1))
   with self.assertRaises(ValueError):module['upgrade_one'](source,out,False,True)
   self.assertFalse(out.exists())
   trace=Path(temp)/'input/tao_trace';trace.mkdir(parents=True)
   (trace/'source.fst').write_bytes(source.read_bytes());(trace/'manifest.txt').write_text('0 binary source.fst\n')
   decoded=Path(temp)/'decoded.jsonl';decoded.write_text(json.dumps(dict(pc=4096,size=1))+'\n')
   output=Path(temp)/'materialized'
   result=subprocess.run([sys.executable,str(ROOT/'tools/materialize_fst_static_map_pilot.py'),'--case',str(trace.parent),'--decoded',str(decoded),'--output',str(output)],capture_output=True,text=True)
   self.assertNotEqual(result.returncode,0);self.assertIn('architectural boundary',result.stderr);self.assertFalse(output.exists())
if __name__=='__main__':unittest.main()
