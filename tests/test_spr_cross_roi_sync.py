"""ROI resets counters while functional wait/barrier/task state stays continuous.

These regressions cover behavior already implemented correctly. They neither
change the guest workload nor introduce a new synchronization/measurement cut.
"""
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools'),str(Path(__file__).resolve().parent)]
from compile_thread_state import compile_plan
from functional_sync_graph import build_graph as sync_graph
from thread_state_graph import build_graph as state_graph
from test_thread_state_graph import blocked_fixture,certify,meta
from test_functional_sync_graph import row as sync_row


def cross_roi_wait():
    rows=blocked_fixture()
    metadata=[certify(meta(0,warmup=4)),certify(meta(1,warmup=4))]
    for item in metadata:item['event_count']=sum(e['core_id']==item['core_id'] for e in rows)
    for event in rows:event['phase']='warmup' if event['record_ordinal']<4 else 'measurement'
    return rows,metadata


def write_state_plan(directory):
    rows,metadata=cross_roi_wait()
    record=struct.Struct('<QQQQ4IHHhBB4BI')
    for item in metadata:
        core=item['core_id'];count=item['source_record_count']
        (directory/f'thread-state-metadata-core{core}.json').write_text(json.dumps(item))
        (directory/f'thread-state-core{core}.jsonl').write_text('\n'.join(json.dumps(e) for e in rows if e['core_id']==core))
        data=bytearray(struct.pack('<8sIIIIQQ4Q',b'FSTRC01\0',7,72,64,core,count,0,0,0,0,0))
        for ordinal in range(count):
            data.extend(record.pack(4096+4*ordinal,0,0,0,0,0,0,0,0,1,0,0,0,255,255,255,255,0))
        (directory/item['source_file']).write_bytes(data)
    plan=directory/'thread-state.plan'
    compile_plan(directory,plan,require_sources=True)
    return plan


DRIVER=r'''
#include "fastsim/functional_sync.hpp"
#include "fastsim/recorded_scheduler.hpp"
#include "fastsim/task_clock.hpp"
#include <algorithm>
#include <cassert>
#include <stdexcept>
using fastsim::FunctionalSyncPlan;
using fastsim::RecordedScheduler;
using fastsim::TaskClock;
size_t find(const FunctionalSyncPlan& p,unsigned core,unsigned kind,uint64_t ordinal){
 for(size_t i=0;i<p.event_count();++i){const auto&e=p.event(i);if(e.core==core&&e.kind==kind&&e.ordinal==ordinal)return i;}
 throw std::runtime_error("missing fixture event");
}
void wait_across_roi(const char*path){
 FunctionalSyncPlan p(path,2);RecordedScheduler scheduler(p.initial_owners(),p.task_ids());
 for(size_t i=0;i<p.event_count();++i){
  const auto&e=p.event(i);if(e.measurement||!p.requires_execution_boundary(i))continue;
  p.observe(i,50);assert(p.can_release(i));p.release(i);
  if(e.kind==6)scheduler.switch_owner(e.core,e.arg0,e.arg1,e.arg2,50);
 }
 const auto wait=find(p,0,2,2),out=find(p,0,6,3),wake=find(p,1,7,5),in=find(p,0,6,7),ret=find(p,0,3,8);
 const auto has=[&](size_t child,size_t parent){const auto&d=p.event(child).predecessors;return std::find(d.begin(),d.end(),parent)!=d.end();};
 assert(has(wake,wait)&&has(wake,out)&&has(in,wake)&&has(ret,wait));
 scheduler.begin_measurement(100);assert(scheduler.owner(0)==0);
 // A switch-in stays pending across ROI until the retained wake releases.
 p.observe(in,200);assert(!p.can_release(in));
 const auto attempt=find(p,1,4,4);
 if(p.requires_execution_boundary(attempt)){p.observe(attempt,110);assert(p.can_release(attempt));p.release(attempt);}
 p.observe(wake,120);assert(p.can_release(wake));p.release(wake);scheduler.wakeup(101);
 assert(scheduler.owner(0)==0); // READY does not mean RUNNING.
 assert(p.can_release(in));assert(p.predecessor_ready(in)>=120);p.release(in);
 scheduler.switch_owner(0,0,101,1,200);assert(scheduler.owner(0)==101);
 scheduler.finish_core(0,300);scheduler.finish_core(1,300);scheduler.finish_common(300,{true,true});
 assert(scheduler.active_q16()[0]==100&&scheduler.other_q16()[0]==100);
 assert(scheduler.active_q16()[1]==200&&scheduler.complete());
}
void ready_before_roi(){
 RecordedScheduler blocked({101},{101});blocked.switch_owner(0,101,0,2,50);blocked.begin_measurement(100);
 bool rejected=false;try{blocked.switch_owner(0,0,101,1,200);}catch(const std::exception&){rejected=true;}assert(rejected);
 RecordedScheduler ready({101},{101});ready.switch_owner(0,101,0,2,50);ready.wakeup(101);ready.begin_measurement(100);
 assert(ready.owner(0)==0);ready.switch_owner(0,0,101,1,200);ready.finish_core(0,300);ready.finish_common(300,{true});
 assert(ready.active_q16()[0]==100&&ready.other_q16()[0]==100);
}
void transition_across_roi(){
 TaskClock clock({{true,true,101}},{101});clock.account_out(0,101,303,50);
 clock.begin_measurement(100);assert(!clock.workload_eligible(0));
 clock.account_in(0,101,303,150);assert(!clock.workload_eligible(0));
 clock.finish_core(0,200);clock.finish_common(200,{true});
 assert(clock.active_q16()[0]==0&&clock.transition_q16()[0]==50&&clock.other_q16()[0]==50&&clock.complete());
 TaskClock resumed({{true,true,101}},{101});resumed.account_out(0,101,303,10);resumed.account_in(0,101,303,30);
 resumed.begin_measurement(100);resumed.account_out(0,303,101,150);resumed.account_in(0,303,101,180);
 resumed.finish_core(0,200);resumed.finish_common(200,{true});
 assert(resumed.other_q16()[0]==50&&resumed.transition_q16()[0]==30&&resumed.active_q16()[0]==20);
}
void barrier_across_roi(const char*path){
 FunctionalSyncPlan p(path,2);p.observe(0,60);assert(p.can_release(0));assert(p.release(0)==60);
 // The coordinator retains this plan object when only measurement counters
 // and the common model origin change at ROI.
 p.observe(1,110);assert(!p.can_release(1));p.observe(2,200);assert(p.can_release(2));p.release(2);
 assert(p.can_release(1)&&p.predecessor_ready(1)==200&&p.release(1)==200);
 p.observe(3,210);assert(p.can_release(3)&&p.predecessor_ready(3)==200);p.release(3);assert(p.all_released());
}
int main(int argc,char**argv){assert(argc==3);wait_across_roi(argv[1]);ready_before_roi();transition_across_roi();barrier_across_roi(argv[2]);}
'''


