"""Baseline-only late-prefetch diagnosis never alters retirement membership."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

class SprNativePrefetchUnknown(unittest.TestCase):
    def test_typed_prefetch_evidence_does_not_change_miss_membership(self):
        code=r'''
#include "cpu/o3/probe/perf_retirement.hh"
#include <cassert>
#include <sstream>
using namespace gem5::o3::retirement;
int main() {
 NativeFacts n;n.resolved=true;n.admissions=n.responses=n.hierarchy_requests=n.accesses=1;
 n.external_misses=n.tag_misses=1;
 auto expected=classifyRetiredCache(n);assert(expected==(L2Unknown|L3Unknown));
 n.l1_late_prefetches=1;assert(classifyRetiredCache(n)==expected);
 n.l2_prefetch_parent_llc[1]=1;assert(classifyRetiredCache(n)==expected);
 n.l2_prefetch_parent_sender[1]=1;n.l2_prefetch_parent_response[2]=1;
 n.l2_prefetch_parent_sender_id=4;assert(classifyRetiredCache(n)==expected);
 Ledger ledger;Member m;m.measured=true;m.read=true;m.owner=7;m.sequence=1;
 ledger.commit(m);ledger.resolveNative(0,1,n);
 auto c=ledger.snapshot({7}).workload_task;
 assert(c.independent_l2_misses_unknown==1 && c.independent_l3_misses_unknown==1);
 std::ostringstream out;ledger.writeNativeDiagnostics(out);
 assert(out.str().find("\"l1_late_prefetch_unknown_uops\":1")!=std::string::npos);
 assert(out.str().find("\"l1_late_prefetches\":1")!=std::string::npos);
 assert(out.str().find("\"l2_prefetch_parent_llc_unknown_uops\":[0,1,0,0,0]")!=std::string::npos);
 assert(out.str().find("\"l2_prefetch_parent_sender_unknown_uops\":[0,1,0]")!=std::string::npos);
 assert(out.str().find("\"l2_prefetch_parent_response_unknown_uops\":[0,0,1]")!=std::string::npos);
 assert(out.str().find("\"l2_prefetch_parent_sender_id\":4")!=std::string::npos);
 // Independent level and unknown counts may overlap, and warmup must not count.
 m.sequence=2;n.l1_late_prefetches=0;n.l2_late_prefetches=1;
 ledger.commit(m);ledger.resolveNative(0,2,n);
 m.sequence=3;m.measured=false;ledger.commit(m);ledger.resolveNative(0,3,n);
 std::ostringstream again;ledger.writeNativeDiagnostics(again);
 assert(again.str().find("\"l1_late_prefetch_unknown_uops\":1")!=std::string::npos);
 assert(again.str().find("\"l2_late_prefetch_unknown_uops\":1")!=std::string::npos);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            p=Path(d);(p/'test.cc').write_text(code)
            result=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-static-libstdc++','-static-libgcc','-I',str(ROOT/'gem5-spr/src'),str(p/'test.cc'),'-o',str(p/'test')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            result=subprocess.run([str(p/'test')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__':unittest.main()
