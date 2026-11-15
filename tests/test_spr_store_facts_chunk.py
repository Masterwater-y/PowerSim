"""Opt-in functional store sidecar lifetime, unknowns, and chunk boundaries."""
import subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SprStoreFactsChunkTest(unittest.TestCase):
 def test_owned_chunk_facts(self):
  code=r'''
#include "fastsim/spr_store_facts_chunk.hpp"
#include <cassert>
#include <memory>
#include <vector>
using namespace fastsim;
using State=SprStoreFactsChunk::Status;
int main() {
 // An OFF integration holds no sidecar and allocates no sidecar storage.
 std::unique_ptr<SprStoreFactsChunk> disabled;
 assert(!disabled);
 std::vector<SprStoreFactsChunk> chunks;
 {
  SprStoreFactsChunk chunk;
  TraceRecord r;r.flags=kRetires|kStore|kPhysicalAddress;r.address=0x1008;r.size=8;
  fst::StoreRequestFacts source;source.address=r.address;source.size=8;source.byte_enable=0xff;
  source.context=7;source.requestor=9;source.attributes=fst::kStoreFactsComplete|fst::kStoreOrdinary;
  chunk.append(1,spr_store_input(r,&source));
  source.context=88; // original producer row must not be borrowed
  chunk.append(5,std::nullopt); // observed store, unknown facts
  chunk.seal(6);
  assert(chunk.lookup(0).status==State::kNotStore);
  auto first=chunk.lookup(1);assert(first.status==State::kKnown && first.value.context==7 && first.value.byte_mask==0xff00);
  assert(chunk.lookup(5).status==State::kUnknown);
  bool failed=false;try{chunk.append(5,std::nullopt);}catch(const std::logic_error&){failed=true;}assert(failed);
  chunks.push_back(std::move(chunk));
 }
 // Adjacent chunks can reuse local indices; compaction/recycle of one chunk
 // does not alter facts of a following live chunk.
 SprStoreFactsChunk second;second.append(0,SprStoreInput{4,2,0x20,3,5});second.seal(2);
 chunks.push_back(std::move(second));
 assert(chunks[0].lookup(1).value.context==7 && chunks[1].lookup(0).value.context==3);
 chunks.erase(chunks.begin());assert(chunks[0].lookup(0).value.byte_mask==2);
 bool out=false;try{chunks[0].lookup(2);}catch(const std::out_of_range&){out=true;}assert(out);
 auto saved=chunks[0].lookup(0).value;chunks[0].reset();
 chunks[0].append(1,std::nullopt);chunks[0].seal(2);
 assert(saved.byte_mask==2 && chunks[0].lookup(0).status==State::kNotStore && chunks[0].lookup(1).status==State::kUnknown);
 SprStoreFactsChunk bad;bad.append(4,std::nullopt);
 bool unsorted=false;try{bad.append(4,std::nullopt);}catch(const std::invalid_argument&){unsorted=true;}assert(unsorted);
 bool invalid=false;try{bad.seal(4);}catch(const std::invalid_argument&){invalid=true;}assert(invalid);
 SprStoreFactsChunk empty;empty.seal(0);assert(empty.store_count()==0);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(exe)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
