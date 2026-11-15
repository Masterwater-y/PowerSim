// Pending C4 integration test. Build separately: it reuses the existing
// single-core runtime fixture in this translation unit.
#include "test_spr_store_group_runtime.cpp"
#include <filesystem>
#include <fstream>

namespace {
SimulationStats c4_run(std::vector<std::vector<Step>> streams, bool enabled,
                       unsigned quantum = 4096, unsigned budget = 16,
                       const std::string& dram_scheduler = "fcfs") {
    auto config = group_config(enabled, 128, budget);
    config.cores = 4;
    config.interval_max_cycles = quantum;
    config.dram.scheduler = dram_scheduler;
    if (dram_scheduler == "frfcfs") config.dram.frfcfs_selection_window = 4;
    config.allow_mmio_escape = true;
    config.validate();
    std::vector<std::unique_ptr<TraceSource>> traces;
    for (auto& stream : streams)
        traces.push_back(std::make_unique<FunctionalStoreSource>(std::move(stream)));
    Simulator simulator(config, std::move(traces));
    return simulator.run();
}

std::vector<Step> delayed_load(std::uint64_t address, unsigned count) {
    std::vector<Step> result;
    for (unsigned i=0;i<count;++i) result.push_back({Kind::kAlu,0,i != 0});
    result.push_back({Kind::kLoad,address,count != 0});
    return result;
}

void c4_unrelated_request() {
    const std::vector<std::vector<Step>> streams{
        same_line(2), delayed_load(0x50000,8),
        {{Kind::kAlu,0}}, {{Kind::kAlu,0}}};
    const auto off=c4_run(streams,false), on=c4_run(streams,true);
    check(on.spr_store_group_candidates == 1 && on.spr_store_group_committed == 1,
          "different-line peer request must permit one actual group");
    check(on.total_sequencer().requests + 1 == off.total_sequencer().requests,
          "the group must remove exactly its follower hierarchy request");
    for (unsigned core=0;core<4;++core)
        check(on.cores.at(core).retired_uops == off.cores.at(core).retired_uops &&
              on.cores.at(core).perf_retired_stores == off.cores.at(core).perf_retired_stores,
              "source retirement and store PMU must be preserved independently per core");
    const auto& owner=frame(on,0,0); const auto& follower=frame(on,1,0);
    check(owner.store_group_id && owner.store_group_id == follower.store_group_id,
          "same-core group identity must be visible");
    check(frame(on,0,1).store_group_id == 0,
          "another core's identical sequence must not inherit the group identity");
}

void c4_peer_same_line() {
    const std::vector<std::vector<Step>> streams{
        same_line(2), delayed_load(0x40000,8),
        {{Kind::kAlu,0}}, {{Kind::kAlu,0}}};
    const auto off=c4_run(streams,false), on=c4_run(streams,true);
    check(on.spr_store_group_committed == 0 &&
          on.total_sequencer().requests == off.total_sequencer().requests,
          "another core's same-line request must reject group suppression");
    for (unsigned core=0;core<4;++core)
        check(on.cores.at(core).retired_uops == off.cores.at(core).retired_uops,
              "same-line rejection must preserve each core's retired population");
}

void c4_future_peer() {
    const std::vector<std::vector<Step>> streams{
        same_line(2), delayed_load(0x40000,40),
        {{Kind::kAlu,0}}, {{Kind::kAlu,0}}};
    const auto off=c4_run(streams,false,32), on=c4_run(streams,true,32);
    check(on.spr_store_group_future_fallbacks > 0,
          "fixture must exercise another core's earlier next-Q memory request");
    check(on.total_sequencer().requests == off.total_sequencer().requests &&
          on.total_core().l1d.accesses == off.total_core().l1d.accesses &&
          on.total_core().perf_l1d_load_fb_hits == off.total_core().perf_l1d_load_fb_hits &&
          on.elapsed_reference_q16 == off.elapsed_reference_q16,
          "cross-core future rejection must restore original timing and PMU populations");
}

void c4_mmio_store_budget(const std::string& dram_scheduler = "fcfs") {
    const std::vector<std::vector<Step>> streams{
        {{Kind::kStore,0x100000000000ull}, {Kind::kStore,0x40000}},
        {{Kind::kAlu,0}}, {{Kind::kAlu,0}}, {{Kind::kAlu,0}}};
    const auto off=c4_run(streams,false,4096,1,dram_scheduler);
    const auto on=c4_run(streams,true,4096,1,dram_scheduler);
    check(on.cores[0].mmio_escape_accesses == 1 &&
          on.cores[0].mmio_escape_accesses == off.cores[0].mmio_escape_accesses,
          "fixture must exercise the real MMIO escape store");
    check(on.cores[0].retired_uops == off.cores[0].retired_uops &&
          on.total_sequencer().requests == off.total_sequencer().requests,
          "MMIO budget must neither create hierarchy requests nor erase retirement");
    const auto& mmio = frame(on,0,0);
    const auto& later = frame(on,1,0);
    check(mmio.has_store && mmio.store_sq_release_cycle >= mmio.actual_retire_cycle + 2 &&
          later.actual_dispatch_cycle >= mmio.store_sq_release_cycle,
          "MMIO store must hold its single shared credit and SQ through the modeled special barrier");
}

class PlanStoreSource final : public TraceSource {
 public:
    PlanStoreSource(): source_({
        {Kind::kAlu,0}, {Kind::kAlu,0}, {Kind::kAlu,0},
        {Kind::kLoad,0x83000}, {Kind::kLoad,0x84000},
        {Kind::kLoad,0x85000}, {Kind::kAlu,0}, {Kind::kAlu,0},
        {Kind::kStore,0x40000}, {Kind::kStore,0x40008}}) {}
    bool next(TraceRecord& record) override {
        if (!source_.next(record)) return false;
        const auto ordinal=*source_.current_source_record_ordinal();
        record.pc=4096+ordinal*4;
        record.set_kernel_mode(ordinal>=2 && ordinal<=6);
        return true;
    }
    std::optional<std::uint64_t> current_source_record_ordinal() const override {
        return source_.current_source_record_ordinal();
    }
    std::optional<FunctionalSyncLayout> functional_sync_layout() const override {
        return FunctionalSyncLayout{10,0,10,0};
    }
    std::uint64_t current_address_space_id() const override { return 1; }
    const fst::StoreRequestFacts* current_store_facts() const override {
        return source_.current_store_facts();
    }
    const fst::StoreRequestFacts* current_memory_facts() const override {
        return source_.current_memory_facts();
    }
    std::string description() const override { return "functional store source with thread-state plan"; }
 private:
    FunctionalStoreSource source_;
};

void fr_group_after_recorded_boundary() {
    const auto dir=std::filesystem::current_path()/"tmp/spr-store-group-fr-scheduler-fixture";
    std::filesystem::create_directories(dir);
    const auto plan=dir/"state.plan";
    {
        std::ofstream out(plan);
        out << "FASTSIM_THREAD_STATE_PLAN_V1\nCORES 1 EVENTS 6 TASKS 1\nCORE 0 10 0 10 101\nTASK 101 101 1\n"
            "EVENT 0 0 0 4096 1 1 101 101 1 0\n"
            "EVENT 1 0 1 4100 2 1 101 1 549755813888 1 0\n"
            "EVENT 2 0 2 4104 6 1 101 303 2 1 1\n"
            "EVENT 3 0 4 4112 7 1 101 1 0 2 1 2\n"
            "EVENT 4 0 6 4120 6 1 303 101 1 1 3\n"
            "EVENT 5 0 7 4124 3 1 101 1 0 2 1 4\n";
    }
    const auto run_plan = [&](bool enabled) {
        auto c=group_config(enabled,16,16);
        c.dram.scheduler="frfcfs"; c.dram.frfcfs_selection_window=4;
        c.native_kernel_trace=true; c.measurement_scope=MeasurementScope::kUserPlusKernel;
        c.syscall_restart_latency=0; c.thread_state_plan=plan.string();
        std::vector<std::unique_ptr<TraceSource>> traces;
        traces.push_back(std::make_unique<PlanStoreSource>());
        Simulator simulator(c,std::move(traces)); return simulator.run();
    };
    const auto off=run_plan(false),on=run_plan(true);
    check(on.thread_state_enabled && on.functional_sync_total_released_events==6 &&
          on.thread_state_task_macro_instructions==off.thread_state_task_macro_instructions &&
          on.cores[0].retired_uops==off.cores[0].retired_uops,
          "FR group must preserve the recorded scheduler plan and retirement");
    check(on.spr_store_group_committed>0 &&
          on.total_sequencer().requests+1==off.total_sequencer().requests &&
          on.dram_frfcfs_single_pass_epochs>0,
          "after the recorded boundary FR must actually process one grouped store pair");
}

void c4_independent_store_budgets() {
    const std::vector<std::vector<Step>> streams{
        {{Kind::kStore,0x100000000000ull}, {Kind::kStore,0x100000001000ull}},
        {{Kind::kStore,0x100000002000ull}},
        {{Kind::kAlu,0}}, {{Kind::kAlu,0}}};
    const auto result=c4_run(streams,true,4096,1);
    const auto& first=frame(result,0,0); const auto& second=frame(result,1,0);
    const auto& other=frame(result,0,1);
    check(result.cores[0].mmio_escape_accesses==2 && result.cores[1].mmio_escape_accesses==1,
          "independent-credit fixture must actually escape MMIO on both cores");
    check(second.actual_dispatch_cycle >= first.store_sq_release_cycle,
          "same-core MMIO stores must share the saturated one-entry budget");
    check(other.actual_dispatch_cycle < first.store_sq_release_cycle,
          "one core's occupied store credit must not block another core's admission");
}

void c4_warmup_credit_isolation() {
    const std::vector<std::vector<Step>> streams{
        {{Kind::kStore,0x40000}, {Kind::kStore,0x50000}},
        {{Kind::kStore,0x100000000000ull}, {Kind::kStore,0x60000}},
        {{Kind::kAlu,0}, {Kind::kAlu,0}},
        {{Kind::kAlu,0}, {Kind::kAlu,0}}};
    auto c=group_config(true,16,1);
    c.cores=4; c.allow_mmio_escape=true; c.interval_max_cycles=32;
    c.functional_warmup_interval_max_cycles=32; c.validate();
    std::vector<std::unique_ptr<TraceSource>> traces;
    for (const auto& stream : streams) {
        auto raw=std::make_unique<FunctionalStoreSource>(stream);
        traces.push_back(std::make_unique<WarmupInstructionTraceSource>(
            std::move(raw),1,1,1,1,true));
    }
    Simulator simulator(c,std::move(traces)); const auto result=simulator.run();
    const auto& live=result.functional_warmup_store_credits_live_by_core;
    const auto& releases=result.functional_warmup_store_credit_next_release_by_core;
    check(live.size()==4 && releases.size()==4 && live[0]==1 && live[1]==0 &&
          releases[0] > result.functional_warmup_barrier_cycles && releases[1]==0,
          "fixture must carry only core0's long-latency warmup store credit");
    check(frame(result,1,0).actual_dispatch_cycle >= releases[0],
          "core0 ROI store must respect its warmup callback");
    check(frame(result,1,1).actual_dispatch_cycle < releases[0],
          "core0's warmup credit must not gate core1 ROI admission");
    for (unsigned core=0;core<4;++core)
        check(result.cores[core].retired_uops==1,
              "only one measured UOP per core must count after warmup");
}

void recorded_scheduler_preserved() {
    const auto dir=std::filesystem::current_path()/"tmp/spr-store-group-scheduler-fixture";
    std::filesystem::create_directories(dir);
    const auto fst=dir/"core0.fst", plan=dir/"state.plan";
    {
        BinaryTraceWriter writer(fst.string(),0);
        writer.set_address_space_id(1);
        for (unsigned i=0;i<9;++i) {
            TraceRecord r; r.pc=4096+i*4; r.set_kernel_mode(i>=2 && i<=6);
            if (i==3 || i==5 || i==8) {
                r.flags |= kLoad | kPhysicalAddress; r.address=0x80000+i*4096; r.size=8;
            }
            writer.append(r);
        }
        std::ofstream out(plan);
        out << "FASTSIM_THREAD_STATE_PLAN_V1\nCORES 1 EVENTS 6 TASKS 1\nCORE 0 9 0 9 101\nTASK 101 101 1\n"
            "EVENT 0 0 0 4096 1 1 101 101 1 0\n"
            "EVENT 1 0 1 4100 2 1 101 1 549755813888 1 0\n"
            "EVENT 2 0 2 4104 6 1 101 303 2 1 1\n"
            "EVENT 3 0 4 4112 7 1 101 1 0 2 1 2\n"
            "EVENT 4 0 6 4120 6 1 303 101 1 1 3\n"
            "EVENT 5 0 7 4124 3 1 101 1 0 2 1 4\n";
    }
    const auto run_plan = [&](bool enabled) {
        auto c=group_config(enabled,7,16);
        c.native_kernel_trace=true; c.measurement_scope=MeasurementScope::kUserPlusKernel;
        c.syscall_restart_latency=0; c.thread_state_plan=plan.string();
        std::vector<std::unique_ptr<TraceSource>> traces;
        traces.push_back(std::make_unique<BinaryTraceSource>(fst.string()));
        Simulator simulator(c,std::move(traces)); return simulator.run();
    };
    const auto off=run_plan(false),on=run_plan(true);
    check(on.thread_state_enabled && on.functional_sync_total_released_events==6 &&
          on.thread_state_task_macro_instructions==off.thread_state_task_macro_instructions &&
          on.thread_state_active_reference_q16==off.thread_state_active_reference_q16 &&
          on.cores[0].retired_uops==off.cores[0].retired_uops &&
          on.total_sequencer().requests==off.total_sequencer().requests,
          "recorded scheduler events and task accounting must remain active and conserved");
}
}

int main() {
    try { c4_unrelated_request(); c4_peer_same_line(); c4_future_peer(); c4_mmio_store_budget();
          c4_mmio_store_budget("frfcfs"); c4_independent_store_budgets(); c4_warmup_credit_isolation();
          recorded_scheduler_preserved(); fr_group_after_recorded_boundary(); }
    catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
    std::cout << "SPR C4 store group runtime tests passed\n";
    return 0;
}
