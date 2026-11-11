"""CPU-independent integration witness: prefetch data is never installed early."""
import subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SprPrefetchComponentFlowTest(unittest.TestCase):
 def test_two_level_fill_and_outer_rollback(self):
  code=r'''
#include "fastsim/spr_ruby_prefetcher.hpp"
#include "fastsim/spr_prefetch_service.hpp"
#include "fastsim/spr_prefetch_publish.hpp"
#include "fastsim/spr_shared_prefetch_publish.hpp"
#include "fastsim/spr_prefetch_grant.hpp"
#include <cassert>
#include <vector>
using namespace fastsim;
CacheConfig cfg(){CacheConfig c;c.size_bytes=64;c.associativity=1;return c;}
int main(){
 using S=SprPrefetchService;using G=SprPrefetchGrantTable;
 SprRubyPrefetcher predictor(true);PrivateHierarchy l1(cfg(),cfg());SetAssociativeCache llc(cfg());
 CoreCounters demand,private_prefetch;CacheCounters shared_prefetch,old;
 llc.complete_fill(9,true,old); // dirty victim in a one-line LLC
 auto private_tx=l1.begin_transaction();auto llc_tx=llc.begin_transaction();
 auto predictor_before=predictor;S shared(4),priv(4);auto shared_before=shared;auto private_before=priv;
 G grants(4);auto grants_before=grants;uint64_t ticket=0;std::vector<uint64_t> writes;
 for(unsigned i=0;i<4;++i)predictor.observe_data_access(0x100,0,false,0x1000+i*64,0x8000+i*64);
 assert(predictor.queued()==1);const auto pa=*predictor.peek();const auto line=pa/64;
 G::Bitmap bits{};bits[0]=1;assert(grants.track(line,1,bits)==G::Track::kAdded);
 assert(shared.begin_at(40,++ticket,0,line,40,S::Level::kL2,1)==S::Begin::kAccepted);
 assert(predictor.dequeue()==pa);assert(shared.select_response(ticket,80)==S::Select::kSelected);
 assert(!llc.probe(line,false,shared_prefetch,&llc_tx).hit && !llc.contains(line));
 assert(l1.probe(line,false,demand,&private_tx).level==HitLevel::kLlc);
 assert(shared.waiting(0,line,79) && !llc.contains(line));
 auto permission=[&](){const auto v=grants.validate(line,0,1);return v==G::Validation::kValid?SprPrefetchPermission::kValid:v==G::Validation::kUnknown?SprPrefetchPermission::kUnknown:SprPrefetchPermission::kRevoked;};
 auto evict=[&](uint64_t victim,bool dirty,uint64_t at){assert(at==80);if(dirty)writes.push_back(victim);};
 auto publish_shared=[&](const S::Entry&e){
  const auto r=publish_spr_shared_prefetch(llc,e.line,e.issue,*e.response,*e.response,shared_prefetch,&llc_tx,permission,evict);
  return r==SprSharedPrefetchPublication::kPublished?S::Commit::kPublished:r==SprSharedPrefetchPublication::kPermissionLost?S::Commit::kPermissionLost:S::Commit::kRetry;
 };
 assert(shared.complete_through(79,publish_shared).published==0 && !llc.contains(line));
 assert(shared.complete_through(80,publish_shared).published==1 && llc.contains(line));
 assert(writes.size()==1 && writes[0]==9 && !l1.contains(line));
 assert(priv.begin_at(80,++ticket,0,line,80,S::Level::kL1,1)==S::Begin::kAccepted);
 assert(priv.select_response(ticket,90)==S::Select::kSelected);
 auto grant=[&](const S::Entry&e){return grants.validate(e.line,e.core,e.grant_generation)==G::Validation::kValid?SprPrefetchPermission::kValid:SprPrefetchPermission::kRevoked;};
 auto pvictim=[](uint32_t,uint64_t,bool,uint64_t){assert(false);};
 auto publish_private=[&](const S::Entry&e){return publish_spr_prefetch(e,*e.response,l1,private_prefetch,&private_tx,grant,pvictim);};
 assert(priv.complete_through(89,publish_private).published==0 && !l1.contains(line));
 assert(priv.complete_through(90,publish_private).published==1 && l1.contains(line));
 assert(l1.probe(line,false,demand,&private_tx).level==HitLevel::kL1);
 assert(private_prefetch.l1d.accesses==0 && private_prefetch.l2.accesses==0);
 // Each owner of side effects restores its own state as part of the same batch.
 llc.restore(llc_tx);l1.restore(private_tx);predictor=predictor_before;shared=shared_before;priv=private_before;grants=grants_before;writes.clear();
 assert(llc.contains(9) && !llc.contains(line) && !l1.contains(line) && predictor.queued()==0 && shared.size()==0 && priv.size()==0 && grants.size()==0);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(ROOT/'src/cache.cpp'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(exe)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
