"""gem5-spr L1 PC-stride predictor functional behavior."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SprIpStrideTest(unittest.TestCase):
    def test_stride_context_and_boundary(self):
        code=r'''
#include "fastsim/spr_ip_stride.hpp"
#include <cassert>
int main() {
 using fastsim::SprIpStride;
 SprIpStride secure_test;
 for (unsigned i=0; i<8; ++i) assert(!secure_test.observe(0x100,1,true,0x1000+64*i,0x8000+64*i));
 SprIpStride p;
 auto f=[&](unsigned pc,unsigned context,unsigned v,unsigned phys) {
   return p.observe(pc,context,false,v,phys);
 };
 assert(!f(0x100,1,0x1000,0x8000));
 assert(!f(0x100,1,0x1000,0x8000));
 assert(!f(0x100,1,0x1040,0x8040));
 assert(!f(0x100,1,0x1080,0x8080));
 assert(f(0x100,1,0x10c0,0x80c0)==0x8100);
 assert(!f(0x100,2,0x1100,0x8100));
 assert(!f(0x200,1,0x12c0,0x82c0));
 assert(!f(0x200,1,0x1280,0x8280));
 assert(!f(0x200,1,0x1240,0x8240));
 assert(f(0x200,1,0x1200,0x8200)==0x81c0);
 assert(!f(0x300,1,0x1f00,0x8f00));
 assert(!f(0x300,1,0x1f40,0x8f40));
 assert(!f(0x300,1,0x1f80,0x8f80));
 assert(!f(0x300,1,0x1fc0,0x8fc0));
 assert(!f(0x300,1,0x2000,0x9000));
 auto saved=p;
 assert(p.observe(0x100,1,false,0x1100,0x8100)==0x8140);
 p=saved; assert(p.observe(0x100,1,false,0x1100,0x8100)==0x8140);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            source=Path(directory)/'main.cc';binary=Path(directory)/'main';source.write_text(code)
            build=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(source),'-o',str(binary)],capture_output=True,text=True)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(binary)],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
    def test_against_gem5_source(self):
        upstream=(ROOT/'gem5-spr/src/mem/ruby/structures/RubyPrefetcher.cc').read_text()
        start=upstream.index('void\nRubyPrefetcher::observeDataAccess(')
        end=upstream.index('\nvoid\nRubyPrefetcher::observeMiss(',start)
        actual=upstream[start:end]
        prefix=r'''
#include "fastsim/spr_ip_stride.hpp"
#include <algorithm>
#include <array>
#include <cassert>
#include <cstdint>
#include <memory>
#include <random>
using Addr=uint64_t;using ContextID=uint64_t;
constexpr auto InvalidContextID=~uint64_t{0};constexpr int RubyRequestType_LD=1;
Addr makeLineAddress(Addr x,unsigned n){return (x>>n)<<n;}
struct Request {
 Addr pc,vaddr,paddr; ContextID context; bool secure;
 bool hasPC()const{return true;}bool hasVaddr()const{return true;}bool hasPaddr()const{return true;}
 bool isUncacheable()const{return false;}bool isStrictlyOrdered()const{return false;}
 bool isInstFetch()const{return false;}bool isPrefetch()const{return false;}
 bool isPTWalk()const{return false;}bool isSecure()const{return secure;}
 Addr getPC()const{return pc;}Addr getVaddr()const{return vaddr;}Addr getPaddr()const{return paddr;}
 bool hasContextId()const{return true;}ContextID contextId()const{return context;}
};
using RequestPtr=std::shared_ptr<Request>;
struct RubyPrefetcher {
 struct PcStrideEntry { bool valid=false,secure=false;ContextID context=0;Addr pc=0,lastLine=0;int64_t stride=0;unsigned samples=0;uint64_t lastUse=0;};
 std::array<PcStrideEntry,64> pcStrideTable{};uint64_t pcStrideClock=0;unsigned m_block_size_bits=6,pageShift=12;bool ipStrideEnabled=true;
 struct {unsigned numIpPageStops=0,numIpCandidates=0;} rubyPrefetcherStats;
 std::optional<Addr> candidate;
 Addr pageAddress(Addr a)const{return a & ~Addr(4095);}
 bool enqueueCandidate(Addr a,int){candidate=a;return true;}
 void observeDataAccess(RequestPtr req);
};
'''
        suffix=r'''
int main(){
 fastsim::SprIpStride actual;RubyPrefetcher reference;
 std::mt19937_64 rng(56297); std::array<uint64_t,128> pos{};
 for (unsigned i=0;i<300000;++i) {
  const unsigned id=rng()%128; const auto pc=0x100+(id%32)*64;const auto context=(id/32)*16;
  if ((rng()%20)==0) pos[id]=(rng()%64); else if ((rng()%12)!=0) pos[id]=(pos[id]+(id%3==0 ? 63 : (id%3==1 ? 1 : 2)))%64;
  const auto vaddr=0x1000+pos[id]*64; const auto paddr=0x8000+pos[id]*64; const bool secure=(rng()%257)==0;
  auto r=std::make_shared<Request>();r->pc=pc;r->context=context;r->vaddr=vaddr;r->paddr=paddr;r->secure=secure;
  reference.candidate.reset();reference.observeDataAccess(r);
  assert(actual.observe(pc,context,secure,vaddr,paddr)==reference.candidate);
 }
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            source=Path(directory)/'main.cc'; binary=Path(directory)/'main';source.write_text(prefix+actual+suffix)
            build=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(source),'-o',str(binary)],capture_output=True,text=True)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(binary)],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)

if __name__=='__main__':unittest.main()
