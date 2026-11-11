"""Shared v2 rules, including negative estimates and incomplete fragments."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SharedSprMissRules(unittest.TestCase):
    def test_shared_source_and_special_services(self):
        code = r'''
#include "fastsim/spr_retired_load_miss.hpp"
#include "mem/spr_retired_load_miss.hh"
#include "fastsim/retired_cache_miss_estimator.hpp"
#include "fastsim/perf_retirement.hpp"
#include <cassert>
#include <cstring>
int main() {
 using namespace spr_retired;
 assert(std::strcmp(Policy,"spr-retired-load-miss-v2")==0);
 for (auto mask : {0u,1u,3u,7u}) {
   auto r=convert(ordinary(mask));
   assert(r.ready && r.miss_mask==mask && r.estimated_mask==0);
   r.stale=true;
   auto stale=convert(r);
   assert(stale.miss_mask==mask && stale.ready);
 }
 Result fb=ordinary(7);fb.level[0]=Level::PendingFill;
 fb.service=Service::PrefetchFollower;
 auto r=convert(fb);
 assert(r.ready && r.miss_mask==1 && r.estimated_mask==7);
 for(auto lower : {Level::Resident,Level::Miss,Level::Remote}) {
   Result l2=ordinary(7);l2.level[1]=Level::PendingFill;l2.level[2]=lower;
   r=convert(l2);
   assert(r.ready && r.miss_mask==(lower==Level::Miss ? 7 : 3));
   assert(r.estimated_mask==7);
 }
 Result llc=ordinary(7);llc.level[2]=Level::PendingFill;
 assert(convert(llc).miss_mask==7 && convert(llc).estimated_mask==4);
 for(auto kind : {Service::Forwarded,Service::Device,Service::SoftwarePrefetch}) {
   Result s;s.service=kind;r=convert(s);
   assert(r.ready && r.miss_mask==0 && r.estimated_mask==7);
 }
 Result permission;permission.level[0]=Level::Permission;
 r=convert(permission);
 assert(r.ready && r.miss_mask==0 && r.estimated_mask==7);
 Result missing;missing.level[0]=Level::Miss;
 r=convert(missing);
 assert(!r.ready && r.known_mask==1 && r.miss_mask==1);
 Aggregate macro;macro.add(r);macro.add(convert(ordinary(7)));
 assert(macro.miss_mask==7 && macro.unknown()==0);
 Aggregate negative;negative.add(r);negative.add(convert(ordinary(0)));
 assert(negative.miss_mask==1 && negative.unknown()==6);
 Result retry;lookup(retry,0,Level::Miss);lookup(retry,0,Level::Resident);
 retry.retried=true;r=convert(retry);
 assert(r.ready && r.miss_mask==1 && r.estimated_mask==7);
 Result split=ordinary(3);split.split=true;split.stale=true;
 r=convert(split);assert(r.miss_mask==3 && r.estimated_mask==7);
 Aggregate paths;paths.add(r);
 assert(paths.path_mask & (1u<<12));
 assert(paths.path_mask & (1u<<13));
 Result partial=ordinary(1);partial.service=Service::PartialForward;
 assert(convert(partial).miss_mask==1);
 fastsim::RetiredCacheMissEstimator shadow(1);
 assert(shadow.observe(64,10,30,7).misses==7);
 auto follower=shadow.observe(64,20,35,7);
 assert(follower.misses==1 && follower.merged && follower.estimated_mask==7);
 auto alias=shadow.observe(128,20,35,7,false,true);
 assert(alias.misses==7 && alias.fallback);
 fastsim::PerfRetirementLedger ledger;
 using P=fastsim::PerfRetirementLedger;
 ledger.accept(false,true,false,false,true,true,P::Miss,P::Miss,1,false,false,0,0);
 ledger.accept(true,true,false,false,true,true,P::Miss,P::Miss,7,false,false,0,0);
 auto counts=ledger.snapshot().workload_task;
 assert(counts.perf_retired_loads==1 && counts.estimated_l3_misses==1);
 assert(counts.spr_estimated_member[0]==0 && counts.spr_estimated_member[1]==1 &&
        counts.spr_estimated_member[2]==0);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            source=Path(directory)/'test.cc'
            binary=Path(directory)/'test'
            source.write_text(code)
            subprocess.run(['/opt/gcc-11/bin/g++', '-std=c++17',
                '-static-libstdc++', '-static-libgcc',
                '-I'+str(ROOT/'include'), '-I'+str(ROOT/'gem5-spr/src'),
                str(source), '-o', str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
