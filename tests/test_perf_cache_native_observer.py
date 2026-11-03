"""Literal native transactions; no committed-instruction registry or gem5 run."""
import os
import ast
import json
import re
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

DRIVER = r'''
#include <cassert>
#include <iostream>
#if __has_include("mem/perf_cache_native.hh")
#include "mem/perf_cache_native.hh"
using namespace gem5::perf_cache_native;
int main() {
    Observer o;
    o.open(0,100);
    auto data=o.issue(0,1,Kind::DataRead,90); // issued before ROI
    o.lookup(data,1,Outcome::TagMiss,101);
    o.lookup(data,2,Outcome::RemoteSupply,102);
    // A replay of one controller edge is not a second transaction.
    o.lookup(data,1,Outcome::TagMiss,103);
    o.finish(1);
    auto rfo=o.issue(0,2,Kind::Rfo,104);
    o.lookup(rfo,1,Outcome::TagMiss,104);
    o.lookup(rfo,2,Outcome::Hit,105);
    o.finish(2);
    auto code=o.issue(0,3,Kind::CodeRead,106);
    o.lookup(code,1,Outcome::Hit,106);
    o.finish(3);
    auto prefetch=o.issue(0,4,Kind::HardwarePrefetch,107);
    o.lookup(prefetch,1,Outcome::TagMiss,107);
    o.lookup(prefetch,2,Outcome::TagMiss,108);
    o.finish(4);
    // A merged request has no extra lookup; only the parent's transaction
    // reaches the controller. A physically issued wrong-path request survives
    // retirement squash because this observer has no retirement filter.
    o.merged(0,109);
    o.retry(0,109);
    auto squashed=o.issue(0,5,Kind::DataRead,109);
    o.lookup(squashed,1,Outcome::TagMiss,110);
    o.writeback(0,true,111); // no instruction identity
    o.close(0,112);
    o.lookup(squashed,2,Outcome::TagMiss,113); // closed ROI cannot grow
    o.writeback(0,true,113);
    o.finish(5);
    auto c=o.snapshot(0);
    assert(c.l2_accesses()==5);
    assert(c.l2_demand_misses()==3); // remote supply never erases L2 miss
    assert(c.llc_accesses()==3);
    assert(c.llc_tag_misses()==1); // unvalidated hardware mapping
    assert(c.writebacks==1 && c.dirty_writebacks==1);
    assert(c.merged==1 && c.retries==1);
    assert(c.duplicate_edges==1);
    assert(o.inflight(0)==0);
    assert(c.levels[1][unsigned(Kind::CodeRead)][unsigned(Outcome::Hit)]==1);
    assert(c.levels[2][unsigned(Kind::DataRead)][unsigned(Outcome::RemoteSupply)]==1);
    assert(o.samples(0).size()==9); // lookup edges plus one real writeback
    const auto &sample=o.samples(0).front();
    assert(sample.transaction_id==data && sample.origin_core==0);
    assert(sample.cache_level==1 && sample.window_id==100 && sample.tick==101);
    assert(!sample.event_context_known);
    // The request table is bounded by live physical transactions and can be
    // recycled after completion without retaining an all-run ID set.
    for (unsigned n=10;n<10010;++n) {
        auto id=o.issue(0,n,Kind::DataRead,114);
        o.lookup(id,1,Outcome::Hit,115); o.finish(n);
    }
    assert(o.inflight(0)==0);
    assert(o.snapshot(0).l2_accesses()==5);
    assert(o.samples(0).size()==9); // post-window requests produce no diagnostics
    Observer lost;
    lost.open(0,1);
    lost.lookup(999,1,Outcome::TagMiss,2,0);
    assert(lost.snapshot(0).unknown_identity==1);
    assert(lost.snapshot(0).ignored_hooks==1);
    assert(lost.snapshot(0).levels[1][unsigned(Kind::Unknown)][unsigned(Outcome::TagMiss)]==1);
    Observer hw;
    hw.open(0,1);
    hw.unidentifiedLookup(0,Kind::HardwarePrefetchL1,1,Outcome::TagMiss,2);
    hw.unidentifiedLookup(0,Kind::HardwarePrefetchL2,2,Outcome::TagMiss,3);
    hw.unidentifiedLookup(0,Kind::HardwarePrefetchL3,2,Outcome::TagMiss,4);
    assert(hw.snapshot(0).l2_accesses()==1);
    assert(hw.snapshot(0).llc_accesses()==1); // LLC prefetch is outside core event
    assert(hw.snapshot(0).llc_tag_misses()==1);
    writeCounts(std::cout,c);
}
#else
int main() { std::cerr << "native request observer missing\n"; return 1; }
#endif
'''


