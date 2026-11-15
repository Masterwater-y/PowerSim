// Three/four-member real Simulator fixtures.
#include "test_spr_store_group_runtime.cpp"

namespace {
SimulationStats run_stores(const std::vector<Step>& steps,bool enabled,
                           unsigned capacity,const std::string& dram,unsigned chunk=128) {
    auto c=group_config(enabled,chunk,capacity);
    c.dram.scheduler=dram; c.allow_mmio_escape=true;
    if(dram=="frfcfs") c.dram.frfcfs_selection_window=4;
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::make_unique<FunctionalStoreSource>(steps));
    Simulator sim(c,std::move(traces)); return sim.run();
}
void check_source_population(const SimulationStats& a,const SimulationStats& b) {
    check(a.total_core().retired_instructions==b.total_core().retired_instructions &&
          a.total_core().retired_uops==b.total_core().retired_uops &&
          a.total_core().memory_accesses==b.total_core().memory_accesses &&
          a.total_core().perf_retired_stores==b.total_core().perf_retired_stores &&
          a.total_core().perf_retirement.physical.perf_retired_stores==
              b.total_core().perf_retirement.physical.perf_retired_stores,
          "all source and retired-store populations must be conserved");
}
void group_n(unsigned n,const std::string& dram) {
    const auto steps=same_line(n);
    const auto off=run_stores(steps,false,16,dram),on=run_stores(steps,true,16,dram);
    check_source_population(off,on);
    check(on.spr_store_group_committed==1 &&
          on.spr_store_group_committed_members==n &&
          (n==3 ? on.spr_store_group_size3==1 : on.spr_store_group_size4==1) &&
          on.total_sequencer().requests+n-1==off.total_sequencer().requests &&
          on.total_core().l1d.accesses+n-1==off.total_core().l1d.accesses,
          "one group must replace every follower hierarchy request exactly once");
    const auto& owner=frame(on,0); std::uint64_t latest=0;
    for(unsigned i=0;i<n;++i) {
        const auto& member=frame(on,i);
        check(owner.store_group_id && member.store_group_id==owner.store_group_id &&
              member.store_group_callback_cycle==owner.store_group_callback_cycle &&
              member.store_group_offload_cycle==owner.store_group_offload_cycle,
              "all members need the same committed group identity and edges");
        latest=std::max({latest,member.actual_retire_cycle,member.actual_completion_cycle});
        check(member.store_group_credit_release_cycle >=
              (i==0?owner.store_group_callback_cycle:owner.store_group_offload_cycle),
              "each member must hold its own credit until its modeled final edge");
    }
    check(owner.store_group_offload_cycle>=latest &&
          owner.store_send_cycle>=owner.store_group_offload_cycle &&
          owner.store_group_callback_cycle>owner.store_send_cycle,
          "offload/send/callback must follow every member's completion and retirement");
}
void negative(const std::vector<Step>& steps,unsigned capacity,const std::string& dram,
              const std::string& reason,unsigned chunk=128) {
    const auto off=run_stores(steps,false,capacity,dram,chunk);
    const auto on=run_stores(steps,true,capacity,dram,chunk);
    check_source_population(off,on);
    check(on.spr_store_group_committed==0 &&
          on.total_sequencer().requests==off.total_sequencer().requests,
          reason+": rejection must retain all hierarchy requests");
}

void group_after_prefix_and_compute_gaps(const std::string& dram) {
    const std::vector<Step> steps{{Kind::kStore,0x40100},{Kind::kAlu,0},
        {Kind::kStore,0x40000},{Kind::kAlu,0},{Kind::kStore,0x40008,true},
        {Kind::kAlu,0},{Kind::kStore,0x40010,true}};
    const auto off=run_stores(steps,false,16,dram),on=run_stores(steps,true,16,dram);
    check_source_population(off,on);
    check(on.spr_store_group_committed==1 &&
          on.total_sequencer().requests+2==off.total_sequencer().requests,
          "prefix store and compute-only gaps must preserve one genuine three-store group");
    const auto& previous=frame(on,0); const auto& owner=frame(on,2); const auto& last=frame(on,6);
    check(owner.store_group_id && owner.store_group_id==last.store_group_id &&
          owner.store_group_offload_cycle>=last.actual_completion_cycle &&
          owner.store_group_offload_cycle>=previous.store_sq_release_cycle,
          "the final dependent member and preceding FIFO store bound group offload");
}

void peer_same_line_blocks(const std::string& dram) {
    std::vector<Step> peer;
    for(unsigned i=0;i<8;++i) peer.push_back({Kind::kAlu,0,i!=0});
    peer.push_back({Kind::kLoad,0x40010,true});
    const auto run = [&](bool enabled) {
        auto c=group_config(enabled,128,16); c.cores=2; c.dram.scheduler=dram;
        if(dram=="frfcfs") c.dram.frfcfs_selection_window=4;
        std::vector<std::unique_ptr<TraceSource>> traces;
        traces.push_back(std::make_unique<FunctionalStoreSource>(same_line(3)));
        traces.push_back(std::make_unique<FunctionalStoreSource>(peer));
        Simulator sim(c,std::move(traces));return sim.run();
    };
    const auto off=run(false),on=run(true);
    check_source_population(off,on);
    check(on.spr_store_group_committed==0 &&
          on.total_sequencer().requests==off.total_sequencer().requests,
          "peer same-line load must prevent a three-store group");
}

