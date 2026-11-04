#include "fastsim/task_clock.hpp"
#include "fastsim/functional_sync.hpp"
#include <stdexcept>
#include <fstream>
#include <cstdio>
using fastsim::TaskClock;
void check(bool b){if(!b)throw std::runtime_error("task clock assertion");}
template<class F>void rejects(F f){bool b=false;try{f();}catch(const std::exception&){b=true;}check(b);}
int main(){
 TaskClock clock({{true,true,101},{true,true,202}},{101,202});
 clock.begin_measurement(100);
 clock.account_out(0,101,303,150);
 check(!clock.workload_eligible(0));
 rejects([&]{clock.account_out(0,101,303,160);});
 rejects([&]{clock.account_in(0,101,404,200);});
 clock.account_in(0,101,303,200);
 check(!clock.workload_eligible(0));
 clock.account_out(0,303,101,250);clock.account_in(0,303,101,300);
 clock.finish_core(0,350);clock.finish_core(1,400);clock.finish_common(400,{true,true});
 check(clock.active_q16()[0]==150&&clock.other_q16()[0]==50&&clock.transition_q16()[0]==100);
 check(clock.active_q16()[1]==300&&clock.complete());
 TaskClock warm({{true,true,101}},{101});
 warm.account_out(0,101,303,10);warm.begin_measurement(20);warm.account_in(0,101,303,30);
 warm.finish_core(0,40);warm.finish_common(50,{false});
 check(warm.active_q16()[0]==0&&warm.transition_q16()[0]==10&&warm.other_q16()[0]==10&&warm.unknown_q16()[0]==10&&!warm.complete());
 TaskClock unknown({{false,false,0}},{101});unknown.begin_measurement(0);
 unknown.finish_core(0,100);unknown.finish_common(100,{true});check(!unknown.complete()&&unknown.unknown_q16()[0]==100);
 const char* path="tmp/task-clock-unit.plan";
 std::ofstream out(path);out<<"FASTSIM_THREAD_STATE_PLAN_V1\nCORES 1 EVENTS 4 TASKS 1\nCORE 0 10 2 8 101\nTASK 101 101 1\nACCOUNTING task-oncpu-switch-entry-v1\nCLOSURE 0 1\nTASK_CLOCK task-lifecycle-basecpu-active-v1\nCLOCK_STATE 0 1 1 101 1\nEVENT 0 0 1 4096 1 0 101 101 1 0\nEVENT 1 0 3 18446744071584184842 8 1 101 303 0 1 0\nEVENT 2 0 4 4096 6 1 101 303 1 1 1\nEVENT 3 0 6 18446744071579270895 9 1 101 303 0 1 2\n";out.close();
 fastsim::FunctionalSyncPlan plan(path,1);check(plan.task_clock_contract()=="task-lifecycle-basecpu-active-v1");check(plan.requires_execution_boundary(1)&&plan.requires_execution_boundary(3));std::remove(path);
}
