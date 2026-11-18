#include "fastsim/task_pmu_coverage.hpp"
#include <iostream>
#include <stdexcept>
static void check(bool ok,const char* why){if(!ok)throw std::runtime_error(why);}
int main(){try{
 fastsim::SimulationStats s;s.cores.resize(2);
 s.thread_state_enabled=true;s.thread_state_accounting_complete=true;
 s.thread_state_accounting_contract="task-oncpu-switch-entry-v1";
 s.cores[0].task_pmu.retired_instructions=3;
 s.cores[0].other_task_pmu.retired_instructions=2;
 s.cores[0].full_task_pmu.retired_instructions=5;
 s.cores[1].other_task_pmu.retired_instructions=4;
 s.cores[1].full_task_pmu.retired_instructions=4;
 check(fastsim::task_pmu_coverage_available(s),"a known-zero task core is valid in a common-end population");
 s.thread_state_accounting_complete=false;
 check(!fastsim::task_pmu_coverage_available(s),"unknown ownership is not a known zero");
 s.thread_state_accounting_complete=true;s.cores[1].full_task_pmu.retired_instructions=5;
 check(!fastsim::task_pmu_coverage_available(s),"per-core conservation remains mandatory");
 s.cores[1].full_task_pmu.retired_instructions=4;s.cores[0].task_pmu.retired_instructions=0;
 s.cores[0].full_task_pmu.retired_instructions=2;
 check(!fastsim::task_pmu_coverage_available(s),"an entirely absent target task population is unavailable");
 std::cout<<"task PMU coverage tests passed\n";
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
