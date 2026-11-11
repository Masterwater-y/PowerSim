"""Bounded per-core forward estimate of sequencer-ordered store visibility."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class OrderedStoreVisibilityTest(unittest.TestCase):
    def test_order_same_line_and_cross_window(self):
        code=r'''
#include "fastsim/spr_ordered_store_visibility.hpp"
#include <cassert>
#include <cstdint>
#include <limits>
#include <stdexcept>
using namespace fastsim;
int main(){
 SprOrderedStoreVisibility s;
 auto a=s.schedule(10,100);assert(a.visibility_cycle==100 && a.delay_cycles==0);
 auto a2=s.schedule(10,30);assert(a2.visibility_cycle==100 && a2.delay_cycles==70 && a2.same_line_at_prior_cycle);
 auto b=s.schedule(11,10);assert(b.visibility_cycle==101 && b.delay_cycles==91 && !b.same_line_at_prior_cycle);
 auto a3=s.schedule(10,3);assert(a3.visibility_cycle==102 && a3.delay_cycles==99);
 auto c=s.schedule(12,200);assert(c.visibility_cycle==200 && c.delay_cycles==0);
 auto d=s.schedule(13,200);assert(d.visibility_cycle==201 && d.delay_cycles==1);
 auto snapshot=s;
 assert(s.schedule(14,1).visibility_cycle==202);
 assert(snapshot.schedule(13,1).visibility_cycle==201);
 s=snapshot;assert(s.schedule(13,1).visibility_cycle==201);
 SprOrderedStoreVisibility warmup;
 warmup.schedule(1,80);warmup.reset_counters(); // retain cross-window ordering
 assert(warmup.schedule(2,0).visibility_cycle==81);
 assert(warmup.counters().stores==1 && warmup.counters().delayed==1);
 warmup.reset();assert(warmup.schedule(2,0).visibility_cycle==0);
 SprOrderedStoreVisibility overflow;overflow.schedule(1,UINT64_MAX);
 bool threw=false;try{overflow.schedule(2,UINT64_MAX);}catch(const std::overflow_error&){threw=true;}
 assert(threw && overflow.schedule(1,UINT64_MAX).visibility_cycle==UINT64_MAX);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            source=Path(d)/'test.cpp';exe=Path(d)/'test';source.write_text(code)
            p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(source),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            p=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)

    def test_against_gem5_fifo_and_same_cycle_predicate(self):
        # Compile the actual target predicate, not a rewritten approximation.
        source=(ROOT/'gem5-spr/src/mem/ruby/system/Sequencer.cc').read_text()
        start=source.index('bool\nSequencer::isOrderedStoreReady(')
        end=source.index('\nvoid\nSequencer::wakeParkedOrderedHead()',start)
        predicate=source[start:end]
        code=r'''
#include "fastsim/spr_ordered_store_visibility.hpp"
#include <cassert>
#include <cstdint>
#include <deque>
#include <memory>
#include <random>
struct TSOOrderedStore {};
struct Request {
 std::uint64_t address;
 template<class T> const T* getExtension() const { static T extension; return &extension; }
 std::uint64_t getPaddr() const { return address; }
};
using RequestPtr=std::shared_ptr<Request>;
struct Sequencer {
 std::deque<const Request*> m_orderedStores;
 bool m_orderedLineValid=false;
 std::uint64_t m_orderedLineCycle=0,m_orderedLine=0,cycle=0;
 std::uint64_t curCycle() const {return cycle;}
 std::uint64_t makeLineAddress(std::uint64_t a) const {return a & ~std::uint64_t(63);}
 bool isOrderedStoreReady(const RequestPtr&) const;
};
''' + predicate + r'''
int main(){
 std::mt19937_64 rng(1903);
 fastsim::SprOrderedStoreVisibility model;
 Sequencer seq;
 for (unsigned i=0;i<20000;++i){
  const auto line=rng()%5;
  const auto ready=rng()%1000;
  auto request=std::make_shared<Request>(Request{line*64});
  auto younger=std::make_shared<Request>(Request{(rng()%5)*64});
  seq.m_orderedStores.push_back(request.get());
  seq.m_orderedStores.push_back(younger.get());
  assert(!seq.isOrderedStoreReady(younger));
  auto result=model.schedule(line,ready);
  assert(result.visibility_cycle>=ready);
  if(seq.m_orderedLineValid){
   assert(result.visibility_cycle>=seq.m_orderedLineCycle);
   if(line*64 != seq.m_orderedLine){
    seq.cycle=seq.m_orderedLineCycle;
    assert(!seq.isOrderedStoreReady(request));
    assert(result.visibility_cycle>seq.cycle);
   }
  }
  seq.cycle=result.visibility_cycle;
  assert(seq.isOrderedStoreReady(request));
  seq.m_orderedLineValid=true;
  seq.m_orderedLineCycle=seq.cycle;
  seq.m_orderedLine=line*64;
  seq.m_orderedStores.clear();
 }
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            source=Path(d)/'test.cpp'; exe=Path(d)/'test'; source.write_text(code)
            p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(source),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            p=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
if __name__=='__main__':unittest.main()
