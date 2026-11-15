// Candidate-only line-shape accounting, independent of actual group acceptance.
#include "test_spr_store_group_runtime.cpp"

namespace {
SimulationStats shape_run(std::vector<std::vector<Step>> streams) {
    auto c=group_config(true,128,16); c.cores=static_cast<std::uint32_t>(streams.size());
    std::vector<std::unique_ptr<TraceSource>> traces;
    for(auto& s:streams) traces.push_back(std::make_unique<FunctionalStoreSource>(std::move(s)));
    Simulator sim(c,std::move(traces)); return sim.run();
}
void own_store_shape(unsigned n) {
    const auto result=shape_run({same_line(n)});
    check(result.spr_store_line_own_store>0 && result.spr_store_line_all_own_stores>0,
          "extra same-core certified stores must be classified");
    if(n==3) check(result.spr_store_line_run_3>0,"three adjacent stores must produce a three-line-shape candidate");
    if(n==4) check(result.spr_store_line_run_4>0,"four adjacent stores must produce a four-line-shape candidate");
    if(n==5) check(result.spr_store_pair_reject_same_line>0 && result.spr_store_group_committed==0 &&
                   result.spr_store_line_run_3==0 && result.spr_store_line_run_4==0,
                   "five stores must not be counted as a bounded three/four shape");
}
void own_read_and_unknown() {
    const auto read=shape_run({{{Kind::kStore,0x40000},{Kind::kStore,0x40008},
                                {Kind::kLoad,0x40010}}});
    check(read.spr_store_line_own_load>0 && read.spr_store_line_all_own_stores==0,
          "same-line own load must be identified and excluded from store-only shape");
    const auto unknown=shape_run({{{Kind::kStore,0x40000},{Kind::kStore,0x40008},
                                   {Kind::kUnknownStore,0x40010}}});
    check(unknown.spr_store_line_own_unknown>0 && unknown.spr_store_line_all_own_stores==0,
          "unknown store facts must remain unknown rather than becoming ordinary");
}
void peer_read_and_interleave() {
    std::vector<Step> peer;
    for(unsigned i=0;i<8;++i) peer.push_back({Kind::kAlu,0,i!=0});
    peer.push_back({Kind::kLoad,0x40010,true});
    const auto result=shape_run({same_line(2),peer});
    check(result.spr_store_line_prefilter_peer>0 && result.spr_store_line_prefilter_peer_load>0 &&
          result.spr_store_line_all_own_stores==0,
          "peer same-line load must be identified at the earlier permission prefilter");
    const auto cut=shape_run({{{Kind::kStore,0x40000},{Kind::kAlu,0},
                               {Kind::kLoad,0x40100,true},{Kind::kAlu,0,true},
                               {Kind::kStore,0x40008,true},{Kind::kStore,0x40010}}});
    check(cut.spr_store_line_all_own_stores>0 && cut.spr_store_line_run_3==0,
          "an intervening different-line batch event prevents a contiguous group shape: all="+
          std::to_string(cut.spr_store_line_all_own_stores)+" run3="+
          std::to_string(cut.spr_store_line_run_3));
}
}
int main() {
    try { own_store_shape(3); own_store_shape(4); own_store_shape(5);
          own_read_and_unknown(); peer_read_and_interleave(); }
    catch(const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
    std::cout << "SPR same-line shape counter tests passed\n";
    return 0;
}