void fr_final_watermark_rollback() {
    auto steps=same_line(3);
    for(unsigned i=0;i<40;++i) steps.push_back({Kind::kAlu,0,i!=0});
    steps.push_back({Kind::kLoad,0x40000,true});
    const auto run=[&](bool enabled) {
        auto c=group_config(enabled,128,16); c.dram.scheduler="frfcfs";
        c.dram.frfcfs_selection_window=4; c.interval_max_cycles=32;
        std::vector<std::unique_ptr<TraceSource>> traces;
        traces.push_back(std::make_unique<FunctionalStoreSource>(steps));
        Simulator sim(c,std::move(traces));return sim.run();
    };
    const auto off=run(false),on=run(true);
    check_source_population(off,on);
    check(on.spr_store_group_future_fallbacks>0 &&
          on.spr_store_group_fr_discarded_accepted>0,
          "fixture must actually discard an accepted FR forecast after final group watermark");
    check(on.total_sequencer().requests==off.total_sequencer().requests &&
          on.total_core().l1d.accesses==off.total_core().l1d.accesses &&
          on.total_core().perf_l1d_load_fb_hits==off.total_core().perf_l1d_load_fb_hits &&
          on.elapsed_reference_q16==off.elapsed_reference_q16 &&
          on.dram_frfcfs_passes==off.dram_frfcfs_passes,
          "rolled-back three-store forecast must restore subsequent same-line and FR state");
}

class MarkedSource final : public TraceSource {
 public:
    explicit MarkedSource(bool precise)
      : source_({{Kind::kStore,0x40000},{Kind::kAlu,0},{Kind::kAlu,0},
                 {Kind::kStore,0x40008},{Kind::kStore,0x40010}}), precise_(precise) {}
    bool next(TraceRecord& record) override {
        if(!source_.next(record)) return false;
        if(precise_ && source_.current_source_record_ordinal()==1) record.flags|=kMicroOp;
        return true;
    }
    std::optional<std::uint64_t> current_source_record_ordinal() const override {
        return source_.current_source_record_ordinal();
    }
    const fst::StoreRequestFacts* current_store_facts() const override { return source_.current_store_facts(); }
    const fst::StoreRequestFacts* current_memory_facts() const override { return source_.current_memory_facts(); }
    bool current_hardware_entry() const override {
        return !precise_ && source_.current_source_record_ordinal()==2;
    }
    bool current_precise_fault_abandonment() const override {
        return precise_ && source_.current_source_record_ordinal()==2;
    }
    std::string description() const override { return "3-member functional exception fixture"; }
 private:
    FunctionalStoreSource source_; bool precise_;
};

void exceptional_gap_rejects(bool precise,const std::string& dram) {
    const auto run=[&](bool enabled) {
        auto c=group_config(enabled,128,16); c.dram.scheduler=dram;
        if(dram=="frfcfs") c.dram.frfcfs_selection_window=4;
        std::vector<std::unique_ptr<TraceSource>> traces;
        traces.push_back(std::make_unique<MarkedSource>(precise));
        Simulator sim(c,std::move(traces));return sim.run();
    };
    const auto off=run(false),on=run(true);
    check_source_population(off,on);
    check(on.spr_store_group_committed==0 &&
          on.total_sequencer().requests==off.total_sequencer().requests,
          "hardware entry or precise fault inside the run must reject the group");
}
}
int main() {
    try {
        for(const std::string dram : {"fcfs","frfcfs"}) {
            group_n(3,dram); group_n(4,dram);
            group_after_prefix_and_compute_gaps(dram);
            peer_same_line_blocks(dram);
            exceptional_gap_rejects(false,dram);
            exceptional_gap_rejects(true,dram);
            negative(same_line(5),16,dram,"fifth same-line store");
            negative(same_line(4),3,dram,"four members exceed shared credit budget");
            negative(same_line(4),16,dram,"members cross producer chunks",2);
            negative({{Kind::kStore,0x40000},{Kind::kStore,0x40008},
                      {Kind::kUnknownStore,0x40010}},16,dram,"unknown facts");
            negative({{Kind::kStore,0x40000},{Kind::kStore,0x40008},
                      {Kind::kOtherContext,0x40010}},16,dram,"different context");
            negative({{Kind::kStore,0x40000},{Kind::kFence,0},
                      {Kind::kStore,0x40008},{Kind::kStore,0x40010}},16,dram,"fence");
            negative({{Kind::kStore,0x40000},{Kind::kLoad,0x40004},
                      {Kind::kStore,0x40008},{Kind::kStore,0x40010}},16,dram,"same-line load");
            negative({{Kind::kStore,0x40000},{Kind::kStore,0x100000000000ull},
                      {Kind::kStore,0x40008},{Kind::kStore,0x40010}},16,dram,"MMIO");
            negative({{Kind::kStore,0x40000},{Kind::kAtomic,0x50000},
                      {Kind::kStore,0x40008},{Kind::kStore,0x40010}},16,dram,"atomic");
        }
        fr_final_watermark_rollback();
    } catch(const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
    std::cout << "SPR 3/4-member store-group tests passed\n";
    return 0;
}
