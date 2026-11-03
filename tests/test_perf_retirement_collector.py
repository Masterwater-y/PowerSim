"""Execute the production gem5 retirement helper without an O3 simulation."""
from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HEADER_ROOT = ROOT / "gem5-fs/src"
DRIVER = r'''
#include <cassert>
#include <iostream>
#if __has_include("cpu/o3/probe/perf_retirement.hh")
#include "cpu/o3/probe/perf_retirement.hh"
using namespace gem5::o3::retirement;
static Member member(uint64_t seq, bool first=true, bool last=true,
                     bool read=false, bool write=false, bool user=true,
                     uint64_t owner=10, bool measured=true) {
    Member m;
    m.sequence=seq; m.first=first; m.last=last; m.read=read; m.write=write;
    m.user=user; m.owner=owner; m.measured=measured;
    return m;
}
int main() {
    // Removing macro union or counting memory UOPs/lines must fail here.
    Ledger ledger;
    ledger.commit(member(1,true,false,true));
    ledger.resolve(0,1,L1);
    ledger.commit(member(2,false,false,true));
    ledger.resolve(0,2,Miss);
    ledger.commit(member(3,false,true));
    auto s=ledger.snapshot({10});
    assert(s.physical.retired_macro_instructions==1);
    assert(s.physical.retired_uops==3);
    assert(s.physical.perf_retired_loads==1);
    assert(s.physical.perf_l1d_load_misses==1);
    assert(s.physical.mixed_source_loads==1);
    // One atomic macro contributes independently to both read and write.
    ledger.commit(member(4,true,true,true,true));
    ledger.resolve(0,4,L1);
    // A split native access with one L1 and one miss remains one load.
    NativeFacts split;
    split.resolved=true; split.responses=2; split.admissions=2;
    split.hierarchy_requests=2; split.accesses=2;
    split.hits=1; split.tag_misses=1;
    split.external_hits=1; split.external_misses=1;
    assert(classify(split)==(L1|Miss));
    ledger.commit(member(5,true,true,true));
    ledger.resolve(0,5,classify(split));
    s=ledger.snapshot({10});
    assert(s.physical.perf_retired_loads==3);
    assert(s.physical.perf_retired_stores==1);
    assert(s.physical.perf_l1d_load_misses==2);
    // Native pure forwarding proves forwarding, not a cache residency hit.
    NativeFacts forward;
    forward.resolved=true; forward.store_forward=true;
    assert(classify(forward)==(Unknown|Forwarded));
    // Hot coalesced L1 parents are L1; cold parent followers are FB evidence.
    NativeFacts hot;
    hot.resolved=true; hot.admissions=1; hot.responses=1;
    hot.coalesced=1; hot.external_hits=1;
    assert(classify(hot)==L1);
    hot.external_hits=0; hot.external_misses=1;
    assert(classify(hot)==FillBuffer);
    // Missing hierarchy and partial/permission/local facts fail closed.
    NativeFacts missing;
    missing.resolved=true; missing.admissions=1; missing.responses=1;
    assert(classify(missing)==Unknown);
    split.accesses=1;
    assert(classify(split)==Unknown);
    split.accesses=2; split.tag_misses=0; split.permission_upgrades=1;
    assert(classify(split)==Unknown);
    forward.other_terminal=true;
    assert(classify(forward)==(Unknown|Forwarded));
    // Delayed service facts retain retirement owner after another task runs.
    Ledger delayed;
    delayed.commit(member(10,true,true,true,true,false,20));
    delayed.commit(member(11,true,true,false,false,true,30));
    auto before=delayed.snapshot({20});
    assert(before.workload_task.unclassified_loads==1);
    delayed.resolve(0,10,Miss);
    auto after=delayed.snapshot({20});
    assert(after.workload_task.perf_l1d_load_misses==1);
    assert(after.other_task.perf_retired_loads==0);
    assert(after.kernel.perf_retired_stores==1);
    // Preserve a warmup member; only measured UOPs enter the denominator.
    Ledger warm;
    warm.commit(member(20,true,false,false,true,false,20,false));
    warm.commit(member(21,false,true,false,false,false,20));
    auto w=warm.snapshot({20});
    assert(w.physical.perf_retired_stores==1);
    assert(w.physical.retired_uops==1);
    assert(w.physical.warmup_spanning_macros==1);
    // Missing leading micro and open tail are visible, never padded.
    warm.commit(member(22,false,true,true,false));
    warm.resolve(0,22,L1);
    warm.commit(member(23,true,false,true));
    w=warm.snapshot({20});
    assert(w.physical.leading_incomplete_macros==1);
    assert(w.physical.incomplete_macros==1);
    assert(w.physical.incomplete_uops==1);
    assert(w.physical.retired_macro_instructions==2);
    // Native syscall expansion is one functional macro/record, three native UOPs.
    Ledger syscall;
    for (unsigned i=0;i<3;++i) {
        auto m=member(30+i,i==0,i==2);
        m.syscall=true; syscall.commit(m);
    }
    auto c=syscall.snapshot({10});
    assert(c.physical.retired_macro_instructions==1);
    assert(c.physical.retired_uops==1);
    assert(c.physical.native_retired_uops==3);
    // Owner/CPL crossings disable certification independently of raw count.
    Ledger crossing;
    crossing.commit(member(40,true,false,true,false,true,10));
    crossing.resolve(0,40,L1);
    crossing.commit(member(41,false,true,false,false,false,20));
    assert(crossing.snapshot({10}).physical.scope_crossing_macros==1);
    // Unsupported ISA data sources remain explicitly unknown.
    auto unsupported=member(42,true,true,true);
    unsupported.unsupported=true;
    crossing.commit(unsupported); crossing.resolve(0,42,L1);
    auto x=crossing.snapshot({10});
    assert(x.physical.unclassified_loads==1);
    assert(x.physical.unsupported_loads==1);
#if defined(TAOTRACE_RETIREMENT_ROM_BOUNDARIES)
    // Exact native ROM extern entries are valid starts despite static flags.
    assert(knownRomEntry(0x8000,0x8000,{0,61,122}));
    assert(!knownRomEntry(0x8001,0x8000,{0,61,122}));
    assert(!knownRomEntry(0,0x8000,{0,61,122}));
    Ledger rom;
    auto entry=member(50,false,false,true,false,false);
    entry.rom=true; entry.rom_entry=true;
    rom.commit(entry); rom.resolve(0,50,L1);
    auto end=member(51,false,true,false,false,false);
    end.rom=true; rom.commit(end);
    auto r=rom.snapshot({10});
    assert(r.physical.leading_incomplete_macros==0);
    assert(r.physical.rom_entry_macros==1);
    assert(r.physical.rom_microcode_macros==1);
    assert(r.physical.unclassified_loads==1);
    assert(r.physical.unsupported_loads==1);
    assert(r.physical.retired_macro_instructions==1);
    // Unknown ROM middle starts still fail; empty ledger is not evidence.
    Ledger unknown;
    auto middle=member(52,false,true,true);
    middle.rom=true; unknown.commit(middle); unknown.resolve(0,52,L1);
    assert(unknown.snapshot({10}).physical.leading_incomplete_macros==1);
    Ledger unknown_tail;
    middle.last=false; middle.sequence=54;
    unknown_tail.commit(middle);
    assert(unknown_tail.snapshot({10}).physical.leading_incomplete_macros==1);
    assert(unknown_tail.snapshot({10}).physical.retired_macro_instructions==0);
    Ledger warmup_tail;
    middle.measured=false; middle.sequence=55;
    warmup_tail.commit(middle);
    assert(warmup_tail.snapshot({10}).physical.leading_incomplete_macros==0);
    // Interrupting an open native macro is a discontinuity, never a merge.
    Ledger interrupted;
    interrupted.commit(member(53,true,false,true));
    interrupted.resolve(0,53,L1);
    interrupted.commit(entry); interrupted.resolve(0,50,L1);
    interrupted.commit(end);
    r=interrupted.snapshot({10});
    assert(r.physical.boundary_discontinuities==1);
    assert(r.physical.incomplete_macros==1);
    assert(r.physical.incomplete_uops==1);
    assert(r.physical.retired_macro_instructions==1);
#else
    std::cerr << "native ROM entry boundaries missing\n";
    return 1;
#endif
    writeCounts(std::cout,x.physical,true);
    std::cout << '\n';
    writeCounts(std::cout,x.physical,false);
    std::cout << '\n';
}
#else
int main() { std::cerr << "independent native retirement ledger missing\n"; return 1; }
#endif
'''


