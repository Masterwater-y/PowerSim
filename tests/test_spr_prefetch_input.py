"""Input is functional only and never invents unavailable translation/eligibility."""
import subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SprPrefetchInputTest(unittest.TestCase):
 def test_input_eligibility_and_mapping(self):
  code=r'''
#include "fastsim/spr_prefetch_input.hpp"
#include "fastsim/fst_store_requests.hpp"
#include <cassert>
using namespace fastsim;
int main(){
 TraceRecord r;r.flags=kRetires|kLoad|kPhysicalAddress;r.pc=0x1234;r.address=0x8048;r.size=8;
 fst::TranslationFragment f;f.virtual_address=0x1048;f.physical_address=0x8048;f.size=8;
 f.mapping.kind=fst::TranslationKind::kPaged;f.mapping.flags=fst::kTranslationLeafKnown;f.mapping.page_bits=12;
 using S=SprPrefetchInputStatus;
 fst::StoreRequestFacts request;request.attributes=fst::kStoreFactsComplete|fst::kMemoryRead|fst::kOrdinaryLoad;
 assert(spr_prefetch_input(r,true,3,{f},nullptr).status==S::kUnknownEligibility);
 request.attributes|=fst::kStoreSecure;
 assert(spr_prefetch_input(r,true,3,{f},&request).status==S::kNotEligible);
 request.attributes&=~fst::kStoreSecure;request.attributes|=fst::kStoreSplit;
 assert(spr_prefetch_input(r,true,3,{f},&request).status==S::kSplitUnsupported);
 request.attributes&=~fst::kStoreSplit;
 auto x=spr_prefetch_input(r,std::nullopt,3,{f},&request);assert(x.status==S::kUnknownEligibility);
 x=spr_prefetch_input(r,false,3,{f},&request);assert(x.status==S::kNotEligible);
 x=spr_prefetch_input(r,true,std::nullopt,{f},&request);assert(x.status==S::kUnknownContext);
 x=spr_prefetch_input(r,true,3,{},&request);assert(x.status==S::kUnknownTranslation);
 x=spr_prefetch_input(r,true,3,{f},&request);assert(x.status==S::kEligible);
 assert(x.pc==r.pc && x.context==3 && x.virtual_address==0x1048 && x.physical_address==r.address);
 auto bad=f;bad.physical_address++;assert(spr_prefetch_input(r,true,3,{bad},&request).status==S::kUnknownTranslation);
 bad=f;bad.mapping.flags=0;assert(spr_prefetch_input(r,true,3,{bad},&request).status==S::kUnknownTranslation);
 bad=f;bad.mapping.kind=fst::TranslationKind::kIo;assert(spr_prefetch_input(r,true,3,{bad},&request).status==S::kNotEligible);
 assert(spr_prefetch_input(r,true,3,{f,f},&request).status==S::kSplitUnsupported);
 auto crossing=r;crossing.address=0x803f;crossing.size=8;f.physical_address=0x803f;f.virtual_address=0x103f;
 assert(spr_prefetch_input(crossing,true,3,{f},&request).status==S::kSplitUnsupported);
 r.flags=kRetires|kStore|kPhysicalAddress;assert(spr_prefetch_input(r,true,3,{f},&request).status==S::kNotDemandLoad);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(exe)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
