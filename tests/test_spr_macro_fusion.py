"""Static fusion pair eligibility; actual batching remains model-owned."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

class FusionTest(unittest.TestCase):
    def test_inputs_gates_and_unknown(self):
        code=r'''
#include "fastsim/spr_macro_fusion.hpp"
#include <cassert>
#include <cstdint>
int main(){
 using namespace fastsim;
 SprFusionPair p;
 p.producer_pc=0x103a;p.branch_pc=0x103c;p.producer_size=2;
 p.producer_asid=9;p.branch_asid=9;p.producer_from_user=true;p.branch_from_user=true;
 p.producer_alu=true;p.producer_group_ready=true;p.producer_has_fused_branch=false;
 p.branch_single=true;p.branch_microcode=false;p.same_delivery_batch=true;
 p.allowed_jcc_mask=0xf0fc;p.branch_condition=4;
 assert(spr_fusion_pair(p)==SprFusionDecision::Eligible);
 auto t=p;t.branch_pc=0x1040;assert(spr_fusion_pair(t)==SprFusionDecision::Ineligible);
 t=p;t.producer_pc=0x103e;t.branch_pc=0x1040;assert(spr_fusion_pair(t)==SprFusionDecision::Ineligible);
 t=p;t.branch_asid=10;assert(spr_fusion_pair(t)==SprFusionDecision::Ineligible);
 t=p;t.branch_from_user=false;assert(spr_fusion_pair(t)==SprFusionDecision::Ineligible);
 t=p;t.branch_condition=0;assert(spr_fusion_pair(t)==SprFusionDecision::Ineligible);
 t=p;t.branch_condition=255;assert(spr_fusion_pair(t)==SprFusionDecision::Ineligible);
 t=p;t.branch_condition=16;assert(spr_fusion_pair(t)==SprFusionDecision::Unknown);
 t=p;t.producer_alu=false;assert(spr_fusion_pair(t)==SprFusionDecision::Ineligible);
 t=p;t.branch_single=false;assert(spr_fusion_pair(t)==SprFusionDecision::Ineligible);
 t=p;t.producer_group_ready=false;assert(spr_fusion_pair(t)==SprFusionDecision::Ineligible);
 t=p;t.producer_has_fused_branch=true;assert(spr_fusion_pair(t)==SprFusionDecision::Ineligible);
 t=p;t.branch_microcode=true;assert(spr_fusion_pair(t)==SprFusionDecision::Ineligible);
 t=p;t.same_delivery_batch=false;assert(spr_fusion_pair(t)==SprFusionDecision::Ineligible);
 t=p;t.branch_asid.reset();assert(spr_fusion_pair(t)==SprFusionDecision::Unknown);
 t=p;t.producer_size=0;assert(spr_fusion_pair(t)==SprFusionDecision::Unknown);
 t=p;t.producer_pc=UINT64_MAX-1;t.producer_size=3;assert(spr_fusion_pair(t)==SprFusionDecision::Unknown);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            path=Path(d)/'test.cpp';exe=Path(d)/'test';path.write_text(code)
            p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(path),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            p=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
    def test_masks_against_gem5_static_methods(self):
        source=(ROOT/'gem5-spr/src/arch/x86/insts/static_inst.hh').read_text()
        start=source.index('    bool coreFusionFactsKnown() const override')
        end=source.index('    bool isNearIndirectBranch()',start)
        methods=source[start:end].replace(' override','')
        code=r'''
#include "fastsim/spr_macro_fusion.hpp"
#include <cassert>
#include <cstdint>
enum OpType {OneByteOpcode,TwoByteOpcode};
struct StaticInst {virtual ~StaticInst()=default;};
struct X86StaticInst : StaticInst {
 struct {struct {OpType type;unsigned op;} opcode;struct {unsigned reg;} modRM;} machInst;
'''+methods+r'''
};
int main(){
 using namespace fastsim;
 SprFusionPair p;
 p.producer_pc=0x1000;p.branch_pc=0x1002;p.producer_size=2;
 p.producer_asid=1;p.branch_asid=1;p.producer_from_user=true;p.branch_from_user=true;
 p.producer_alu=true;p.producer_group_ready=true;p.producer_has_fused_branch=false;
 p.branch_single=true;p.branch_microcode=false;p.same_delivery_batch=true;
 for(unsigned op=0;op<256;++op)for(unsigned group=0;group<8;++group)
  for(unsigned condition=0;condition<16;++condition)for(unsigned length=0;length<2;++length){
   X86StaticInst producer,branch;
   producer.machInst.opcode={OneByteOpcode,op};producer.machInst.modRM.reg=group;
   branch.machInst.opcode={length?TwoByteOpcode:OneByteOpcode,(length?0x80u:0x70u)+condition};
   p.allowed_jcc_mask=producer.coreFusionAllowedJccMask();
   p.branch_condition=branch.coreFusionJccCondition();
   assert((spr_fusion_pair(p)==SprFusionDecision::Eligible)==producer.canMacroFuseWith(branch));
 }
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            path=Path(d)/'test.cpp';exe=Path(d)/'test';path.write_text(code)
            p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(path),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            p=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)

if __name__=='__main__':unittest.main()
