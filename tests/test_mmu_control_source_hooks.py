#!/usr/bin/env python3
"""Native source-boundary invariants complement production helper tests."""
from pathlib import Path
import unittest,re,hashlib
ROOT=Path(__file__).resolve().parents[1]
class Hooks(unittest.TestCase):
 def test_protocol_mirror_and_layout_digest(self):
  s=(ROOT/'include/fastsim/fst_mmu_controls.hpp').read_text()
  self.assertEqual(s,(ROOT/'gem5-spr/taogen/shared/fst_mmu_controls.hh').read_text())
  section=re.split(r'static const std::string s\s*=',s)[1].split('return s;')[0]
  descriptor=''.join(re.findall(r'"(.*?)"',section))
  digest=bytes(int(x,16) for x in re.findall(r'0x[0-9a-f]{2}',re.split(r'LayoutDigest\s*=\s*\{',s)[1].split('};')[0]))
  self.assertEqual(digest,hashlib.sha256(descriptor.encode()).digest())
 def test_actual_source_policy_closure_and_scons_parity(self):
  import runpy
  native=ROOT/'gem5-spr'
  module=runpy.run_path(str(native/'src/cpu/o3/probe/generate_static_uop_policy.py'))
  files=set(module['policy_files'](native))
  for path in ('src/mem/functional_mmu_controls.hh','taogen/shared/fst_mmu_controls.hh',
               'src/arch/generic/mmu.cc','src/arch/generic/tlb.hh','src/cpu/o3/commit.cc',
               'src/cpu/o3/cpu.cc','src/arch/x86/faults.cc','src/arch/x86/kvm/x86_cpu.cc',
               'src/cpu/kvm/base.cc','configs/fastsim/fs_capture.py','src/sim/mem_state.cc'):
   self.assertIn(path,files)
  self.assertTrue(all((native/path).is_file() for path in files))
  self.assertIn("static_policy_module['policy_files'](static_policy_root)",
                (native/'src/cpu/o3/probe/SConscript').read_text())
 def test_mmu_cutoff_is_at_existing_common_end_for_every_participant(self):
  s=(ROOT/'gem5-spr/src/cpu/o3/probe/tao_trace.cc').read_text()
  body=s.split('TaoTrace::noteFunctionalRecordEmitted(')[1].split('TaoTrace::scheduleFunctionalTargetDrain(')[0]
  cutoff='trace->fst_mmu_controls_->close(trace->fst_record_count_)'
  self.assertIn(cutoff,body)
  self.assertLess(body.index('functional_target_exit_scheduled_ = true'),body.index(cutoff))
  self.assertLess(body.index('for (auto *trace : functional_warmup_instances_)'),body.index(cutoff))
  self.assertLess(body.index(cutoff),body.index('trace->closeTaskClock()'))
  self.assertLess(body.index(cutoff),body.index('scheduleFunctionalTargetDrain(this)'))
 def test_post_update_seal(self):
  s=(ROOT/'gem5-spr/src/cpu/o3/commit.cc').read_text()
  self.assertRegex(s,r'head_inst->updateMiscRegs\(\);\s*ppCommitMiscRegsApplied->notify\(head_inst\);')
 def test_original_misc_duplicate_and_unconditional_calls(self):
  s=(ROOT/'gem5-spr/src/cpu/o3/dyn_inst.hh').read_text()
  self.assertIn('if (idx == misc_reg)\n                return;',s)
  self.assertIn('functional_mmu::OwnerScope',s)
 def test_all_three_tlb_entries_observed_before_native_effect(self):
  s=(ROOT/'gem5-spr/src/arch/x86/tlb.cc').read_text()
  for name in ('flushAll','flushNonGlobal','demapPage'):
   body=s.split('TLB::'+name+'(')[1].split('\n}')[0]
   self.assertLess(body.index('functional_mmu::CommandScope'),body.index('++invalidationGeneration'))
   self.assertIn('functional_mmu::EdgeScope',body)
 def test_resume_boundaries(self):
  s=(ROOT/'gem5-spr/src/cpu/o3/cpu.cc').read_text()
  self.assertLess(s.index('functional_mmu::resume',s.index('CPU::startup()')),s.index('bac.startupStage()'))
  body=s.split('CPU::takeOverFrom(BaseCPU *oldCPU)')[1].split('\n}')[0]
  self.assertGreater(body.index('functional_mmu::resume'),body.index('commit.takeOverFrom()'))
 def test_setter_terminal_permit(self):
  s=(ROOT/'gem5-spr/src/arch/x86/isa.cc').read_text()
  self.assertIn('functional_mmu::StoragePermit',s)
  self.assertEqual(s.count('make_unique<functional_mmu::WriteScope>'),2)
 def test_default_off(self):
  s=(ROOT/'gem5-spr/src/cpu/o3/probe/TaoTrace.py').read_text()
  self.assertRegex(s,r'functional_mmu_controls\s*=\s*Param.Bool\(False')
if __name__=='__main__':unittest.main()
