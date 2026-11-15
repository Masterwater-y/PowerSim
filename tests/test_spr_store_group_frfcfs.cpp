// Real single-pass FR-FCFS/group integration fixture. The simulator retains
// both retired UOPs; only the explicitly grouped hierarchy follower is absent.
#include "test_spr_store_group_runtime.cpp"

namespace {
SimulationStats fr_run(std::vector<Step> steps, bool enabled, unsigned quantum=4096) {
    auto config=group_config(enabled,128,16);
    config.interval_max_cycles=quantum;
    config.dram.scheduler="frfcfs";
    config.dram.frfcfs_selection_window=4;
    config.validate();
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::make_unique<FunctionalStoreSource>(std::move(steps)));
    Simulator sim(config,std::move(traces));
    return sim.run();
}

void fr_single_group() {
    const auto off=fr_run(same_line(2),false), on=fr_run(same_line(2),true);
    population(off,on,2);
    check(on.spr_store_group_committed==1 &&
          on.total_sequencer().requests+1==off.total_sequencer().requests &&
          on.total_core().l1d.accesses+1==off.total_core().l1d.accesses,
          "FR grouping must suppress exactly one real follower request");
    check(on.dram_frfcfs_single_pass_epochs>0 &&
          on.dram_frfcfs_passes<=on.dram_frfcfs_candidate_epochs,
          "FR scheduling must run one forward pass per candidate, never convergence");
    const auto& owner=frame(on,0), &follower=frame(on,1);
    check(owner.store_group_id && owner.store_group_id==follower.store_group_id &&
          owner.store_group_offload_cycle>=follower.actual_retire_cycle &&
          owner.store_group_callback_cycle>=owner.store_group_offload_cycle,
          "the FR-corrected group callback must follow both members");
}

void fr_future_fallback() {
    auto steps=same_line(2);
    for(unsigned i=0;i<40;++i) steps.push_back({Kind::kAlu,0,i!=0});
    steps.push_back({Kind::kLoad,0x40000,true});
    const auto off=fr_run(steps,false,32), on=fr_run(steps,true,32);
    check(on.spr_store_group_future_fallbacks>0,
          "fixture must cross the final FR-corrected group callback watermark");
    check(on.spr_store_group_fr_discarded_passes>0 &&
          on.spr_store_group_fr_discarded_accepted>0 &&
          on.dram_frfcfs_passes==off.dram_frfcfs_passes &&
          on.dram_frfcfs_single_pass_epochs==off.dram_frfcfs_single_pass_epochs,
          "discarded FR forecast must not count as committed scheduling work");
    check(on.total_sequencer().requests==off.total_sequencer().requests &&
          on.total_core().l1d.accesses==off.total_core().l1d.accesses &&
          on.total_core().retired_uops==off.total_core().retired_uops &&
          on.total_core().perf_l1d_load_fb_hits==off.total_core().perf_l1d_load_fb_hits &&
          on.elapsed_reference_q16==off.elapsed_reference_q16,
          "rejected group must restore real hierarchy, ROI population and timing");
}

void fr_rejects_dvfs() {
    auto config=group_config(true,16,16);
    config.dram.scheduler="frfcfs";
    config.dram.frfcfs_selection_window=4;
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::make_unique<FunctionalStoreSource>(same_line(2)));
    Simulator sim(config,std::move(traces));
    bool rejected=false;
    try { sim.set_core_frequencies({config.reference_frequency_hz-1}); }
    catch (const std::invalid_argument&) { rejected=true; }
    check(rejected,"group FR must reject dynamic frequency changes explicitly");
}

SimulationStats fr_c4(bool enabled, bool atomic_peer, unsigned mshrs) {
    auto c=group_config(enabled,128,16);
    c.cores=4; c.dram.scheduler="frfcfs"; c.dram.frfcfs_selection_window=4;
    c.llc_mshrs=mshrs;
    std::vector<Step> peer;
    for(unsigned i=0;i<8;++i) peer.push_back({Kind::kAlu,0,i!=0});
    peer.push_back({atomic_peer?Kind::kAtomic:Kind::kLoad,0x50000,true});
    if (!atomic_peer) peer.push_back({Kind::kLoad,0x60000});
    const std::vector<std::vector<Step>> streams{same_line(2),peer,
        {{Kind::kAlu,0}},{{Kind::kAlu,0}}};
    std::vector<std::unique_ptr<TraceSource>> traces;
    for(const auto& stream:streams) traces.push_back(std::make_unique<FunctionalStoreSource>(stream));
    Simulator simulator(c,std::move(traces)); return simulator.run();
}

void fr_parallel_fallbacks() {
    const auto credit_off=fr_c4(false,false,1), credit_on=fr_c4(true,false,1);
    check(credit_on.spr_store_group_committed==1 &&
          credit_on.total_sequencer().requests+1==credit_off.total_sequencer().requests &&
          credit_on.dram_frfcfs_fallback_credit_epochs>0,
          "saturated LLC MSHR must reject the FR forecast without erasing any nongroup request");
    const auto atomic_off=fr_c4(false,true,16), atomic_on=fr_c4(true,true,16);
    check(atomic_on.spr_store_group_committed==0 &&
          atomic_on.total_sequencer().requests==atomic_off.total_sequencer().requests &&
          atomic_on.dram_frfcfs_fallback_nonreplayable_epochs>0,
          "non-replayable peer atomic must block grouping and preserve the canonical hierarchy path");
    for (unsigned c=0;c<4;++c) {
        check(credit_on.cores[c].retired_uops==credit_off.cores[c].retired_uops &&
              atomic_on.cores[c].retired_uops==atomic_off.cores[c].retired_uops,
              "FR fallback must conserve retirement on every core");
    }
}
}

int main() {
    try { fr_single_group(); fr_future_fallback(); fr_rejects_dvfs(); fr_parallel_fallbacks(); }
    catch(const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
    std::cout << "SPR store group single-pass FR-FCFS tests passed\n";
    return 0;
}
