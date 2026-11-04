#include "fastsim/functional_sync.hpp"
#include "fastsim/simulator.hpp"
#include "fastsim/trace.hpp"
#include <filesystem>
#include <fstream>
#include <stdexcept>

namespace {
void require_scheduler(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
}

void test_recorded_scheduler_integration() {
    const auto dir = std::filesystem::current_path() / "tmp/recorded-scheduler/integration-test";
    std::filesystem::create_directories(dir);
    const auto path = dir / "certified.plan";
    {
        std::ofstream out(path);
        out << "FASTSIM_THREAD_STATE_PLAN_V1\nCORES 2 EVENTS 2 TASKS 2\n"
            "CORE 0 3 0 3 101\nCORE 1 100 0 100 202\n"
            "TASK 101 101 1\nTASK 202 202 2\n"
            "ACCOUNTING task-oncpu-switch-entry-v1\nCLOSURE 0 1\nCLOSURE 1 1\n"
            "EVENT 0 0 0 4096 1 1 101 101 1 0\n"
            "EVENT 1 1 0 8192 1 1 202 202 2 0\n";
    }
    fastsim::FunctionalSyncPlan plan(path.string(), 2);
    require_scheduler(plan.event_count() == 2, "certified plan retains events");
    require_scheduler(plan.accounting_contract() == "task-oncpu-switch-entry-v1", "accounting contract retained");
    require_scheduler(plan.owner_continuity_to_common_cut() == std::vector<bool>({true,true}), "closure evidence retained");
    require_scheduler(plan.execution_boundary_count()==0, "local identity observations do not fragment execution");
    for (unsigned core=0; core<2; ++core) {
        fastsim::BinaryTraceWriter writer((dir / ("core"+std::to_string(core)+".fst")).string(),core);
        writer.set_address_space_id(1);
        for (unsigned i=0; i<(core?100u:3u); ++i) {
            fastsim::TraceRecord record;
            record.pc=(core?8192:4096)+i*4;
            record.set_kernel_mode(core ? (i>0 && i<99) : i==2);
            writer.append(record);
        }
    }
    const auto run=[&](const std::filesystem::path& state_path, const std::filesystem::path& sync_path={}, bool batch=true) {
        fastsim::SimulatorConfig config;
        config.cores=2; config.cha_count=1; config.core_model="interval_weave";
        config.interval_scheduler="time_epoch";
        config.native_kernel_trace=true;
        config.syscall_restart_latency=0;
        config.measurement_scope=fastsim::MeasurementScope::kUserPlusKernel;
        config.thread_state_plan=state_path.string();
        config.functional_sync_plan=sync_path.string();
        config.scheduler_batch_observations=batch;
        config.chunk_instructions=17; config.interval_target_uops=17;
        std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
        for (unsigned core=0; core<2; ++core)
            traces.push_back(std::make_unique<fastsim::BinaryTraceSource>((dir / ("core"+std::to_string(core)+".fst")).string()));
        fastsim::Simulator simulation(config,std::move(traces));
        return simulation.run();
    };
    const auto certified=run(path);
    require_scheduler(certified.thread_state_accounting_complete, "certified owner coverage makes active available");
    require_scheduler(certified.thread_state_task_macro_instructions==103, "independent task macro denominator");
    require_scheduler(certified.scheduler_passive_observations==2 && certified.scheduler_execution_boundaries==0,
                      "passive observations are validated without producer suspension");
    for (unsigned core=0; core<2; ++core) {
        require_scheduler(certified.thread_state_unknown_reference_q16[core]==0, "certified common tail has known owner");
        require_scheduler(certified.thread_state_active_reference_q16[core]==certified.elapsed_reference_q16, "all target-task owner time closes at common end");
    }
    const auto legacy_path=dir/"legacy.plan";
    const auto unbatched=run(path,{},false);
    require_scheduler(unbatched.thread_state_accounting_complete &&
                      unbatched.thread_state_task_macro_instructions==103 &&
                      unbatched.scheduler_execution_boundaries==2 &&
                      unbatched.scheduler_passive_observations==0,
                      "batching ablation keeps the certified accounting contract and source anchors");
    {
        std::ifstream input(path);
        std::string content((std::istreambuf_iterator<char>(input)),{});
        const std::string certificate="ACCOUNTING task-oncpu-switch-entry-v1\nCLOSURE 0 1\nCLOSURE 1 1\n";
        content.erase(content.find(certificate),certificate.size());
        std::ofstream output(legacy_path);output << content;
    }
    const auto legacy=run(legacy_path);
    require_scheduler(!legacy.thread_state_accounting_complete && legacy.thread_state_unknown_reference_q16[0]>0,
                      "uncertified source retains conservative unknown tail");
    const auto sync_path=dir/"sync.plan";
    {
        std::ofstream out(sync_path);
        out << "FASTSIM_FUNCTIONAL_SYNC_PLAN_V1\nCORES 2 EVENTS 2\n"
            "CORE 0 3 0 3\nCORE 1 100 0 100\n"
            "EVENT 0 0 1 4100 1 5 1 0\n"
            "EVENT 1 1 99 8588 1 6 1 1 0\n";
    }
    const auto combined=run(path,sync_path);
    require_scheduler(combined.functional_sync_enabled && combined.thread_state_enabled,
                      "functional sync and logical ownership coexist");
    require_scheduler(combined.functional_sync_total_released_events==4 && combined.thread_state_total_released_events==2,
                      "combined scheduler keeps event domains distinct");
    require_scheduler(combined.thread_state_accounting_complete && combined.scheduler_execution_boundaries==2,
                      "barrier constraints retain permission handoff while identity remains passive");
    // Functional task accounting cuts bracket the assembly ownership update.
    // The transition instructions belong to neither eligible task clock.
    const auto clock_path=dir/"task-clock.plan";
    {
        std::ofstream out(clock_path);
        out << "FASTSIM_THREAD_STATE_PLAN_V1\nCORES 2 EVENTS 8 TASKS 2\n"
            "CORE 0 12 0 12 101\nCORE 1 100 0 100 202\n"
            "TASK 101 101 1\nTASK 202 202 2\n"
            "ACCOUNTING task-oncpu-switch-entry-v1\nCLOSURE 0 1\nCLOSURE 1 1\n"
            "TASK_CLOCK task-lifecycle-basecpu-active-v1\nCLOCK_STATE 0 1 1 101 1\nCLOCK_STATE 1 1 1 202 1\n"
            "EVENT 0 0 0 4096 1 1 101 101 1 0\n"
            "EVENT 1 0 3 " << 0xffffffff81517a0aULL << " 8 1 101 303 0 1 0\n"
            "EVENT 2 0 4 4112 6 1 101 303 1 1 1\n"
            "EVENT 3 0 6 " << 0xffffffff81067eefULL << " 9 1 101 303 0 1 2\n"
            "EVENT 4 0 8 " << 0xffffffff81517a0aULL << " 8 1 303 101 0 1 3\n"
            "EVENT 5 0 9 4132 6 1 303 101 1 1 4\n"
            "EVENT 6 0 10 " << 0xffffffff81067eefULL << " 9 1 303 101 0 1 5\n"
            "EVENT 7 1 0 8192 1 1 202 202 2 0\n";
    }
    {
        fastsim::BinaryTraceWriter writer((dir/"core0.fst").string(),0);
        writer.set_address_space_id(1);
        for(unsigned i=0;i<12;++i) {
            fastsim::TraceRecord record;record.pc=4096+i*4;record.set_kernel_mode(i>0);
            if(i==3||i==8)record.pc=0xffffffff81517a0aULL;
            if(i==6||i==10)record.pc=0xffffffff81067eefULL;
            writer.append(record);
        }
    }
    const auto clock=run(clock_path);
    require_scheduler(clock.task_clock_enabled && clock.task_clock_complete,
                      "paired lifecycle cuts establish independently computed model eligibility");
    require_scheduler(clock.task_clock_macro_instructions==105,
                      "task clock denominator excludes out-to-in transition instructions");
    require_scheduler(clock.task_clock_macro_instructions_by_core==std::vector<std::uint64_t>({5,100}),
                      "lifecycle denominator retains exact per-core populations");
    require_scheduler(clock.thread_state_task_macro_instructions==107,
                      "assembly owner population remains separate from lifecycle clock population");
    require_scheduler(clock.cores[0].full_task_pmu.retired_instructions==12,
                      "legacy physical retirement population is preserved");
    require_scheduler(clock.task_clock_transition_q16[0]>0 && clock.task_clock_active_q16[0]>0 &&
                      clock.task_clock_other_q16[0]>0 && clock.task_clock_unknown_q16[0]==0,
                      "model computed clocks retain active, transition and other buckets separately");

}
