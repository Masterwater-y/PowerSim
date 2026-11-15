#define main previous_c4_main
#include "test_spr_store_group_c4.cpp"
#undef main
namespace {
SimulationStats multi_run(std::vector<std::vector<Step>> streams, bool group,
                          unsigned quantum, unsigned budget, const std::string& dram,
                          bool multi = true) {
    auto config=group_config(group,128,budget); config.cores=4;
    config.interval_max_cycles=quantum; config.dram.scheduler=dram;
    if(dram=="frfcfs")config.dram.frfcfs_selection_window=4;
    config.allow_mmio_escape=true; config.spr_store_multi_group=group && multi;
    config.validate(); std::vector<std::unique_ptr<TraceSource>> traces;
    for(auto& stream:streams)traces.push_back(std::make_unique<FunctionalStoreSource>(std::move(stream)));
    Simulator simulator(config,std::move(traces)); return simulator.run();
}
std::vector<Step> delayed_pair(unsigned n,std::uint64_t address) {
    std::vector<Step> v; for(unsigned i=0;i<n;++i)v.push_back({Kind::kAlu,0,i!=0});
    v.push_back({Kind::kStore,address});v.push_back({Kind::kStore,address+8});return v;
}
void same_window_two_groups(const std::string& dram) {
    const std::vector<std::vector<Step>> in{same_line(2),delayed_pair(8,0x50000),{{Kind::kAlu,0}},{{Kind::kAlu,0}}};
    const auto off=multi_run(in,false,4096,16,dram),single=multi_run(in,true,4096,16,dram,false),
               on=multi_run(in,true,4096,16,dram);
    check(single.spr_store_group_candidates==1 && single.spr_store_group_committed==1 &&
          single.total_sequencer().requests+1==off.total_sequencer().requests,
          "multi switch OFF must retain the existing single group behavior");
    check(on.spr_store_group_candidates==2 && on.spr_store_group_committed==2,
          "two different cores must each commit one group in the same Q: candidates="+
          std::to_string(on.spr_store_group_candidates)+" committed="+std::to_string(on.spr_store_group_committed));
    check(on.total_sequencer().requests+2==off.total_sequencer().requests &&
          on.total_core().l1d.accesses+2==off.total_core().l1d.accesses,
          "two distinct followers must each suppress exactly one hierarchy request");
    for(unsigned c=0;c<4;++c)check(on.cores[c].retired_uops==off.cores[c].retired_uops &&
      on.cores[c].perf_retired_stores==off.cores[c].perf_retired_stores,
      "each core must conserve retired operations and stores");
    const auto& a=frame(on,0,0); const auto& b=frame(on,8,1);
    check(a.store_group_id && b.store_group_id && a.store_group_id!=b.store_group_id &&
          a.store_group_window_id==b.store_group_window_id,
          "groups must have distinct identities and belong to one model Q");
}
void conflict_one_group(const std::string& dram) {
    const std::vector<std::vector<Step>> in{same_line(2),delayed_pair(8,0x50000),delayed_load(0x40000,4),{{Kind::kAlu,0}}};
    const auto off=multi_run(in,false,4096,16,dram),on=multi_run(in,true,4096,16,dram);
    check(on.spr_store_group_committed==1 && on.total_sequencer().requests+1==off.total_sequencer().requests,
          "peer same-line load must suppress only the conflicting group");
}
void capacity_blocks_both(const std::string& dram) {
    const std::vector<std::vector<Step>> in{same_line(2),delayed_pair(8,0x50000),{{Kind::kAlu,0}},{{Kind::kAlu,0}}};
    const auto off=multi_run(in,false,4096,1,dram),on=multi_run(in,true,4096,1,dram);
    check(on.spr_store_group_committed==0 && on.total_sequencer().requests==off.total_sequencer().requests,
          "each core's insufficient SQ/credit budget must prevent both groups");
}
void capacity_is_per_core(const std::string& dram) {
    auto pressured=delayed_pair(8,0x60000);
    pressured.pop_back(); // preceding single store, not a candidate group
    pressured.push_back({Kind::kStore,0x50000});
    pressured.push_back({Kind::kStore,0x50008});
    const std::vector<std::vector<Step>> in{same_line(2),pressured,{{Kind::kAlu,0}},{{Kind::kAlu,0}}};
    const auto off=multi_run(in,false,4096,2,dram),on=multi_run(in,true,4096,2,dram);
    check(on.spr_store_group_committed==1 && on.total_sequencer().requests+1==off.total_sequencer().requests,
          "core 1's insufficient budget must not disable core 0's group");
    for(unsigned c=0;c<4;++c)
      check(on.cores[c].retired_uops==off.cores[c].retired_uops &&
            on.cores[c].perf_retired_stores==off.cores[c].perf_retired_stores,
            "per-core capacity fallback must preserve retirement");
}
void future_peer_rolls_back_all(const std::string& dram) {
    const std::vector<std::vector<Step>> in{same_line(2),delayed_pair(8,0x50000),
        delayed_load(0x40000,40),{{Kind::kAlu,0}}};
    const auto off=multi_run(in,false,32,16,dram),on=multi_run(in,true,32,16,dram);
    check(on.spr_store_group_candidates>=2 && on.spr_store_group_future_fallbacks>0 &&
          on.spr_store_group_committed==0,
          "two candidates with an early future peer must exercise the shared rollback");
    check(on.total_sequencer().requests==off.total_sequencer().requests &&
          on.total_core().l1d.accesses==off.total_core().l1d.accesses &&
          on.total_core().perf_l1d_load_fb_hits==off.total_core().perf_l1d_load_fb_hits &&
          on.elapsed_reference_q16==off.elapsed_reference_q16,
          "one unsafe group must roll back the entire batch and preserve OFF behavior");
    for(unsigned c=0;c<4;++c)
      check(on.cores[c].retired_uops==off.cores[c].retired_uops &&
            on.cores[c].perf_retired_stores==off.cores[c].perf_retired_stores,
            "whole-batch fallback must restore each core's population");
}
}
int main(){try{auto invalid=group_config(false,128,16); invalid.spr_store_multi_group=true;
    bool rejected=false;try{invalid.validate();}catch(const std::invalid_argument&){rejected=true;}
    check(rejected,"multi group requires the existing group option");
    for(const std::string dram:{"fcfs","frfcfs"}){same_window_two_groups(dram);conflict_one_group(dram);capacity_blocks_both(dram);capacity_is_per_core(dram);future_peer_rolls_back_all(dram);}}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}std::cout<<"multiple independent store group tests passed\n";}
