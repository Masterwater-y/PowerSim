"""Behavior of SPR RubyPrefetcher's stride filters and stream callbacks."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SprStreamPrefetchTest(unittest.TestCase):
    def test_streams(self):
        code=r'''
#include "fastsim/spr_stream_prefetch.hpp"
#include <cassert>
int main() {
 using fastsim::SprStreamPrefetch;
 SprStreamPrefetch p;
 for(unsigned i=0;i<4;++i) assert(!p.observe_miss(0x1000+64*i,i));
 assert(p.observe_miss(0x1100,4)==0x1140);
 assert(p.observe_prefetch_access(0x1140,5)==0x1180);
 assert(!p.observe_prefetch_access(0x1140,6));
 SprStreamPrefetch n;
 for(unsigned i=0;i<4;++i) assert(!n.observe_miss(0x1f00-64*i,i));
 assert(n.observe_miss(0x1e00,4)==0x1dc0);
 SprStreamPrefetch nonunit;
 for(unsigned i=0;i<6;++i) assert(!nonunit.observe_miss(0x1000+128*i,i));
 assert(nonunit.observe_miss(0x1300,6)==0x1380);
 SprStreamPrefetch page;
 for(unsigned i=0;i<4;++i) assert(!page.observe_miss(0x1ec0+64*i,i));
 assert(!page.observe_miss(0x1fc0,4));
 SprStreamPrefetch clone=p;
 assert(clone.observe_prefetch_access(0x1180,7)==0x11c0);
 assert(p.observe_prefetch_access(0x1180,7)==0x11c0);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            source=Path(d)/'test.cc';binary=Path(d)/'test';source.write_text(code)
            c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(source),'-o',str(binary)],capture_output=True,text=True)
            self.assertEqual(c.returncode,0,c.stderr)
            r=subprocess.run([str(binary)],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
    def test_against_gem5_source(self):
        upstream=(ROOT/'gem5-spr/src/mem/ruby/structures/RubyPrefetcher.cc').read_text()
        start=upstream.index('void\nRubyPrefetcher::observeMiss(')
        end=upstream.index('\nvoid\nRubyPrefetcher::print(',start)
        actual=upstream[start:end]
        prefix=r'''
#include "fastsim/spr_stream_prefetch.hpp"
#include <cassert>
#include <cstdint>
#include <deque>
#include <array>
#include <bitset>
#include <optional>
#include <random>
#include <vector>
using Addr=uint64_t;using RubyRequestType=int;using Cycles=uint64_t;
#define DPRINTF(...) ((void)0)
Addr makeLineAddress(Addr a,unsigned bits){return (a>>bits)<<bits;}
template<class T> struct CircularQueue:std::deque<T>{void push_back(T v){if(this->size()==8)this->pop_front();std::deque<T>::push_back(v);}};
struct PrefetchEntry {Addr m_address=0;int m_stride=0;Cycles m_use_time=0;bool m_is_valid=false;RubyRequestType m_type=0;std::bitset<8> requestIssued,requestCompleted;};
struct Controller {Cycles now=0;Cycles curCycle(){return now;}};
struct RubyPrefetcher {
 struct UnitFilterEntry {Addr addr;unsigned hits;UnitFilterEntry(Addr a=0):addr(a),hits(0){}};
 struct NonUnitFilterEntry:UnitFilterEntry {int stride=0;NonUnitFilterEntry(Addr a=0):UnitFilterEntry(a){}void clear(){addr=stride=hits=0;}};
 unsigned m_num_streams=4,m_train_misses=4,m_num_startup_pfs=1,m_block_size_bits=6,m_block_size_bytes=64;bool m_prefetch_cross_pages=false;
 std::vector<PrefetchEntry> m_array=std::vector<PrefetchEntry>(4);CircularQueue<UnitFilterEntry> unitFilter,negativeFilter;CircularQueue<NonUnitFilterEntry> nonUnitFilter;
 struct {uint64_t numMissObserved=0,numMissedPrefetchedBlocks=0,numPartialHits=0,numHits=0,numPagesCrossed=0,numStreamCandidates=0,numAllocatedStreams=0;} rubyPrefetcherStats;
 Controller ctrl,*m_controller=&ctrl;std::optional<Addr> candidate;
 Addr pageAddress(Addr a){return a & ~Addr(4095);}Addr makeNextStrideAddress(Addr a,int n){return a+uint64_t(int64_t(n)*64);}
 bool enqueueCandidate(Addr a,int){assert(!candidate);candidate=a;return true;}
 void observeMiss(Addr,const RubyRequestType&);void observePfMiss(Addr);void observePfHit(Addr);void issueNextPrefetch(Addr,PrefetchEntry*);
 uint32_t getLRUindex();void initializeStream(Addr,int,uint32_t,const RubyRequestType&);PrefetchEntry* getPrefetchEntry(Addr,uint32_t&);
 bool accessUnitFilter(CircularQueue<UnitFilterEntry>*,Addr,int,const RubyRequestType&);bool accessNonunitFilter(Addr,const RubyRequestType&);
};
'''
        suffix=r'''
int main(){
 RubyPrefetcher reference;fastsim::SprStreamPrefetch actual;std::mt19937_64 rng(92141);std::array<Addr,16> positions{};
 for(unsigned i=0;i<250000;++i) {
  auto id=rng()%positions.size();if((rng()%13)==0)positions[id]=rng()%64;
  else positions[id]=(positions[id]+(id%3==0 ? 63 : id%3==1 ? 1 : 2))%64;
  const Addr line=0x1000+(id%8)*0x1000+positions[id]*64;
  reference.ctrl.now=i;reference.candidate.reset();
  std::optional<Addr> got;
  if((rng()%5)==0){reference.observePfHit(line);got=actual.observe_prefetch_access(line,i);}
  else {reference.observeMiss(line,1);got=actual.observe_miss(line,i);}
  assert(got==reference.candidate);
 }
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            source=Path(d)/'test.cc';binary=Path(d)/'test';source.write_text(prefix+actual+suffix)
            c=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(source),'-o',str(binary)],capture_output=True,text=True)
            self.assertEqual(c.returncode,0,c.stderr)
            r=subprocess.run([str(binary)],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)

if __name__=='__main__':unittest.main()