class NativeCacheObserverTests(unittest.TestCase):
    def test_real_sequencer_origin_is_not_default_corepair_99(self):
        # First live capture had coreid=99 on every Sequencer but versions0..3.
        # Execute the production observer argument expressions with that literal
        # configuration rather than testing an independently chosen origin.
        source = (ROOT / "gem5-fs/src/mem/ruby/system/Sequencer.cc").read_text()
        issue = re.search(r"observer\(\)\.issue\(\s*([^,]+),", source).group(1)
        merged = re.search(r"observer\(\)\.merged\(([^,]+),", source).group(1)
        retry = re.search(r"observer\(\)\.retry\(([^,]+),", source).group(1)
        fixture = r'''
#include <cassert>
#include "mem/perf_cache_native.hh"
using namespace gem5::perf_cache_native;
struct NativeSequencer {
    int m_version=0,m_coreId=99;
    int coreId() const { return m_coreId; }
    void observe() {
        observer().issue(ISSUE_ORIGIN,1,Kind::DataRead,101);
        observer().merged(MERGE_ORIGIN,102);
        observer().retry(RETRY_ORIGIN,103);
    }
};
int main() {
    observer().prepare(0); observer().open(0,100);
    NativeSequencer s; s.observe();
    assert(observer().snapshot(0).issued==1);
    assert(observer().snapshot(0).merged==1);
    assert(observer().snapshot(0).retries==1);
}
'''.replace("ISSUE_ORIGIN", issue).replace("MERGE_ORIGIN", merged).replace("RETRY_ORIGIN", retry)
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temp:
            path = Path(temp)
            (path / "test.cc").write_text(fixture)
            build = subprocess.run([
                "/opt/gcc-11/bin/g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                "-I", str(ROOT / "gem5-fs/src"), str(path / "test.cc"),
                "-o", str(path / "test")], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(path / "test")], capture_output=True, text=True,
                                 env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"))
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_merge_hook_uses_completed_coalesced_service(self):
        # Sequencer aliases can later reissue (not merge). The existing native
        # callback identifies which packets actually shared a service.
        source = (ROOT / "gem5-fs/src/mem/ruby/system/Sequencer.cc").read_text()
        make = source.split("Sequencer::makeRequest(PacketPtr pkt)", 1)[1].split(
            "Sequencer::issueRequest", 1)[0]
        self.assertNotIn("observer().merged", make)
        callback = source.split("Sequencer::hitCallback(", 1)[1].split(
            "Sequencer::", 1)[0]
        self.assertIn("if (was_coalesced)", callback)
        self.assertIn("observer().merged", callback)

    def test_production_raw_sidecar_serializes_independent_evidence(self):
        # Reuse the maintained collector's small production-member fixture;
        # execute the actual serializer, not a test reimplementation.
        tree = ast.parse((ROOT / "tests/test_perf_retirement_collector.py").read_text())
        preamble = next(n.value.value for n in ast.walk(tree)
                        if isinstance(n, ast.Assign) and any(
                            isinstance(t, ast.Name) and t.id == "preamble" for t in n.targets))
        preamble = preamble.replace("#include <fstream>",
                                    '#include <fstream>\n#include "mem/perf_cache_native.hh"\nnamespace perf_cache_native=gem5::perf_cache_native;')
        preamble = preamble.replace("void finalizePerfRetirement();",
                                    "void finalizePerfCache();")
        source = (ROOT / "gem5-fs/src/cpu/o3/probe/tao_trace.cc").read_text()
        serializer = "void\nTaoTrace::finalizePerfCache()" + source.split(
            "void\nTaoTrace::finalizePerfCache()", 1)[1].split(
                "// Read-only copy from the independent native lifecycle", 1)[0]
        driver = r'''
int main(int argc,char **argv) {
    if(argc!=2) return 2;
    TaoTrace t; t.output_dir_=argv[1]; t.perf_retirement_finalized_=true;
    t.thread_state_tasks_[42]={10,100,100,1};
    retirement::Member m; m.owner=10; m.measured=true; m.read=true; m.sequence=1;
    t.perf_retirement_.commit(m); t.perf_retirement_.resolve(0,1,retirement::L1|retirement::Miss);
    m.sequence=2; m.read=false; m.write=true; m.user=false; t.perf_retirement_.commit(m);
    auto &o=gem5::perf_cache_native::observer(); o.open(0,100);
    auto id=o.issue(0,1,gem5::perf_cache_native::Kind::CodeRead,101);
    o.lookup(id,1,gem5::perf_cache_native::Outcome::TagMiss,102);
    o.lookup(id,2,gem5::perf_cache_native::Outcome::RemoteSupply,103);
    o.writeback(0,true,104); o.finish(1); o.close(0,200);
    t.finalizePerfCache();
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temp:
            path = Path(temp)
            (path / "oracle").mkdir()
            (path / "test.cc").write_text(preamble + serializer + driver)
            build = subprocess.run([
                "/opt/gcc-11/bin/g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                "-I", str(ROOT / "gem5-fs/src"), str(path / "test.cc"),
                "-o", str(path / "test")], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(path / "test"), str(path)], capture_output=True, text=True,
                                 env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"))
            self.assertEqual(run.returncode, 0, run.stderr)
            raw = json.loads((path / "oracle/perf-cache-core0.json").read_text())
            self.assertEqual(raw["schema"], "gem5-perf-cache-observations-v2")
            physical = raw["scopes"]["physical"]
            self.assertEqual(physical["retired_macro_instructions"], 2)
            self.assertEqual(physical["retirement_events"]["perf_l1d_load_hits"], 1)
            self.assertEqual(physical["retirement_events"]["perf_l1d_load_misses"], 1)
            self.assertEqual(physical["request_events"]["perf_l2_demand_misses"], 1)
            self.assertEqual(physical["request_events"]["perf_llc_misses_core"], 0)
            self.assertEqual(physical["private_l2_outgoing_writebacks"], 1)
            self.assertEqual(raw["observer"]["overflow"], 0)
            self.assertEqual(physical["request_primitives"]["levels"]["llc"]["code_read"]["remote_supply"], 1)
            self.assertFalse(physical["event_capabilities"]["perf_l1d_load_misses"]["source_qualification"])
            self.assertFalse(physical["event_capabilities"]["perf_l2_writebacks"]["event_context"])
            self.assertEqual(raw["scopes"]["user"]["request_events"]["perf_l2_demand_misses"], 0)

    def test_transaction_population_levels_and_window_edges(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temp:
            path = Path(temp)
            (path / "test.cc").write_text(DRIVER)
            build = subprocess.run([
                "/opt/gcc-11/bin/g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                "-I", str(ROOT / "gem5-fs/src"), str(path / "test.cc"),
                "-o", str(path / "test")], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(path / "test")], capture_output=True, text=True,
                                 env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"))
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_independent_retirement_qualification(self):
        source = r'''
