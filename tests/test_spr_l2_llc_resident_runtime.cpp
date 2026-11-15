#include "fastsim/simulator.hpp"
#include <iostream>
#include <stdexcept>
#include <vector>
using namespace fastsim;
namespace {
void check(bool x,const char* msg){if(!x)throw std::runtime_error(msg);}
class Trace final : public TraceSource {
 public:
  explicit Trace(bool seed, unsigned gap) {
    if(seed)load(0x1000,0x8040);
    else load(0x1000,0xc040); // keep the same demand population
    for(unsigned i=0;i<3000;++i)compute(); // prior Q and modeled memory response
    load(0x1004,0x8000);
    for(unsigned i=0;i<8;++i)compute();
    load(0x1008,0xa000);
    for(unsigned i=0;i<gap;++i)compute();
    load(0x100c,0x8040);
  }
  bool next(TraceRecord& out) override {
    if(index_==records_.size())return false;
    out=records_[index_++]; fragments_.clear();
    if(has_flag(out.flags,kLoad)) {fst::TranslationFragment f;f.virtual_address=out.address-0x7000;
      f.physical_address=out.address;f.size=out.size;f.mapping.kind=fst::TranslationKind::kPaged;
      f.mapping.flags=fst::kTranslationLeafKnown;f.mapping.page_bits=12;fragments_.push_back(f);}
    return true;
  }
  std::string description()const override{return "llc-resident-prefetch";}
  std::optional<bool> current_ordinary_load_eligible()const override{return true;}
  const fst::StoreRequestFacts* current_memory_facts()const override{return &facts_;}
  const std::vector<fst::TranslationFragment>& current_translation_fragments()const override{return fragments_;}
 private:
  void load(std::uint64_t pc,std::uint64_t address){TraceRecord r;r.pc=pc;r.address=address;r.size=8;
    r.flags=kRetires|kLoad|kPhysicalAddress;r.op_class=56;r.n_dst=1;if(!records_.empty()){r.n_src=1;r.producer_dists[0]=1;}records_.push_back(r);}
  void compute(){TraceRecord r;r.pc=0x3000+4*records_.size();r.flags=kRetires;r.n_dst=1;r.n_src=1;r.producer_dists[0]=1;records_.push_back(r);}
  std::vector<TraceRecord> records_;std::vector<fst::TranslationFragment> fragments_;std::size_t index_=0;
  fst::StoreRequestFacts facts_{0,0,0,0,0,0,0,fst::kStoreFactsComplete|fst::kMemoryRead|fst::kOrdinaryLoad,0,0};
};
SimulationStats run(bool enabled,bool seed,unsigned gap){
 SimulatorConfig c;c.cores=1;c.cha_count=1;c.core_model="interval_weave";c.interval_scheduler="time_epoch";
 c.chunk_instructions=16;c.interval_target_uops=16;c.response_queue_feedback=true;c.response_sparse_scoreboard=true;
 c.spr_l1_prefetch_l2_only=enabled;c.spr_l2_llc_resident_prefetch=enabled;
 c.needs_tso=true;c.tso_store=TsoStoreMode::kBuffer;c.dram.scheduler="frfcfs";
 c.l1d.size_bytes=64;c.l1d.associativity=1;c.l2.size_bytes=64;c.l2.associativity=1;
 c.llc.size_bytes=4096;c.llc.associativity=4;
 std::vector<std::unique_ptr<TraceSource>> ts;ts.push_back(std::make_unique<Trace>(seed,gap));
 return Simulator(c,std::move(ts)).run();
}
}
int main(){
 const auto off=run(false,true,1000);const auto on=run(true,true,1000);const auto absent=run(true,false,1000);
 std::cerr << "issued="<<on.spr_l2_llc_prefetch_issued<<" complete="<<on.spr_l2_llc_prefetch_completed
           <<" missing="<<absent.spr_l2_llc_prefetch_missing<<" on_missing="<<on.spr_l2_llc_prefetch_missing
           <<" uncertified="<<on.spr_l2_llc_prefetch_uncertified<<" dropped="<<on.spr_l2_shared_prefetch_dropped
           <<" l2miss="<<off.cores[0].l2.misses<<"/"<<on.cores[0].l2.misses<<"\n";
 check(on.spr_l2_llc_prefetch_issued>0 && on.spr_l2_llc_prefetch_completed>0,"resident LLC candidate must issue and complete");
 check(absent.spr_l2_llc_prefetch_missing>0,"nonresident candidate must be counted and skipped");
 check(on.spr_l2_llc_prefetch_dram_requests==0,"prefetch must never issue DRAM requests");
 check(on.cha.at(0).dram_reads==off.cha.at(0).dram_reads &&
       on.cha.at(0).dram_writes==off.cha.at(0).dram_writes,
       "LLC-resident prefetch must not add DRAM traffic in the controlled fixture");
 check(on.cores[0].perf_retirement.physical.perf_retired_loads==off.cores[0].perf_retirement.physical.perf_retired_loads,"prefetch must not add retired loads");
 check(on.cores[0].l2.misses<off.cores[0].l2.misses,"completed prefetch must affect a later demand");
}
