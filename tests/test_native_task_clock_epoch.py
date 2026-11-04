"""Actual native observer bootstraps inherited CPU state without notifications."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
DRIVER=r'''
#include "native_task_clock.hh"
#include <cassert>
#include <iostream>
#include <sstream>
using namespace gem5::o3::task_clock;
int main(){
 Ledger sleep;sleep.bootstrap(1000,100,10,30,State::Sleep);sleep.initial(0,true);
 sleep.begin(1000,100,10,0);sleep.end(1100,110,10,0);
 assert(sleep.valid());assert(sleep.snapshots().back().active_counter==0);
 Ledger on;on.bootstrap(1000,100,10,98,State::On);on.initial(11,true);
 on.begin(1000,100,10,0);on.active(3);on.cycles(3,1010,101,10);
 on.end(1020,102,10,1);assert(on.valid());assert(on.intervals().back().active_cycles==2);
 Ledger wake;wake.bootstrap(1000,100,10,99,State::Wakeup);wake.initial(0,true);
 wake.begin(1000,100,10,0);wake.cycles(2,1010,101,10);
 wake.active(1);wake.cycles(1,1020,102,10);wake.end(1020,102,10,1);
 assert(wake.valid());assert(wake.snapshots().back().active_counter==1);
 Ledger ancient;uint64_t c=(UINT64_C(1)<<32)+100;
 ancient.bootstrap(c*10,c,10,0,State::Sleep);ancient.initial(0,true);
 ancient.begin(c*10,c,10,0);ancient.sleeping(false);ancient.cycles(101,(c+1)*10,c+1,10);
 ancient.cycles(1,(c+2)*10,c+2,10);ancient.active(1);ancient.cycles(1,(c+3)*10,c+3,10);
 ancient.end((c+3)*10,c+3,10,1);assert(ancient.valid());
 Ledger prefix;prefix.bootstrap(1000,100,10,99,State::On);prefix.begin(1000,100,10,0);
 prefix.initial(0,false);prefix.macro(true,false);prefix.out(0,22,1010,101,10,1,true);
 assert(prefix.initial_resolution());assert(prefix.initial_resolution_ordinal()==1);
 assert(!prefix.initial_known());assert(!prefix.snapshots().front().known);
 assert(prefix.task_macros(0)==1);
 Ledger before;before.bootstrap(1000,100,10,99,State::On);before.initial(0,false);
 before.out(0,22,1000,100,10,1,true);std::ostringstream clean;before.write_json(clean);
 assert(clean.str().find("\"per_pid_macros\":[]")!=std::string::npos);
 Ledger positive;positive.bootstrap(1000,100,10,99,State::On);positive.initial(11,false);
 positive.out(11,22,1000,100,10,1);assert(!positive.initial_resolution());
 Ledger task;task.bootstrap(1000,100,10,99,State::On);task.initial(11,false);
 task.begin(1000,100,10,0);task.macro(true,false);task.out(11,22,1010,101,10,1,true);
 assert(task.initial_resolution()&&task.task_macros(11)==1&&!task.initial_known());
 assert(!task.intervals().front().known&&task.resolved_prefix(task.intervals().front()));
 Ledger halted;halted.bootstrap(1000,100,10,99,State::Sleep);halted.initial(11,false);
 halted.begin(1000,100,10,0);halted.out(11,22,1010,101,10,1,true);
 assert(!halted.initial_resolution());
 Ledger midhalt;midhalt.bootstrap(1000,100,10,99,State::On);midhalt.initial(11,false);
 midhalt.begin(1000,100,10,0);midhalt.active(2);midhalt.sleeping(true);midhalt.cycles(2,1010,101,10);
 midhalt.out(11,22,1010,101,10,1,true);assert(!midhalt.initial_resolution());
 Ledger bad;bad.bootstrap(1000,100,10,101,State::On);assert(!bad.valid());
 Ledger twice;twice.bootstrap(1000,100,10,98,State::On);twice.bootstrap(1000,100,10,98,State::On);
 assert(!twice.valid());
 std::cout<<"{";sleep.write_json(std::cout);std::cout<<"}\n{";on.write_json(std::cout);std::cout<<"}\n{";
 ancient.write_json(std::cout);std::cout<<"}\n";
}
'''
class NativeTaskClockEpochTest(unittest.TestCase):
 def test_cpu_observation_is_a_readonly_state_copy(self):
  source=(ROOT/'gem5-spr/src/cpu/base.hh').read_text()
  from tests.test_spr_functional_retirement_capture import definition
  method=definition(source,'observeCycleCounter() const')
  code="#include <cstdint>\n#include <cassert>\nstruct CPU {enum {CPU_STATE_ON,CPU_STATE_SLEEP,CPU_STATE_WAKEUP};uint64_t previousCycle=98;int previousState=CPU_STATE_SLEEP;struct CycleCounterObservation{uint64_t previous_cycle;bool was_on,was_sleeping;};\n"+"CycleCounterObservation "+method+"\n};int main(){const CPU cpu;auto x=cpu.observeCycleCounter();assert(x.previous_cycle==98&&!x.was_on&&x.was_sleeping);assert(cpu.previousCycle==98&&cpu.previousState==CPU::CPU_STATE_SLEEP);}"
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   p=Path(d);(p/'observe.cpp').write_text(code)
   r=subprocess.run(['g++','-std=c++17',str(p/'observe.cpp'),'-o',str(p/'observe')],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
   self.assertEqual(subprocess.run([str(p/'observe')]).returncode,0)
 def test_real_inherited_epoch_and_independent_audit(self):
  from audit_task_clock import validate_native_counter
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   p=Path(d);(p/'clock.cpp').write_text(DRIVER)
   r=subprocess.run(['g++','-std=c++17','-static-libstdc++','-Wall','-Wextra','-Werror','-I'+str(ROOT/'gem5-spr/src/cpu/o3/probe'),str(p/'clock.cpp'),'-o',str(p/'clock')],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
   r=subprocess.run([str(p/'clock')],capture_output=True,text=True);self.assertEqual(r.returncode,0,r.stderr)
   rows=[json.loads(l) for l in r.stdout.splitlines()]
   self.assertEqual([validate_native_counter(row,10)['active_counter_delta'] for row in rows],[0,2,1])
   self.assertEqual(rows[0]['counter_epoch']['previous_state'],0)
   import copy
   for mutate in (lambda row:row['counter_epoch'].update(previous_cycle=101),lambda row:row['counter_epoch'].update(previous_state=2),lambda row:row['counter_epoch'].update(period=11),lambda row:row['counter_epoch'].update(sequence=1)):
    row=copy.deepcopy(rows[0]);mutate(row)
    with self.assertRaises(ValueError):validate_native_counter(row,10)
