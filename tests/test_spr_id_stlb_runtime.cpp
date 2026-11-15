#include "fastsim/simulator.hpp"
#include "fastsim/spr_stlb.hpp"
#include "fastsim/trace.hpp"
#include <algorithm>
#include <iostream>
#include <memory>
#include <stdexcept>
using namespace fastsim;
void require(bool v,const char *why){if(!v)throw std::runtime_error(why);}
struct Src:TraceSource {
 unsigned at=0;bool unknown=false,flush=false,reverse=false,evict=false,cross=false,delay=false,global=false,switch_asid=false,bad_root=false,bundle_ready=true,repeat_loads=false;std::uint64_t asid=0x1000;unsigned page_bits=12;
 unsigned active_pcid=0, mapping_pcid=0;bool state_available=true,pcide=true,switch_pcid=false,late_mismatch=false;
 mutable fst::mmuctl::State control_state{};
 std::vector<fst::TranslationFragment> data;std::shared_ptr<fst::InstructionFetchFacts> inst;
 fst::mmuctl_v2::Group ctl;fst::mmuctl_v2::Footer footer;
 fst::RetiredFrontendMacro macro;
 bool dynamic_translations()const override{return true;}
 unsigned mmu_control_version()const override{return 2;}
 bool architectural_boundary_bundle_ready()const override{return bundle_ready;}
 const fst::mmuctl_v2::Group* current_mmu_controls_v2()const override{return &ctl;}
 const fst::mmuctl::State* current_mmu_control_state_v2()const override {
  if(!state_available || std::any_of(ctl.operations.begin(),ctl.operations.end(),
     [](const auto& op){return std::holds_alternative<fst::mmuctl::Write>(op);}))return nullptr;
  control_state={0,asid|active_pcid,pcide?(1ull<<17):0,0};return &control_state;
 }
 const fst::mmuctl_v2::Footer* mmu_control_v2_coverage()const override{return &footer;}
 const std::vector<fst::TranslationFragment>& current_translation_fragments()const override{return data;}
 std::shared_ptr<const fst::InstructionFetchFacts> current_instruction_fetch()const override{return inst;}
 const fst::RetiredFrontendMacro* current_frontend_macro()const override{return &macro;}
 std::optional<std::uint64_t> current_frontend_record_ordinal()const override{return at?std::optional<std::uint64_t>(at-1):std::nullopt;}
 std::uint64_t current_address_space_id()const override{return asid;}
 std::string description()const override{return "retired I/D shared STLB fixture";}
 fst::TranslationMapping mapping(std::uint64_t va,std::uint64_t asid,unsigned bits=12)const{fst::TranslationMapping m;
  const auto mask=(std::uint64_t{1}<<bits)-1;
  m.address_space_id=asid;m.walk_root=asid;m.virtual_base=va&~mask;
  m.physical_base=m.virtual_base+(bits==12?0x1000000ull:0x100000000ull);m.pcid=mapping_pcid;m.page_bits=static_cast<std::uint8_t>(bits);m.flags=fst::kTranslationLeafKnown;return m;}
 bool next(TraceRecord&r)override{
  const unsigned last=late_mismatch?3u:(switch_pcid?2u:(repeat_loads?31u:(evict?9u:(reverse&&delay?4097u:1u))));
  if(at==last+1)return false;const auto n=at++;
  if(switch_asid&&n==last)asid=0x2000;
  if(switch_pcid&&n==last){active_pcid=8;mapping_pcid=8;}
  if(late_mismatch && n>=2)mapping_pcid=n==2?6:7;
  ctl={};ctl.owner.owner_kind=fst::mmuctl_v2::OwnerKind::RetiredInstruction;ctl.owner.status=fst::mmuctl_v2::Sealed;
  if(flush&&n==last)ctl.operations.emplace_back(fst::mmuctl::Command{});
  if(switch_pcid&&n==1)ctl.operations.emplace_back(fst::mmuctl::Write{});
  r={};r.pc=evict ? (n==9?0x401000:0x400000+std::uint64_t(n)*256*4096) :
       (reverse ? (n==last?0x400000:(page_bits==12?0x401000:0x400000+(std::uint64_t{1}<<page_bits))) : (n?0x401000:0x400000));r.flags=kRetires;data.clear();
  if((flush || switch_asid) && !reverse && n==last && page_bits>12) r.pc=0x400000+(std::uint64_t{1}<<page_bits);
  macro={};macro.begin_record=n;macro.end_record=n+1;macro.pc=r.pc;macro.address_space_id=asid;
  macro.code_identity=42+r.pc;macro.semantic_identity=84+r.pc;macro.instruction_size=4;macro.observed_uops=1;
  macro.known_flags=fst::kFrontendRequiredKnown;macro.flags=fst::kFrontendComplete|fst::kFrontendStartsMacro;
  inst=std::make_shared<fst::InstructionFetchFacts>();inst->pc=r.pc;inst->bytes=4;
  inst->decode_context_known=true;inst->decode_address_space_id=asid+(cross&&n==0?0x1000:0);
  if(unknown&&n==0){inst->status=fst::InstructionFetchStatus::Unknown;}
  else {inst->status=fst::InstructionFetchStatus::Mapped;inst->reason=fst::InstructionFetchUnknown::None;
   auto m=mapping(r.pc,asid,page_bits);if(global)m.flags|=fst::kTranslationGlobal;if(bad_root)m.walk_root+=4096;inst->fragments.push_back({r.pc,m.physical_base+(r.pc-m.virtual_base),4,m});}
  if(repeat_loads || (late_mismatch ? n>0 : (switch_pcid ? n==last : (evict ? n==9 : (reverse ? n==0 : n==1))))){auto m=mapping(0x400000,asid,page_bits);if(global)m.flags|=fst::kTranslationGlobal;r.flags|=kLoad|kPhysicalAddress;r.op_class=56;r.address=m.physical_base+(0x400000-m.virtual_base);r.size=8;
   data.push_back({0x400000,r.address,8,m});}
  return true;
 }
};
struct CapacitySrc final:Src {
 bool next(TraceRecord&r)override {
  if(at==258)return false;const auto n=at++;
  const auto va = n==256 ? 0x7000000000ull : 0x1000000000ull+std::uint64_t(n==257?0:n)*(1ull<<21);
  const unsigned bits=n==256?12:21;ctl={};ctl.owner.owner_kind=fst::mmuctl_v2::OwnerKind::RetiredInstruction;ctl.owner.status=fst::mmuctl_v2::Sealed;
  r={};r.pc=va;r.flags=kRetires;data.clear();macro={};macro.begin_record=n;macro.end_record=n+1;macro.pc=va;macro.address_space_id=asid;
  macro.code_identity=va;macro.semantic_identity=va;macro.instruction_size=4;macro.observed_uops=1;
  macro.known_flags=fst::kFrontendRequiredKnown;macro.flags=fst::kFrontendComplete|fst::kFrontendStartsMacro;
  inst=std::make_shared<fst::InstructionFetchFacts>();inst->pc=va;inst->bytes=4;inst->decode_context_known=true;inst->decode_address_space_id=asid;
  inst->status=fst::InstructionFetchStatus::Mapped;inst->reason=fst::InstructionFetchUnknown::None;
  auto m=mapping(va,asid,bits);inst->fragments.push_back({va,m.physical_base+(va-m.virtual_base),4,m});return true;
 }
};
struct FrontendSrc final:Src {
 unsigned fn=0;bool middle_unknown=false;fst::RetiredFrontendMacro macro;
 const fst::RetiredFrontendMacro* current_frontend_macro()const override{return &macro;}
 std::optional<std::uint64_t> current_frontend_record_ordinal()const override{return fn?std::optional<std::uint64_t>(fn-1):std::nullopt;}
 bool next(TraceRecord&r)override{
  if(fn==4)return false;const auto n=fn++;ctl={};ctl.owner.owner_kind=fst::mmuctl_v2::OwnerKind::RetiredInstruction;
  ctl.owner.status=fst::mmuctl_v2::Sealed;r={};r.pc=n<2?0x400000:(n==2?0x400004:0x401000);
  r.flags=kRetires;if(n<2){r.flags|=kMicroOp;if(n==1)r.flags|=kLastMicroOp;}data.clear();
  macro={};macro.begin_record=n<2?0:n;macro.end_record=n<2?2:n+1;macro.pc=r.pc;macro.address_space_id=asid;
  macro.code_identity=42+r.pc;macro.semantic_identity=84+r.pc;macro.instruction_size=4;macro.observed_uops=n<2?2:1;
  macro.known_flags=fst::kFrontendRequiredKnown;macro.flags=fst::kFrontendComplete|fst::kFrontendStartsMacro;
  inst=std::make_shared<fst::InstructionFetchFacts>();inst->pc=r.pc;inst->bytes=4;inst->decode_context_known=true;inst->decode_address_space_id=asid;
  if(middle_unknown&&n==2)inst->status=fst::InstructionFetchStatus::Unknown;
  else{inst->status=fst::InstructionFetchStatus::Mapped;inst->reason=fst::InstructionFetchUnknown::None;
   auto m=mapping(r.pc,asid);inst->fragments.push_back({r.pc,m.physical_base+(r.pc-m.virtual_base),4,m});}
  if(n==3){auto m=mapping(0x400000,asid);r.flags|=kLoad|kPhysicalAddress;r.op_class=56;r.address=m.physical_base;r.size=8;data.push_back({0x400000,m.physical_base,8,m});}
  return true;
 }
};
struct CrossPageSrc final:Src {
 bool missing=false;
 bool next(TraceRecord&r)override {
  if(at++)return false;ctl={};ctl.owner.owner_kind=fst::mmuctl_v2::OwnerKind::RetiredInstruction;
  ctl.owner.status=fst::mmuctl_v2::Sealed;r={};r.pc=0x400ffe;r.flags=kRetires;data.clear();
  macro={};macro.begin_record=0;macro.end_record=1;macro.pc=r.pc;macro.address_space_id=asid;
  macro.code_identity=42;macro.semantic_identity=84;macro.instruction_size=4;macro.observed_uops=1;
  macro.known_flags=fst::kFrontendRequiredKnown;macro.flags=fst::kFrontendComplete|fst::kFrontendStartsMacro;
  inst=std::make_shared<fst::InstructionFetchFacts>();inst->pc=r.pc;inst->bytes=4;
  inst->decode_context_known=true;inst->decode_address_space_id=asid;
  if(missing) inst->status=fst::InstructionFetchStatus::Unknown;
  else {inst->status=fst::InstructionFetchStatus::Mapped;inst->reason=fst::InstructionFetchUnknown::None;
   auto a=mapping(0x400000,asid),b=mapping(0x401000,asid);
   inst->fragments.push_back({r.pc,a.physical_base+4094,2,a});inst->fragments.push_back({0x401000,b.physical_base,2,b});}
  return true;
 }
};
SimulationStats run(bool shared,bool flush=false,bool unknown=false,bool reverse=false,bool evict=false,bool cross=false,bool two=false,bool delay=false,unsigned hit_latency=7,bool global=false,bool allow=false,bool switch_asid=false,bool bad_root=false,unsigned page_bits=12,bool large=false,bool capacity=false){SimulatorConfig c;
 c.cores=two?2:1;c.cha_count=c.dram.channels=c.dram.banks_per_channel=1;c.core_model="interval_weave";c.interval_scheduler="time_epoch";
 c.response_queue_feedback=c.response_sparse_scoreboard=true;c.interval_private_preview=false;
 c.cpi_attribution=true;c.response_frontier_audit_stride_uops=1;
 c.dtlb.enabled=true;c.dtlb.entries=1;c.dtlb.miss_model="timing_walk";c.dtlb.page_walkers=1;c.dtlb.page_walk_latency=50;c.dtlb.coalesce_misses=false;
 c.spr_dtlb_stlb_proxy=true;c.fetch_buffer_bytes=64;c.l1i_enabled=true;c.fetch_supply_model=true;
 c.spr_retired_frontend=true;c.fetch_queue_entries=144;c.rename_width=6;c.fetch_to_decode=c.decode_to_rename=0;
 c.spr_id_stlb_retired_order=shared;c.spr_id_stlb_allow_global=allow;c.spr_id_stlb_allow_large=large;
 c.spr_itlb_stlb_hit_latency=hit_latency;c.validate();std::vector<std::unique_ptr<TraceSource>> t;
 if(capacity)t.push_back(std::make_unique<CapacitySrc>());
 else {auto source=std::make_unique<Src>();source->flush=flush;source->unknown=unknown;source->reverse=reverse;source->evict=evict;source->cross=cross;source->delay=delay;source->global=global;source->switch_asid=switch_asid;source->bad_root=bad_root;source->page_bits=page_bits;t.push_back(std::move(source));}
 if(two){auto other=std::make_unique<Src>();other->unknown=true;t.push_back(std::move(other));}
 Simulator sim(c,std::move(t));return sim.run();}
