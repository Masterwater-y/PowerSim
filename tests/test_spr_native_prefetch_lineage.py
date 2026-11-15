"""Exact prefetch parent observations stay separate from PMU membership."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SprNativePrefetchLineage(unittest.TestCase):
    def test_native_window_balance_and_typed_diagnostics(self):
        code = r'''
#include "mem/perf_cache_native.hh"
#include <cassert>
#include <iostream>
using namespace gem5::perf_cache_native;
int main() {
 Observer o;o.prepare(0);o.prepare(1);
 auto warm=o.issue(0,1,Kind::DataRead,90);
 o.open(0,100);o.open(1,100);
 assert(o.snapshot(0).inflight_begin==1);
 o.lookup(warm,1,Outcome::TagMiss,101);
 o.lookup(warm,1,Outcome::TagMiss,102); // repeated hook is not a lookup
 o.finish(1);
 auto first=o.issue(0,2,Kind::DataRead,103);
 auto retry=o.issue(0,2,Kind::DataRead,104);
 assert(first!=retry && o.snapshot(0).superseded==1);
 o.lookup(retry,2,Outcome::MergedMiss,105);
 o.issue(1,3,Kind::Rfo,105); // independent origin population
 o.writebackAt(0,0,false,106);o.writebackAt(0,1,true,106);
 o.invalidation(0,0,106);o.globalWriteback(2,true);
 auto c=o.snapshot(0);
 assert(c.inflight_end==1 && c.inflight_balance_residual==0);
 assert(c.writebacks==1 && c.dirty_writebacks==1);
 assert(c.outgoing_writebacks[0]==1 && c.invalidations[0]==1);
 assert(c.duplicate_edges==1 && o.globalDirtyWritebacks(2)==1);
 o.close(0,110);o.close(1,110);o.finish(2);o.finish(3);
 o.lookup(retry,2,Outcome::Hit,111,0);
 o.writebackAt(0,1,true,111);o.close(0,112);
 auto frozen=o.snapshot(0);
 assert(frozen.inflight_end==1 && frozen.released==1);
 assert(frozen.inflight_balance_residual==0 && frozen.writebacks==1);
 assert(o.snapshot(1).inflight_balance_residual==0 && o.inflight(1)==0);
 // Next epoch starts from actual live service and resets sample storage.
 o.open(0,120);assert(o.snapshot(0).inflight_begin==0);
 assert(o.samples(0).empty());
 // A full observation table fails explicitly without evicting live facts.
 Observer bounded;bounded.prepare(0);
 for(unsigned i=1;i<=65536;++i)
  assert(bounded.issue(0,i,Kind::DataRead,90));
 bounded.open(0,100);
 assert(!bounded.issue(0,65537,Kind::DataRead,101));
 auto full=bounded.snapshot(0);
 assert(full.overflow==1 && full.issued==0 && full.inflight_end==65536);
 assert(full.inflight_balance_residual==0);
 bounded.finish(1);
 assert(bounded.issue(0,65537,Kind::DataRead,102));
 assert(bounded.snapshot(0).inflight_balance_residual==0);
 writeCounts(std::cout,frozen);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as d:
            p = Path(d)
            (p / 'test.cc').write_text(code)
            built = subprocess.run([
                '/opt/gcc-11/bin/g++', '-std=c++17', '-Wall', '-Wextra',
                '-Werror', '-static-libstdc++', '-static-libgcc',
                '-I', str(ROOT / 'gem5-spr/src'), str(p / 'test.cc'),
                '-o', str(p / 'test')], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([str(p / 'test')], capture_output=True, text=True)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            import json
            counts = json.loads(ran.stdout)
            self.assertEqual(counts['transaction_balance']['balance_residual'], 0)
            self.assertEqual(counts['per_level_diagnostics']['l1d']['invalidations'], 1)

    def test_multiple_followers_retries_overflow_and_production_finish(self):
        source = (ROOT / 'gem5-spr/src/mem/ruby/slicc_interface/RubySlicc_Util.hh').read_text()
        hooks = source[source.index('inline void prefetchOracleRejectFollower('):
                       source.index('// Actual LLC response/eviction only')]
        code = r'''
#include "mem/prefetch_lineage_oracle.hh"
#include <cassert>
#include <tuple>
namespace gem5 {
enum class TaoTraceParentReject : unsigned char {
 None=0, Uniqueness=1, StaleInvalidated=2
};
}
namespace gem5::ruby {
using Addr=std::uint64_t;
enum { MachineType_L2Cache=2,MachineType_L1Cache=1 };
enum PrefetchBit { PrefetchBit_No,PrefetchBit_Yes };
struct MachineID {int type,num;};
prefetch_lineage::Oracle &prefetchOracle() {
 static prefetch_lineage::Oracle o(8); return o;
}
struct TaoTraceNativeAccessRegistry {
 inline static std::vector<std::tuple<unsigned,Addr,Addr,unsigned>> seen;
 inline static std::vector<std::tuple<unsigned,Addr,unsigned,unsigned>> rejected;
 static void notePrefetchParentRejected(unsigned c,Addr s,unsigned chain,
                                       unsigned char reason) {
  rejected.emplace_back(c,s,chain,reason);
 }
 static void noteL1PrefetchParentLlc(unsigned c,Addr s,Addr a,unsigned o,
                                   int,int,int) {seen.emplace_back(c,s,a,o);}
 static void noteL2PrefetchParentLlc(unsigned c,Addr s,Addr a,unsigned o,
                                   int,int,int) {seen.emplace_back(c,s,a,o);}
};
''' + hooks + r'''
}
int main() {
 using namespace gem5::ruby;
 using R=TaoTraceNativeAccessRegistry;
 auto &o=prefetchOracle();
 auto g=o.begin(0,0x1000);
 for(unsigned i=0;i<3;++i)
  prefetchOracleAttach(g,0,0x1000,true,7,100+i,500+i);
 // Repeated observation of the same edge does not invent a retry/follower.
 prefetchOracleAttach(g,0,0x1000,true,7,100,500);
 assert(R::rejected.empty() && o.followersOf(g,0,0x1000).size()==3);
 prefetchOracleLookup(g,{MachineType_L1Cache,0},0x1000,PrefetchBit_Yes,1);
 prefetchOracleResponse(g,0,0x1000,{MachineType_L2Cache,2},1);
 prefetchOracleFinish(g,0,0x1000);
 assert(R::seen.size()==3 && R::rejected.empty() && o.live()==0);
 for(unsigned i=0;i<3;++i)
  assert(R::seen[i]==std::make_tuple(7u,Addr(100+i),Addr(500+i),1u));
 // Same retired identity, different attempt: cannot prove effective service.
 g=o.begin(0,0x2000);
 prefetchOracleAttach(g,0,0x2000,true,7,100,600);
 prefetchOracleAttach(g,0,0x2000,true,7,100,601);
 prefetchOracleLookup(g,{MachineType_L1Cache,0},0x2000,PrefetchBit_Yes,0);
 prefetchOracleResponse(g,0,0x2000,{MachineType_L2Cache,2},1);
 prefetchOracleFinish(g,0,0x2000);
 assert(R::seen.size()==3 && !R::rejected.empty());
 // Overflow rejects the complete stored population plus the dropped identity.
 R::rejected.clear();g=o.begin(0,0x3000);
 for(unsigned i=0;i<9;++i)
  prefetchOracleAttach(g,0,0x3000,true,7,200+i,700+i);
 assert(o.followerOverflows()==1);
 for(unsigned i=0;i<9;++i) {
  bool rejected=false;
  for(const auto &r:R::rejected)
   rejected|=std::get<1>(r)==200+i && std::get<3>(r)==1;
  assert(rejected);
 }
 prefetchOracleLookup(g,{MachineType_L1Cache,0},0x3000,PrefetchBit_Yes,1);
 prefetchOracleResponse(g,0,0x3000,{MachineType_L2Cache,2},1);
 prefetchOracleFinish(g,0,0x3000);
 assert(R::seen.size()==3 && o.live()==0);
 // Invalidation and conflicting responses reject all attached followers.
 for(unsigned mode=0;mode<2;++mode) {
  R::rejected.clear();g=o.begin(0,0x4000);
  for(unsigned i=0;i<2;++i)
   prefetchOracleAttach(g,0,0x4000,true,7,300+i,800+i);
  prefetchOracleLookup(g,{MachineType_L1Cache,0},0x4000,PrefetchBit_Yes,1);
  prefetchOracleResponse(g,0,0x4000,{MachineType_L2Cache,2},1);
  if(mode==0) prefetchOracleInvalidate(g,0,0x4000);
  else prefetchOracleResponse(g,0,0x4000,{MachineType_L1Cache,1},1);
  prefetchOracleFinishL1Parent(g,0,0x4000);
  for(unsigned i=0;i<2;++i) {
   bool rejected=false;
   for(const auto &r:R::rejected)
    rejected|=std::get<1>(r)==300+i &&
              std::get<3>(r)==(mode==0?2u:1u);
   assert(rejected);
  }
 }
 assert(o.live()==0);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as d:
            p = Path(d)
            (p / 'test.cc').write_text(code)
            built = subprocess.run([
                '/opt/gcc-11/bin/g++', '-std=c++17', '-Wall', '-Wextra',
                '-Werror', '-static-libstdc++', '-static-libgcc',
                '-I', str(ROOT / 'gem5-spr/src'), str(p / 'test.cc'),
                '-o', str(p / 'test')], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([str(p / 'test')], capture_output=True, text=True)
            self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_production_sender_hook_classifies_only_immediate_sender(self):
        source = (ROOT / 'gem5-spr/src/mem/ruby/slicc_interface/RubySlicc_Util.hh').read_text()
        begin = source.index('inline void prefetchOracleResponse(')
        hook = source[begin:source.index('inline void prefetchOracleFinish(', begin)]
        # Task 4 fail-closed gate: the production hook rejects followers via
        # the production wrappers; pull their verbatim text into the harness.
        rb = source.index('inline void prefetchOracleRejectFollower(')
        reject_wrappers = source[rb:source.index(
            'inline void prefetchOracleAttach(', rb)]
        code = r'''
#include "mem/prefetch_lineage_oracle.hh"
#include <cassert>
#include <cstdint>
namespace gem5 {
enum class TaoTraceParentReject : unsigned char
{
    None = 0,
    Uniqueness = 1u << 0,
    StaleInvalidated = 1u << 1,
};
}
namespace gem5::ruby {
using Addr=std::uint64_t;
enum { MachineType_L2Cache=2,MachineType_L1Cache=1,MachineType_Other=3 };
struct MachineID { int type;int num; };
prefetch_lineage::Oracle &prefetchOracle() {static prefetch_lineage::Oracle o(8);return o;}
struct TaoTraceNativeAccessRegistry {
    static void notePrefetchParentRejected(uint32_t, uint64_t, unsigned,
                                           uint8_t) {}
    static void noteL1PrefetchParentLlc(uint32_t, uint64_t, uint64_t,
                                        unsigned, int, int, int) {}
    static void noteL2PrefetchParentLlc(uint32_t, uint64_t, uint64_t,
                                        unsigned, int, int, int) {}
};
''' + reject_wrappers + hook + r'''
}
int main() {
 using namespace gem5::ruby;
 auto &o=prefetchOracle();
 for(int i=0;i<4;++i) {
   auto line=std::uint64_t(0x1000+i*0x1000);auto id=o.begin(1,line);
   assert(o.lookup(id,1,line,1) && o.attach(id,1,line,7,42+i,51+i));
   const MachineID senders[]={{MachineType_L2Cache,4},{MachineType_L1Cache,3},
                              {MachineType_L1Cache,1},{MachineType_Other,9}};
   prefetchOracleResponse(id,1,line,senders[i],1);
   auto w=o.finish(id,1,line);assert(w);
   const int expected[]={1,2,0,0};assert(w->response_sender_kind==expected[i]);
   assert(w->response_kind==1);
   assert(w->response_sender_id==(i==0?4:i==1?3:-1));
 }
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as d:
            p=Path(d); (p/'test.cc').write_text(code)
            built=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-Wall','-Wextra',
                                  '-Werror','-static-libstdc++','-static-libgcc','-I',
                                  str(ROOT/'gem5-spr/src'),str(p/'test.cc'),'-o',str(p/'test')],
                                 capture_output=True,text=True)
            self.assertEqual(built.returncode,0,built.stderr)
            ran=subprocess.run([str(p/'test')],capture_output=True,text=True)
            self.assertEqual(ran.returncode,0,ran.stderr)

    def test_completion_hook_precedes_response_and_mapping_release(self):
        src = (ROOT / 'gem5-spr/src/mem/ruby/system/Sequencer.cc').read_text()
        section = src[src.index('Sequencer::hitCallback('):]
        completion = section.index('noteHierarchyCompletion(')
        lookup = section.index('attemptForPacket(packet_id)', completion)
        response = section.index('noteResponse(', lookup)
        release = section.index('observer().finish(', response)
        self.assertLess(completion, lookup)
        self.assertLess(lookup, response)
        self.assertLess(response, release)

    def test_completion_attempt_is_read_before_packet_mapping_release(self):
        code = r'''
#include "mem/perf_cache_native.hh"
#include <cassert>
using namespace gem5::perf_cache_native;
int main() {
    Observer o; o.prepare(3);
    auto first=o.issue(3,0xabc,Kind::DataRead,10);
    assert(first && o.attemptForPacket(0xabc)==first);
    assert(o.attemptForPacket(0xdef)==0);
    auto retry=o.issue(3,0xabc,Kind::DataRead,11);
    assert(retry && retry!=first && o.attemptForPacket(0xabc)==retry);
    o.finish(0xabc);
    assert(o.attemptForPacket(0xabc)==0);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as d:
            p = Path(d)
            (p / 'test.cc').write_text(code)
            built = subprocess.run([
                '/opt/gcc-11/bin/g++', '-std=c++17', '-Wall', '-Wextra',
                '-Werror', '-static-libstdc++', '-static-libgcc',
                '-I', str(ROOT / 'gem5-spr/src'), str(p / 'test.cc'),
                '-o', str(p / 'test')], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([str(p / 'test')], capture_output=True, text=True)
            self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_protocol_wires_parent_witness_without_fst_fields(self):
        proto = ROOT / 'gem5-spr/src/mem/ruby/protocol'
        l2 = (proto / 'MESI_Three_Level-L1cache.sm').read_text()
        llc = (proto / 'MESI_Two_Level-L2cache.sm').read_text()
        msg = (proto / 'MESI_Two_Level-msg.sm').read_text()
        self.assertIn('PrefetchOracleGeneration', msg)
        self.assertIn('prefetchOracleBegin(version, address)', l2)
        self.assertIn('prefetchOracleAttach', l2)
        self.assertIn('prefetchOracleFinish', l2)
        self.assertIn('prefetchOracleInvalidate', l2)
        self.assertEqual(llc.count('prefetchOracleLookup('), 5)

    def test_l2_parent_generation_and_lookup_witness(self):
        code = r'''
#include "mem/prefetch_lineage_oracle.hh"
#include <cassert>
using namespace gem5::prefetch_lineage;
int main() {
    Oracle o(2);
    const auto a = o.begin(1, 0x1000);
    assert(a && o.lookup(a, 1, 0x1000, 1));
    assert(o.attach(a, 1, 0x1000, 7, 42, 55));
    assert(!o.response(a, 2, 0x1000, 1, 4, 2));
    assert(!o.response(a, 1, 0x2000, 1, 4, 2));
    assert(o.response(a, 1, 0x1000, 1, 4, 2));
    auto result = o.finish(a, 1, 0x1000);
    assert(result && result->context == 7 && result->sequence == 42);
    assert(result->attempt == 55 && result->llc_outcome == 1);
    assert(result->response_sender_kind == 1 && result->response_sender_id == 4 && result->response_kind == 2);
    assert(!o.finish(a, 1, 0x1000));
    const auto b = o.begin(1, 0x1000);
    assert(b && b != a && !o.lookup(a, 1, 0x1000, 0));
    assert(!o.response(a, 1, 0x1000, 1, 4, 1));
    assert(!o.attach(b, 2, 0x1000, 7, 43, 56));
    assert(!o.attach(b, 1, 0x2000, 7, 43, 56));
    assert(!o.attach(b, 1, 0x1000, 7, 43, 0));
    assert(o.attach(b, 1, 0x1000, 7, 43, 56));
    assert(o.lookup(b, 1, 0x1000, 0));
    assert(o.response(b, 1, 0x1000, 2, 3, 1));
    auto peer=o.finish(b, 1, 0x1000);assert(peer && peer->llc_outcome == 0);
    assert(peer->response_sender_kind == 2 && peer->response_sender_id == 3 && peer->response_kind == 1);
    const auto c = o.begin(1, 0x2000);
    assert(o.attach(c, 1, 0x2000, 7, 44, 57));
    assert(o.lookup(c, 1, 0x2000, 1));
    assert(o.lookup(c, 1, 0x2000, 1)); // ambiguous repeated lookup
    assert(!o.finish(c, 1, 0x2000));
    const auto d = o.begin(1, 0x3000);
    assert(o.attach(d, 1, 0x3000, 7, 45, 58));
    assert(o.lookup(d, 1, 0x3000, 1));
    assert(o.response(d, 1, 0x3000, 1, 4, 1));
    assert(o.invalidate(d, 1, 0x3000));
    assert(!o.finish(d, 1, 0x3000));
    const auto amb = o.begin(1, 0x7000);
    assert(o.attach(amb, 1, 0x7000, 7, 46, 59));
    assert(o.lookup(amb, 1, 0x7000, 1));
    assert(o.response(amb, 1, 0x7000, 1, 4, 1));
    assert(o.response(amb, 1, 0x7000, 2, 3, 1));
    auto unknown=o.finish(amb, 1, 0x7000);
    assert(unknown && unknown->response_sender_kind == 0 && unknown->response_kind == 0);
    const auto missing = o.begin(1, 0x8000);
    assert(o.attach(missing, 1, 0x8000, 7, 47, 60));
    assert(o.lookup(missing, 1, 0x8000, 1));
    auto absent=o.finish(missing, 1, 0x8000);
    assert(absent && absent->response_sender_kind == 0 && absent->response_kind == 0);
    const auto e = o.begin(1, 0x4000), f = o.begin(1, 0x5000);
    assert(e && f && !o.begin(1, 0x6000) && o.overflows() == 1);
    assert(!o.finish(e, 1, 0x4000)); // no follower or lookup
    assert(!o.finish(f, 1, 0x5000));
    assert(o.live() == 0);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as d:
            p = Path(d)
            (p / 'test.cc').write_text(code)
            built = subprocess.run([
                '/opt/gcc-11/bin/g++', '-std=c++17', '-Wall', '-Wextra',
                '-Werror', '-static-libstdc++', '-static-libgcc',
                '-I', str(ROOT / 'gem5-spr/src'), str(p / 'test.cc'),
                '-o', str(p / 'test')], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([str(p / 'test')], capture_output=True, text=True)
            self.assertEqual(ran.returncode, 0, ran.stderr)


    def test_l0_begun_cross_level_chain_witness(self):
        # Task 3 (TR-3.1): the L1D-begun generation joins across controllers
        # (begin L0, lookup LLC, attach late demand at L0, response observed at
        # L1, finish at L0) into one qualified witness.
        code = r'''
#include "mem/prefetch_lineage_oracle.hh"
#include <cassert>
using namespace gem5::prefetch_lineage;
int main() {
    Oracle o(8);
    // Duplicate live (core,line) generations are rejected.
    const auto g0 = o.begin(3, 0x1000);
    assert(g0 && !o.begin(3, 0x1000));
    // Follower demand merges at L0 before the LLC lookup completes.
    assert(o.attach(g0, 3, 0x1000, 7, 100, 500));
    assert(o.lookup(g0, 3, 0x1000, 1));          // LLC tag miss
    assert(o.response(g0, 3, 0x1000, 1, 2, 1)); // immediate sender: LLC
    auto w = o.finish(g0, 3, 0x1000);
    assert(w && w->context == 7 && w->sequence == 100 && w->attempt == 500);
    assert(w->llc_outcome == 1 && w->response_sender_kind == 1);
    assert(w->response_sender_id == 2 && w->response_kind == 1);
    // Monotonic token; the finished generation can never be rejoined.
    const auto g0b = o.begin(3, 0x1000);
    assert(g0b && g0b != g0);
    assert(!o.attach(g0, 3, 0x1000, 7, 100, 500));
    assert(!o.lookup(g0, 3, 0x1000, 0));
    assert(!o.finish(g0, 3, 0x1000));
    assert(!o.finish(g0b, 3, 0x1000)); // no follower/lookup yet
    // Re-begin after close is allowed and distinct.
    const auto g0c = o.begin(3, 0x1000);
    assert(g0c && g0c != g0 && g0c != g0b);
    assert(!o.finish(g0c, 3, 0x1000));

    // Peer private-L1 supply is recorded as immediate sender kind 2.
    const auto gp = o.begin(3, 0x1100);
    assert(o.lookup(gp, 3, 0x1100, 4));         // remote supply outcome
    assert(o.attach(gp, 3, 0x1100, 7, 101, 501));
    assert(o.response(gp, 3, 0x1100, 2, 1, 1)); // peer core 1
    auto pw = o.finish(gp, 3, 0x1100);
    assert(pw && pw->llc_outcome == 4);
    assert(pw->response_sender_kind == 2 && pw->response_sender_id == 1);

    // Wrong controller and wrong line can never join the generation.
    const auto g1 = o.begin(3, 0x2000);
    assert(g1);
    assert(!o.attach(g1, 4, 0x2000, 7, 102, 502));
    assert(!o.attach(g1, 3, 0x2100, 7, 102, 502));
    assert(!o.attach(g1, 3, 0x2000, 7, 102, 0));
    assert(!o.lookup(g1, 4, 0x2000, 0));
    assert(!o.lookup(g1, 3, 0x2000, 9));
    assert(!o.response(g1, 3, 0x2100, 1, 2, 1));
    assert(!o.finish(g1, 4, 0x2000));
    assert(!o.finish(g1, 3, 0x2100));
    assert(!o.finish(g1, 3, 0x2000)); // unqualified -> no witness, removed

    // More than one follower is ambiguous and yields no witness.
    const auto g2 = o.begin(3, 0x3000);
    assert(o.attach(g2, 3, 0x3000, 7, 103, 503));
    assert(o.attach(g2, 3, 0x3000, 7, 104, 504));
    assert(o.lookup(g2, 3, 0x3000, 0));
    assert(o.response(g2, 3, 0x3000, 1, 2, 1));
    assert(!o.finish(g2, 3, 0x3000));

    // Invalidation while live keeps the witness fail-closed.
    const auto g3 = o.begin(3, 0x4000);
    assert(o.attach(g3, 3, 0x4000, 7, 105, 505));
    assert(o.lookup(g3, 3, 0x4000, 1));
    assert(o.response(g3, 3, 0x4000, 1, 2, 1));
    assert(o.invalidate(g3, 3, 0x4000));
    assert(!o.finish(g3, 3, 0x4000));

    // Private L2 supplies the L1D prefetch locally: no LLC lookup occurred,
    // so the chain cannot certify an LLC outcome (fail closed; Task 4 path).
    const auto g4 = o.begin(3, 0x5000);
    assert(o.attach(g4, 3, 0x5000, 7, 106, 506));
    assert(o.response(g4, 3, 0x5000, 1, 2, 1));
    assert(!o.finish(g4, 3, 0x5000));

    // Prefetch completed before any demand attached: no witness, no leak.
    const auto g5 = o.begin(3, 0x6000);
    assert(o.lookup(g5, 3, 0x6000, 0));
    assert(o.response(g5, 3, 0x6000, 1, 2, 1));
    assert(!o.finish(g5, 3, 0x6000));
    assert(o.live() == 0);

    // Bounded capacity overflow is explicit.
    Oracle small(2);
    const auto a = small.begin(1, 0x1000);
    const auto b = small.begin(1, 0x2000);
    assert(a && b && !small.begin(1, 0x3000));
    assert(small.overflows() == 1);
    assert(!small.finish(a, 1, 0x1000));
    assert(!small.finish(b, 1, 0x2000));
    assert(small.live() == 0);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as d:
            pth = Path(d)
            (pth / 'test.cc').write_text(code)
            built = subprocess.run([
                '/opt/gcc-11/bin/g++', '-std=c++17', '-Wall', '-Wextra',
                '-Werror', '-static-libstdc++', '-static-libgcc',
                '-I', str(ROOT / 'gem5-spr/src'), str(pth / 'test.cc'),
                '-o', str(pth / 'test')], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([str(pth / 'test')], capture_output=True,
                                 text=True)
            self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_protocol_carries_l0_generation_and_l1_never_finishes_it(self):
        proto = ROOT / 'gem5-spr/src/mem/ruby/protocol'
        l0 = (proto / 'MESI_Three_Level-L0cache.sm').read_text()
        l1 = (proto / 'MESI_Three_Level-L1cache.sm').read_text()
        msg = (proto / 'MESI_Three_Level-msg.sm').read_text()
        utilh = (ROOT /
                 'gem5-spr/src/mem/ruby/slicc_interface/RubySlicc_Util.hh').read_text()
        reg = (ROOT / 'gem5-spr/src/mem/taotrace_response.hh').read_text()

        # CoherenceMsg gains the observer-only token.
        coh = msg[msg.index('structure(CoherenceMsg'):msg.index(
            'bool functionalRead', msg.index('structure(CoherenceMsg'))]
        self.assertIn('Addr PrefetchOracleGeneration', coh)

        # L0: TBE field, begin, propagation, attach, invalidate, finish hook.
        self.assertIn('Addr PrefetchOracleGeneration, default="0",'
                      ' desc="Observer-only L1D prefetch generation";', l0)
        self.assertIn('tbe.PrefetchOracleGeneration := '
                      'prefetchOracleBegin(version, address);', l0)
        self.assertIn('out_msg.PrefetchOracleGeneration := '
                      'tbe.PrefetchOracleGeneration;', l0)
        self.assertIn('prefetchOracleAttach(tbe.PrefetchOracleGeneration,', l0)
        self.assertIn('prefetchOracleFinishL1Parent(tbe.PrefetchOracleGeneration,'
                      ' version, address);', l0)
        self.assertIn('oi_invalidateL1ParentOracle', l0)

        # L1: the carried token is stored separately, forwarded to the LLC and
        # observed for response/invalidation, but L1 must never finish it.
        self.assertIn('Addr PrefetchOracleParentGeneration, default="0"', l1)
        self.assertIn('tbe.PrefetchOracleParentGeneration := '
                      'in_msg.PrefetchOracleGeneration;', l1)
        self.assertIn('out_msg.PrefetchOracleGeneration := '
                      'in_msg.PrefetchOracleGeneration;', l1)
        self.assertGreaterEqual(
            l1.count('prefetchOracleResponse(tbe.PrefetchOracleParentGeneration'),
            2)
        self.assertIn('prefetchOracleInvalidate(tbe.PrefetchOracleParentGeneration',
                      l1)
        self.assertEqual(l1.count('prefetchOracleFinishL1Parent'), 0)

        # Finish routing: L0-finished witnesses populate the independent
        # L1D-parent registry population, never the L2-parent one. The L0
        # hook routes to chain 0 of the shared finish template; chain 1 is
        # the private-L2 population and must never appear in this hook.
        begin = utilh.index('inline void prefetchOracleFinishL1Parent')
        hook = utilh[begin:utilh.index(
            '// Actual LLC response/eviction only', begin)]
        self.assertIn('prefetchOracleFinishChain<0>', hook)
        self.assertNotIn('prefetchOracleFinishChain<1>', hook)
        tpl_begin = utilh.index('template <unsigned Chain>')
        tpl = utilh[tpl_begin:utilh.index(
            'inline void prefetchOracleFinish(', tpl_begin)]
        # Chain 1 (if constexpr branch) is L2-only; chain 0 (else branch) is
        # L1D-parent-only. compile-time elimination makes the routing exact.
        c1 = tpl.index('if constexpr (Chain == 1)')
        c0 = tpl.index('} else {', c1)
        chain1 = tpl[c1:c0]
        l1call = tpl.index('noteL1PrefetchParentLlc', c0)
        chain0 = tpl[c0:tpl.index(');', l1call)]
        self.assertIn('noteL2PrefetchParentLlc', chain1)
        self.assertNotIn('noteL1PrefetchParentLlc', chain1)
        self.assertIn('noteL1PrefetchParentLlc', chain0)
        self.assertNotIn('noteL2PrefetchParentLlc', chain0)
        self.assertIn('noteL1PrefetchParentLlc(uint32_t context_id', reg)
        self.assertIn('l1PrefetchParentLlc', reg)


if __name__ == '__main__':
    unittest.main()
