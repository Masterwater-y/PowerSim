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
    const auto run=[&](const std::filesystem::path& state_path, const std::filesystem::path& sync_path={}, bool batch=true, bool warmup=false) {
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
        for (unsigned core=0; core<2; ++core) {
            auto source=std::make_unique<fastsim::BinaryTraceSource>((dir / ("core"+std::to_string(core)+".fst")).string());
            if(warmup) {
                const auto records=source->record_count();
                traces.push_back(std::make_unique<fastsim::WarmupInstructionTraceSource>(
                    std::move(source),4,records-4,4,records-4,true));
            } else traces.push_back(std::move(source));
        }
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
        require_scheduler(certified.thread_state_active_reference_q16[core]==certified.cores[core].cycles*65536, "task owner clock stops at local accepted completion");
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
    require_scheduler(!legacy.thread_state_accounting_complete && legacy.thread_state_unknown_reference_q16[0]==0,
                      "uncertified source stays incomplete without inventing tail duration");
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

    require_scheduler(clock.task_clock_completion_reference_q16.size()==2,
                      "model exposes per-core retirement completion boundaries");
    for(unsigned core=0;core<2;++core) {
        require_scheduler(clock.task_clock_active_q16[core]+clock.task_clock_other_q16[core]+
                          clock.task_clock_transition_q16[core]+clock.task_clock_unknown_q16[core]==
                          clock.task_clock_completion_reference_q16[core],
                          "clock buckets partition only the provided local stream, without global padding");
    }

    const auto cross_roi_path=dir/"cross-roi-wait.plan";
    {
        std::ofstream out(cross_roi_path);
        out << "FASTSIM_THREAD_STATE_PLAN_V1\nCORES 2 EVENTS 9 TASKS 2\n"
            "CORE 0 12 4 8 101\nCORE 1 12 4 8 202\nTASK 101 101 1\nTASK 202 202 2\n"
            "ACCOUNTING task-oncpu-switch-entry-v1\nCLOSURE 0 1\nCLOSURE 1 1\n"
            "EVENT 0 0 1 4100 1 0 101 101 1 0\n"
            "EVENT 1 0 2 4104 2 0 101 7 549755813888 1 0\n"
            "EVENT 2 0 3 4108 6 0 101 0 2 1 1\n"
            "EVENT 3 0 7 4124 6 1 0 101 1 2 2 7\n"
            "EVENT 4 0 8 4128 3 1 101 7 0 2 1 3\n"
            "EVENT 5 1 1 4100 1 0 202 202 2 0\n"
            "EVENT 6 1 4 4112 4 1 202 9 554050781184 1 5\n"
            "EVENT 7 1 5 4116 7 1 101 7 1 3 1 2 6\n"
            "EVENT 8 1 6 4120 5 1 202 9 1 2 6 7\n";
    }
    for(unsigned core=0;core<2;++core) {
        fastsim::BinaryTraceWriter writer((dir/("core"+std::to_string(core)+".fst")).string(),core);
        writer.set_address_space_id(1);
        for(unsigned i=0;i<12;++i) {
            fastsim::TraceRecord record;record.pc=4096+i*4;record.set_kernel_mode(i>1);writer.append(record);
        }
    }
    const auto cross_roi=run(cross_roi_path,{},true,true);
    require_scheduler(cross_roi.functional_warmup_enabled && cross_roi.thread_state_accounting_complete,
                      "warmup blocked ownership and released causal history survive the actual ROI seam");
    require_scheduler(cross_roi.cores[0].retired_instructions==8 && cross_roi.cores[1].retired_instructions==8 &&
                      cross_roi.thread_state_task_macro_instructions==12,
                      "ROI excludes warmup and preserves idle owner until recorded switch-in");
    const fastsim::FunctionalSyncAudit* roi_wake=nullptr;
    const fastsim::FunctionalSyncAudit* roi_switch=nullptr;
    for(const auto& event:cross_roi.functional_sync_audit) {
        if(event.core==1&&event.kind==7)roi_wake=&event;
        if(event.core==0&&event.kind==6)roi_switch=&event;
    }
    require_scheduler(roi_wake && roi_switch && roi_switch->predecessor_ready_reference_q16>=roi_wake->release_reference_q16,
                      "ROI switch-in keeps the wake dependency whose wait and block began during warmup");

}
