"""SPR DSB validation is a real native frontend terminal, never FST input."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
from merge_kernel_events_oracle_v3 import merge
from test_p0_contract import row
from test_scoped_frontend_ledger import frontend
from validate_kernel_events_oracle import validate_frontend_accounting


def native_frontend_row():
    """Observed C4 core0 terminal population; all other primitive gates conserve."""
    result = row()
    result["frontend_accounting"] = f = frontend()
    f.update(inflight_at_start=0, requests_started=1054,
             user_mode_requests_started=106, kernel_mode_requests_started=948,
             invalid_same_block_refetches=0, invalid_new_block_requests=0,
             valid_block_changes=1054, translations_completed=1054,
             icache_send_attempts=725, icache_requests_sent=725, icache_send_rejects=0,
             translation_squashes=2, translation_faults=8, icache_responses=709,
             icache_squashed_responses=16, inflight_at_end=0,
             dsb_validation_terminals=319)
    return result


class SprFrontendTerminalCaptureTests(unittest.TestCase):
    def test_production_registry_and_serializer_include_actual_dsb_terminal(self):
        header = (ROOT / "gem5-spr/src/mem/taotrace_response.hh").read_text()
        start = header.index("class TaoTraceFrontendRegistry")
        registry = header[start:header.index("\n} // namespace gem5", start)]
        producer = (ROOT / "gem5-spr/src/cpu/o3/probe/tao_trace.cc").read_text()
        start = producer.index("    const auto frontend = TaoTraceFrontendRegistry::snapshot(")
        serializer = producer[start:producer.index('    std::fprintf(events_file, ",\\\"event_counts', start)]
        harness = r'''
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <unordered_map>
struct Request { bool hasContextId()const{return true;} uint32_t contextId()const{return 0;} };
using RequestPtr=std::shared_ptr<Request>;
''' + registry + r'''
void serialize(std::FILE* events_file){
 int32_t cpl_core_id_=0;uint64_t period=1;
''' + serializer + r'''
}
int main(){
 using R=TaoTraceFrontendRegistry;R::prepareContext(0);R::enableContext(0);
 for(unsigned i=0;i<1054;++i){
  auto request=std::make_shared<Request>();R::noteRequestStart(request,i<106,true,false,10);
  R::noteTranslationComplete(request);
  const auto terminal=i<319 ? R::Terminal::DsbValidation :
   i<1028 ? R::Terminal::Response : i<1044 ? R::Terminal::SquashedResponse :
   i<1046 ? R::Terminal::TranslationSquash : R::Terminal::TranslationFault;
  if(terminal==R::Terminal::Response||terminal==R::Terminal::SquashedResponse)
   R::noteSendAttempt(request,true);
  R::noteTerminal(request,terminal,20);
 }
 for(unsigned i=0;i<10;++i)R::noteStatusCycle(0,0);
 R::disableContext(0);
 std::fputs("{\"fixture\":true",stdout);serialize(stdout);std::fputs("}\n",stdout);
}
'''
        with tempfile.TemporaryDirectory(prefix="spr-frontend-terminal-", dir=ROOT / "tmp") as directory:
            p = Path(directory)
            (p / "test.cc").write_text(harness)
            built = subprocess.run(["/opt/gcc-11/bin/g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                                    str(p / "test.cc"), "-o", str(p / "test")], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            result = subprocess.run([str(p / "test")], capture_output=True, text=True,
                                    env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"))
            self.assertEqual(result.returncode, 0, result.stderr)
        captured = json.loads(result.stdout)["frontend_accounting"]
        self.assertTrue(captured["request_population_conserved"])
        self.assertEqual(captured.get("dsb_validation_terminals"), 319)
        self.assertEqual(captured["requests_started"], 1054)
        value = row(); value["frontend_accounting"] = captured
        validate_frontend_accounting(value, "serialized")

    def test_native_terminal_population_passes_only_with_real_missing_counter(self):
        value = native_frontend_row()
        validate_frontend_accounting(value, "native-fixture")
        merged = merge([value])
        self.assertEqual(merged["aggregate"]["frontend_accounting"]["dsb_validation_terminals"], 319)
        for tamper in (None, 0, 318, 320):
            with self.subTest(tamper=tamper):
                broken = copy.deepcopy(value)
                if tamper is None:
                    del broken["frontend_accounting"]["dsb_validation_terminals"]
                else:
                    broken["frontend_accounting"]["dsb_validation_terminals"] = tamper
                with self.assertRaisesRegex(ValueError, "request_population"):
                    validate_frontend_accounting(broken, "tampered")

    def test_legacy_absence_means_zero_and_malformed_optional_counter_rejects(self):
        value = row();value["frontend_accounting"] = frontend()
        validate_frontend_accounting(value, "legacy")
        value["frontend_accounting"]["dsb_validation_terminals"] = 0
        validate_frontend_accounting(value, "explicit-zero")
        for invalid in (-1, True, None, 1.5, "319"):
            with self.subTest(invalid=invalid):
                value["frontend_accounting"]["dsb_validation_terminals"] = invalid
                with self.assertRaisesRegex(ValueError, "dsb_validation_terminals"):
                    validate_frontend_accounting(value, "malformed")


if __name__ == "__main__":
    unittest.main()
