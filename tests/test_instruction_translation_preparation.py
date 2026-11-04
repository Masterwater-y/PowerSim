"""New instruction inputs qualify independently of absent logical MMU controls."""
import copy
import importlib.util
from pathlib import Path
import runpy
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
SPEC=importlib.util.spec_from_file_location('prepare_spr_capture',ROOT/'tools/prepare_spr_capture.py')
MODULE=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(MODULE)

class InstructionPreparationTest(unittest.TestCase):
    def audit(self):
        root=ROOT/'gem5-spr'
        policy=runpy.run_path(str(root/'src/cpu/o3/probe/generate_static_uop_policy.py'))['policy_digest'](root).hex()
        return dict(static_uop_facts=dict(policy_identities=[policy]),instruction_translation_facts=dict(
            schema='fst-instruction-translations-v2',files_declared=4,wire_version_1_files=0,wire_version_2_files=4,macro_spans=10,
            observed_path_spans=6,leaf_only_spans=2,unknown_spans=1,pseudo_not_applicable_spans=1,partial_macro_spans=0,
            decode_context_known_spans=9,decode_context_unknown_spans=0,
            origin_decode_asid_mismatch_spans=0,origin_retirement_asid_mismatch_spans=0,
            decode_retirement_asid_mismatch_spans=0,context_crossing_spans=0,
            mixed_origin_asid_spans=0,historical_walk_root_spans=0))
    def test_leaf_and_unknown_are_not_complete_paths_or_mmu_controls(self):
        result=MODULE.instruction_translation_qualification(self.audit(),4,ROOT/'gem5-spr',True)
        self.assertEqual(result['byte_mapping_status'],'incomplete')
        self.assertEqual(result['actual_path_status'],'incomplete')
        self.assertEqual(result['retained_macro_status'],'complete')
        self.assertFalse(result['strict_full_macro_eligible'])
        self.assertFalse(result['logical_mmu_controls_complete'])
        self.assertFalse(result['full_spr_profile_qualified'])
    def test_complete_functional_paths_do_not_qualify_logical_controls(self):
        audit=self.audit();facts=audit['instruction_translation_facts']
        facts.update(observed_path_spans=9,leaf_only_spans=0,unknown_spans=0)
        result=MODULE.instruction_translation_qualification(audit,4,ROOT/'gem5-spr',True)
        self.assertEqual(result['actual_path_status'],'complete')
        self.assertFalse(result['logical_mmu_controls_complete'])
        self.assertEqual(result['retained_macro_status'],'complete')
        self.assertTrue(result['strict_full_macro_eligible'])

    def test_partial_retirement_preserves_proven_byte_and_path_coverage(self):
        audit=self.audit();facts=audit['instruction_translation_facts']
        facts.update(observed_path_spans=9,leaf_only_spans=0,unknown_spans=0,partial_macro_spans=1)
        before=copy.deepcopy(audit)
        result=MODULE.instruction_translation_qualification(audit,4,ROOT/'gem5-spr',True)
        self.assertEqual(result['byte_mapping_status'],'complete')
        self.assertEqual(result['actual_path_status'],'complete')
        self.assertEqual(result['retained_macro_status'],'incomplete')
        self.assertFalse(result['strict_full_macro_eligible'])
        self.assertEqual(result['populations']['partial_macro_spans'],1)
        self.assertEqual(audit,before)
        self.assertFalse(result['logical_mmu_controls_complete'])
        self.assertFalse(result['full_spr_profile_qualified'])

    def test_leaf_only_preserves_byte_coverage_without_certifying_path(self):
        audit=self.audit();facts=audit['instruction_translation_facts']
        facts.update(observed_path_spans=7,leaf_only_spans=2,unknown_spans=0)
        result=MODULE.instruction_translation_qualification(audit,4,ROOT/'gem5-spr',True)
        self.assertEqual(result['byte_mapping_status'],'complete')
        self.assertEqual(result['actual_path_status'],'incomplete')
        self.assertEqual(result['retained_macro_status'],'complete')
        self.assertFalse(result['strict_full_macro_eligible'])
    def test_historical_origin_preserves_byte_paths_but_context_qualification_is_separate(self):
        audit=self.audit();facts=audit['instruction_translation_facts']
        facts.update(observed_path_spans=9,leaf_only_spans=0,unknown_spans=0,
                     origin_decode_asid_mismatch_spans=1,origin_retirement_asid_mismatch_spans=1,
                     context_crossing_spans=1,historical_walk_root_spans=1)
        result=MODULE.instruction_translation_qualification(audit,4,ROOT/'gem5-spr',True)
        self.assertEqual(result['byte_mapping_status'],'complete')
        self.assertEqual(result['actual_path_status'],'complete')
        self.assertEqual(result['retained_macro_status'],'complete')
        self.assertEqual(result['observed_asid_alignment_status'],'cross-context')
        self.assertFalse(result['strict_full_macro_eligible'])
        self.assertFalse(result['logical_mmu_controls_complete'])
        self.assertFalse(result['full_spr_profile_qualified'])
        self.assertEqual(result['context_populations']['origin_retirement_asid_mismatch_spans'],1)

    def test_global_historical_walk_root_is_not_a_byte_origin_asid_mismatch(self):
        audit=self.audit();facts=audit['instruction_translation_facts']
        facts.update(observed_path_spans=9,leaf_only_spans=0,unknown_spans=0,historical_walk_root_spans=1)
        result=MODULE.instruction_translation_qualification(audit,4,ROOT/'gem5-spr',True)
        self.assertEqual(result['observed_asid_alignment_status'],'aligned')
        self.assertEqual(result['context_populations']['historical_walk_root_spans'],1)
        self.assertFalse(result['logical_mmu_controls_complete'])

    def test_legacy_v1_context_is_unavailable_not_inferred(self):
        audit=self.audit();facts=audit['instruction_translation_facts']
        facts.update(schema='fst-instruction-translations-v1',observed_path_spans=9,leaf_only_spans=0,unknown_spans=0)
        for key in list(facts):
            if key.startswith(('wire_version_','decode_context_','origin_','decode_retirement_','context_crossing_','mixed_origin_','historical_walk_')):
                del facts[key]
        result=MODULE.instruction_translation_qualification(audit,4,ROOT/'gem5-spr',True)
        self.assertEqual(result['actual_path_status'],'complete')
        self.assertEqual(result['observed_asid_alignment_status'],'unavailable')
        self.assertFalse(result['strict_full_macro_eligible'])

    def test_missing_wrong_policy_or_population_fail_closed(self):
        audit=self.audit()
        for field,value in [('files_declared',3),('macro_spans',11),('unknown_spans',-1)]:
            bad=copy.deepcopy(audit);bad['instruction_translation_facts'][field]=value
            with self.assertRaises(ValueError):MODULE.instruction_translation_qualification(bad,4,ROOT/'gem5-spr',True)
        audit['static_uop_facts']['policy_identities']=['0'*64]
        with self.assertRaisesRegex(ValueError,'policy'):MODULE.instruction_translation_qualification(audit,4,ROOT/'gem5-spr',True)
    def test_old_capture_does_not_inherit_new_capability(self):
        result=MODULE.instruction_translation_qualification({},4,None,False)
        self.assertEqual(result['status'],'not-declared')
        self.assertEqual(result['retained_macro_status'],'unavailable')
        self.assertFalse(result['strict_full_macro_eligible'])
        with self.assertRaises(ValueError):MODULE.instruction_translation_qualification({},4,None,True)

if __name__=='__main__':unittest.main()
