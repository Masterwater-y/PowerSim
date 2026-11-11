// Real Simulator coverage for disjoint group-rejection reasons.
#include "test_spr_store_group_runtime.cpp"

namespace {
SimulationStats run_rejection(std::vector<std::vector<Step>> streams, bool enabled) {
    auto c=group_config(enabled,128,16);
    c.cores=static_cast<std::uint32_t>(streams.size());
    c.allow_mmio_escape=true;
    std::vector<std::unique_ptr<TraceSource>> traces;
    for(auto& s:streams) traces.push_back(std::make_unique<FunctionalStoreSource>(std::move(s)));
    Simulator sim(c,std::move(traces)); return sim.run();
}
void same_line_rejected() {
    const auto off=run_rejection({same_line(5)},false);
    const auto on=run_rejection({same_line(5)},true);
    check(on.spr_store_pair_reject_same_line>0 && on.spr_store_pair_reject_peer_special==0,
          "extra same-line event must have its own rejection counter");
    check(on.spr_store_group_committed==0 &&
          on.total_sequencer().requests==off.total_sequencer().requests,
          "same-line rejection diagnostics must not change traffic");
}
void peer_special_rejected(Kind marker) {
    std::vector<Step> peer;
    if(marker==Kind::kAtomic) {
        for(unsigned i=0;i<8;++i)peer.push_back({Kind::kAlu,0,i!=0});
        peer.push_back({marker,0x50000});
    } else peer.push_back({marker,marker==Kind::kStore?0x100000000000ull:0});
    const auto off=run_rejection({same_line(2),peer},false);
    const auto on=run_rejection({same_line(2),peer},true);
    check(on.spr_store_pair_reject_peer_special>0 && on.spr_store_pair_reject_same_line==0,
          "other-core special must be distinguished from line overlap");
    if(marker==Kind::kFence)
        check(on.spr_store_pair_peer_serialize>0,"peer serialize rejection must be counted");
    if(marker==Kind::kStore)
        check(on.spr_store_pair_peer_mmio>0 && on.cores[1].mmio_escape_accesses==1,
              "peer MMIO rejection must reflect an actual escaped request");
    if(marker==Kind::kAtomic)
        check(on.spr_store_pair_peer_atomic>0,"peer atomic rejection must be counted");
    check(on.spr_store_group_committed==0 &&
          on.total_sequencer().requests==off.total_sequencer().requests &&
          on.total_core().retired_uops==off.total_core().retired_uops,
          "special rejection diagnostics must preserve traffic and retirement");
}
}
int main() {
    try { same_line_rejected(); peer_special_rejected(Kind::kFence);
          peer_special_rejected(Kind::kStore); peer_special_rejected(Kind::kAtomic); }
    catch(const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
    std::cout << "SPR store group rejection counter tests passed\n";
    return 0;
}