SimulationStats run_unready_boundary(bool repeat_loads=false,bool flush=false){SimulatorConfig c;
 c.cores=1;c.cha_count=c.dram.channels=c.dram.banks_per_channel=1;c.core_model="interval_weave";c.interval_scheduler="time_epoch";
 c.response_queue_feedback=c.response_sparse_scoreboard=true;c.interval_private_preview=false;
 c.dtlb.enabled=true;c.dtlb.entries=1;c.dtlb.miss_model="timing_walk";c.dtlb.page_walkers=1;c.dtlb.page_walk_latency=50;c.dtlb.coalesce_misses=false;
 c.spr_dtlb_stlb_proxy=true;c.fetch_buffer_bytes=64;c.l1i_enabled=true;c.fetch_supply_model=true;
 c.spr_retired_frontend=true;c.fetch_queue_entries=144;c.rename_width=6;c.fetch_to_decode=c.decode_to_rename=0;c.spr_id_stlb_retired_order=true;c.validate();
 auto source=std::make_unique<Src>();source->bundle_ready=false;source->repeat_loads=repeat_loads;source->flush=flush;std::vector<std::unique_ptr<TraceSource>> t;t.push_back(std::move(source));
 Simulator sim(c,std::move(t));return sim.run();}
SimulationStats run_pcid(unsigned active,unsigned origin,bool known=true,bool flush=false,bool pcide=true,bool switch_pcid=false,bool late_mismatch=false){SimulatorConfig c;
 c.cores=1;c.cha_count=c.dram.channels=c.dram.banks_per_channel=1;c.core_model="interval_weave";c.interval_scheduler="time_epoch";
 c.response_queue_feedback=c.response_sparse_scoreboard=true;c.interval_private_preview=false;
 c.dtlb.enabled=true;c.dtlb.entries=1;c.dtlb.miss_model="timing_walk";c.dtlb.page_walkers=1;c.dtlb.page_walk_latency=50;c.dtlb.coalesce_misses=false;
 c.spr_dtlb_stlb_proxy=true;c.fetch_buffer_bytes=64;c.l1i_enabled=true;c.fetch_supply_model=true;
 c.spr_retired_frontend=true;c.fetch_queue_entries=144;c.rename_width=6;c.fetch_to_decode=c.decode_to_rename=0;c.spr_id_stlb_retired_order=true;c.validate();
 auto source=std::make_unique<Src>();source->active_pcid=active;source->mapping_pcid=origin;
 source->state_available=known;source->flush=flush;source->pcide=pcide;source->switch_pcid=switch_pcid;source->late_mismatch=late_mismatch;
 std::vector<std::unique_ptr<TraceSource>> traces;traces.push_back(std::move(source));
 Simulator sim(c,std::move(traces));return sim.run();}
