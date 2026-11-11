#include "fastsim/simulator.hpp"
#include <stdexcept>
#include <vector>
namespace {
using namespace fastsim;
void require(bool x,const char* msg) {if(!x)throw std::runtime_error(msg);}
class FunctionalTrace final : public TraceSource {
 public:
  explicit FunctionalTrace(unsigned gap,bool secure=false,bool stream=false,bool same_set=false,bool special=false,bool software=false)
      :secure_all_(secure) {
    if(secure)facts.attributes|=fst::kStoreSecure;
    if(stream) {
      add_load(0x2000,0x8140);add_load(0x2004,0x8180);
      for(unsigned i=0;i<5;++i)add_load(0x4000+4*i,0x8000+64*i);
      for(unsigned i=0;i<gap;++i){TraceRecord a;a.pc=0x3000+4*i;a.flags=kRetires;records.push_back(a);}
      if(same_set)add_load(0x5008,0x8100); // distinct line in same set
      if(special)add_load(0x500c,0x8140); // secure demand consumes the mark
      if(software)add_load(0x500d,0x8140); // software prefetch also probes L1
      add_load(0x5000,0x8140);
      for(unsigned i=0;i<gap;++i){TraceRecord a;a.pc=0x6000+4*i;a.flags=kRetires;records.push_back(a);}
      add_load(0x5004,0x8180);
      return;
    }
    add_load(0x2000,0x8100);
    for(unsigned i=0;i<4;++i)add_load(0x1000,0x8000+64*i);
    for(unsigned i=0;i<gap;++i){TraceRecord a;a.pc=0x3000+4*i;a.flags=kRetires;records.push_back(a);}
    add_load(0x1000,0x8100);
  }
  bool next(TraceRecord& out) override {
    if(index==records.size())return false;out=records[index++];frag.clear();
    last_pc_=out.pc;
    if(secure_all_ || out.pc==0x500c)facts.attributes|=fst::kStoreSecure;
    else facts.attributes&=~fst::kStoreSecure;
    if(has_flag(out.flags,kLoad)) {
      fst::TranslationFragment f;f.virtual_address=out.address-0x7000;
      f.physical_address=out.address;f.size=out.size;f.mapping.kind=fst::TranslationKind::kPaged;
      f.mapping.flags=fst::kTranslationLeafKnown;f.mapping.page_bits=12;frag.push_back(f);
    }return true;
  }
  std::string description()const override{return "functional-prefetch-fixture";}
  std::optional<bool> current_ordinary_load_eligible()const override{return true;}
  const fst::StoreRequestFacts* current_memory_facts()const override{return &facts;}
  std::optional<fst::MemoryRequestRole> current_memory_request_role()const override {
    if(last_pc_==0x500d)return fst::MemoryRequestRole::SoftwarePrefetch;
    return std::nullopt;
  }
  const std::vector<fst::TranslationFragment>& current_translation_fragments()const override{return frag;}
 private:
  void add_load(uint64_t pc,uint64_t pa){TraceRecord x;x.pc=pc;x.address=pa;x.size=8;x.flags=kRetires|kLoad|kPhysicalAddress;x.op_class=56;records.push_back(x);}
  std::vector<TraceRecord> records;std::vector<fst::TranslationFragment> frag;size_t index=0;
  bool secure_all_;uint64_t last_pc_=0;
  fst::StoreRequestFacts facts{0,0,0,0,0,0,0,
    fst::kStoreFactsComplete|fst::kMemoryRead|fst::kOrdinaryLoad,0,0};
};
SimulationStats run(bool enabled,unsigned gap,bool secure=false,bool stream=false,bool stream_trace=false,
                    bool line_marks=false,bool same_set=false,bool special=false,bool software=false,
                    bool suppress_issue=false){
 SimulatorConfig c;c.cores=1;c.cha_count=1;c.core_model="interval_weave";c.interval_scheduler="time_epoch";
 c.chunk_instructions=16;c.interval_target_uops=16;c.response_queue_feedback=true;c.response_sparse_scoreboard=true;
 c.spr_l1_prefetch_l2_only=enabled;c.l1d.size_bytes=64;c.l1d.associativity=1;
 c.spr_l1_prefetch_stream_hits=stream;
 c.spr_l1_prefetch_line_marks=line_marks;
 c.spr_l1_prefetch_suppress_issue=suppress_issue;
 if(same_set){c.l1d.size_bytes=128;c.l1d.associativity=2;}
 c.l2.size_bytes=4096;c.l2.associativity=1;c.l2.hit_latency=20;
 std::vector<std::unique_ptr<TraceSource>> traces;traces.push_back(std::make_unique<FunctionalTrace>(gap,secure,stream_trace,same_set,special,software));
 Simulator sim(c,std::move(traces));return sim.run();
}
}
void test_spr_l1_prefetch_runtime(){
 SimulatorConfig limited;limited.spr_l1_prefetch_l2_only=true;
 limited.core_model="interval_weave";limited.interval_scheduler="time_epoch";
 limited.response_queue_feedback=true;
 limited.validate(); // The unmodified base must be accepted before each rejection check.
 const auto rejected=[&](SimulatorConfig c) {
   try { c.validate(); } catch(const std::invalid_argument&) { return true; }
   return false;
 };
 auto orphan=limited;orphan.spr_l1_prefetch_l2_only=false;orphan.spr_l1_prefetch_stream_hits=true;
 require(rejected(orphan),"stream hits require the prefetch service");
 auto orphan_mark=limited;orphan_mark.spr_l1_prefetch_line_marks=true;
 require(rejected(orphan_mark),"line marks require stream-hit tracking");
 auto orphan_suppress=limited;orphan_suppress.spr_l1_prefetch_l2_only=false;
 orphan_suppress.spr_l1_prefetch_suppress_issue=true;
 require(rejected(orphan_suppress),"suppress issue requires active prefetch replay");
 auto bank=limited;bank.spr_load_bank_feedback=true;
 require(rejected(bank),"bank feedback requires separate prefetch integration");
 auto ifetch=limited;ifetch.fetch_supply_lower_hierarchy=true;
 require(rejected(ifetch),"I-fetch shared service has no safe prefetch watermark");
 auto lifecycle=limited;lifecycle.buffer_tso_prepare_lifecycle=true;
 require(rejected(lifecycle),"prepare lifecycle requires separate prefetch integration");
 auto frfcfs=limited;frfcfs.dram.scheduler="frfcfs";
 require(rejected(frfcfs),"FR-FCFS timing repair requires separate prefetch integration");
 auto sequencer=limited;sequencer.ruby_sequencer_max_outstanding=16;
 sequencer.l1d.hit_latency=2;sequencer.l2.hit_latency=2;
 sequencer.validate();
 sequencer.ruby_sequencer_load_admission=true;
 require(rejected(sequencer),"Sequencer load admission has no safe prefetch watermark");
 auto causal=limited;causal.interval_causal_timing=true;
 require(rejected(causal),"causal retiming requires separate prefetch integration");
 auto retime=limited;retime.interval_response_retime=true;
 require(rejected(retime),"response retiming requires separate prefetch integration");
 auto suffix=limited;suffix.interval_rob_head_suffix_replay=true;
 require(rejected(suffix),"suffix replay requires separate prefetch integration");
 const auto off=run(false,160),on=run(true,160);
 const auto no_issue=run(true,160,false,false,false,false,false,false,false,true);
 require(no_issue.spr_prefetch_suppressed>0 && no_issue.spr_prefetch_issued==0 &&
         no_issue.spr_prefetch_completed==0 && no_issue.spr_prefetch_queued_pending==0,
         "suppress issue must drain candidates without generating traffic");
 require(no_issue.spr_prefetch_trained==on.spr_prefetch_trained &&
         no_issue.cores[0].perf_retirement.physical.perf_retired_loads==
         on.cores[0].perf_retirement.physical.perf_retired_loads,
         "suppress issue must retain training and retired demand population");
 require(on.spr_prefetch_issued>0 && on.spr_prefetch_completed>0,"L2-hit prefetch must issue and finish");
 require(on.cores[0].l1d.misses<off.cores[0].l1d.misses,"completed prefetch must change demand L1 result");
 require(on.cores[0].perf_retirement.physical.perf_retired_loads==off.cores[0].perf_retirement.physical.perf_retired_loads,"prefetch is not a retired load");
 const auto early=run(true,0);
 require(early.spr_prefetch_issued>0 && early.spr_prefetch_completed==0,"prefetch cannot publish after retirement ends");
 const auto secure=run(true,160,true);
 require(secure.spr_prefetch_trained==0 && secure.spr_prefetch_issued==0 &&
         secure.spr_prefetch_unsupported_input>0,"secure loads cannot train prefetch");
 const auto stream_off=run(true,160,false,false,true);
 const auto stream=run(true,160,false,true,true);
 require(stream.spr_prefetch_stream_hits>0 && stream.spr_prefetch_issued>1,
         "completed prefetched demand hit must continue the stream");
 require(stream.spr_prefetch_issued>stream_off.spr_prefetch_issued &&
         stream.cores[0].l1d.misses<stream_off.cores[0].l1d.misses,
         "stream continuation must affect actual demand service");
 require(stream.cores[0].perf_retirement.physical.perf_retired_loads==
         stream_off.cores[0].perf_retirement.physical.perf_retired_loads,
         "stream continuation must not create retired loads");
 const auto set_marks=run(true,160,false,true,true,false,true);
 const auto line_marks=run(true,160,false,true,true,true,true);
 require(line_marks.spr_prefetch_stream_hits>set_marks.spr_prefetch_stream_hits &&
         line_marks.spr_prefetch_issued>set_marks.spr_prefetch_issued,
         "line mark must survive a same-set hit on a different way");
 require(line_marks.cores[0].perf_retirement.physical.perf_retired_loads==
         set_marks.cores[0].perf_retirement.physical.perf_retired_loads,
         "line marks must not create retired loads");
 const auto special=run(true,160,false,true,true,true,true,true);
 require(special.spr_prefetch_stream_consumed_not_used>0,
         "unsupported demand must consume its prefetch mark without training");
 const auto software=run(true,160,false,true,true,true,true,false,true);
 require(software.spr_prefetch_stream_consumed_not_used>0,
         "software prefetch L1 probe must consume a mark without training");
}