class CrossRoiSynchronizationTest(unittest.TestCase):
    def test_warmup_wait_and_block_are_parents_of_roi_wake_and_switchin(self):
        rows,metadata=cross_roi_wait();graph=state_graph(rows,metadata)
        indexed={e['event_id']:i for i,e in enumerate(graph['events'])}
        def parents(name):return {graph['events'][i]['event_id'] for i in graph['dependencies'][indexed[name]]}
        self.assertEqual(graph['events'][indexed['core0:e2']]['phase'],'warmup')
        self.assertEqual(graph['events'][indexed['core1:e5']]['phase'],'measurement')
        self.assertTrue({'core0:e2','core0:e3','core1:e4'}<=parents('core1:e5'))
        self.assertIn('core1:e5',parents('core0:e7'))
        self.assertIn('core0:e2',parents('core0:e8'))
        self.assertEqual(graph['summary']['pending_wait_requests'],0)
        self.assertEqual(graph['summary']['proven_block_wake_admissions'],1)

    def test_barrier_arrival_before_roi_remains_parent_of_roi_departures(self):
        rows=[sync_row(0,0,1,0,phase='warmup'),sync_row(0,1,3,0,phase='warmup'),
              sync_row(1,0,3,1,phase='warmup'),sync_row(0,2,5,0,phase='warmup'),
              sync_row(1,4,5,1),sync_row(0,3,6,0),sync_row(1,5,6,1)]
        events,deps,_=sync_graph(rows);by={(e['core_id'],e['record_ordinal']):i for i,e in enumerate(events)}
        for departure in ((0,3),(1,5)):
            self.assertTrue({by[(0,2)],by[(1,4)]}<=deps[by[departure]])

    def test_production_plan_scheduler_and_task_clock_preserve_state_across_roi(self):
        with tempfile.TemporaryDirectory(prefix='cross-roi-',dir=ROOT/'tmp') as temporary:
            directory=Path(temporary);plan=write_state_plan(directory);barrier=directory/'barrier.plan'
            barrier.write_text('FASTSIM_FUNCTIONAL_SYNC_PLAN_V1\nCORES 2 EVENTS 4\n'
                'CORE 0 6 3 3\nCORE 1 8 3 5\n'
                'EVENT 0 0 2 4096 1 5 0 0\nEVENT 1 0 4 4096 1 6 1 2 0 2\n'
                'EVENT 2 1 5 4096 1 5 1 0\nEVENT 3 1 6 4096 1 6 1 2 0 2\n')
            source=directory/'cross-roi.cpp';source.write_text(DRIVER);binary=directory/'cross-roi'
            compiled=subprocess.run([shutil.which('g++') or 'g++','-std=c++17','-static-libstdc++','-static-libgcc',
                '-Wall','-Wextra','-Werror','-I'+str(ROOT/'include'),str(source),
                str(ROOT/'src/functional_sync.cpp'),str(ROOT/'src/recorded_scheduler.cpp'),'-o',str(binary)],capture_output=True,text=True)
            self.assertEqual(compiled.returncode,0,compiled.stderr)
            run=subprocess.run([str(binary),str(plan),str(barrier)],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)


if __name__=='__main__':unittest.main()
