"""Run real SPR retired static-map observation and serialization methods."""
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from tests.test_spr_functional_retirement_capture import definition
from tests.test_asid_static_instruction_maps import fixture
from audit_fst_static_instruction_maps import audit_map

PREAMBLE = r'''
#include <algorithm>
#include <array>
#include <cassert>
#include <cstdio>
#include <cstdint>
#include <fstream>
#include <map>
#include <memory>
#include <set>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>
namespace X86ISA {struct PCState {
 uint64_t pc=4096,next=4098;uint8_t length=2;
 PCState(uint64_t p=4096):pc(p){} uint64_t instAddr()const{return pc;}
 uint8_t size()const{return length;}void size(uint8_t s){length=s;}void npc(uint64_t p){next=p;}
 template<class T>const T&as()const{return *this;}
};}
struct StaticInst {
 uint64_t target=12288;std::vector<uint32_t>src={0},dst={1};bool micro=false,first=true;
 bool isMicroop()const{return micro;}bool isFirstMicroop()const{return first;}
 size_t numSrcRegs()const{return src.size();}size_t numDestRegs()const{return dst.size();}
 uint32_t srcRegIdx(size_t i)const{return src.at(i);}uint32_t destRegIdx(size_t i)const{return dst.at(i);}
 bool isControl()const{return true;}bool isCondCtrl()const{return false;}
 bool isIndirectCtrl()const{return false;}bool isCall()const{return false;}
 bool isReturn()const{return false;}bool isLoad()const{return false;}
 bool isStore()const{return false;}bool isAtomic()const{return false;}
 bool isDirectCtrl()const{return true;}
 std::shared_ptr<X86ISA::PCState>branchTarget(const X86ISA::PCState&)const{
 auto p=std::make_shared<X86ISA::PCState>(target);return p;}
};
struct Inst{std::shared_ptr<StaticInst>staticInst=std::make_shared<StaticInst>();
 X86ISA::PCState state;uint32_t tid=0;const auto&pcState()const{return state;}};
using DynInstPtr=std::shared_ptr<Inst>;
bool fstCanonicalRegister(uint32_t id,uint32_t&out){out=id;return true;}
constexpr uint32_t kFstInstructionMapVersion=2,kFstInstructionOperandsComplete=2,kFstInstructionIsaX86_64=1;
constexpr uint8_t kFstInstructionOperandsValid=1;
'''

DRIVER = r'''
int main(int argc,char**argv){assert(argc==2);TaoTrace t;t.fst_path_=argv[1];
 t.functional_asid_static_map_=true;t.out_fst_=std::tmpfile();assert(t.out_fst_);
 auto i=std::make_shared<Inst>();
 t.noteFstAddressSpace(4096);t.observeFstStaticInstruction(i,true);++t.fst_record_count_;
 t.noteFstAddressSpace(8192);i->state.length=5;i->staticInst->target=16384;
 i->staticInst->src={2};i->staticInst->dst={3};
 t.observeFstStaticInstruction(i,true);++t.fst_record_count_;
 t.noteFstAddressSpace(4096);i->state.length=2;i->staticInst->target=12288;
 i->staticInst->src={0};i->staticInst->dst={1};
 t.observeFstStaticInstruction(i,true);++t.fst_record_count_;
 assert(t.fst_static_instructions_.size()==2);t.writeFstInstructionMap();
 std::fclose(t.out_fst_);
 // Same-key conflicts are invalidated, never overwritten or merged.
 TaoTrace c;c.functional_asid_static_map_=true;c.out_fst_=std::tmpfile();
 c.noteFstAddressSpace(4096);c.observeFstStaticInstruction(i,true);
 i->staticInst->src={5};c.observeFstStaticInstruction(i,true);
 assert(c.fst_static_instructions_.empty());
 c.fst_path_=std::string(argv[1])+".conflict";
 {std::ofstream stale(c.fst_path_+".imap");stale<<"stale";}
 bool rejected=false;try{c.writeFstInstructionMap();}catch(const std::runtime_error&){rejected=true;}
 assert(rejected);assert(!std::ifstream(c.fst_path_+".imap").good());
 std::fclose(c.out_fst_);
}
'''

class SprAsidMapProducerTest(unittest.TestCase):
    def test_actual_producer_preserves_two_asids_and_refuses_same_key_conflict(self):
        source=(ROOT/'gem5-spr/src/cpu/o3/probe/tao_trace.cc').read_text()
        header=(ROOT/'gem5-spr/src/cpu/o3/probe/tao_trace.hh').read_text()
        fields=header[header.index('    struct FstStaticInstruction {'):header.index('    // 把当前 records.micro')]
        structs=source[source.index('struct FstInstructionMapHeaderV2 {'):source.index('// Map gem5\'s x86 renameable')]
        flags=definition(source,'enum FstStaticFlag')+';'
        methods='\n'.join('void\n'+definition(source,'TaoTrace::'+name) for name in [
            'noteFstAddressSpace(', 'observeFstStaticInstruction(', 'writeFstInstructionMap('])
        code=PREAMBLE+flags+structs+r'''
struct TaoTrace {
 bool functional_asid_static_map_=false,functional_include_kernel_=true,fst_finalized_=false;
 std::FILE*out_fst_=nullptr;std::string fst_path_;uint64_t fst_record_count_=0;int32_t fst_core_id_=0;
 struct FstAddressSpaceTransition{uint64_t record_ordinal=0,address_space_id=0;};
 std::vector<FstAddressSpaceTransition>fst_address_space_transitions_;
'''+fields+r'''
 uint32_t getTraceThreadId(const DynInstPtr&i)const{return i->tid;}
 void noteFstAddressSpace(uint64_t);
 void observeFstStaticInstruction(const DynInstPtr&,bool);
 void writeFstInstructionMap();
};
'''+methods+DRIVER
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            directory=Path(directory);cpp=directory/'producer.cpp';cpp.write_text(code)
            binary=directory/'producer'
            subprocess.run(['g++','-std=c++17','-static-libstdc++',str(cpp),'-o',str(binary)],check=True)
            fst=fixture(directory)
            result=subprocess.run([str(binary),str(fst)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            report=audit_map(fst)
            self.assertEqual(report['version'],4)
            self.assertEqual(report['instruction_rows'],2)
            self.assertFalse(report['map_complete'])
            self.assertTrue(report['operands_complete'])

if __name__=='__main__':unittest.main()