#include <cassert>
#include <iostream>
#include "cpu/o3/probe/perf_retirement.hh"
using namespace gem5::o3::retirement;
int main() {
#ifdef TAOTRACE_RETIREMENT_INDEPENDENT_EVENTS
    Ledger l;
    Member m; m.sequence=1; m.read=true; m.measured=true;
    l.commit(m); l.resolve(0,1,L1|Miss|FillBuffer);
    auto c=l.snapshot({}).physical;
    assert(c.independent_l1_hits==1 && c.independent_l1_misses==1);
    assert(c.independent_fb_hits==1 && c.perf_l1d_load_hits==0);
    NativeFacts forward; forward.resolved=true; forward.store_forward=true;
    forward.admissions=2; // discarded older requests don't define data source
    assert(classifyIndependent(forward)==(L1|Forwarded));
    m.sequence=2; l.commit(m); l.resolve(0,2,classifyIndependent(forward));
    c=l.snapshot({}).physical;
    assert(c.independent_l1_hits==2 && c.independent_l1_misses==1);
    assert(c.independent_l1_hits_unknown==0);
    m.sequence=3; l.commit(m); l.resolve(0,3,L1|Unknown);
    c=l.snapshot({}).physical;
    assert(c.independent_l1_hits==3 && c.independent_l1_hits_unknown==0);
    assert(c.independent_l1_misses_unknown==1 && c.independent_fb_hits_unknown==1);
    m.sequence=4; m.unsupported=true; l.commit(m); l.resolve(0,4,L1);
    c=l.snapshot({}).physical;
    assert(c.independent_l1_hits==3 && c.independent_l1_hits_unknown==1);
    assert(c.independent_retired_loads_unknown==1);
    m.sequence=5; m.unsupported=false; m.last=false; l.commit(m);
    c=l.snapshot({}).physical;
    assert(c.independent_l1_hits_unknown==1); // incomplete tail isn't retired
#else
    std::cerr << "independent retirement qualification missing\n"; return 1;
#endif
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temp:
            path = Path(temp)
            (path / "test.cc").write_text(source)
            build = subprocess.run([
                "/opt/gcc-11/bin/g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                "-I", str(ROOT / "gem5-fs/src"), str(path / "test.cc"),
                "-o", str(path / "test")], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(path / "test")], capture_output=True, text=True,
                                 env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"))
            self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == "__main__":
    unittest.main()
