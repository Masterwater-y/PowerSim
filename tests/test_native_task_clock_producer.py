"""Compile the real standalone native counter observer; no gem5 timing mocks."""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / 'gem5-spr/src/cpu/o3/probe/native_task_clock.hh'
DRIVER = r'''
#include "native_task_clock.hh"
#include <cassert>
#include <sstream>
using gem5::o3::task_clock::Ledger;
void update(Ledger &l,uint64_t c,uint64_t d,int sleeping=-1,int64_t active=-1) {
 if(active>=0)l.active(active);
 if(sleeping>=0)l.sleeping(sleeping!=0);
 l.cycles(d,c*10,c,10);
}
int main(){
 Ledger l; update(l,100,100,0); // WAKEUP: not active
 update(l,101,1); // first ON tick, previous WAKEUP => excluded
 l.initial(11,true);
 l.begin(1010,101,10,3);
 update(l,102,1,-1,1);
 l.out(11,22,1020,102,10,5);
 update(l,103,1,-1,1); // transition has real native cycles
 l.in(11,22,1030,103,10,7);
 update(l,104,1,-1,1);
 l.end(1050,105,10,10); // unsampled ON tail: one more cycle
 assert(l.valid());assert(l.snapshots().front().active_counter==0);
 assert(l.snapshots().back().active_counter==4);
 assert(l.intervals().size()==3);
 assert(l.intervals()[0].active_cycles==1);
 assert(l.intervals()[1].active_cycles==1&&!l.intervals()[1].enabled);
 assert(l.intervals()[2].active_cycles==2&&l.intervals()[2].pid==22);
 assert(l.task_macros(11)==0); // macro counting is explicit, not inferred from cuts
 std::ostringstream json;l.write_json(json);assert(json.str().find("active_counter")!=std::string::npos);
 Ledger s;update(s,1,1,0);update(s,2,1);s.initial(11,true);s.begin(20,2,10,0);
 update(s,3,1,1,1);update(s,8,5,0);update(s,9,1);update(s,10,1,-1,1);
 s.end(100,10,10,2);assert(s.valid());assert(s.halted_pids().count(11));
 assert(s.snapshots().back().active_counter==2); // halt/wakeup gaps excluded
 Ledger sleep_start;update(sleep_start,1,1,0);update(sleep_start,2,1);sleep_start.initial(11,true);
 update(sleep_start,3,1,1,1);sleep_start.begin(80,8,10,0);sleep_start.end(90,9,10,1);
 assert(sleep_start.halted_pids().count(11)); // warmup halt survives across ROI
 Ledger wake_start;update(wake_start,1,1,0);wake_start.initial(11,true);
 wake_start.begin(10,1,10,0);wake_start.end(10,1,10,1);
 assert(wake_start.halted_pids().count(11)); // initial WAKEUP gap is unsupported too
 Ledger idle_start;update(idle_start,1,1,0);update(idle_start,2,1);idle_start.initial(0,true);
 update(idle_start,3,1,1,1);idle_start.begin(80,8,10,0);idle_start.end(90,9,10,1);
 assert(idle_start.valid()); // idle is excluded when selecting late workload PIDs
 Ledger bad;update(bad,1,1,0);update(bad,2,1);bad.initial(11,true);bad.begin(20,2,10,0);
 update(bad,3,1,-1,0);assert(!bad.valid()); // wrong independent ActiveCycles delta
 Ledger order;update(order,1,1,0);update(order,2,1);order.initial(11,true);
 order.begin(20,2,10,0);order.in(11,22,20,2,10,1);assert(!order.valid());
 Ledger freq;update(freq,1,1,0);freq.cycles(1,20,2,11);assert(!freq.valid());
 Ledger overflow;update(overflow,1,1,0);overflow.cycles(0,10,UINT64_C(4294967297),10);
 assert(!overflow.valid());
 Ledger pop;update(pop,1,1,0);update(pop,2,1);pop.initial(11,true);pop.begin(20,2,10,0);
 pop.macro(true,false);pop.macro(true,true);pop.macro(false,false); // pseudo/partial never denominator
 assert(pop.task_macros(11)==1);pop.out(11,22,20,2,10,1);pop.macro(true,false);
 pop.in(11,22,20,2,10,2);pop.macro(true,false);pop.end(20,2,10,3);
 assert(pop.task_macros(11)==1&&pop.task_macros(22)==1);
 Ledger warm;update(warm,1,1,0);update(warm,2,1);warm.initial(11,true);
 warm.out(11,22,20,2,10,0);warm.in(11,22,20,2,10,1);warm.begin(20,2,10,2);
 assert(warm.intervals().empty());warm.end(20,2,10,3);assert(warm.intervals().size()==1);
 Ledger rle;update(rle,1,1,0);update(rle,2,1);rle.initial(11,true);rle.begin(20,2,10,0);
 update(rle,3,1,-1,1);update(rle,4,1,-1,1);update(rle,5,1,-1,1);
 assert(rle.updates().back().count==3);rle.end(50,5,10,3);
 assert(rle.updates().back().first_active_counter==0&&rle.updates().back().last_active_counter==3);
}
'''

