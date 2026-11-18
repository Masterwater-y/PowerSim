#pragma once
#include "fastsim/types.hpp"

namespace fastsim {
// Common-end collection retains physical participants which may never run a
// workload task. Complete ownership plus conserved zero counts is available;
// missing ownership is not. At least one workload member is still required.
inline bool task_pmu_coverage_available(const SimulationStats& stats) {
    if(!stats.thread_state_enabled || !stats.thread_state_accounting_complete ||
       stats.thread_state_accounting_contract!="task-oncpu-switch-entry-v1")return false;
    bool has_task=false;
    for(const auto& core:stats.cores) {
        auto conserved=core.task_pmu;conserved+=core.other_task_pmu;
        if(!(conserved==core.full_task_pmu))return false;
        has_task=has_task || core.task_pmu.retired_instructions>0;
    }
    return has_task;
}
}
