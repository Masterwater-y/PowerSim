"""Preallocated line dedup, including cluster deletion and snapshot."""
import subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class BoundedLineSetTest(unittest.TestCase):
 def test_preallocation_and_randomized(self):
  code=r'''
#include "fastsim/spr_bounded_line_set.hpp"
#include <atomic>
#include <cassert>
#include <cstdlib>
#include <new>
#include <random>
#include <unordered_set>
std::atomic<unsigned long> allocations{0};
void* operator new(std::size_t n) {++allocations;if(auto p=std::malloc(n))return p;throw std::bad_alloc();}
void operator delete(void* p) noexcept {std::free(p);}
void operator delete(void* p,std::size_t) noexcept {std::free(p);}
int main(){
 using S=fastsim::SprBoundedLineSet;
 bool zero=false;try{S invalid(0);}catch(const std::invalid_argument&){zero=true;}assert(zero);
 bool big=false;try{S invalid((1u<<20)+1);}catch(const std::invalid_argument&){big=true;}assert(big);
 S bounded(8);auto base=allocations.load();
 for(unsigned round=0;round<10000;++round) {
   for(unsigned i=0;i<8;++i)assert(bounded.insert(i)==S::Insert::kInserted);
   assert(bounded.insert(0)==S::Insert::kDuplicate);
   assert(bounded.insert(99)==S::Insert::kFull);
   for(unsigned i=0;i<8;++i)assert(bounded.erase(i));
 }
 assert(allocations.load()==base && bounded.size()==0);
 S set(17);std::unordered_set<uint64_t> ref;std::mt19937_64 rng(9173);
 for(unsigned i=0;i<100000;++i){
   uint64_t line=rng()%128;auto op=rng()%3;
   if(op==0){bool had=ref.count(line),full=ref.size()==17;auto got=set.insert(line);
     assert(got==(had?S::Insert::kDuplicate:full?S::Insert::kFull:S::Insert::kInserted));
     if(!had && !full)ref.insert(line);
   }else if(op==1)assert(set.erase(line)==(ref.erase(line)!=0));
   else assert(set.contains(line)==(ref.count(line)!=0));
   assert(set.size()==ref.size());
 }
 auto snapshot=set;for(uint64_t line=0;line<128;++line)assert(set.contains(line)==snapshot.contains(line));
 for(uint64_t line=0;line<128;++line)set.erase(line);
 assert(set.size()==0);for(auto line:ref)assert(snapshot.contains(line));
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(c.returncode,0,c.stderr)
   r=subprocess.run([str(exe)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
if __name__=='__main__':unittest.main()
