"""SPR shared L2 TLB functional state: domains, LRU, flush and snapshots."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SprStlbTest(unittest.TestCase):
    def test_shared_domains_lru_asid_and_rollback(self):
        code=r'''
#include "fastsim/spr_stlb.hpp"
#include <cassert>
#include <stdexcept>
using namespace fastsim;
int main() {
 SprStlb s;
 assert(SprStlb::domain(0,12)==0 && SprStlb::domain(128ull<<12,12)==1);
 assert(SprStlb::domain(0,21)==0 && SprStlb::domain(0,22)==0 && SprStlb::domain(0,30)==1);
 assert(SprStlb::set(0,12)==0 && SprStlb::set(127ull<<12,12)==127);
 // One per-core state is shared across I/D consumers.
 const uint64_t base=0x1000;assert(s.insert(42,7,base,12,false));
 assert(s.lookup(42,7,base+7) && !s.lookup(43,7,base+7));
 assert(!s.lookup(42,8,base+7)); // PCID is independently required
 assert(!s.insert(42,7,base,12,false)); // gem5 duplicate insert does not update LRU
 // 9 pages in same domain/set, differing only by multiples of 256 pages.
 SprStlb lru;for(unsigned i=0;i<8;++i) assert(lru.insert(1,0,uint64_t(i)*256*4096,12,false));
 assert(lru.lookup(1,0,0)); // page zero becomes MRU
 assert(!lru.insert(1,0,256ull*4096,12,false)); // duplicate insert must not become MRU
 assert(lru.insert(1,0,8ull*256*4096,12,false));
 assert(lru.lookup(1,0,0) && !lru.lookup(1,0,256ull*4096));
 // A 2M entry competes with 4K entries in the same (domain,set).
 SprStlb mixed;for(unsigned i=0;i<8;++i) assert(mixed.insert(1,0,uint64_t(i)*256*4096,12,false));
 assert(mixed.insert(1,0,0x10000000,21,false));
 assert(!mixed.lookup(1,0,0) && mixed.lookup(1,0,0x10000000));
 auto snapshot=lru;assert(lru.insert(1,0,9ull*256*4096,12,false));
 assert(snapshot.lookup(1,0,2ull*256*4096));
 lru=snapshot;assert(lru.lookup(1,0,2ull*256*4096));
 // gem5's Trie returns the first matching prefix, which is the broader
 // mapping when a small page was inserted before an overlapping large page.
 SprStlb overlap;assert(overlap.insert(1,0,0x201000,12,false));
 assert(overlap.insert(1,0,0x200000,21,false));
 assert(overlap.lookup(1,0,0x201000)->page_bits==21);
 assert(overlap.invalidate_address(1,0,0x201000));
 assert(overlap.lookup(1,0,0x201000)->page_bits==12);
 SprStlb pages;assert(pages.insert(1,0,0x200000,21,false));
 assert(pages.lookup(1,0,0x2fffff)->page_bits==21);
 assert(!pages.insert(1,0,0x201000,12,false)); // existing larger translation
 assert(pages.insert(1,0,0x80000000,30,true));
 assert(pages.lookup(1,0,0xbfffffff)->page_bits==30);
 const auto generation=pages.generation();pages.flush_non_global();
 assert(pages.generation()==generation+1 && !pages.lookup(1,0,0x201000));
 assert(pages.lookup(1,0,0x80000000));
 assert(!pages.invalidate_address(1,1,0x80000000));
 assert(pages.lookup(1,0,0x80000000));
 assert(pages.invalidate_address(1,0,0x80000000));
 assert(!pages.lookup(1,0,0x80000000));
 pages.flush_all();assert(!pages.lookup(1,0,0x80000000));
 bool invalid=false;try{pages.insert(0,0,0,16,false);}catch(const std::invalid_argument&){invalid=true;}
 assert(invalid);
 invalid=false;try{pages.insert(1,4096,0,12,false);}catch(const std::invalid_argument&){invalid=true;}
 assert(invalid);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            path=Path(d)/'test.cpp';exe=Path(d)/'test';path.write_text(code)
            p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(path),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            p=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
    def test_domain_set_index_against_gem5_source(self):
        upstream=(ROOT/'gem5-spr/src/arch/x86/tlb.cc').read_text()
        start=upstream.index('        auto location = [](Addr addr, unsigned log_bytes) {')
        end=upstream.index('        };',start)+len('        };')
        location=upstream[start:end]
        source=r'''
#include "fastsim/spr_stlb.hpp"
#include <cassert>
#include <cstdint>
#include <random>
#include <utility>
using Addr=uint64_t;
int main(){
'''+location+r'''
 std::mt19937_64 rng(91273);
 for(unsigned n=0;n<200000;++n){
  const auto addr=rng();
  for(unsigned bits:{12u,21u,22u,30u}){
   const auto reference=location(addr,bits);
   assert(fastsim::SprStlb::domain(addr,bits)==reference.first);
   assert(fastsim::SprStlb::set(addr,bits)==reference.second);
  }
 }
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            path=Path(d)/'test.cpp';exe=Path(d)/'test';path.write_text(source)
            p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(path),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            p=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)

    def test_gem5_trie_prefix_overlap_and_pcid(self):
        trie=(ROOT/'gem5-spr/src/base/trie.hh').read_text()
        for dep in ('#include "base/cprintf.hh"','#include "base/logging.hh"','#include "base/types.hh"'):
            trie=trie.replace(dep,'')
        source=r'''
#include "fastsim/spr_stlb.hpp"
#include <memory>
#include <stdexcept>
namespace gem5 {
 template<class... T>void ccprintf(T&&...){}
 template<class... T>[[noreturn]]void panic(T&&...){throw std::runtime_error("panic");}
}
'''+trie+r'''
int main(){
 int small=12,large=21,pcid=1;
 gem5::Trie<uint64_t,int> t;
 t.insert(0x201000,64-12,&small);t.insert(0x200000,64-21,&large);
 assert(*t.lookup(0x201123)==21);
 fastsim::SprStlb candidate;
 assert(candidate.insert(4,0,0x201000,12,false));
 assert(candidate.insert(4,0,0x200000,21,false));
 assert(candidate.lookup(4,0,0x201123)->page_bits==21);
 gem5::Trie<uint64_t,int> keys;keys.insert(0x400000|1,64-12,&pcid);
 assert(keys.lookup(0x400000|2)==&pcid); // low PCID bits masked by gem5 trie
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            path=Path(d)/'test.cpp';exe=Path(d)/'test';path.write_text(source)
            p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(path),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            p=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)

if __name__=='__main__':unittest.main()