class NativeRetirementCollectorTests(unittest.TestCase):
    def test_native_macro_and_source_behavior(self):
        with tempfile.TemporaryDirectory(prefix="perf-retirement-", dir=ROOT / "tmp") as temp:
            path = Path(temp)
            (path / "test.cc").write_text(DRIVER)
            compiler = "/opt/gcc-11/bin/g++"
            built = subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                                    "-I", str(HEADER_ROOT), str(path / "test.cc"),
                                    "-o", str(path / "test")], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            env = dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64")
            run = subprocess.run([str(path / "test")], env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            counts, unavailable = [json.loads(line) for line in run.stdout.splitlines()]
            self.assertEqual(counts["coverage"]["unclassified_loads"], 1)
            self.assertEqual(counts["perf_retired_loads"], 2)
            self.assertFalse(unavailable["available"])
            self.assertIsNone(unavailable["perf_retired_loads"])
            self.assertIsNone(unavailable["coverage"]["classified_loads"])

    def test_production_sidecar_serializer_and_population_gate(self):
        """A kernel prefix stays counted and bad FST population fails closed."""
        source = (HEADER_ROOT / "cpu/o3/probe/tao_trace.cc").read_text()
        if "TaoTrace::finalizePerfRetirement()" not in source:
            self.fail("independent native retirement sidecar serializer missing")
        serializer = source.split("void\nTaoTrace::finalizePerfRetirement()", 1)[1]
        serializer = "void\nTaoTrace::finalizePerfRetirement()" + serializer.split(
            "\n// The independent task population", 1)[0]
        preamble = r'''
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <string>
#include "cpu/o3/probe/perf_retirement.hh"
#include "mem/llc_fill_audit.hh"
using namespace gem5::o3;
namespace llc_fill_audit=gem5::llc_fill_audit;
struct TaoTrace {
    struct StateTask { uint64_t task_id=0; int32_t pid=-1; uint64_t tgid=0,runtime_id=0; };
    retirement::Ledger perf_retirement_;
    retirement::BoundaryStream perf_retirement_boundaries_;
    bool perf_retirement_finalized_=false;
    uint64_t thread_state_marker_pc_=1;
    std::string fst_path_="core0.fst",output_dir_;
    bool functional_include_kernel_=true,functional_user_only_=false;
    bool functional_warmup_=true,functional_measurement_started_=true;
    bool functional_target_exit_scheduled_=true,thread_state_initial_owner_=true;
    uint64_t functional_common_end_tick_=200,cpl_measurement_end_tick_=200;
    uint64_t cpl_measurement_begin_tick_=100,functional_warmup_record_count_=1;
    uint64_t fst_record_count_=3,functional_measurement_instruction_count_=2;
    uint64_t fst_memory_request_hash_=0;
    uint64_t thread_state_owner_pointer_=42,thread_state_read_failures_=0;
    uint64_t thread_state_identity_failures_=0,thread_state_unretained_hooks_=0;
    uint64_t perf_retirement_owner_unknown_=0,functional_target_drain_polls_=1;
    int fst_core_id_=0;
    std::map<uint64_t,StateTask> thread_state_tasks_;
    std::map<uint64_t,int> pending_native_response_diag_;
    std::vector<std::string> perf_retirement_boundary_samples_;
    uint64_t perf_retirement_boundary_samples_omitted_=0;
    void finalizePerfRetirement();
};
'''
        driver = r'''
int main(int argc,char **argv) {
    if(argc!=2) return 2;
    TaoTrace trace; trace.output_dir_=argv[1];
    trace.thread_state_tasks_[42]={10,100,100,1};
    retirement::Member kernel;
    kernel.owner=10; kernel.user=false; kernel.measured=true;
    kernel.write=true; kernel.sequence=1;
    trace.perf_retirement_.commit(kernel);
    retirement::Member user;
    user.owner=10; user.measured=true; user.read=true; user.sequence=2;
    trace.perf_retirement_.commit(user);
    trace.perf_retirement_.resolve(0,2,retirement::L1);
    retirement::NativeMemberDiagnostic diagnostic;
    diagnostic.name="slli"; diagnostic.disasm="Microcode_ROM:\t\"slli\"\\t";
    diagnostic.micro_pc=0x8000;
    std::ostringstream sample;
    retirement::writeNativeMemberDiagnostic(sample,diagnostic);
    trace.perf_retirement_boundary_samples_.push_back(sample.str());
    trace.finalizePerfRetirement();
    trace.fst_core_id_=1; trace.fst_record_count_=4;
    trace.perf_retirement_finalized_=false;
    trace.finalizePerfRetirement();
    // Valid physical totals cannot certify a broken ownership population.
    trace.fst_core_id_=2; trace.fst_record_count_=3;
    trace.thread_state_read_failures_=1;
    trace.perf_retirement_finalized_=false;
    trace.finalizePerfRetirement();
    TaoTrace rom;
    rom.output_dir_=argv[1]; rom.fst_core_id_=3;
    rom.thread_state_tasks_[42]={10,100,100,1};
    rom.functional_measurement_instruction_count_=1;
    retirement::Member entry;
    entry.first=false; entry.last=false; entry.rom=true; entry.rom_entry=true;
    entry.read=true; entry.measured=true; entry.owner=10; entry.sequence=50;
    rom.perf_retirement_.commit(entry);
    rom.perf_retirement_.resolve(0,50,retirement::L1);
    auto end=entry; end.rom_entry=false; end.last=true; end.read=false; end.sequence=51;
    rom.perf_retirement_.commit(end);
    rom.finalizePerfRetirement();
    TaoTrace interrupted;
    interrupted.output_dir_=argv[1]; interrupted.fst_core_id_=4;
    interrupted.fst_record_count_=4;
    interrupted.functional_measurement_instruction_count_=1;
    interrupted.thread_state_tasks_[42]={10,100,100,1};
    auto prefix=entry; prefix.first=true; prefix.rom=false; prefix.rom_entry=false;
    prefix.sequence=49;
    interrupted.perf_retirement_.commit(prefix);
    interrupted.perf_retirement_.resolve(0,49,retirement::L1);
    interrupted.perf_retirement_.commit(entry);
    interrupted.perf_retirement_.resolve(0,50,retirement::L1);
    interrupted.perf_retirement_.commit(end);
    interrupted.finalizePerfRetirement();
    TaoTrace missing_tail;
    missing_tail.output_dir_=argv[1]; missing_tail.fst_core_id_=5;
    missing_tail.fst_record_count_=2;
    missing_tail.functional_measurement_instruction_count_=0;
    missing_tail.thread_state_tasks_[42]={10,100,100,1};
    auto missing=entry; missing.rom_entry=false; missing.sequence=60;
    missing_tail.perf_retirement_.commit(missing);
    missing_tail.perf_retirement_.resolve(0,60,retirement::L1);
    missing_tail.finalizePerfRetirement();
}
'''
        with tempfile.TemporaryDirectory(prefix="perf-sidecar-", dir=ROOT / "tmp") as temp:
            path = Path(temp)
            (path / "oracle").mkdir()
            (path / "test.cc").write_text(preamble + serializer + driver)
            built = subprocess.run(["/opt/gcc-11/bin/g++", "-std=c++17", "-Wall", "-Wextra",
                                    "-Werror", "-I", str(HEADER_ROOT), str(path / "test.cc"),
                                    "-o", str(path / "test")], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            run = subprocess.run([str(path / "test"), str(path)], capture_output=True,
                                 text=True, env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"))
            self.assertEqual(run.returncode, 0, run.stderr)
            native = json.loads((path / "oracle/perf-retirement-core0.json").read_text())
            self.assertEqual(native["schema"], "gem5-perf-retirement-v1")
            self.assertEqual(native["task_ids"], [100])
            self.assertEqual(native["scopes"]["physical"]["retired_macro_instructions"], 2)
            self.assertEqual(native["scopes"]["kernel"]["perf_retired_stores"], 1)
            self.assertEqual(native["scopes"]["workload_task"]["perf_retired_loads"], 1)
            self.assertEqual(native["owners"][0]["scopes"]["kernel"]["retired_uops"], 1)
            self.assertTrue(native["coverage"]["functional_records_match"])
            self.assertEqual(native["boundary_diagnostics"][0]["micro_pc"], 0x8000)
            self.assertEqual(native["boundary_diagnostics"][0]["disasm"], 'Microcode_ROM:\t"slli"\\t')
            bad = json.loads((path / "oracle/perf-retirement-core1.json").read_text())
            self.assertFalse(bad["coverage"]["functional_records_match"])
            self.assertFalse(bad["scopes"]["physical"]["available"])
            self.assertIsNone(bad["scopes"]["physical"]["perf_retired_stores"])
            owner_error = json.loads((path / "oracle/perf-retirement-core2.json").read_text())
            self.assertEqual(owner_error["coverage"]["read_failures"], 1)
            self.assertTrue(owner_error["scopes"]["physical"]["available"])
            self.assertTrue(owner_error["coverage"]["functional_records_match"])
            self.assertFalse(owner_error["scopes"]["workload_task"]["available"])
            self.assertFalse(owner_error["scopes"]["other_task"]["available"])
            for scope in ("physical", "user", "kernel"):
                counts = owner_error["owners"][0]["scopes"][scope]
                self.assertFalse(counts["available"], scope)
                self.assertIsNone(counts["perf_retired_stores"], scope)
                self.assertIsNone(counts["coverage"]["classified_loads"], scope)
            rom = json.loads((path / "oracle/perf-retirement-core3.json").read_text())
            self.assertTrue(rom["scopes"]["physical"]["available"])
            self.assertEqual(rom["coverage"]["observed_leading_incomplete_macros"], 0)
            self.assertEqual(rom["scopes"]["physical"]["coverage"]["rom_entry_macros"], 1)
            self.assertEqual(rom["scopes"]["physical"]["coverage"]["unclassified_loads"], 1)
            interrupted = json.loads((path / "oracle/perf-retirement-core4.json").read_text())
            self.assertTrue(interrupted["coverage"]["functional_records_match"])
            self.assertTrue(interrupted["coverage"]["functional_macros_match"])
            self.assertEqual(interrupted["coverage"]["observed_boundary_discontinuities"], 1)
            self.assertFalse(interrupted["scopes"]["physical"]["available"])
            missing_tail = json.loads((path / "oracle/perf-retirement-core5.json").read_text())
            self.assertTrue(missing_tail["coverage"]["functional_records_match"])
            self.assertTrue(missing_tail["coverage"]["functional_macros_match"])
            self.assertEqual(missing_tail["coverage"]["observed_leading_incomplete_macros"], 1)
            self.assertFalse(missing_tail["scopes"]["physical"]["available"])


if __name__ == "__main__":
    unittest.main()
