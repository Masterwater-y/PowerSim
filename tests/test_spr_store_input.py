"""Store-merge eligibility consumes only complete functional facts."""
import subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SprStoreInputTest(unittest.TestCase):
 def test_functional_qualification_and_line_mask(self):
  code=r'''
#include "fastsim/spr_store_input.hpp"
#include "fastsim/spr_committed_store_buffer.hpp"
#include <cassert>
#include <limits>
using namespace fastsim;
int main() {
 TraceRecord r;r.flags=kRetires|kStore|kPhysicalAddress;r.address=0x1008;r.size=8;
 fst::StoreRequestFacts f;f.address=r.address;f.size=r.size;f.byte_enable=0xff;
 f.attributes=fst::kStoreFactsComplete|fst::kStoreOrdinary;f.context=7;f.requestor=9;f.request_flags=0x20;
 auto x=spr_store_input(r,&f);assert(x && x->line==0x1008/64 && x->byte_mask==0xff00 && x->context==7 && x->requestor==9 && x->flags==0x20);
 SprCommittedStoreBuffer buffer(8,4,4);assert(buffer.admit_sq());
 auto first=buffer.offload({1,x->line,x->context,x->requestor,x->flags,x->byte_mask},0);assert(first.accepted && !first.merged);
 auto second_record=r;auto second_facts=f;second_record.address+=8;second_facts.address+=8;
 auto y=spr_store_input(second_record,&second_facts);assert(y && buffer.admit_sq());
 auto second=buffer.offload({2,y->line,y->context,y->requestor,y->flags,y->byte_mask},0);
 assert(second.accepted && second.merged && first.id==second.id && buffer.byte_mask(first.id)==0xffff00);
 assert(!spr_store_input(r,nullptr));
 auto t=r;t.flags=kRetires|kStore;assert(!spr_store_input(t,&f));
 t=r;t.flags|=kAtomic;assert(!spr_store_input(t,&f));
 t=r;t.flags|=kLoad;assert(!spr_store_input(t,&f));
 auto a=f;a.attributes&=~fst::kStoreFactsComplete;assert(!spr_store_input(r,&a));
 a=f;a.attributes&=~fst::kStoreOrdinary;assert(!spr_store_input(r,&a));
 for(auto bit:{fst::kStoreSecure,fst::kStoreSplit,fst::kStoreMasked,fst::kStoreAtomic,
               fst::kStoreNoAccess,fst::kStoreUncacheable,fst::kStoreStrictlyOrdered,
               fst::kStoreHtm,fst::kStoreSpecial,fst::kStorePagedFragments,fst::kMemoryRead,fst::kOrdinaryLoad}) {
   a=f;a.attributes|=bit;assert(!spr_store_input(r,&a));
 }
 a=f;a.attributes|=1u<<31;assert(!spr_store_input(r,&a));
 a=f;a.request_flags|=0x10000000ull;assert(!spr_store_input(r,&a));
 a=f;a.request_flags|=0x400ull;assert(!spr_store_input(r,&a));
 a=f;a.byte_enable=0x7f;assert(!spr_store_input(r,&a));
 a=f;a.byte_enable|=0x100;assert(!spr_store_input(r,&a));
 a=f;a.address++;assert(!spr_store_input(r,&a));
 a=f;a.size++;assert(!spr_store_input(r,&a));
 t=r;t.size=0;a=f;a.size=0;assert(!spr_store_input(t,&a));
 t=r;t.size=65;a=f;a.size=65;a.byte_enable=~0ull;assert(!spr_store_input(t,&a));
 t=r;t.address=0x103f;t.size=2;a=f;a.address=t.address;a.size=2;a.byte_enable=3;assert(!spr_store_input(t,&a));
 t=r;t.address=0x103f;t.size=1;a=f;a.address=t.address;a.size=1;a.byte_enable=1;
 x=spr_store_input(t,&a);assert(x && x->byte_mask==(1ull<<63));
 t=r;t.address=0x1000;t.size=64;a=f;a.address=t.address;a.size=64;a.byte_enable=~0ull;
 x=spr_store_input(t,&a);assert(x && x->byte_mask==~0ull);
 t.address=UINT64_MAX;t.size=2;a.address=t.address;a.size=t.size;a.byte_enable=3;assert(!spr_store_input(t,&a));
 // Zero is a valid captured context/requestor; validity comes from completeness.
 t=r;a=f;a.context=0;a.requestor=0;x=spr_store_input(t,&a);assert(x && !x->context && !x->requestor);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(exe)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
