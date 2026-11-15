// RED fixture for bounded group membership around ordinary retired UOPs.
// It reuses the real Simulator source from the existing group fixture.
#include "test_spr_store_group_runtime.cpp"

namespace {
SimulationStats neighbor_run(const std::vector<Step>& steps, bool enabled,
                             unsigned budget, const std::string& dram) {
    auto config=group_config(enabled,128,budget);
    config.dram.scheduler=dram;
    if (dram=="frfcfs") config.dram.frfcfs_selection_window=4;
    config.allow_mmio_escape=true;
    config.validate();
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::make_unique<FunctionalStoreSource>(steps));
    Simulator sim(config,std::move(traces)); return sim.run();
}

void check_population(const SimulationStats& off,const SimulationStats& on) {
    check(off.total_core().retired_instructions==on.total_core().retired_instructions &&
          off.total_core().retired_uops==on.total_core().retired_uops &&
          off.total_core().memory_accesses==on.total_core().memory_accesses &&
          off.total_core().perf_retirement.physical.perf_retired_stores==
              on.total_core().perf_retirement.physical.perf_retired_stores,
          "grouping must preserve source UOP, memory and architectural store populations");
}

void group_with_compute_prefix_and_gap(const std::string& dram) {
    const std::vector<Step> steps{{Kind::kAlu,0},{Kind::kAlu,0,true},
        {Kind::kStore,0x40000},{Kind::kAlu,0},{Kind::kAlu,0,true},
        {Kind::kStore,0x40008,true}};
    const auto off=neighbor_run(steps,false,16,dram);
    const auto on=neighbor_run(steps,true,16,dram);
    check_population(off,on);
    check(on.spr_store_group_committed==1 &&
          on.total_sequencer().requests+1==off.total_sequencer().requests,
          "compute-only prefix and gap must permit one real store group");
    const auto& owner=frame(on,2); const auto& follower=frame(on,5);
    check(owner.store_group_id && owner.store_group_id==follower.store_group_id &&
          owner.store_group_offload_cycle>=follower.actual_retire_cycle &&
          owner.store_group_offload_cycle>=follower.actual_completion_cycle &&
          owner.store_group_callback_cycle>=owner.store_group_offload_cycle,
          "group offload must wait for the actual dependent follower");
}

void group_after_earlier_store(const std::string& dram) {
    const std::vector<Step> steps{{Kind::kStore,0x40100},{Kind::kAlu,0},
        {Kind::kStore,0x40000},{Kind::kAlu,0},{Kind::kStore,0x40008}};
    const auto off=neighbor_run(steps,false,16,dram);
    const auto on=neighbor_run(steps,true,16,dram);
    check_population(off,on);
    check(on.spr_store_group_committed==1 &&
          on.total_sequencer().requests+1==off.total_sequencer().requests,
          "earlier distinct-line store must retain its own request while later pair combines");
    const auto& earlier=frame(on,0); const auto& owner=frame(on,2);
    check(owner.store_group_id && owner.store_group_offload_cycle>=earlier.store_sq_release_cycle,
          "group offload must preserve the modeled FIFO predecessor");
}

void no_group_across(Kind boundary,const std::string& dram) {
    const auto address=boundary==Kind::kLoad ? 0x40100ull :
        boundary==Kind::kStore ? 0x100000000000ull : 0ull;
    const std::vector<Step> steps{{Kind::kStore,0x40000},
        {boundary,address},{Kind::kStore,0x40008}};
    const auto off=neighbor_run(steps,false,16,dram);
    const auto on=neighbor_run(steps,true,16,dram);
    check_population(off,on);
    check(on.spr_store_group_committed==0 &&
          on.total_sequencer().requests==off.total_sequencer().requests,
          "load, fence or escaped MMIO between stores must block grouping");
}

void insufficient_budget_falls_back(const std::string& dram) {
    const std::vector<Step> steps{{Kind::kStore,0x40100},{Kind::kAlu,0},
        {Kind::kStore,0x40000},{Kind::kAlu,0},{Kind::kStore,0x40008}};
    const auto off=neighbor_run(steps,false,2,dram);
    const auto on=neighbor_run(steps,true,2,dram);
    check_population(off,on);
    check(on.spr_store_group_committed==0 &&
          on.total_sequencer().requests==off.total_sequencer().requests,
          "prefix stores and both candidate members must fit the shared budget");
}

void cross_core_memory_cuts_group(const std::string& dram) {
    std::vector<Step> left{{Kind::kStore,0x40000}};
    for (unsigned i=0;i<12;++i) left.push_back({Kind::kAlu,0,i!=0});
    left.push_back({Kind::kStore,0x40008,true});
    std::vector<Step> right;
    for (unsigned i=0;i<5;++i) right.push_back({Kind::kAlu,0,i!=0});
    right.push_back({Kind::kLoad,0x50000,true});
    const auto run_pair = [&](bool enabled) {
        auto c=group_config(enabled,128,16); c.cores=2; c.dram.scheduler=dram;
        if (dram=="frfcfs") c.dram.frfcfs_selection_window=4;
        std::vector<std::unique_ptr<TraceSource>> traces;
        traces.push_back(std::make_unique<FunctionalStoreSource>(left));
        traces.push_back(std::make_unique<FunctionalStoreSource>(right));
        Simulator sim(c,std::move(traces)); return sim.run();
    };
    const auto off=run_pair(false), on=run_pair(true);
    const auto& first=frame(on,0,0); const auto& peer=frame(on,5,1);
    const auto& last=frame(on,13,0);
    check(first.actual_issue_cycle < peer.actual_issue_cycle &&
          peer.actual_issue_cycle < last.actual_issue_cycle,
          "fixture must put the peer request between the two store issue edges");
    check(on.spr_store_group_committed==0 &&
          on.total_sequencer().requests==off.total_sequencer().requests &&
          on.total_core().retired_uops==off.total_core().retired_uops,
          "another core's intervening memory event must cut the group");
}
}

int main() {
    try {
        for (const std::string mode : {"fcfs","frfcfs"}) {
            group_with_compute_prefix_and_gap(mode);
            group_after_earlier_store(mode);
            no_group_across(Kind::kLoad,mode);
            no_group_across(Kind::kFence,mode);
            no_group_across(Kind::kStore,mode);
            insufficient_budget_falls_back(mode);
            cross_core_memory_cuts_group(mode);
        }
    } catch(const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
    std::cout << "SPR bounded store neighbor tests passed\n";
    return 0;
}