class NativeTaskClockProducerTest(unittest.TestCase):
    def test_real_cut_emitter_exists_separately_from_assembly_owner_switch(self):
        source=(ROOT/'gem5-spr/src/cpu/o3/probe/tao_trace.cc').read_text()
        self.assertTrue('TaoTrace::emitTaskClockCut(' in source,
                      'paired accounting cut emitter is missing')

    def test_actual_probe_cut_callbacks_and_oracle_bind_the_native_ledger(self):
        from tests.test_spr_functional_retirement_capture import definition
        source=(ROOT/'gem5-spr/src/cpu/o3/probe/tao_trace.cc').read_text()
        methods='\n'.join(result+'\n'+definition(source,'TaoTrace::'+name) for result,name in [
            ('void','ensureThreadStateOwner('),('bool','emitTaskClockCut('),('void','onTaskClockActive('),
            ('void','onTaskClockSleeping('),('void','onTaskClockCycles('),
            ('void','closeTaskClock('),('std::string','taskClockMetadata('),
            ('void','finalizeTaskClock(')])
        code=r'''
#include "native_task_clock.hh"
#include <cassert>
#include <fstream>
#include <memory>
#include <sstream>
#include <unordered_map>
#include <iostream>
using namespace gem5::o3;
uint64_t now=1000;uint64_t curTick(){return now;}
namespace sim_clock {constexpr uint64_t Frequency=1000000;}
namespace X86ISA {namespace int_reg {enum {Rbx,R13,R15};}namespace misc_reg {enum {Cr3};}}
struct TC {uint64_t regs[3]={11,22,22};uint64_t getReg(int r){return regs[r];}
 uint64_t readMiscRegNoEffect(int){return 4096;}};
struct SI {bool micro=false,last=true;bool isMicroop(){return micro;}bool isLastMicroop(){return last;}};
struct Inst {std::shared_ptr<SI>staticInst=std::make_shared<SI>();TC tc;bool user=false;uint64_t pc=0xffffffff81517a0a;
 TC*tcBase(){return &tc;}const Inst&pcState(){return *this;}uint64_t instAddr()const{return pc;}};
using DynInstPtr=std::shared_ptr<Inst>;
struct CPU {uint64_t cycle=100;uint64_t curCycle(){return cycle;}uint64_t clockPeriod(){return 10;}};
struct TaoTrace {
 bool functional_task_clock_=true,functional_target_exit_scheduled_=true;
 bool thread_state_initial_owner_=false;std::FILE*out_thread_state_=nullptr;
 uint64_t scheduler_account_out_pc_=0xffffffff81517a0a,scheduler_account_in_pc_=0xffffffff81067eef;
 task_clock::Ledger native_task_clock_;CPU cpu;CPU*task_clock_cpu_=&cpu;
 uint64_t thread_state_owner_pointer_=11,thread_state_events_=0,fst_record_count_=10;
 uint64_t fst_memory_request_hash_=123,functional_warmup_record_count_=3;
 uint64_t cpl_measurement_begin_tick_=1010,functional_common_end_tick_=1050;int fst_core_id_=0;
 std::string fst_path_="trace-core0.fst",output_dir_,scheduler_kernel_sha256_="kernel",scheduler_profile_id_="profile";
 struct StateTask {uint64_t task_id;int32_t pid;uint64_t runtime_id;};
 std::unordered_map<uint64_t,StateTask>thread_state_tasks_={{11,{1,11,1}},{22,{2,22,2}}};
 std::vector<std::string>rows;
 bool isUserInstruction(const DynInstPtr&i){return i->user;}
 uint64_t currentStateTask(TC*,bool){return 11;}
 uint64_t registerStateTask(TC*,uint64_t p){return thread_state_tasks_.at(p).task_id;}
 void writeThreadStateRow(const std::string&r,const std::string&,uint64_t,const char*){rows.push_back(r);++thread_state_events_;}
 void ensureThreadStateOwner(const DynInstPtr&,uint64_t,bool);
 bool emitTaskClockCut(const DynInstPtr&,uint64_t,bool);
 void onTaskClockActive(const uint64_t&);void onTaskClockSleeping(const bool&);void onTaskClockCycles(const uint64_t&);
 void closeTaskClock();std::string taskClockMetadata()const;void finalizeTaskClock();
};
'''+methods+r'''
int main(int argc,char**argv){assert(argc==2);
 TaoTrace kernel;kernel.out_thread_state_=std::tmpfile();assert(kernel.out_thread_state_);
 auto kernel_first=std::make_shared<Inst>();kernel.ensureThreadStateOwner(kernel_first,0,false);
 assert(!kernel.native_task_clock_.initial_known());
 assert(!kernel.native_task_clock_.initial_enabled());
 assert(kernel.native_task_clock_.initial_pid()==11);std::fclose(kernel.out_thread_state_);
 TaoTrace user;user.out_thread_state_=std::tmpfile();assert(user.out_thread_state_);
 kernel_first->user=true;user.ensureThreadStateOwner(kernel_first,0,false);
 assert(user.native_task_clock_.initial_known()&&user.native_task_clock_.initial_enabled());
 std::fclose(user.out_thread_state_);
 TaoTrace t;t.output_dir_=argv[1];
 t.onTaskClockSleeping(false);t.onTaskClockCycles(100);
 now=1010;t.cpu.cycle=101;t.onTaskClockCycles(1);t.native_task_clock_.initial(11,true);
 t.native_task_clock_.begin(now,101,10,3);auto i=std::make_shared<Inst>();
 i->staticInst->micro=true;i->staticInst->last=false;
 assert(!t.emitTaskClockCut(i,4,true));assert(t.rows.empty());i->staticInst->last=true;
 now=1020;t.cpu.cycle=102;t.onTaskClockActive(1);t.onTaskClockCycles(1);
 t.native_task_clock_.macro(true,false);assert(t.emitTaskClockCut(i,5,true));
 assert(t.thread_state_owner_pointer_==11); // accounting never mutates scheduler ownership
 now=1030;t.cpu.cycle=103;t.onTaskClockActive(1);t.onTaskClockCycles(1);
 t.native_task_clock_.macro(true,false);i->pc=0xffffffff81067eef;assert(t.emitTaskClockCut(i,7,true));
 assert(t.thread_state_owner_pointer_==11);
 now=1040;t.cpu.cycle=104;t.onTaskClockActive(1);t.onTaskClockCycles(1);t.native_task_clock_.macro(true,false);
 now=1050;t.cpu.cycle=105;t.closeTaskClock();t.finalizeTaskClock();
 for(const auto&r:t.rows)std::cout<<r<<'\n';std::cout<<"{"<<"\"meta\":true"<<t.taskClockMetadata()<<"}\n";
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            directory=Path(directory);(directory/'oracle').mkdir()
            cpp=directory/'probe.cpp';cpp.write_text(code);binary=directory/'probe'
            run=subprocess.run(['g++','-std=c++17','-static-libstdc++','-I'+str(HEADER.parent),
                str(cpp),'-o',str(binary)],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            run=subprocess.run([str(binary),str(directory)],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            rows=[json.loads(row) for row in run.stdout.splitlines()]
            self.assertEqual([r['event_kind'] for r in rows[:2]],['account_out','account_in'])
            self.assertEqual(rows[0]['prev_pid'],11);self.assertEqual(rows[1]['next_pid'],22)
            for row in rows[:2]:
                self.assertFalse(set(row)&{'tick','cycle','active_cycles','commit_tick'})
            self.assertEqual(rows[2]['task_clock_initial_pid'],11)
            oracle=json.loads((directory/'oracle/task-clock-native-core0.json').read_text())
            self.assertTrue(oracle['valid']);self.assertEqual(oracle['source_stream_hash'],123)
            self.assertEqual(oracle['active_cycles'],3)
            self.assertEqual(oracle['transition_active_cycles'],1)
            self.assertEqual(oracle['task_macro_instructions'],2)
            self.assertEqual(oracle['perf_equivalence'],'unverified')
            self.assertTrue(oracle['updates'][0]['bootstrap'])

    def test_native_counter_order_roi_pending_tail_cuts_population_and_rejection(self):
        self.assertTrue(HEADER.is_file(), 'native counter observer is not implemented')
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            cpp=Path(directory)/'clock.cpp';cpp.write_text(DRIVER)
            binary=Path(directory)/'clock'
            result=subprocess.run(['g++','-std=c++17','-static-libstdc++','-Wall','-Wextra','-Werror',
                '-I'+str(HEADER.parent),str(cpp),'-o',str(binary)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            result=subprocess.run([str(binary)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__':unittest.main()