SimulationStats run_cross_page(bool missing){SimulatorConfig c;
 c.cores=1;c.cha_count=c.dram.channels=c.dram.banks_per_channel=1;c.core_model="interval_weave";c.interval_scheduler="time_epoch";
 c.response_queue_feedback=c.response_sparse_scoreboard=true;c.interval_private_preview=false;
 c.cpi_attribution=true;c.response_frontier_audit_stride_uops=1;
 c.dtlb.enabled=true;c.dtlb.entries=1;c.dtlb.miss_model="timing_walk";c.dtlb.page_walkers=1;c.dtlb.page_walk_latency=50;c.dtlb.coalesce_misses=false;
 c.spr_dtlb_stlb_proxy=true;c.fetch_buffer_bytes=64;c.l1i_enabled=true;c.fetch_supply_model=true;c.spr_id_stlb_retired_order=true;
 c.spr_retired_frontend=true;c.fetch_queue_entries=144;c.rename_width=6;c.fetch_to_decode=c.decode_to_rename=0;c.validate();
 std::vector<std::unique_ptr<TraceSource>> t;auto source=std::make_unique<CrossPageSrc>();source->missing=missing;t.push_back(std::move(source));
 Simulator sim(c,std::move(t));return sim.run();}
SimulationStats run_frontend(bool middle_unknown){SimulatorConfig c;
 c.cores=1;c.cha_count=c.dram.channels=c.dram.banks_per_channel=1;c.core_model="interval_weave";c.interval_scheduler="time_epoch";
 c.response_queue_feedback=c.response_sparse_scoreboard=true;c.interval_private_preview=false;
 c.dtlb.enabled=true;c.dtlb.entries=1;c.dtlb.miss_model="timing_walk";c.dtlb.page_walkers=1;c.dtlb.page_walk_latency=50;c.dtlb.coalesce_misses=false;
 c.spr_dtlb_stlb_proxy=true;c.fetch_buffer_bytes=64;c.l1i_enabled=true;c.fetch_supply_model=true;c.spr_id_stlb_retired_order=true;
 c.spr_retired_frontend=true;c.fetch_queue_entries=144;c.rename_width=6;c.fetch_to_decode=c.decode_to_rename=0;c.validate();
 std::vector<std::unique_ptr<TraceSource>> t;auto source=std::make_unique<FrontendSrc>();source->middle_unknown=middle_unknown;t.push_back(std::move(source));
 Simulator sim(c,std::move(t));return sim.run();}
