#!/usr/bin/env python3
"""Source attachment invariants supplement production observer/codec tests."""
from pathlib import Path
import hashlib
import re
import unittest
ROOT=Path(__file__).resolve().parents[1]
class Hooks(unittest.TestCase):
 def test_post_advance_is_after_native_advance(self):
  s=(ROOT/'gem5-spr/src/cpu/o3/commit.cc').read_text()
  advance=s.index('head_inst->staticInst->advancePC(*pc[tid]);')
  notify=s.index('ppCommitPCAdvanced->notify',advance)
  self.assertLess(advance,notify)
  self.assertLess(notify,s.index('lastCommitedSeqNum[tid] = head_inst->seqNum;',advance))
 def test_scope_precedes_actual_page_demap(self):
  s=(ROOT/'gem5-spr/src/arch/x86/faults.cc').read_text()
  start=s.index('PageFault::invoke(')
  self.assertIn('this,tc',s[start:s.index('tc->getMMUPtr()->demapPage(addr, 0);',start)])
 def test_switch_arm_after_original_queue_transition(self):
  s=(ROOT/'gem5-spr/configs/fastsim/fs_capture.py').read_text()
  self.assertLess(s.index('Root.getInstance().sim_quantum = 0'),s.index('armArchitecturalBoundaryEpoch'))
 def test_layout_digests(self):
  values={'spr-archb-v1.layout':'3622b47c0d37b08c19403e8cbcf5b8a9af29459ca07c1d6d7a37507b2ab048a0','spr-mmuctl-v2.layout':'df78bcac05371ed0ea3b8497f56255323f7221e52b3ef7844f630d29acdc3d10','spr-core-facts-v2.layout':'5319ee9aaefab9945cd9189a2e3686088fc2a515a038414a061d13152704aaaf'}
  for name,digest in values.items():self.assertEqual(hashlib.sha256((ROOT/'docs/superpowers/specs/layouts'/name).read_bytes()).hexdigest(),digest)
 def test_exported_descriptor_bytes_and_digest(self):
  pairs=(('fst_architectural_boundaries.hpp','spr-archb-v1.layout'),('fst_mmu_controls_v2.hpp','spr-mmuctl-v2.layout'),('fst_core_functional_facts.hpp','spr-core-facts-v2.layout'))
  for header,layout in pairs:
   s=(ROOT/'include/fastsim'/header).read_text()
   descriptor=re.findall(r'R"FSTLAYOUT\((.*?)\)FSTLAYOUT"',s,re.S)
   self.assertEqual(len(descriptor),1)
   data=descriptor[0].encode()
   self.assertEqual(data,(ROOT/'docs/superpowers/specs/layouts'/layout).read_bytes())
   constant=re.search(r'LayoutDigest\s*=\s*\{([^}]+)\}',s,re.S).group(1)
   digest=bytes(int(x.strip(),16) for x in constant.split(',') if x.strip())
   self.assertEqual(hashlib.sha256(data).digest(),digest)
 def test_actual_acceptance_and_token_transport(self):
  commit=(ROOT/'gem5-spr/src/cpu/o3/commit.cc').read_text()
  begin=commit.index('auto arch_scope=',commit.index('Fault accepted_interrupt'))
  self.assertLess(begin,commit.index('cpu->processInterrupts(accepted_interrupt);',begin))
  self.assertIn('head_inst->functionalMacroOrigin',commit)
  self.assertIn('head_inst->notAnInst()?1:0',commit)
  for file in ('fetch.cc','frontend_record.cc'):
   source=(ROOT/'gem5-spr/src/cpu/o3'/file).read_text()
   self.assertIn('functionalBoundary=',source);self.assertIn('functionalMacroOrigin=',source)
  pc=(ROOT/'gem5-spr/src/arch/x86/pcstate.hh').read_text()
  serial=pc[pc.index('serialize(CheckpointOut'):pc.index('unserialize(CheckpointIn')]
  self.assertNotIn('_functionalBoundary',serial)
  self.assertNotIn('_functionalMacroOrigin',serial)
if __name__=='__main__':unittest.main()
