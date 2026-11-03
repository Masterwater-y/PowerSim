"""Literal physical timelines exercise attribution, never proxy cache truth."""
import copy
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))

class FillPairingTests(unittest.TestCase):
    def fixture(self,role=6,tick=10):
        begin=lambda p:dict(type='begin',schema='llc-fill-audit-v1',producer=p,line_bytes=64,stride=16)
        n=[begin('native'),dict(type='fill',line=16,role=role,core=0,identity_valid=False,context=0,sequence=4,tick=tick),
            dict(type='member',core=0,context=0,sequence=8,ordinal=1,pc=4096,line=16,flags=3,measurement=True,
                 source=2,admission_tick=20,response_tick=25,resolved=True),dict(type='end',rows=2,omitted=0,truncated=False,completed=True)]
        m=[begin('model'),dict(type='member',core=0,ordinal=1,pc=4096,line=16,flags=3,measurement=True,estimate=7),
           dict(type='end',rows=1,omitted=0,truncated=False,completed=True)]
        return n,m
    def pair(self,n,m):
        import pair_llc_fill_audit as p
        return p.pair_records(n,m)
    def test_prepare_completed_before_admission(self):
        r=self.pair(*self.fixture());self.assertEqual(r['known_false_positive_origins'],{'prepare':1})
    def test_future_fill_is_not_explanation(self):
        r=self.pair(*self.fixture(tick=30));self.assertEqual(r['known_false_positive_origins'],{'unobserved_fill':1})
    def test_fill_while_load_inflight_is_not_prior_hit(self):
        r=self.pair(*self.fixture(tick=22));self.assertEqual(r['known_false_positive_origins'],{'overlapping_fill':1})
    def test_eviction_removes_old_prepare(self):
        n,m=self.fixture();n.insert(2,dict(type='evict',line=16,tick=15));n[-1]['rows']+=1
        self.assertEqual(self.pair(n,m)['known_false_positive_origins'],{'unobserved_fill':1})
    def test_uncommitted_is_not_automatically_squashed(self):
        n,m=self.fixture(role=1);n[1]['identity_valid']=True
        self.assertEqual(self.pair(n,m)['known_false_positive_origins'],{'unresolved_demand_origin':1})
        n.insert(2,dict(type='squash',core=0,cutoff=3,youngest=5,tick=9));n[-1]['rows']+=1
        self.assertEqual(self.pair(n,m)['known_false_positive_origins'],{'squashed_demand':1})
    def test_unknown_qualification_not_false_positive(self):
        n,m=self.fixture();n[2]['source']=1024
        r=self.pair(n,m);self.assertEqual(r['native_unknown_members'],1);self.assertEqual(r['known_false_positive_origins'],{})
    def test_code_pte_and_committed_origin(self):
        for role,want in [(3,'code'),(4,'pte')]:
            with self.subTest(role=role):self.assertEqual(self.pair(*self.fixture(role))['known_false_positive_origins'],{want:1})
        n,m=self.fixture(role=1);n[1]['identity_valid']=True
        prior=copy.deepcopy(n[2]);prior.update(sequence=4,ordinal=0,measurement=False,tick=12,admission_tick=5,response_tick=9)
        n.insert(2,prior);n[-1]['rows']+=1
        self.assertEqual(self.pair(n,m)['known_false_positive_origins'],{'committed_demand':1})
    def test_reject_pc_mismatch_duplicate_and_truncation(self):
        for defect in ['pc','duplicate','truncated']:
            n,m=self.fixture()
            if defect=='pc':m[1]['pc']=8192
            elif defect=='duplicate':m.insert(2,copy.deepcopy(m[1]));m[-1]['rows']+=1
            else:n[-1]['truncated']=True;n[-1]['omitted']=1
            with self.subTest(defect=defect):
                with self.assertRaises(ValueError):self.pair(n,m)
    def test_empty_or_aborted_model_is_rejected(self):
        n,m=self.fixture();m.pop(1);m[-1]['rows']=0
        with self.assertRaises(ValueError):self.pair(n,m)
        n,m=self.fixture();m[-1]['completed']=False
        with self.assertRaises(ValueError):self.pair(n,m)
    def test_equal_tick_event_is_ambiguous_not_old_prepare(self):
        for event in [dict(type='evict',line=16,tick=20),dict(type='fill',line=16,tick=20,role=4)]:
            n,m=self.fixture();n.insert(2,event);n[-1]['rows']+=1
            self.assertEqual(self.pair(n,m)['known_false_positive_origins'],{'same_tick_ambiguity':1})
    def test_split_member_does_not_supply_per_line_native_truth(self):
        n,m=self.fixture();n[2]['split']=True;m[1]['split']=True
        n[2]['source']=256
        result=self.pair(n,m)
        self.assertEqual(result['excluded_split_members'],1)
        self.assertEqual(result['known_members'],0)
    def test_replay_artifact_digest_rejects_other_same_source_run(self):
        import tempfile,hashlib
        import pair_llc_fill_audit as p
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            path=Path(d)/'model.jsonl';path.write_text('first replay')
            metadata=dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),size_bytes=path.stat().st_size)
            p.validate_model_artifact(path,{'llc_fill_audit':metadata})
            path.write_text('other replay')
            with self.assertRaises(ValueError):p.validate_model_artifact(path,{'llc_fill_audit':metadata})
    def test_native_seal_requires_complete_current_capture_bundle(self):
        import tempfile,hashlib,json
        from unittest.mock import patch
        import pair_llc_fill_audit as p
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            case=Path(d);(case/'oracle').mkdir();(case/'tao_trace').mkdir()
            paths={k:case/v for k,v in dict(gem5_binary='gem5.opt',runner='runner.py',
                capture_command='capture-command.json',config_ini='config.ini',effective_target='effective-target.json',
                capture_receipt='capture-receipt.json',trace_metadata='tao_trace/trace.json',
                thread_state_plan='thread-state.plan',**{'oracle/a.json':'oracle/a.json','oracle/b.json':'oracle/b.json'}).items()}
            for path in paths.values():path.write_text('capture')
            paths['capture_command'].write_text(json.dumps({'argv':[str(paths['gem5_binary'].resolve()),str(paths['runner'].resolve())]}))
            native=case/'audit.jsonl';native.write_text('native')
            sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
            original=dict(schema='native-llc-fill-capture-binding-v1',result_dir=str(case),functional_source_identity={},
                native_audit_sha256=sha(native),files={k:dict(path=str(v.resolve()),sha256=sha(v)) for k,v in paths.items()})
            binding=case/'seal.json'
            with patch('perf_cache_source.validate_identity'):
                binding.write_text(json.dumps(original));p.validate_native_capture(binding,native,{'functional_source_identity':{}},case)
                for defect in ['capture_receipt','trace_metadata','thread_state_plan','oracle/b.json','other-case-path','unbound-binary']:
                    b=copy.deepcopy(original)
                    if defect=='other-case-path':
                        other=case/'other.ini';other.write_text('capture');b['files']['config_ini']['path']=str(other.resolve())
                    elif defect=='unbound-binary':
                        other=case/'other.opt';other.write_text('capture');b['files']['gem5_binary']['path']=str(other.resolve())
                    else:del b['files'][defect]
                    binding.write_text(json.dumps(b))
                    with self.subTest(defect=defect),self.assertRaises(ValueError):
                        p.validate_native_capture(binding,native,{'functional_source_identity':{}},case)
if __name__=='__main__':unittest.main()
