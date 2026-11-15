"""Production observer event conservation, fragment and uncertainty gates."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from retired_cache_contract import validate_membership_event, sum_membership_events


class RetiredMembership(unittest.TestCase):
    def test_reason_certainty_audit(self):
        event=dict(count=1,unknown=0,nonmember=1,exact_count=1,estimated_count=0,
                   exact_nonmember=0,estimated_nonmember=1,
                   reasons={'E-DIRECT-L1-MISS':1,'R-STORE-FORWARD-NEG':1})
        validate_membership_event(event,2)
        total=sum_membership_events([event,event])
        validate_membership_event(total,4)
        for changes in ({'unknown':1}, {'estimated_count':1},
                        {'reasons':{'E-DIRECT-L1-MISS':2}},
                        {'reasons':{'invented':2}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_membership_event({**event,**changes},2)

    def test_closed_fragments_and_ledger(self):
        code = r'''
#include "cpu/o3/probe/perf_retirement.hh"
#include <cassert>
#include <sstream>
using namespace gem5::retired_membership;
int main() {
 FragmentEvidence h;h.hierarchy_requests=1;h.accesses[0]=h.hits[0]=1;
 auto miss=h;miss.hits[0]=0;miss.tag_misses[0]=1;
 miss.accesses[1]=miss.tag_misses[1]=1;
 miss.accesses[2]=miss.tag_misses[2]=1;
 CaseInput input;input.fragments={h,miss};
 auto r=evaluate(input);
 assert(r.labels[L1Hit].membership==Membership::Member);
 for(auto e:{L1Miss,L2Miss,L3Miss})
   assert(r.labels[e].membership==Membership::Member);
 FragmentEvidence f;f.coalesced_follower=true;f.coalesced=1;
 f.attempts=0;f.completions=1;
 input.fragments={f};r=evaluate(input);
 assert(r.labels[FbHit].membership==Membership::Member);
 assert(r.labels[FbHit].certainty==Certainty::Estimated);
 f.coalesced_resident=true;input.fragments={f};r=evaluate(input);
 assert(r.labels[L1Hit].membership==Membership::Member);
 assert(r.labels[FbHit].membership==Membership::Nonmember);
 f=miss;f.accesses[2]=f.tag_misses[2]=0;
 f.late_l2_parent=true;f.parent_llc_outcome=1;
 input.fragments={f};r=evaluate(input);
 assert(r.labels[L2Miss].certainty==Certainty::Exact);
 assert(r.labels[L3Miss].certainty==Certainty::Estimated);
 f.parent_reject_uniqueness=true;input.fragments={f};r=evaluate(input);
 assert(r.labels[L3Miss].membership==Membership::Unknown);
 assert(r.fail_multi_follower==1);
 f=h;f.store_forward_full=true;input.fragments={f};r=evaluate(input);
 assert(r.labels[L1Miss].certainty==Certainty::Estimated);
 f=h;f.completions=2;input.fragments={f};r=evaluate(input);
 assert(r.labels[L1Hit].membership==Membership::Unknown);
 f=h;f.stale=true;input.fragments={f};r=evaluate(input);
 assert(r.fail_stale==1);
 f=miss;f.tag_misses[0]=0;f.permission_upgrades[0]=1;
 input.fragments={f};r=evaluate(input);
 for(auto e:{L1Miss,L2Hit,L2Miss,L3Hit,L3Miss})
     assert(r.labels[e].membership==Membership::Nonmember);
 assert(r.labels[L1Hit].certainty==Certainty::Estimated);
 using namespace gem5::o3::retirement;
 Ledger ledger;Member m;m.measured=true;m.read=true;m.owner=7;m.sequence=1;
 ledger.commit(m);NativeFacts n;n.membership_fragments={h,miss};
 ledger.resolveNative(0,1,n);
 auto c=ledger.snapshot({7}).workload_task;
 for(const auto &e:c.membership) {
   assert(e.count+e.unknown+e.nonmember==1);
   assert(e.count==e.exact_count+e.estimated_count);
   uint64_t reasons=0;for(auto v:e.reasons) reasons+=v;
   assert(reasons==1);
 }
 std::ostringstream out;writeMembershipEvents(out,c);
 assert(out.str().find("\"estimated_count\":1")!=std::string::npos);
 Ledger multiple;m.sequence=2;m.last=false;multiple.commit(m);
 n.membership_fragments={h};multiple.resolveNative(0,2,n);
 m.sequence=3;m.first=false;m.last=true;multiple.commit(m);
 n.membership_fragments={miss};multiple.resolveNative(0,3,n);
 auto combined=multiple.snapshot({7}).workload_task;
 assert(combined.membership[gem5::retired_membership::L1Miss].exact_count==1);
 assert(combined.membership[gem5::retired_membership::L2Miss].estimated_count==1);
 for(const auto &e:combined.membership)
     assert(e.count+e.nonmember+e.unknown==1);
 Ledger partial;m.first=true;m.last=false;m.sequence=4;partial.commit(m);
 n.membership_fragments={miss};partial.resolveNative(0,4,n);
 m.first=false;m.last=true;m.sequence=5;partial.commit(m);
 auto incomplete=partial.snapshot({7}).workload_task;
 assert(incomplete.membership[gem5::retired_membership::L1Miss].exact_count==1);
 assert(incomplete.membership[gem5::retired_membership::L1Hit].unknown==1);
 for(auto reason:allReasons) std::cout << reason << "\n";
}
'''
        code = '#include <iostream>\n' + code
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            path = Path(directory)
            (path / 'test.cc').write_text(code)
            result = subprocess.run([
                '/opt/gcc-11/bin/g++', '-std=c++17', '-static-libstdc++',
                '-static-libgcc', '-I', str(ROOT / 'gem5-spr/src'),
                str(path / 'test.cc'), '-o', str(path / 'test')],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(path / 'test')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        rules = json.loads((ROOT / 'configs/retired-load-cache-membership-v1.json').read_text())
        self.assertEqual(result.stdout.splitlines(), [r['id'] for r in rules['reasons']])


if __name__ == '__main__':
    unittest.main()
