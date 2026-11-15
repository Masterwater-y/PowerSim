"""L1 prefetch only when target already resides in private L2."""
import subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SprL2ResidentPrefetchTest(unittest.TestCase):
 def test_delayed_visibility_and_abort(self):
  code=r'''
#include "fastsim/spr_l2_resident_prefetch.hpp"
#include <cassert>
using namespace fastsim;
CacheConfig cfg(unsigned n){CacheConfig c;c.size_bytes=64*n;c.associativity=1;return c;}
int main(){
 PrivateHierarchy cache(cfg(1),cfg(16));CoreCounters seed,dem;
 cache.complete_fill(0x8100/64,false,seed); // candidate in L2
 cache.complete_fill(0x9000/64,false,seed); // evicts candidate only from L1
 SprL2ResidentPrefetch engine(1,2,8);
 SprPrefetchInput unknown{SprPrefetchInputStatus::kEligible,0x999,0,0x1000,0x8000};
 engine.observe(0,unknown,HitLevel::kUnknown,0,10,cache,nullptr);
 assert(engine.stats().unsupported_input==1 && engine.stats().trained==0);
 auto noevict=[](uint32_t,uint64_t,bool,uint64_t){assert(false);};
 std::vector<PrivateHierarchy*> caches{&cache};std::vector<PrivateTransaction*> tx{nullptr};
 for(unsigned i=0;i<4;++i){
  SprPrefetchInput x{SprPrefetchInputStatus::kEligible,0x100,0,0x1000+i*64,0x8000+i*64};
  engine.advance(i,caches,tx,noevict);
  if(i==3)engine.reset_counters(); // warmup resets counts, never predictor state
  engine.observe(0,x,HitLevel::kL1,i,10,cache,nullptr);
 }
 assert(engine.stats().issued==1 && engine.pending()==1);
 assert(cache.probe(0x8100/64,false,dem).level==HitLevel::kL2);
 engine.advance(12,caches,tx,noevict);assert(engine.pending()==1);
 engine.advance(13,caches,tx,noevict);assert(engine.pending()==0);
 assert(cache.probe(0x8100/64,false,dem).level==HitLevel::kL1);
 assert(engine.stats().completed==1);
 // New demand write invalidates the pending speculative permission.
 PrivateHierarchy c2(cfg(1),cfg(16));CoreCounters dummy;c2.complete_fill(0x8100/64,false,dummy);c2.complete_fill(0x9000/64,false,dummy);
 SprL2ResidentPrefetch abort(1,2,8);std::vector<PrivateHierarchy*> c{&c2};
 for(unsigned i=0;i<4;++i){SprPrefetchInput x{SprPrefetchInputStatus::kEligible,0x100,0,0x1000+i*64,0x8000+i*64};abort.advance(i,c,tx,noevict);abort.observe(0,x,HitLevel::kL1,i,10,c2,nullptr);}
 assert(abort.pending()==1);abort.cancel_line(0x8100/64,4);assert(abort.pending()==0);
 abort.advance(20,c,tx,noevict);assert(c2.probe(0x8100/64,false,dummy).level==HitLevel::kL2 && abort.stats().cancelled_write==1);
 // A same-cycle store is conservatively ordered before prefetch publication.
 PrivateHierarchy c3(cfg(1),cfg(16));CoreCounters d3;c3.complete_fill(0x8100/64,false,d3);c3.complete_fill(0x9000/64,false,d3);
 SprL2ResidentPrefetch tied(1,2,8);std::vector<PrivateHierarchy*> c3s{&c3};
 for(unsigned i=0;i<4;++i){SprPrefetchInput x{SprPrefetchInputStatus::kEligible,0x100,0,0x1000+i*64,0x8000+i*64};tied.advance(i,c3s,tx,noevict);tied.observe(0,x,HitLevel::kL1,i,10,c3,nullptr);}
 assert(tied.pending()==1);
 tied.advance_before_write(0x8100/64,13,c3s,tx,noevict);
 assert(tied.pending()==0 && tied.stats().cancelled_write==1 && tied.stats().completed==0);
 assert(c3.probe(0x8100/64,false,d3).level==HitLevel::kL2);
 // Restore an outer batch after publication, including cache and predictor.
 PrivateHierarchy c4(cfg(1),cfg(16));CoreCounters d4;c4.complete_fill(0x8100/64,false,d4);c4.complete_fill(0x9000/64,false,d4);
 SprL2ResidentPrefetch rollback(1,2,8);std::vector<PrivateHierarchy*> c4s{&c4};
 for(unsigned i=0;i<4;++i){SprPrefetchInput x{SprPrefetchInputStatus::kEligible,0x100,0,0x1000+i*64,0x8000+i*64};rollback.advance(i,c4s,tx,noevict);rollback.observe(0,x,HitLevel::kL1,i,10,c4,nullptr);}
 auto saved=rollback;auto transaction=c4.begin_transaction(4);std::vector<PrivateTransaction*> transactional{&transaction};
 rollback.advance(13,c4s,transactional,noevict);
 assert(rollback.pending()==0 && c4.probe(0x8100/64,false,d4).level==HitLevel::kL1);
 c4.restore(transaction);rollback=saved;
 assert(rollback.pending()==1 && rollback.stats().completed==0);
 assert(c4.probe(0x8100/64,false,d4).level==HitLevel::kL2);
 rollback.advance_before_write(0x8100/64,13,c4s,tx,noevict);
 assert(rollback.pending()==0 && rollback.stats().cancelled_write==1);
 // A write from another core cancels the first core's in-flight prefetch.
 PrivateHierarchy c5(cfg(1),cfg(16)), c6(cfg(1),cfg(16));CoreCounters d5;
 c5.complete_fill(0x8100/64,false,d5);c5.complete_fill(0x9000/64,false,d5);
 SprL2ResidentPrefetch cross(2,2,8);std::vector<PrivateHierarchy*> both{&c5,&c6};
 std::vector<PrivateTransaction*> both_tx{nullptr,nullptr};
 for(unsigned i=0;i<4;++i){SprPrefetchInput x{SprPrefetchInputStatus::kEligible,0x100,0,0x1000+i*64,0x8000+i*64};cross.advance(i,both,both_tx,noevict);cross.observe(0,x,HitLevel::kL1,i,10,c5,nullptr);}
 cross.advance_before_write(0x8100/64,13,both,both_tx,noevict);
 assert(cross.pending()==0 && cross.stats().cancelled_write==1);
 assert(c5.probe(0x8100/64,false,d5).level==HitLevel::kL2);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(ROOT/'src/cache.cpp'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(exe)],capture_output=True,text=True);self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