int main(){try {
 SprStlb plain(0), folded(8);
 for (std::uint64_t i=0;i<9;++i) {
  const auto va=((i<<8)|i)<<12;
  require(plain.insert(0x1000,0,va,12,false),"plain STLB insertion failed");
  require(folded.insert(0x1000,0,va,12,false),"folded STLB insertion failed");
 }
 require(plain.lookup(0x1000,0,0).has_value(),"unfolded sets must retain the first page");
 require(!folded.lookup(0x1000,0,0).has_value(),"folded 4K set must evict its ninth page");
 require(SprStlb::set(0x100000,12,8)==1 && SprStlb::set(0x100000,12,0)==0,
         "4K set must use the selected VPN fold");
 for (const std::uint64_t va : {0ull, 0x0000100000000000ull,
                               0x00ff123456789000ull, 0xffff923456789000ull}) {
  auto vpn=va>>12; unsigned folded_byte=0;
  for (unsigned shift=0;shift<64;shift+=8) folded_byte^=unsigned((vpn>>shift)&0xffu);
  require(SprStlb::set(va,12,8)==(folded_byte&127u) &&
          SprStlb::domain(va,12,8)==((folded_byte>>7)&1u),
          "SPR fold must include every unsigned VPN byte");
 }
 SprStlb large_stride(8);
 for (std::uint64_t i=0;i<9;++i)
  require(large_stride.insert(0x1000,0,i<<32,12,false),
          "4 GiB stride fixture insertion failed");
 for (std::uint64_t i=0;i<9;++i)
  require(large_stride.lookup(0x1000,0,i<<32).has_value(),
          "4 GiB stride must not force nine pages into one 8-way set");
 require(SprStlb::set(0x200000,21,8)==SprStlb::set(0x200000,21,0),
         "large-page sets must not use the 4K VPN fold");
 bool invalid_shift=false;try { SprStlb invalid(64); } catch(const std::invalid_argument&) { invalid_shift=true; }
 require(invalid_shift,"invalid STLB shift must be rejected");
 const auto base=run(false),on=run(true);
 require(base.total_core().spr_dtlb_stlb_hits==0,"baseline fixture must have no D hit");
 require(on.total_core().spr_dtlb_stlb_hits==1,"prior instruction translation must share D STLB");
 const auto pcid_match=run_pcid(7,7),pcid_mismatch=run_pcid(7,6),pcid_unknown=run_pcid(7,7,false);
 require(pcid_match.total_core().spr_dtlb_stlb_hits==1,"matching nonzero current PCID must permit I/D sharing");
 require(pcid_mismatch.total_core().spr_dtlb_stlb_hits==0 && pcid_mismatch.total_core().dtlb.untracked>0,
         "fill-origin PCID differing from current state must remain untracked");
 require(pcid_unknown.total_core().spr_dtlb_stlb_hits==0 && pcid_unknown.total_core().dtlb.untracked>0,
         "missing current control state must remain untracked");
 const auto pcid_flush=run_pcid(7,7,true,true),pcide_off=run_pcid(5,0,true,false,false);
 require(pcid_flush.total_core().spr_dtlb_stlb_hits==0 && pcid_flush.total_core().spr_dtlb_stlb_flushes>0,
         "control effects must invalidate nonzero-PCID shared state");
 require(pcide_off.total_core().spr_dtlb_stlb_hits==1,"CR3 low bits are not a PCID when CR4.PCIDE is clear");
 const auto pcid_switch=run_pcid(7,7,true,false,true,true);
 require(pcid_switch.total_core().spr_dtlb_stlb_hits==0 && pcid_switch.total_core().spr_dtlb_stlb_flushes>0,
         "a CR3 PCID switch must not reuse an earlier PCID's translation");
 const auto late_mismatch=run_pcid(7,7,true,false,true,false,true);
 require(late_mismatch.total_core().dtlb.untracked==1 && late_mismatch.total_core().dtlb.misses==2,
         "a later PCID mismatch must invalidate an already populated fallback DTLB");
 const auto incomplete=run_unready_boundary();
 require(incomplete.total_core().spr_dtlb_stlb_hits==0 && incomplete.total_core().spr_dtlb_stlb_fallbacks>0,
         "sealed rows must not qualify an incomplete architectural boundary bundle");
 require(incomplete.total_core().spr_dtlb_stlb_unqualified_boundary==2 && incomplete.total_core().spr_dtlb_stlb_missing_controls==0,
         "sealed rows with an unqualified boundary need a distinct diagnostic");
 const auto repeated=run_unready_boundary(true);
 require(repeated.total_core().dtlb.misses==1,
         "unqualified shared STLB alone must not invalidate the fallback DTLB on every record");
 const auto repeated_flush=run_unready_boundary(true,true);
 require(repeated_flush.total_core().dtlb.misses==2,
         "observed control effects must still invalidate the fallback DTLB");
 require(on.total_core().retired_uops==base.total_core().retired_uops,"retirement population changed");
 const auto flushed=run(true,true),unknown=run(true,false,true),cross=run(true,false,false,false,false,true);
 require(flushed.total_core().spr_dtlb_stlb_hits==0 && flushed.total_core().spr_dtlb_stlb_flushes>0,"bound invalidation must clear I/D STLB");
 require(unknown.total_core().spr_itlb_unknown==1 && unknown.total_core().spr_dtlb_stlb_hits==0,"unknown instruction must not fill shared STLB");
 require(cross.total_core().spr_itlb_unknown==1 && cross.total_core().spr_dtlb_stlb_hits==0,"cross-context instruction must remain unknown");
 const auto reverse=run(true,false,false,true);
 require(reverse.total_core().spr_itlb_stlb_hits==1,"prior data translation must serve later I STLB lookup");
 const auto evict=run(true,false,false,false,true);
 require(evict.total_core().spr_dtlb_stlb_hits==0 && evict.total_core().spr_itlb_stlb_misses>=9,"eight colliding instruction pages must evict old shared page");
 const auto two=run(true,false,false,false,false,false,true);
 require(two.cores.size()==2 && two.cores[0].spr_dtlb_stlb_hits==1 && two.cores[1].spr_dtlb_stlb_hits==0,"STLB must remain per core");
 require(two.total_core().retired_uops==4,"two-core retirement population changed");
 const auto macros=run_frontend(false),middle_unknown=run_frontend(true);
 require(macros.total_core().spr_itlb_stlb_misses==2 && macros.total_core().spr_itlb_hits==0 &&
         macros.total_core().retired_uops==4 && macros.total_core().spr_dtlb_stlb_hits==1,
         "same-macro UOPs and same-block macro must not repeat ITLB lookup");
 require(middle_unknown.total_core().spr_itlb_unknown==1 && middle_unknown.total_core().spr_dtlb_stlb_hits==0,
         "same-block unknown macro must clear shared state without a repeated ITLB lookup");
 const auto cross_page=run_cross_page(false),cross_missing=run_cross_page(true);
 require(cross_page.total_core().spr_itlb_stlb_misses==2 && cross_page.total_core().spr_itlb_unknown==0 && cross_page.total_core().retired_uops==1,
         "known cross-page macro must issue exactly two known translation lookups");
 require(cross_missing.total_core().spr_itlb_stlb_misses==0 && cross_missing.total_core().spr_itlb_unknown==2 && cross_missing.total_core().retired_uops==1,
         "cross-page unknown macro must not infer either translation");
 const auto hit_timing=run(true,false,false,true,false,false,false,true),miss_timing=run(true,true,false,true,false,false,false,true);
 const auto& h=hit_timing.response_frontier_audit.at(0).back();
 const auto& m=miss_timing.response_frontier_audit.at(0).back();
 require(h.sequence==4097 && m.sequence==4097,"final timing sample must be the second-page instruction");
 require(hit_timing.total_core().spr_itlb_stlb_hits==1 && miss_timing.total_core().spr_itlb_stlb_hits==0,
         "the timing fixture must distinguish shared hit and miss");
 if (!(m.actual_fetch_cycle>h.actual_fetch_cycle && m.actual_retire_cycle>h.actual_retire_cycle))
  throw std::runtime_error("shared miss penalty must reach final modeled fetch and retirement: hit="+
      std::to_string(h.actual_fetch_cycle)+"/"+std::to_string(h.actual_retire_cycle)+" miss="+
      std::to_string(m.actual_fetch_cycle)+"/"+std::to_string(m.actual_retire_cycle));
 std::cout<<"retired-order I/D STLB hit fetch/retire="<<h.actual_fetch_cycle<<'/'<<h.actual_retire_cycle
          <<" miss="<<m.actual_fetch_cycle<<'/'<<m.actual_retire_cycle<<'\n';
 const auto hit15=run(true,false,false,true,false,false,false,true,15);
 const auto& h15=hit15.response_frontier_audit.at(0).back();
 require(hit15.total_core().spr_itlb_stlb_hits==1 && h15.actual_fetch_cycle==h.actual_fetch_cycle+8,
         "non-default STLB latency must reach the final fetch path");
 const auto global_off=run(true,false,false,false,false,false,false,false,7,true,false);
 const auto global_on=run(true,false,false,false,false,false,false,false,7,true,true);
 require(global_off.total_core().spr_dtlb_stlb_hits==0 && global_off.total_core().spr_itlb_unknown>0,
         "disabled global case must retain unknown");
 require(global_on.total_core().spr_dtlb_stlb_hits==1 && global_on.total_core().spr_itlb_unknown==0,
         "qualified global I-to-D page must share STLB");
 const auto global_reverse=run(true,false,false,true,false,false,false,false,7,true,true);
 require(global_reverse.total_core().spr_itlb_stlb_hits==1,"qualified global D-to-I page must share STLB");
 const auto global_flushed=run(true,true,false,false,false,false,false,false,7,true,true);
 const auto global_switch=run(true,false,false,false,false,false,false,false,7,true,true,true);
 const auto global_bad_root=run(true,false,false,false,false,false,false,false,7,true,true,false,true);
 require(global_flushed.total_core().spr_dtlb_stlb_hits==0,"bound control must invalidate global shared state");
 require(global_switch.total_core().spr_dtlb_stlb_hits==0,"context change must not reuse global shared state across ASID");
 require(global_bad_root.total_core().spr_dtlb_stlb_hits==0 && global_bad_root.total_core().spr_itlb_unknown>0,
         "global input with unmatched root must remain unknown");
 const auto non_global_on=run(true,false,false,false,false,false,false,false,7,false,true);
 require(non_global_on.total_core().spr_dtlb_stlb_hits==on.total_core().spr_dtlb_stlb_hits && non_global_on.total_core().cycles==on.total_core().cycles,
         "global option must not change non-global population or timing");
 require(global_on.total_core().retired_uops==global_off.total_core().retired_uops,"global option must preserve retirement population");
 for(const unsigned bits:{21u,22u,30u}) {
  const auto big_off=run(true,false,false,false,false,false,false,false,7,true,true,false,false,bits,false);
  const auto big_on=run(true,false,false,false,false,false,false,false,7,true,true,false,false,bits,true);
  const auto big_reverse=run(true,false,false,true,false,false,false,false,7,true,true,false,false,bits,true);
  const auto big_flush=run(true,true,false,false,false,false,false,false,7,true,true,false,false,bits,true);
  const auto big_bad=run(true,false,false,false,false,false,false,false,7,true,true,false,true,bits,true);
  const auto big_unknown=run(true,false,true,false,false,false,false,false,7,true,true,false,false,bits,true);
  const auto big_switch=run(true,false,false,false,false,false,false,false,7,true,true,true,false,bits,true);
  require(big_off.total_core().spr_itlb_unknown>0 && big_off.total_core().spr_dtlb_stlb_hits==0,"disabled large-page path must remain unknown");
  require(big_on.total_core().spr_itlb_unknown==0 && big_on.total_core().spr_dtlb_stlb_hits==1,"large I-to-D shared STLB missing");
  require(big_reverse.total_core().spr_itlb_stlb_hits==1,"large D-to-I shared STLB missing");
  require(big_flush.total_core().spr_dtlb_stlb_hits==0,"bound control must invalidate large shared mapping");
  require(big_bad.total_core().spr_itlb_unknown>0 && big_bad.total_core().spr_dtlb_stlb_hits==0,"unknown large root must not become known");
  require(big_unknown.total_core().spr_itlb_unknown>0,"missing large-page instruction facts must remain unknown");
  require(big_switch.total_core().spr_dtlb_stlb_hits==0,"large global mapping must not leak across ASID");
  require(big_on.total_core().retired_uops==big_off.total_core().retired_uops,"large page path changed retirement population");
 }
 const auto capacity=run(true,false,false,false,false,false,false,false,7,false,false,false,false,12,true,true);
 require(capacity.total_core().spr_itlb_hits==0 && capacity.total_core().spr_itlb_stlb_hits==1,
         "one 4K entry must compete for the same 256-slot pool and evict oldest large page");
 const auto small_evict=run(true,false,false,false,true,false,false,false,7,false,false,false,false,12,true);
 require(small_evict.total_core().spr_dtlb_stlb_hits==0 && small_evict.total_core().spr_itlb_stlb_misses>=9,
         "large-page mode must retain the 4K 8-way set constraint");
 const auto plain_small=run(true,false,false,false,false,false,false,false,7,false,false,false,false,12,true);
 require(plain_small.total_core().cycles==on.total_core().cycles && plain_small.total_core().spr_dtlb_stlb_hits==on.total_core().spr_dtlb_stlb_hits,
         "nonbinding 4K population and timing must stay unchanged");
 }catch(const std::exception&e){std::cerr<<e.what()<<'\n';return 1;}std::cout<<"shared I/D STLB runtime passed\n";}
