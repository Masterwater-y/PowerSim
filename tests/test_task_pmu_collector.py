"""Source-level regression guards for native task PMU producer plumbing.

Actual O3/Ruby behavior is additionally validated by the bounded FS capture.
These guards cover the easy-to-regress join and ownership entry points.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
CC = ROOT / "gem5-fs/src/cpu/o3/probe/tao_trace.cc"
HH = ROOT / "gem5-fs/src/cpu/o3/probe/tao_trace.hh"


class TaskPmuCollectorTests(unittest.TestCase):
    def test_independent_common_start_accounting_precedes_legacy_cpl(self):
        commit = CC.read_text().split("TaoTrace::onCommit(", 1)[1]
        self.assertLess(commit.index("accountTaskPmuCommit(inst)"),
                        commit.index("accountCplCommit(inst, syscall_boundary)"))

    def test_prefix_native_is_captured_without_first_user(self):
        source = CC.read_text()
        self.assertTrue("TaoTrace::accountTaskPmuCommit(" in source)
        body = source.split("TaoTrace::accountTaskPmuCommit(", 1)[1].split(
            "\nvoid\n", 1)[0]
        self.assertIn("!cpl_initialized_ && !isUserInstruction(inst)", body)
        self.assertIn("writeNativeResponseCommit", body)
        self.assertIn("TaoTraceNativeTerminalReason::PredicatedOff", body)
        self.assertIn("TaoTraceNativeTerminalReason::NoRequest", body)

    def test_pending_native_freezes_owner_and_legacy_population(self):
        header = HH.read_text().split("struct PendingNativeResponseDiag", 1)[1].split("};", 1)[0]
        self.assertIn("owner_task_id", header)
        self.assertIn("legacy_included", header)
        source = CC.read_text().split("TaoTrace::accountNativeResolved(", 1)[1].split("\nstatic bool", 1)[0]
        self.assertIn("pending.owner_task_id", source)
        self.assertIn("pending.legacy_included", source)

    def test_task_sidecar_exposes_population_and_ordinal_proof(self):
        source = CC.read_text()
        for token in ("fastsim-task-pmu-v1", "measurement_begin_ordinal",
                      "measurement_end_ordinal_exclusive", "owner_unknown",
                      "population_contract", "pending_native", "task-pmu-core"):
            self.assertTrue(token in source, token)

    def test_native_commit_not_repeated_for_initialized_legacy_commits(self):
        source = CC.read_text()
        body = source.split("TaoTrace::accountTaskPmuCommit(", 1)[1].split("\nvoid\n", 1)[0]
        self.assertLess(body.index("if (!legacy_skips_commit) return;"),
                        body.index("writeNativeResponseCommit"))


class TaskNativeReducerMechanismTests(unittest.TestCase):
    def test_actual_reducer_keeps_frozen_owner_and_legacy_isolation(self):
        """Compile the production reducer against a minimal registry fixture.

        This executes the actual accountNativeResolved body, rather than a
        second Python implementation of its selection and conservation rules.
        """
        import os
        import shutil
        import subprocess
        import tempfile
        compiler = shutil.which("g++")
        if not compiler:
            self.skipTest("C++ compiler unavailable")
        source = CC.read_text()
        reducer = source.split("void\nTaoTrace::accountNativeResolved(", 1)[1]
        reducer = "void\nTaoTrace::accountNativeResolved(" + reducer.split("\nstatic bool", 1)[0]
        preamble = r'''
#include <array>
#include <cassert>
#include <cstdint>
#include <cstddef>
#include <map>
struct Facts { std::array<uint64_t, 3> accesses{}; };
struct TaoTraceNativeAccessRegistry {
  struct Snapshot {
    uint64_t admissions=0, aliasedAdmissions=0, hierarchyRequests=0;
    uint64_t responses=0, coalesced=0, externalHits=0, externalMisses=0;
    uint64_t responderMachineUnknown=0, terminalReasonMask=0;
    Facts hierarchy;
  };
};
struct TaoTrace {
  struct PendingNativeResponseDiag {
    uint64_t owner_task_id=0, line_requests=0;
    unsigned cycle_class=0, proxy_path_class=0;
    bool legacy_included=true;
  };
  struct NativeScopeAggregate {
    uint64_t memory_uops=0, architectural_line_requests=0;
    uint64_t ruby_admission_fragments=0, ruby_aliased_admission_fragments=0;
    uint64_t ruby_hierarchy_request_fragments=0, ruby_response_fragments=0;
    uint64_t sequencer_coalesced_fragments=0, no_ruby_uops=0;
    uint64_t native_outcome_uops=0, native_external_hit_fragments=0;
    uint64_t native_external_miss_fragments=0, native_responder_machine_unknown=0;
    uint64_t hierarchy_complete_uops=0, hierarchy_incomplete_uops=0;
    struct Hierarchy {
      std::array<uint64_t,3> accesses{};
      void merge(const Facts &facts) {
        for (unsigned i=0;i<3;++i) accesses[i] += facts.accesses[i];
      }
    } hierarchy;
  };
  std::map<uint64_t, NativeScopeAggregate> task_native_by_owner_;
  std::array<NativeScopeAggregate,7> native_diag_by_scope_{};
  std::array<std::array<uint64_t,2>,5> native_diag_proxy_confusion_{};
  std::array<uint64_t,7> native_diag_terminal_reason_counts_{};
  uint64_t native_diag_response_without_fact_uops_=0, anomalies=0;
  void accountNativeResolved(const PendingNativeResponseDiag &,
                            const TaoTraceNativeAccessRegistry::Snapshot &);
  void recordNativeAnomaly(const char *, const PendingNativeResponseDiag &,
                          const TaoTraceNativeAccessRegistry::Snapshot &) { ++anomalies; }
};
'''
        driver = r'''
int main() {
  TaoTrace trace;
  TaoTrace::PendingNativeResponseDiag prefix;
  prefix.owner_task_id=10; prefix.legacy_included=false;
  prefix.line_requests=2; prefix.cycle_class=6;
  TaoTraceNativeAccessRegistry::Snapshot native;
  native.responses=2; native.externalHits=1; native.externalMisses=1;
  native.admissions=2; native.aliasedAdmissions=1;
  native.hierarchyRequests=2; native.hierarchy.accesses[0]=2;
  trace.accountNativeResolved(prefix,native);
  assert(trace.task_native_by_owner_[10].memory_uops==1);
  assert(trace.task_native_by_owner_[10].ruby_response_fragments==2);
  assert(trace.native_diag_by_scope_[6].memory_uops==0);
  assert(trace.native_diag_proxy_confusion_[0][0]==0);
  // A delayed request retains the original task even though another owner
  // is running when the captured pending identity resolves.
  TaoTrace::PendingNativeResponseDiag pending;
  pending.owner_task_id=20; pending.line_requests=2; pending.cycle_class=3;
  const uint64_t current_owner=30;
  trace.accountNativeResolved(pending,native);
  assert(trace.task_native_by_owner_[20].memory_uops==1);
  assert(trace.task_native_by_owner_.count(current_owner)==0);
  assert(trace.native_diag_by_scope_[3].memory_uops==1);
  assert(trace.native_diag_by_scope_[3].hierarchy.accesses[0]==2);
  assert(trace.native_diag_proxy_confusion_[0][0]==1);
  assert(trace.native_diag_proxy_confusion_[0][1]==1);
  assert(trace.task_native_by_owner_[10].memory_uops+
         trace.task_native_by_owner_[20].memory_uops==2);
  // Prefix anomalies stay outside legacy anomaly totals, but incomplete
  // native coverage remains visible in its per-owner evidence population.
  native.hierarchy.accesses[0]=1;
  trace.accountNativeResolved(prefix,native);
  assert(trace.task_native_by_owner_[10].hierarchy_incomplete_uops==1);
  assert(trace.anomalies==0);
  trace.accountNativeResolved(pending,native);
  assert(trace.anomalies==1);
}
'''
        with tempfile.TemporaryDirectory(prefix="task-pmu-native-reducer-") as temp:
            path = Path(temp)
            (path / "test.cc").write_text(preamble + reducer + driver)
            built = subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            str(path / "test.cc"), "-o", str(path / "test")],
                           capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            subprocess.run([str(path / "test")], check=True,
                           capture_output=True, text=True, env=dict(os.environ))


if __name__ == "__main__":
    unittest.main()
