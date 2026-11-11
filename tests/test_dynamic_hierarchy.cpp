#ifdef NDEBUG
#undef NDEBUG
#endif
#include "fastsim/simulator.hpp"
#include "fastsim/trace.hpp"
#include <cassert>
#include <iostream>
using namespace fastsim;
struct Source : TraceSource {
 unsigned at=0,repeats=1;bool cross=false,write=false,follower=false,atomic=false;std::vector<fst::TranslationFragment> facts;
 explicit Source(bool x,bool w=false,bool f=false):cross(x),write(w),follower(f){}
 bool complete_dependencies() const override{return true;}
 bool dynamic_translations() const override{return true;}
 const std::vector<fst::TranslationFragment>& current_translation_fragments() const override{return facts;}
 std::string description() const override{return "dynamic hierarchy fixture";}
 uint64_t current_address_space_id() const override{return 0x1000;}
 const StaticInstructionInfo* static_instruction(uint64_t) const override {
  static const auto locked=[] {StaticInstructionInfo i;i.pc=0x400000;i.size=4;
   i.flags=kStaticMemory|kStaticReadBarrier|kStaticWriteBarrier|kStaticLockedRmw;return i;}();
  return atomic?&locked:nullptr;
 }
 bool next(TraceRecord& r) override {
  const auto ordinal=at++;
  if(repeats>1 ? ordinal>=repeats : ordinal>(follower?33u:0u))return false;
  if(repeats==1&&ordinal>0&&ordinal<33) {
   facts.clear();r={};r.pc=0x400000+4*ordinal;r.flags=kRetires;
   if(ordinal>1)r.producer_dists[0]=1;
   return true;
  }
  r={};r.pc=0x400000;r.address=cross?0x200ff8:0x200000;r.size=cross?16:8;
  r.flags=kRetires|(write?kStore:kLoad)|kPhysicalAddress|(cross?0:kVirtualPageToken)|(atomic?kAtomic:0);r.reserved=cross?0:7;
  auto mapping=[](uint64_t va,uint64_t pa,unsigned bits){fst::TranslationMapping m;
   m.address_space_id=0x1000;m.walk_root=0x1000;m.virtual_base=va;m.physical_base=pa;m.page_bits=static_cast<uint8_t>(bits);
   m.flags=3;m.levels=bits==21?3:4;for(unsigned i=0;i<m.levels;++i)m.pte_addresses[i]=0x1008+i*4096;return m;};
  if(cross)facts={{0x400ff8,0x200ff8,8,mapping(0x400000,0x200000,12)},
                  {0x401000,0x400000,8,mapping(0x401000,0x400000,12)}};
  else facts={{0x400000,0x200000,8,mapping(0x400000,0x200000,21)}};
  if(repeats==1&&ordinal==33)r.producer_dists[0]=1;
  return true;
 }
};
struct MergeSource : Source {
 unsigned records=0;
 MergeSource():Source(true){}
 bool next(TraceRecord& r) override {
  if(records==3)return false;
  // Reuse the two-leaf fact fixture for three independent memory UOPs.
  at=0;Source::next(r);
  if(records++==0){r.size=8;facts.resize(1);}
  return true;
 }
};
// Simulator-level alternating-page workload: with a one-entry DTLB the
// third access misses L1 but can hit the D-only STLB proxy.
struct StlbSource final : TraceSource {
 unsigned at=0; bool bound_flush=false, missing=false; std::uint64_t diagnostics=0;
 std::vector<fst::TranslationFragment> fragments;
 fst::mmuctl_v2::Group control;
 fst::mmuctl_v2::Footer footer;
 explicit StlbSource(bool flush=false,bool absent=false,std::uint64_t unbound=0)
   :bound_flush(flush),missing(absent),diagnostics(unbound) {footer.unbound_commands=unbound;}
 bool dynamic_translations()const override{return true;}
 unsigned mmu_control_version()const override{return missing?0:2;}
 const fst::mmuctl_v2::Group* current_mmu_controls_v2()const override{return missing?nullptr:&control;}
 const fst::mmuctl_v2::Footer* mmu_control_v2_coverage()const override{return missing?nullptr:&footer;}
 const std::vector<fst::TranslationFragment>& current_translation_fragments()const override{return fragments;}
 std::uint64_t current_address_space_id()const override{return 0x1000;}
 std::string description()const override{return "functional D-side STLB fixture";}
 bool next(TraceRecord& r)override {
  if(at==3)return false; const auto n=at++;
  control={};control.owner.owner_kind=fst::mmuctl_v2::OwnerKind::RetiredInstruction;
  control.owner.status=fst::mmuctl_v2::Sealed;
  if(bound_flush && n==2)control.operations.emplace_back(fst::mmuctl::Command{});
  const auto va=n==1?0x401000ull:0x400000ull;const auto pa=n==1?0x201000ull:0x200000ull;
  r={};r.pc=0x4000+4*n;r.address=pa;r.size=8;r.op_class=56;r.flags=kRetires|kLoad|kPhysicalAddress;
  if(n)r.producer_dists[0]=1;
  fst::TranslationMapping m;m.address_space_id=0x1000;m.walk_root=0x1000;
  m.virtual_base=va;m.physical_base=pa;m.page_bits=12;m.flags=fst::kTranslationLeafKnown;
  fragments={{va,pa,8,m}};return true;
 }
};
SimulationStats stlb_run(bool enabled,bool flush=false,bool missing=false,std::uint64_t diagnostics=0) {
 SimulatorConfig c;c.cores=1;c.cha_count=c.dram.channels=c.dram.banks_per_channel=1;
 c.core_model="interval_weave";c.interval_scheduler="time_epoch";
 c.response_queue_feedback=c.response_sparse_scoreboard=true;c.interval_private_preview=false;
 c.dtlb.enabled=true;c.dtlb.entries=1;c.dtlb.miss_model="timing_walk";
 c.dtlb.page_walkers=1;c.dtlb.page_walk_latency=50;c.dtlb.coalesce_misses=false;
 c.spr_dtlb_stlb_proxy=enabled;
 std::vector<std::unique_ptr<TraceSource>> traces;
 traces.push_back(std::make_unique<StlbSource>(flush,missing,diagnostics));
 Simulator sim(c,std::move(traces));return sim.run();
}
SimulationStats run(bool hierarchy,bool cross,unsigned profile=0,bool write=false,bool follower=false,bool coalesced=false,bool observe=true,bool atomic=false,unsigned repeats=1,bool block_summary=false) {
 SimulatorConfig c;c.cores=1;c.core_model="interval_weave";c.interval_scheduler="time_epoch";
 c.response_queue_feedback=c.response_sparse_scoreboard=true;c.interval_private_preview=false;
 c.ruby_sequencer_max_outstanding=16;c.dtlb.enabled=true;c.dtlb.entries=64;c.dtlb.hit_latency=0;
 c.dtlb.page_walkers=coalesced?2:1;c.dtlb.coalesce_misses=coalesced;c.dtlb.page_walk_levels=4;
 c.dtlb.page_walk_restart_latency=7;c.dtlb.page_walk_latency=4*c.l1d.hit_latency+2;
 c.dtlb.hierarchy_walk=hierarchy;c.cpi_attribution=observe;c.response_frontier_audit_stride_uops=observe?1:0;c.dram.channels=c.cha_count=1;
 if(profile==0){c.needs_tso=true;c.tso_store=TsoStoreMode::kBuffer;c.sq_entries=c.tso_store_buffer_size=114;
 c.store_timing_model="bounded_analytic";c.store_commit_to_iew_delay=1;c.l1d.store_hit_latency=1;c.l2.store_hit_latency=5;c.store_private_callback_timing=true;}
 if(profile==1)c.response_pending_fill=true;
 if(profile==2)c.response_shared_service_constraints=true;
 c.needs_tso=true;c.dram.scheduler="fcfs";c.committed_static_memory_ordering=atomic;c.validate();std::vector<std::unique_ptr<TraceSource>> traces;if(coalesced)traces.push_back(std::make_unique<MergeSource>());
 else {auto source=std::make_unique<Source>(cross,write,follower);source->atomic=atomic;source->repeats=repeats;traces.push_back(std::move(source));}
 if(repeats>1){c.rob_entries=16;c.chunk_instructions=c.interval_target_uops=32;c.response_block_summary=block_summary;}
 Simulator sim(c,std::move(traces));return sim.run();
}
// These assertions consume final repaired event times, not canonical forecasts.
void causal(const SimulationStats& stats,bool follower,bool store) {
 assert(stats.response_frontier_audit.size()==1);
 unsigned memory_frames=0,store_frames=0,walk_frames=0;
 uint64_t fill=0;
 for(const auto& frame:stats.response_frontier_audit[0]) {
  if(frame.memory_events.empty())continue;
  ++memory_frames;
  uint64_t preceding=0,final=0;unsigned levels=0,data=0;
  for(const auto& event:frame.memory_events) {
   if(event.instruction_fetch)continue;
   if(event.line>=0x1000/64&&event.line<=0x3000/64) {
    assert(event.line==(0x1008+4096*levels)/64);
    assert(event.corrected_issue_cycle>=preceding);
    assert(event.response_cycle>event.corrected_issue_cycle);
    preceding=final=event.response_cycle;++levels;
   } else if(event.line==0x200000/64) {
    ++data;
    assert(event.corrected_issue_cycle>=(final?final+7:fill));
    assert(event.response_cycle>=event.corrected_issue_cycle);
    assert(frame.actual_issue_cycle>=(final?final+7:fill));
    assert(frame.actual_completion_cycle>=frame.actual_issue_cycle);
    if(!store)assert(frame.actual_completion_cycle>=event.response_cycle);
   }
  }
  assert(data==1);
  if(levels){assert(levels==3);fill=final;++walk_frames;}
  else {assert(follower&&fill>0);assert(frame.actual_issue_cycle>=fill);}
  if(frame.regular_store){++store_frames;assert(frame.store_send_cycle>=fill+7);assert(frame.store_send_cycle>=frame.actual_issue_cycle);}
 }
 assert(memory_frames==(follower?2u:1u)&&walk_frames==1);
 assert(store_frames==(store?(follower?2u:1u):0u));
}
int main(){
 {
  const auto off=stlb_run(false), on=stlb_run(true), flushed=stlb_run(true,true);
  const auto missing=stlb_run(true,false,true), diagnostics=stlb_run(true,false,false,121);
  assert(on.total_core().spr_dtlb_stlb_hits>0);
  assert(on.total_core().spr_dtlb_stlb_misses>0);
  assert(flushed.total_core().spr_dtlb_stlb_flushes>0);
  assert(flushed.total_core().spr_dtlb_stlb_hits<on.total_core().spr_dtlb_stlb_hits);
  assert(missing.total_core().spr_dtlb_stlb_fallbacks>0 && missing.total_core().spr_dtlb_stlb_hits==0);
  assert(missing.total_core().cycles==off.total_core().cycles);
  assert(on.total_core().memory_accesses==off.total_core().memory_accesses);
  assert(on.total_core().cycles==diagnostics.total_core().cycles &&
         on.total_core().spr_dtlb_stlb_hits==diagnostics.total_core().spr_dtlb_stlb_hits);
  SimulatorConfig invalid;invalid.spr_dtlb_stlb_proxy=true;
  invalid.core_model="interval_weave";invalid.interval_scheduler="time_epoch";
  invalid.dtlb.enabled=true;invalid.dtlb.miss_model="timing_walk";
  invalid.dtlb.hierarchy_walk=true;
  bool rejected=false;try {invalid.validate();}catch(const std::invalid_argument&){rejected=true;}
  assert(rejected);
 }
 for(unsigned p=0;p<3;++p){
  const auto fixed=run(false,false,p),walk=run(true,false,p);auto a=fixed.total_core(),b=walk.total_core();
  assert(b.retired_uops==1&&b.memory_accesses==1&&b.dtlb.misses==1);
  assert(b.dtlb_hierarchy_walks==1&&b.dtlb_hierarchy_requests==3&&b.dtlb_hierarchy_physical_requests==3);
  assert(b.l1d.accesses==a.l1d.accesses+3);
  assert(b.cycles>a.cycles&&b.dtlb_hierarchy_walk_extra_cycles>0);
  causal(walk,false,false);
  const auto follow=run(true,false,p,false,true);
  assert(follow.total_core().dtlb_timing.hits==1);
  assert(follow.total_core().dtlb_hierarchy_premature_hits>0);
  causal(follow,true,false);
 }
 auto sf=run(false,false,0,true),sw=run(true,false,0,true);
 assert(sf.bounded_store_estimates==1&&sw.bounded_store_estimates==1);
 assert(sw.total_core().cycles>sf.total_core().cycles);
 assert(sw.total_core().dtlb_hierarchy_requests==3);
 // Deferred stores consume Sequencer entries only after commit in their owner.
 // The hierarchy source-order prepass must add PTE reads, not a second store.
 assert(sf.total_sequencer().requests==0);
 assert(sw.total_sequencer().requests==3);
 assert(sw.total_sequencer().requests-sf.total_sequencer().requests==
        sw.total_core().dtlb_hierarchy_requests);
 const auto& observed=sw.total_response_residuals().rob_blockers;
 assert(observed.store_uops==1&&observed.store_completion_before_response==1);
 assert(observed.store_retire_before_response==1&&observed.store_retire_at_or_after_response==0);
 const auto quiet=run(true,false,0,true,false,false,false);
 assert(quiet.total_core().cycles==sw.total_core().cycles);
 assert(quiet.total_sequencer().requests==sw.total_sequencer().requests);
 assert(quiet.total_core().l1d.misses==sw.total_core().l1d.misses);
 assert(quiet.total_response_residuals().rob_blockers.store_uops==0);
 const auto atomic_fixed=run(false,false,0,true,false,false,true,true);
 const auto atomic_walk=run(true,false,0,true,false,false,true,true);
 assert(atomic_fixed.total_sequencer().requests==1);
 assert(atomic_walk.total_sequencer().requests==4);
 for (bool summary : {false,true}) {
 const auto burst=run(true,false,0,true,false,false,true,false,300,summary);
 const auto blockers=burst.total_response_residuals().rob_blockers;
 const auto store_kind=static_cast<std::size_t>(RobBlockerKind::kStore);
 assert(blockers.events[store_kind]>0);
 assert(blockers.events[static_cast<std::size_t>(RobBlockerKind::kUnknown)]==0);
 assert(blockers.store_uops==300);
 assert(blockers.store_retire_before_response+blockers.store_retire_at_or_after_response==300);
 }
 causal(sw,false,true);
 // Noncontiguous physical fragments and both four-level chains stay causal.
 for(unsigned p=0;p<3;++p) {
  auto cross=run(true,true,p);auto x=cross.total_core();
  assert(x.retired_uops==1&&x.memory_accesses==2&&x.dtlb.accesses==2&&x.dtlb.misses==2);
  assert(x.dtlb_hierarchy_walks==2&&x.dtlb_hierarchy_requests==8);
  unsigned ptes=0,data=0;uint64_t previous=0,final=0;
  for(const auto& frame:cross.response_frontier_audit[0])for(const auto& event:frame.memory_events) {
   if(event.instruction_fetch)continue;
   if(ptes<8) {
    assert(event.line==(0x1008+(ptes%4)*4096)/64);
    if(ptes%4)assert(event.corrected_issue_cycle>=previous);
    assert(event.response_cycle>event.corrected_issue_cycle);
    previous=event.response_cycle;final=std::max(final,previous);++ptes;
   } else {
    assert(event.line==(data?0x400000:0x200fc0)/64);
    assert(event.corrected_issue_cycle>=final+7);
    assert(frame.actual_completion_cycle>=event.response_cycle);++data;
   }
  }
  assert(ptes==8&&data==2);
 }
 const auto merged=run(false,true,0,false,false,true).total_core().dtlb_timing;
 assert(merged.accesses==5&&merged.misses==2&&merged.merged_misses==3&&merged.hits==0);
 assert(merged.accesses==merged.misses+merged.merged_misses+merged.hits);
 std::cout<<"dynamic hierarchy tests passed\n";
}
