"""Execute SPR's production functional-origin capture and wire serializer.

The harness substitutes committed DynInst inputs, not retirement policy. It
executes the producer's classifier, validation and companion-writing methods;
the maintained Python reader independently checks the resulting hot population.
"""
from pathlib import Path
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "gem5-spr/src/cpu/o3/probe"
sys.path.insert(0, str(ROOT / "tools"))
from fst_retirement_eligibility import load_retirement_eligibility


def definition(source, signature):
    """Read a complete production definition without changing its body."""
    start = source.find(signature)
    if start < 0:
        raise AssertionError("SPR functional retirement capture missing: " + signature)
    brace = source.index("{", start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


PREAMBLE = r'''
#include <cassert>
#include <cctype>
#include <cstdio>
#include <cstdint>
#include <fstream>
#include <memory>
#include <stdexcept>
#include <string>
#include <typeinfo>
#include <vector>
#include "cpu/o3/probe/perf_retirement.hh"
#include "cpu/o3/probe/fst_dependencies.hh"
#include "cpu/o3/probe/fst_memory_requests.hh"
#include "cpu/o3/probe/fst_retirement.hh"
using namespace gem5::o3;
constexpr uint64_t MicroPCRomBit=0x8000;
bool isRomMicroPC(uint64_t pc) { return bool(pc&MicroPCRomBit); }
namespace X86ISAInst { namespace rom_labels {
constexpr unsigned extern_label_longModeInterrupt=0,extern_label_longModeSoftInterrupt=61,
 extern_label_longModeInterruptWithError=122,extern_label_legacyModeInterrupt=183,
 extern_label_initIntHalt=244,extern_label_realModeInterrupt=305;
} }
struct StaticInst {
 bool micro=false,first=true,last=true; std::string mnemonic="nop";
 std::vector<std::shared_ptr<StaticInst>> members;
 virtual ~StaticInst()=default;
 bool isMicroop()const{return micro;} bool isFirstMicroop()const{return first;}
 bool isLastMicroop()const{return last;} std::string getName()const{return mnemonic;}
 std::shared_ptr<StaticInst> fetchMicroop(uint64_t i)const{return members.at(i);}
};
namespace X86ISA { struct MicroDebug:StaticInst{}; }
struct PC { uint64_t pc=4096,upc=0; uint64_t instAddr()const{return pc;}
 uint64_t microPC()const{return upc;} };
struct Inst { std::shared_ptr<StaticInst> staticInst,macroop; PC state;
 uint64_t seqNum=0;uint32_t context=0;bool user=true;
 uint64_t memReqFlags=0;bool isDataPrefetch()const{return false;}
 const PC& pcState()const{return state;} };
namespace Request { constexpr uint64_t NO_ACCESS=1; }
using DynInstPtr=std::shared_ptr<Inst>;
struct TaoTraceParams {
 bool functional_retirement=false,functional_include_kernel=true;
 bool functional_user_only=false,emit_micro=true,functional_syscall_microops=true;
 bool measure_cpl=true; uint64_t marker_pc=0; std::string trace_format="fst";
};
std::string toLower(std::string s){for(auto&c:s)c=std::tolower((unsigned char)c);return s;}
struct TaoTrace {
 bool functional_retirement_=true;
 retirement::Ledger functional_retirement_ledger_,perf_retirement_;
 std::FILE *out_fst_retirement_=nullptr,*out_fst_memory_requests_=nullptr;
 uint64_t fst_memory_request_hash_=fastsim::fst::kRequestStreamHashSeed;
 uint64_t fst_record_count_=0,fst_retirement_rows_=0,fst_memory_request_rows_=0;
 int32_t fst_core_id_=0;
 uint32_t getTraceThreadId(const DynInstPtr&i)const{return i->context;}
 bool isUserInstruction(const DynInstPtr&i)const{return i->user;}
 void validateFunctionalRetirement(const TaoTraceParams&)const;
 void accountFstRetirementCommit(const DynInstPtr&);
 void noteFstMemoryRequest(const DynInstPtr&,const void*,bool,uint32_t,bool=false,bool=false,bool=false);
 void writeFstMemoryRequestHeader(bool);
 void exerciseProductionWriterCallsites(const DynInstPtr&);
};
'''

DRIVER = r'''
int main(int argc,char**argv){
 assert(argc==2); std::string dir=argv[1];
 TaoTrace t; TaoTraceParams params;
 t.validateFunctionalRetirement(params);
 auto rejects=[&](const TaoTraceParams&p){
  try{t.validateFunctionalRetirement(p);}catch(const std::runtime_error&){return true;}
  return false;
 };
 params.functional_retirement=true;t.validateFunctionalRetirement(params);
 for(unsigned i=0;i<7;++i){auto p=params;
  if(i==0)p.functional_include_kernel=false;if(i==1)p.functional_user_only=true;
  if(i==2)p.emit_micro=false;if(i==3)p.functional_syscall_microops=false;
  if(i==4)p.measure_cpl=false;if(i==5)p.trace_format="jsonl";if(i==6)p.marker_pc=1;
  assert(rejects(p));p.functional_retirement=false;t.validateFunctionalRetirement(p);
 }
 for(int i=0;i<4;++i)assert(parseFstCoreIdFromName("board.processor.switch"+std::to_string(i)+".core.tao_trace")==i);
 assert(parseFstCoreIdFromName("board.processor.cores3.core.tao_trace")==3);
 assert(parseFstCoreIdFromName("board.processor.switch42_other.core.tao_trace")==-1);
 t.out_fst_retirement_=std::fopen((dir+"/core0.fst.fsem").c_str(),"w+b");
 t.out_fst_memory_requests_=std::fopen((dir+"/core0.fst.mreq").c_str(),"w+b");
 assert(t.out_fst_retirement_&&t.out_fst_memory_requests_);
 t.writeFstMemoryRequestHeader(false);
 std::ofstream hot(dir+"/hot.bin",std::ios::binary);
 auto inst=std::make_shared<Inst>(); inst->staticInst=std::make_shared<StaticInst>();
 auto emit=[&](bool micro,bool first,bool last,uint64_t upc,bool user){
  auto si=inst->staticInst;si->micro=micro;si->first=first;si->last=last;
  inst->state.upc=upc;inst->user=user;++inst->seqNum;
  FstRecord r;r.pc=inst->state.pc;r.flags=1|(micro?1024:0)|(last&&micro?2048:0);
  r.op_class=user?0:-2;
  t.noteFstMemoryRequest(inst,&r,false,uint32_t(upc));
  hot.write(reinterpret_cast<const char*>(&r),sizeof(r));++t.fst_record_count_;
 };
 // An ordinary record is part of the population hash, despite having no row.
 emit(false,true,true,0,true);
 // A real incomplete prefix and independently accepted matching precise fault.
 emit(true,true,false,0,true);
 assert(!t.functional_retirement_ledger_.notePreciseFault(0,4097));
 assert(t.functional_retirement_ledger_.notePreciseFault(0,4096));
 emit(true,false,false,MicroPCRomBit|122,false);
 emit(true,false,true,MicroPCRomBit|123,false);
 // The exceptional state cannot leak to another thread or the next macro.
 inst->context=1;emit(false,true,true,0,true);
 inst->context=0;emit(false,true,true,0,true);
 // ROM reached through a decoded macro is not external hardware entry.
 inst->macroop=std::make_shared<StaticInst>();
 emit(true,true,true,MicroPCRomBit|61,false);inst->macroop.reset();
 // A genuine hardware entry without a matching precise fault is origin 1.
 emit(true,false,true,MicroPCRomBit|0,false);
 t.writeFstMemoryRequestHeader(true);hot.close();
 std::fclose(t.out_fst_retirement_);std::fclose(t.out_fst_memory_requests_);
 // Missing origin is explicitly unsupported, rather than called architectural.
 TaoTrace unknown;auto stray=std::make_shared<Inst>();stray->staticInst=std::make_shared<StaticInst>();
 stray->staticInst->micro=true;stray->staticInst->first=false;
 stray->state.upc=MicroPCRomBit|999;bool rejected=false;
 try{unknown.accountFstRetirementCommit(stray);}catch(const std::runtime_error&){rejected=true;}
 assert(rejected);
 // The default legacy mode still accepts every production writer callsite,
 // including the collapsed syscall path rejected by the explicit new mode.
 TaoTrace legacy;legacy.functional_retirement_=false;
 legacy.exerciseProductionWriterCallsites(inst);
 assert(legacy.fst_memory_request_hash_!=fastsim::fst::kRequestStreamHashSeed);
}
'''


class SprFunctionalRetirementCaptureTests(unittest.TestCase):
    def test_production_capture_classifies_origins_and_binds_full_population(self):
        source = (PROBE / "tao_trace.cc").read_text()
        functions = "\n".join(definition(source, signature) for signature in (
            "int32_t\nparseFstCoreIdFromName(",
            "void\nTaoTrace::validateFunctionalRetirement(",
            "void\nTaoTrace::accountFstRetirementCommit(",
            "void\nTaoTrace::noteFstMemoryRequest(",
            "void\nTaoTrace::writeFstMemoryRequestHeader(",
        ))
        wire = source[source.index("enum FstFlag :"):source.index("struct FstSyscallMetadataV1")]
        calls = [source[match.start():source.index(";", match.start()) + 1]
                 for match in re.finditer(r"(?<!::)noteFstMemoryRequest\(", source)]
        callsite_driver = ("\nvoid TaoTrace::exerciseProductionWriterCallsites(const DynInstPtr& inst) {\n"
                           "FstRecord record,rec;uint32_t micro_pc=0;\n" + "\n".join(calls) + "\n}\n")
        header = (PROBE / "tao_trace.hh").read_text()
        declaration = header[header.index("void noteFstMemoryRequest("):]
        declaration = declaration[:declaration.index(";") + 1]
        preamble = PREAMBLE.replace(
            "void noteFstMemoryRequest(const DynInstPtr&,const void*,bool,uint32_t,bool=false,bool=false,bool=false);",
            declaration)
        with tempfile.TemporaryDirectory(prefix="spr-fsem-", dir=ROOT / "tmp") as directory:
            p = Path(directory)
            cpp = p / "capture.cc"
            cpp.write_text(preamble + wire + functions + callsite_driver + DRIVER)
            built = subprocess.run([
                "/opt/gcc-11/bin/g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                "-Wno-misleading-indentation", "-I", str(ROOT / "gem5-spr/src"),
                str(cpp), "-o", str(p / "capture"),
            ], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            run = subprocess.run([str(p / "capture"), str(p)], capture_output=True,
                                 text=True, env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"))
            self.assertEqual(run.returncode, 0, run.stderr)
            data = (p / "hot.bin").read_bytes()
            self.assertEqual(len(data), 8 * 64)
            fst = p / "core0.fst"
            fst.write_bytes(struct.pack("<8sIIIIQQ4Q", b"FSTRC01\0", 7, 72, 64,
                                        0, 8, 16 | 128 | 256 | 1024, 0, 0, 0, 0) + data)
            excluded, aborts, _ = load_retirement_eligibility(fst, fst.read_bytes(), 0, 8)
            self.assertEqual(excluded, {2, 3, 7})
            self.assertEqual(aborts, {2})
            companion = Path(str(fst) + ".fsem").read_bytes()
            self.assertEqual(struct.unpack_from("<I", companion, 8)[0], 2)
            self.assertEqual(struct.unpack_from("<Q", companion, 24)[0], 8)
            self.assertEqual(struct.unpack_from("<Q", companion, 32)[0], 3)
            # Changing a non-exceptional row must invalidate the complete binding.
            changed = bytearray(fst.read_bytes()); changed[72] ^= 1
            with self.assertRaisesRegex(ValueError, "stream identity"):
                load_retirement_eligibility(fst, changed, 0, 8)


if __name__ == "__main__":
    unittest.main()
