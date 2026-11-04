"""Native source-counter retirement anchors, including the real commit callback."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_spr_functional_retirement_capture import definition

ROOT=Path(__file__).resolve().parents[1]
PROBE=ROOT/'gem5-spr/src/cpu/o3/probe'

LEDGER_DRIVER=r'''
#include "native_task_clock.hh"
#include <cassert>
#include <sstream>
using namespace gem5::o3::task_clock;
int main(){
 Ledger empty;std::ostringstream absent;empty.write_json(absent);
 assert(absent.str().find("retirement_stop")==std::string::npos);
 Ledger l;l.bootstrap(100,10,10,10,State::On);l.initial(11,true);l.begin(100,10,10,0);
 const auto n=l.snapshots().size(),u=l.updates().size();
 l.observe_retirement(120,12,10,1); // pending ON tail, no counter callback yet
 assert(l.retirement_stop()&&l.retirement_stop()->active_counter==2);
 const auto seq=l.retirement_stop()->next_sequence;
 for(uint64_t i=2;i<=1000000;++i)l.observe_retirement(120,12,10,i);
 assert(l.retirement_stop()->record_ordinal==1000000);
 assert(l.retirement_stop()->next_sequence==seq);
 assert(l.snapshots().size()==n&&l.updates().size()==u); // O(1), no sequence or timing mutation
 l.active(2);l.cycles(2,120,12,10);
 l.active(1);l.cycles(1,130,13,10);l.observe_retirement(130,13,10,1000001);
 const auto interior=l.retirement_stop()->next_sequence;
 l.active(1);l.cycles(1,140,14,10);l.active(1);l.cycles(1,150,15,10);
 assert(l.updates().back().first_sequence<interior&&interior<=l.updates().back().last_sequence);
 assert(l.retirement_stop()->active_counter==3); // later RLE extension must not move anchor
 l.end(160,16,10,1000001);assert(l.valid());
 std::ostringstream out;l.write_json(out);assert(out.str().find("gem5-retirement-stop-v1")!=std::string::npos);
 Ledger sleep;sleep.bootstrap(100,10,10,5,State::Sleep);sleep.initial(0,true);sleep.begin(100,10,10,0);
 sleep.observe_retirement(120,12,10,1);assert(sleep.retirement_stop()->active_counter==0);
 Ledger stale;stale.bootstrap(100,10,10,10,State::On);stale.initial(11,true);stale.begin(100,10,10,0);
 stale.observe_retirement(100,10,10,2);stale.observe_retirement(100,10,10,1);
 assert(!stale.valid()&&stale.retirement_stop()->record_ordinal==2);
 Ledger closed;closed.bootstrap(100,10,10,10,State::On);closed.initial(11,true);closed.begin(100,10,10,0);
 closed.end(110,11,10,1);closed.observe_retirement(110,11,10,1);
 assert(closed.valid()&&closed.retirement_stop()->active_counter==1); // target closes inside accumulateMicro
 closed.observe_retirement(120,12,10,2);assert(!closed.valid());
 assert(closed.retirement_stop()->record_ordinal==1);
}
'''

PROBE_PREAMBLE=r'''
#include "native_task_clock.hh"
#include <cassert>
#include <memory>
#include <unordered_map>
#include <sstream>
#include <iostream>
using namespace gem5::o3;
uint64_t now=100;uint64_t curTick(){return now;}
namespace X86ISA{namespace int_reg{enum {Rax};}}
struct SI{bool isMicroop()const{return true;}bool isLastMicroop()const{return true;}};
struct TC{uint64_t getReg(int){return 0;}};
struct Inst{uint64_t seqNum=1;std::shared_ptr<SI>staticInst=std::make_shared<SI>();TC tc;
 TC*tcBase(){return &tc;}const Inst&pcState(){return *this;}uint64_t instAddr()const{return 4096;}};
using DynInstPtr=std::shared_ptr<Inst>;
struct CPU{uint64_t cycle=10;uint64_t curCycle()const{return cycle;}uint64_t clockPeriod()const{return 10;}};
struct Marker{void observe(const DynInstPtr&,uint64_t){}};
struct TaoTrace{
 bool functional_task_clock_=true,functional_warmup_=false,functional_measurement_started_=true;
 bool premeasurement_page_fault_pending_=false,functional_user_only_=false,functional_syscall_microops_=true;
 bool retain=true,target=false,cut=true;uint64_t fst_record_count_=0,functional_sync_marker_pc_=0;
 uint64_t thread_state_unretained_hooks_=0;Marker*fixed_work_marker_=nullptr;void*out_thread_state_=this;
 CPU cpu;CPU*task_clock_cpu_=&cpu;task_clock::Ledger native_task_clock_;
 std::unordered_map<uint64_t,int>pending_syscalls_,pending_shared_attr_;
 void eraseWrongPathCommitted(const DynInstPtr&){}bool isUserInstruction(const DynInstPtr&){return true;}
 bool isSyscallBoundary(const DynInstPtr&){return false;}bool functionalCaptureActive(){return true;}
 bool cplMeasurementActive(){return true;}bool functionalTraceEnabled(){return true;}
 bool isSyscallMacro(const DynInstPtr&){return false;}bool isSyscallInst(const DynInstPtr&){return false;}
 uint64_t getTraceThreadId(const DynInstPtr&){return 0;}bool functionalTargetReached(){return target;}
 bool isThreadStateHook(uint64_t){return cut;}
 void emitFunctionalSyscallMarker(const DynInstPtr&,uint64_t){}void noteCplSyscallEntry(const DynInstPtr&){}
 void accountPerfRetirementCommit(const DynInstPtr&){}void accountTaskPmuCommit(const DynInstPtr&){}
 void accountCplCommit(const DynInstPtr&,bool){}void emitSyscallRecord(const DynInstPtr&){}
 void emitFunctionalSync(const DynInstPtr&,uint64_t,bool){}
 void ensureTaskClockCounter(){}
 void accumulateMicro(const DynInstPtr&){if(retain){++fst_record_count_;if(target)native_task_clock_.end(now,cpu.cycle,10,fst_record_count_);}}
 void emitThreadState(const DynInstPtr&,uint64_t ordinal,bool){if(cut)native_task_clock_.out(11,22,now,cpu.cycle,10,ordinal);}
 void observeTaskClockRetirement(uint64_t record_ordinal);void onCommit(const DynInstPtr&);
};
'''

PROBE_DRIVER=r'''
int main(){auto i=std::make_shared<Inst>();TaoTrace t;
 t.native_task_clock_.bootstrap(100,10,10,10,task_clock::State::On);t.native_task_clock_.initial(11,true);
 t.native_task_clock_.begin(100,10,10,0);t.onCommit(i);
 assert(t.native_task_clock_.retirement_stop());
 const auto &stop=*t.native_task_clock_.retirement_stop();
 assert(stop.record_ordinal==1&&stop.next_sequence==t.native_task_clock_.snapshots().back().sequence+1);
 assert(t.native_task_clock_.snapshots().back().kind=="account_out");
 t.cut=false;t.retain=false;t.onCommit(i);assert(t.native_task_clock_.retirement_stop()->record_ordinal==1);
 TaoTrace target;target.cut=false;target.target=true;target.cpu.cycle=11;now=110;
 target.native_task_clock_.bootstrap(100,10,10,10,task_clock::State::On);
 target.native_task_clock_.initial(11,true);target.native_task_clock_.begin(100,10,10,0);
 target.onCommit(i);assert(target.native_task_clock_.closed()&&target.native_task_clock_.valid());
 assert(target.native_task_clock_.retirement_stop()->record_ordinal==1);
 assert(target.native_task_clock_.retirement_stop()->active_counter==1);
 std::cout<<'{';target.native_task_clock_.write_json(std::cout);std::cout<<'}';
}
'''

class NativeRetirementStopTest(unittest.TestCase):
    def compile_run(self,code):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            path=Path(directory)/'retirement.cpp';path.write_text(code);binary=Path(directory)/'retirement'
            run=subprocess.run(['g++','-std=c++17','-static-libstdc++','-Wall','-Wextra','-Werror',
                '-I'+str(PROBE),str(path),'-o',str(binary)],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            run=subprocess.run([str(binary)],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            return run.stdout

    def test_native_retirement_anchor_has_constant_storage_and_exact_counter_prefix(self):
        self.assertTrue('observe_retirement(' in (PROBE/'native_task_clock.hh').read_text(),
                        'native last-retirement source counter anchor is missing')
        self.compile_run(LEDGER_DRIVER)

    def test_actual_commit_records_anchor_after_functional_cut_and_target_closure(self):
        source=(PROBE/'tao_trace.cc').read_text()
        self.assertTrue('TaoTrace::observeTaskClockRetirement(' in source,
                        'actual retained-commit retirement observation is missing')
        methods='void\n'+definition(source,'TaoTrace::observeTaskClockRetirement(')+'\nvoid\n'+definition(source,'TaoTrace::onCommit(')
        data=json.loads(self.compile_run(PROBE_PREAMBLE+methods+PROBE_DRIVER))
        self.assertEqual(data['retirement_stop']['schema'],'gem5-retirement-stop-v1')
        self.assertEqual(data['retirement_stop']['record_ordinal'],1)
        self.assertEqual(data['retirement_stop']['active_counter'],1)

if __name__=='__main__':unittest.main()
