"""Execute the SPR production Request extension and lifecycle registry."""
import os
import json
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT / "tests"))
from native_pio_serializers import serializers
REQUEST = r'''
#pragma once
#include <cstdint>
#include <memory>
#include "base/extensible.hh"
namespace gem5 {
struct Request : Extensible<Request> {
 uint32_t context=1; uint64_t sequence=49188;
 bool contextValid=true, sequenceValid=true;
 bool hasContextId()const{return contextValid;}
 bool hasInstSeqNum()const{return sequenceValid;}
 uint32_t contextId()const{return context;}
 uint64_t getReqInstSeqNum()const{return sequence;}
};
using RequestPtr=std::shared_ptr<Request>;
}
'''

class NativePioLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="native-pio-registry-", dir=ROOT / "tmp")
        p = Path(cls.temp.name)
        (p / "mem").mkdir(); (p / "sim").mkdir()
        (p / "mem/request.hh").write_text(REQUEST)
        (p / "sim/cur_tick.hh").write_text('#pragma once\n#include <cstdint>\nnamespace gem5 { inline uint64_t fixtureTick=100; inline uint64_t curTick(){return fixtureTick;} }\n')
        producer=(ROOT / "gem5-spr/src/cpu/o3/probe/tao_trace.cc").read_text()
        start=producer.index("template<class Native>\nstatic retirement::NativeFacts")
        projection=producer[start:producer.index("\nvoid\nTaoTrace::resolvePerfRetirementNative",start)]
        (p / "projection.hh").write_text('#include "cpu/o3/probe/perf_retirement.hh"\nnamespace retirement=gem5::o3::retirement;\n'+projection)
        lsq=(ROOT / "gem5-spr/src/cpu/o3/lsq.hh").read_text()
        start=lsq.index("        void\n        noteNativeTerminal(")
        terminal=lsq[start:lsq.index("\n        /**",start)]
        (p / "lsq_terminal.hh").write_text('#include <vector>\nstruct TerminalRequest {std::vector<RequestPtr> _reqs; RequestPtr mainReq(){return _reqs.at(0);}\n'+terminal+'\n};\n')
        ruby=(ROOT / "gem5-spr/src/mem/ruby/system/RubyPort.cc").read_text()
        start=ruby.index("            assert(owner.memRequestPort.isConnected());")
        acceptance=ruby[start:ruby.index("            return true;",start)+len("            return true;")]
        start=ruby.index("bool RubyPort::MemRequestPort::recvTimingResp(PacketPtr pkt)")
        response=ruby[ruby.index("{",start):ruby.index("\nbool\nRubyPort::PioResponsePort",start)]
        hooks=(ROOT / "tests/native_pio_hook_stubs.inc").read_text()
        hooks+='struct AcceptFixture : RubyPort::MemResponsePort {using SenderState=RubyPort::SenderState; HookOwner owner; bool route(PacketPtr pkt){'+acceptance+'}};\n'
        hooks+='struct ResponseFixture {using MemResponsePort=RubyPort::MemResponsePort; HookOwner owner; bool receive(PacketPtr pkt)'+response+'};\n'
        (p / "hooks.hh").write_text(hooks)
        (p / "serializers.hh").write_text(serializers(ROOT,producer))
        cls.exe = p / "fixture"
        built = subprocess.run(["/opt/gcc-11/bin/g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I"+str(p), "-I"+str(ROOT / "gem5-spr/src"), str(ROOT / "tests/test_native_pio_lifecycle.cpp"), "-o", str(cls.exe)], capture_output=True, text=True)
        if built.returncode:
            raise RuntimeError(built.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_production_lifecycle(self):
        for case in ("preboundary", "empty", "crossboundary", "late_store", "split", "mixed", "ram", "identity", "terminal", "clone", "projection", "forward_after_issue", "prefix_import", "split_ram", "cleanup", "prefetch", "bad_import", "hooks", "late_accept", "mixed_reason",
                     "partial_squash_before_response", "partial_response_before_squash",
                     "partial_mixed_squash", "partial_outstanding",
                     "retained_open", "partial_ruby_squash", "partial_late_hierarchy", "partial_disabled_squash",
                     "partial_aliased_reissue", "abandoned_recycling"):
            with self.subTest(case=case):
                run = subprocess.run([str(self.exe), case], capture_output=True, text=True, env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"))
                self.assertEqual(run.returncode, 0, run.stderr)

    def test_production_serializers(self):
        run=subprocess.run([str(self.exe),"serialize"],capture_output=True,text=True,
                           env=dict(os.environ,LD_LIBRARY_PATH="/opt/gcc-11/lib64"))
        self.assertEqual(run.returncode,0,run.stderr)
        commit,resolution,population,summary=map(json.loads,run.stdout.splitlines())
        for row in (commit,resolution):
            self.assertEqual(row["native_bypass_accepted"],1)
            self.assertEqual(row["native_bypass_responses"],1)
            self.assertEqual(row["native_bypass_protocol_errors"],0)
            self.assertEqual(row["native_admission_count"],0)
            self.assertEqual(row["native_response_count"],0)
            self.assertEqual(row["native_terminal_reason_mask"],512)
        self.assertEqual(summary["source_sideband_schema"],"taotrace-native-response-v8")
        self.assertEqual(summary["native_bypass_accepted_fragments"],1)
        self.assertEqual(summary["native_bypass_response_fragments"],1)
        self.assertEqual(summary["native_device_bypass_uops"],1)
        self.assertEqual(summary["native_mixed_bypass_uops"],0)
        self.assertEqual(summary["native_admission_fragments"],0)
        self.assertEqual(population["bypass_accepted_fragments"],1)
        self.assertEqual(population["bypass_response_fragments"],1)
        self.assertEqual(population["device_bypass_uops"],1)
        self.assertEqual(population["mixed_bypass_uops"],0)
        self.assertEqual(population["hierarchy"]["l1d"]["accesses"],0)

if __name__ == "__main__":
    unittest.main()
