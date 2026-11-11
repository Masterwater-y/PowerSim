"""Execute the SPR production Request extension and lifecycle registry."""
import os
import re
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
 bool hasPaddr()const{return true;}
 uint64_t getPaddr()const{return 0x1000;}
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
        (p / "projection.hh").write_text('#include <type_traits>\n#include "cpu/o3/probe/perf_retirement.hh"\nnamespace retirement=gem5::o3::retirement;\n'+projection)
        sequencer=(ROOT / 'gem5-spr/src/mem/ruby/system/Sequencer.cc').read_text()
        marker='    const auto spr_previous_attempt'
        start=sequencer.index(marker) if marker in sequencer else sequencer.index('    msg->m_perfTransactionId = perf_cache_native::observer().issue(')
        end=sequencer.index('    m_mandatory_q_ptr->enqueue(',start)
        issue=sequencer[start:end]
        (p / 'issue_fixture.hh').write_text('''#include "mem/perf_cache_native.hh"
struct FixturePacket { RequestPtr req; };
struct FixtureMessage { uint64_t m_perfTransactionId=0; };
struct IssueFixture { int m_version=0; uint64_t issue(FixturePacket *pkt) {
    FixtureMessage value; auto *msg=&value; auto kind=perf_cache_native::Kind::DataRead;
'''+issue+'''    return msg->m_perfTransactionId;
}};
''')
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
        for case in ("preboundary", "empty", "crossboundary", "late_store", "split", "mixed", "ram", "identity", "terminal", "clone", "projection", "forward_after_issue", "prefix_import", "split_ram", "cleanup", "prefetch", "late_prefetch_oracle", "prefetch_parent_witness", "prefetch_parent_attempt_guard", "prefetch_parent_issue_before_begin", "prefetch_parent_extension_fails_closed", "bad_import", "hooks", "late_accept", "mixed_reason",
                     "partial_squash_before_response", "partial_response_before_squash",
                     "partial_mixed_squash", "partial_outstanding",
                     "retained_open", "partial_ruby_squash", "partial_late_hierarchy", "partial_disabled_squash",
                     "partial_aliased_reissue", "abandoned_recycling",
                     "membership_attempts", "membership_coalesced", "membership_overflow",
                     "spr_v2_live_service", "spr_v2_extension_projection", "spr_v2_reissue_recycles", "spr_v2_replaced_live_service", "spr_v2_alias_diagnostic", "spr_prepare_service", "spr_rmw_alias", "spr_prepare_rebind", "spr_prepare_leader_origin"):
            with self.subTest(case=case):
                run = subprocess.run([str(self.exe), case], capture_output=True, text=True, env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"))
                self.assertEqual(run.returncode, 0, run.stderr)

    def test_slicc_hit_completion_uses_precompletion_observation(self):
        source=(ROOT / 'gem5-spr/src/mem/ruby/protocol/MESI_Three_Level-L0cache.sm').read_text()
        transitions=re.findall(r'transition\(([^\n]*)\)\s*\{([^}]*)\}', source)
        hits=[(name, body) for name,body in transitions
              if ('h_load_hit;' in body or 'hh_store_hit;' in body) and 'uu_profileDataHit;' in body]
        self.assertGreaterEqual(len(hits), 6)
        mapping={'spr_observeDataHit':'o','h_load_hit':'h','hh_store_hit':'h','uu_profileDataHit':'p'}
        for name,body in hits:
            actions=''.join(mapping[a] for a in re.findall(r'\b(\w+)\s*;',body) if a in mapping)
            with self.subTest(transition=name):
                run=subprocess.run([str(self.exe),'spr_v2_slicc_hit_order',actions],
                    capture_output=True,text=True,env=dict(os.environ,LD_LIBRARY_PATH='/opt/gcc-11/lib64'))
                self.assertEqual(run.returncode,0,run.stderr)

    def test_skipped_software_prefetch_does_not_invalidate_retired_hint(self):
        source=(ROOT/'gem5-spr/src/cpu/o3/probe/tao_trace.cc').read_text()
        start=source.index('    member.unsupported =')
        expression=source[start:source.index(';',start)+1]
        code='''#include <cassert>
#include <initializer_list>
#include <string>
bool containsAny(const std::string &,std::initializer_list<const char *>) { return false; }
struct Inst { bool p=true,m=false; bool readPredicate() const {return p;} bool readMemAccPredicate() const{return m;} };
struct Member {bool unsupported=false,software_prefetch=false;};
bool evaluate(bool hint,bool p,bool m) { Member member;member.software_prefetch=hint;
    Inst value;value.p=p;value.m=m;auto *inst=&value;std::string name,disasm;
'''+expression+'''\nreturn member.unsupported; }
int main() { assert(!evaluate(true,true,false)); assert(evaluate(false,true,false));
  assert(evaluate(true,false,true)); }
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            p=Path(directory); (p/'test.cpp').write_text(code)
            built=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17',str(p/'test.cpp'),'-o',str(p/'test')],capture_output=True,text=True)
            self.assertEqual(built.returncode,0,built.stderr)
            run=subprocess.run([str(p/'test')],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)

    def test_slicc_prepare_data_reaches_coalesced_read(self):
        source=(ROOT/'gem5-spr/src/mem/ruby/protocol/MESI_Three_Level-L0cache.sm').read_text()
        transitions=dict(re.findall(r'transition\(([^\n]*)\)\s*\{([^}]*)\}',source))
        blocks={name:source[source.index('  action('+name+','):source.index('  action(',source.index('  action('+name+',')+9)]
                for name in ('spr_beginResidentPrepare','spr_beginFillPrepare','u_writeDataToCache','s_deallocateTBE')}
        mapping={'hhx_store_hit':'h','fi_sendInvAck':'i'}
        if 'sprPrepareBegin(version, address, false)' in blocks['spr_beginResidentPrepare']:
            mapping['spr_beginResidentPrepare']='b'
        if 'sprPrepareBegin(version, address, true)' in blocks['spr_beginFillPrepare']:
            mapping['spr_beginFillPrepare']='f'
        if 'sprPrepareData(tbe.SprPrepareGeneration)' in blocks['u_writeDataToCache']:
            mapping['u_writeDataToCache']='d'
        if 'sprPrepareClose(tbe.SprPrepareGeneration)' in blocks['s_deallocateTBE']:
            mapping['s_deallocateTBE']='c'
        def actions(body):
            return ''.join(mapping[a] for a in re.findall(r'\b(\w+)\s*;',body) if a in mapping)
        for kind,entry in [('resident','S, Store_Prepare, SP_IE'),('fill','I, Store_Prepare, SP_IE')]:
            for response in ['SP_IE, Data_Exclusive, E','{IM,SM}, Data_Exclusive, M']:
                inv=next(v for k,v in transitions.items() if 'SP_IE' in k and '{InvOwn, InvElse}' in k)
                for prior in ['', actions(inv)]:
                    with self.subTest(kind=kind,response=response,invalidation=bool(prior)):
                        run=subprocess.run([str(self.exe),'spr_prepare_slicc_order',kind,
                            actions(transitions[entry]),prior+actions(transitions[response])],
                            capture_output=True,text=True,env=dict(os.environ,LD_LIBRARY_PATH='/opt/gcc-11/lib64'))
                        self.assertEqual(run.returncode,0,run.stderr)
        leader=transitions['SP_IE, Store, IM']
        self.assertLess(leader.index('spr_recordPrepareLeader;'),
                        leader.index('ud_profileDataMergedMiss;'))
        start=source.index('  action(spr_recordPrepareLeader,')
        end=source.index('  action(',start+9)
        self.assertIn('sprPrepareLeader(in_msg.perfTransactionId(), tbe.SprPrepareGeneration)',
                      source[start:end])

    def test_coalesced_read_modify_write_enters_data_observer(self):
        source=(ROOT/'gem5-spr/src/mem/ruby/protocol/MESI_Three_Level-L0cache.sm').read_text()
        start=source.index('  void notifyCoalescedAdmission(')
        begin=source.index('if (',start)+len('if (')
        end=source.index(') {',begin)
        condition=source[begin:end].replace('RubyRequestType:', 'RubyRequestType::')
        code='''#include <cassert>
enum class RubyRequestType {LD,Load_Linked,RMW_Read,Locked_RMW_Read,ST,IFETCH,Store_Conditional};
bool observed(RubyRequestType type){ return '''+condition+'''; }
int main(){ assert(observed(RubyRequestType::LD)); assert(observed(RubyRequestType::Load_Linked));
assert(observed(RubyRequestType::RMW_Read)); assert(observed(RubyRequestType::Locked_RMW_Read));
assert(!observed(RubyRequestType::ST)); assert(!observed(RubyRequestType::IFETCH));
assert(!observed(RubyRequestType::Store_Conditional)); }
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            p=Path(directory);(p/'test.cpp').write_text(code)
            built=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17',str(p/'test.cpp'),'-o',str(p/'test')],capture_output=True,text=True)
            self.assertEqual(built.returncode,0,built.stderr)
            run=subprocess.run([str(p/'test')],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)

    def test_slicc_data_witness_only_after_real_data_write(self):
        source=(ROOT/'gem5-spr/src/mem/ruby/protocol/MESI_Three_Level-L0cache.sm').read_text()
        transitions=dict(re.findall(r'transition\(([^\n]*)\)\s*\{([^}]*)\}',source))
        data=transitions['{IM,SM}, Data_Exclusive, M']
        self.assertLess(data.index('u_writeDataToCache;'),data.index('hhx_store_hit;'))
        start=source.index('  action(hhx_store_hit,')
        end=source.index('  action(',start+9)
        self.assertIn('sequencer.writeCallbackData(address, cache_entry.DataBlk)',source[start:end])
        start=source.index('  action(hh_store_hit,')
        end=source.index('  action(',start+9)
        self.assertNotIn('writeCallbackData',source[start:end])

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
