import copy
import importlib.util
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
class Diagnostics(unittest.TestCase):
    def module(self):
        spec=importlib.util.spec_from_file_location('fill_diagnostics',ROOT/'tools/analyze_retired_fill.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
    def rows(self):
        return [dict(type='begin',schema='llc-fill-audit-v1',producer='retired-fill-model',stride=1,source_identity='id'),
            dict(type='decision',core=0,ordinal=4,fragment=0,line=10,pc=100,measurement=1,task=1,
                 own=0,result=1,issue=10,canonical=30,final_issue=40,parent_end=20,decision=1,
                 private_level=0,input_fallback_reasons=0),
            dict(type='end',completed=True,truncated=False,omitted=0,rows=1)]
    def test_member_time_relation_is_not_macro_error(self):
        r=self.module().analyze_rows(self.rows(),'id')
        self.assertEqual(r['sampled_hit_to_pending'],1)
        self.assertEqual(r['canonical_at_or_after_parent_end'],1)
        self.assertEqual(r['final_at_or_after_parent_end'],1)
        self.assertEqual(r['unit'],'sampled load-member-fragment')
    def test_identity_and_truncation_fail_closed(self):
        for field,value in [('source_identity',''),('source_identity','other')]:
            rows=self.rows();rows[0][field]=value
            with self.assertRaises(ValueError):self.module().analyze_rows(rows,'id')
        for field,value in [('truncated',True),('omitted',1),('completed',False),('rows',2)]:
            rows=self.rows();rows[-1][field]=value
            with self.assertRaises(ValueError):self.module().analyze_rows(rows,'id')
    def test_duplicate_fragment_rejects(self):
        rows=self.rows();rows.insert(2,copy.deepcopy(rows[1]));rows[-1]['rows']=2
        with self.assertRaises(ValueError):self.module().analyze_rows(rows,'id')
    def test_unknown_private_mask_zero_is_not_a_hit(self):
        rows=self.rows();rows[1]['private_level']=5;rows[1]['input_fallback_reasons']=2
        r=self.module().analyze_rows(rows,'id')
        self.assertEqual(r['sampled_hit_to_pending'],0)
        self.assertEqual(r['sampled_unknown_private_to_pending'],1)
    def test_native_confusion_preserves_unknown_and_rejects_anchor_mismatch(self):
        m=self.module();model=self.rows()[1];native=dict(core=0,ordinal=4,line=10,spr_miss_mask=0,spr_unknown_mask=0)
        r=m.pair_spr_members([model],[native]);self.assertEqual(r['l1']['fp'],1)
        native['spr_unknown_mask']=1;r=m.pair_spr_members([model],[native]);self.assertEqual(r['l1']['unknown'],1)
        self.assertEqual(r['l1']['fp'],0)
        native['ordinal']=5
        with self.assertRaises(ValueError):m.pair_spr_members([model],[native])
if __name__=='__main__':unittest.main()
