#ifdef NDEBUG
#undef NDEBUG
#endif
#include "fastsim/interval_core.hpp"
#include "fastsim/trace.hpp"
#include "fastsim/fst_translation_facts.hpp"
#include <cassert>
#include <iostream>
using namespace fastsim;
struct FactsSource : TraceSource {
 std::vector<fst::TranslationFragment> facts;
 bool dynamic_translations() const override {return true;}
 const std::vector<fst::TranslationFragment>& current_translation_fragments() const override {return facts;}
 bool next(TraceRecord&) override {return false;}
 std::string description() const override {return "dynamic-dtlb-test";}
 void page(uint64_t va,uint64_t pa,unsigned bits) {
  fst::TranslationMapping m;m.address_space_id=0x1000;m.walk_root=0x1000;
  m.virtual_base=va&~((1ull<<bits)-1);m.physical_base=pa&~((1ull<<bits)-1);
  m.page_bits=static_cast<uint8_t>(bits);m.levels=bits==21?3:4;m.flags=3;
  for(unsigned i=0;i<m.levels;++i)m.pte_addresses[i]=0x1000+i*4096;
  facts={{va,pa,8,m}};
 }
};
TraceRecord load(uint64_t pa,uint32_t token=1) {
 TraceRecord r;r.pc=0x1000;r.address=pa;r.size=8;
 r.flags=kRetires|kLoad|kPhysicalAddress|kVirtualPageToken;r.reserved=token;return r;
}
IntervalTiming issue(IntervalCoreModel& m,FactsSource& s,uint64_t va,uint64_t pa,unsigned bits,uint32_t tok=1) {
 s.page(va,pa,bits);return m.schedule(load(pa,tok),false,false,0,false,&s,nullptr,0x1000);
}
int main() {
 SimulatorConfig c;c.core_model="interval_bound";c.dtlb.enabled=true;c.dtlb.entries=64;
 c.dtlb.hit_latency=0;c.dtlb.page_walk_latency=100;c.dtlb.page_walkers=1;c.dtlb.coalesce_misses=false;c.validate();
 FactsSource s;IntervalCoreModel m(c);
 assert(issue(m,s,0x400000,0x800000,21).dtlb_miss);
 assert(issue(m,s,0x401000,0x801000,21,2).dtlb_hit);
 // Same single LRU holds 64 entries, independent of their page sizes.
 for(unsigned i=0;i<63;++i)assert(issue(m,s,0x1000000+i*4096,0x2000000+i*4096,12,3+i).dtlb_miss);
 assert(issue(m,s,0x401000,0x801000,21,2).dtlb_hit);
 assert(issue(m,s,0x2000000,0x3000000,12,90).dtlb_miss);
 assert(issue(m,s,0x1000000,0x2000000,12,3).dtlb_miss);
 // Splitting and remerging while the old walk is pending must not resurrect it.
 IntervalCoreModel pending(c);const auto original=issue(pending,s,0x400000,0x800000,21);
 assert(issue(pending,s,0x400000,0x800000,12).dtlb_miss);
 assert(issue(pending,s,0x400000,0x800000,21).dtlb_miss);
 pending.set_frontend_floor(110);
 const auto follower=issue(pending,s,0x401000,0x801000,21,2);
 assert(follower.dtlb_timing_miss && !follower.dtlb_timing_hit);
 assert(follower.dtlb_fill_generation!=original.dtlb_fill_generation);
 // Different PTE placement invalidates even an unchanged leaf/PA.
 IntervalCoreModel changed(c);s.page(0x400000,0x800000,21);
 (void)changed.schedule(load(0x800000),false,false,0,false,&s,nullptr,0x1000);
 s.facts[0].mapping.pte_addresses[2]+=8;
 assert(changed.schedule(load(0x800000),false,false,0,false,&s,nullptr,0x1000).dtlb_miss);
 // Global translations survive CR3 switches, non-global translations do not.
 IntervalCoreModel globals(c);s.page(0x400000,0x800000,21);s.facts[0].mapping.flags|=fst::kTranslationGlobal;
 assert(globals.schedule(load(0x800000),false,false,0,false,&s,nullptr,0x1000).dtlb_miss);
 s.facts[0].mapping.address_space_id=0x2000;
 assert(globals.schedule(load(0x800000),false,false,0,false,&s,nullptr,0x2000).dtlb_hit);
 // Replacing a global mapping invalidates current-ASID local state and vice versa.
 // Run before and after the original walk finishes; neither old fill may revive.
 for(bool first_global:{false,true})for(bool finished:{false,true}) {
  IntervalCoreModel replacement(c);
  auto access=[&](bool global,uint64_t pa) {
   s.page(0x400000,pa,21);
   if(global)s.facts[0].mapping.flags|=fst::kTranslationGlobal;
   return replacement.schedule(load(pa),false,false,0,false,&s,nullptr,0x1000);
  };
  auto first=access(first_global,0x800000);assert(first.dtlb_miss);
  if(finished) {
   replacement.set_frontend_floor(110);
   assert(access(first_global,0x800000).dtlb_timing_hit);
  }
  assert(access(!first_global,0xa00000).dtlb_miss);
  assert(access(first_global,0x800000).dtlb_miss);
  replacement.set_frontend_floor(finished?210:110);
  auto last=access(first_global,0x800000);
  assert(last.dtlb_timing_miss&&!last.dtlb_timing_hit);
  assert(last.dtlb_fill_generation!=first.dtlb_fill_generation);
 }
 // Same-leaf split fragments share one lookup; different leaves need two.
 IntervalCoreModel split(c);s.page(0x400000,0x800000,21);
 auto second=s.facts.front();second.virtual_address+=8;second.physical_address+=8;s.facts.push_back(second);
 auto wide=load(0x800000,0);wide.size=16;
 assert(split.schedule(wide,false,false,0,false,&s,nullptr,0x1000).translation_accesses==1);
 IntervalCoreModel two(c);s.page(0x400ff8,0x800ff8,12);second=s.facts.front();
 second.virtual_address=0x401000;second.physical_address=0x900000;
 second.mapping.virtual_base=0x401000;second.mapping.physical_base=0x900000;
 s.facts.push_back(second);wide.address=0x800ff8;
 auto twopages=two.schedule(wide,false,false,0,false,&s,nullptr,0x1000);
 assert(twopages.translation_accesses==2&&twopages.translation_misses==2&&twopages.translation_walks.size()==2);
 // Coalescing is counted independently for each distinct leaf.
 auto cc=c;cc.dtlb.coalesce_misses=true;cc.dtlb.page_walkers=2;
 IntervalCoreModel coalesced(cc);
 s.page(0x400ff8,0x800ff8,12);
 auto leaf1=s.facts.front();auto leaf2=leaf1;
 leaf2.virtual_address=0x401000;leaf2.physical_address=0x900000;
 leaf2.mapping.virtual_base=0x401000;leaf2.mapping.physical_base=0x900000;
 assert(coalesced.schedule(load(0x800ff8),false,false,0,false,&s,nullptr,0x1000).dtlb_timing_miss);
 s.facts={leaf1,leaf2};
 auto mixed=coalesced.schedule(wide,false,false,0,false,&s,nullptr,0x1000);
 assert(mixed.translation_accesses==2&&mixed.translation_timing_misses==1&&mixed.translation_timing_merged_misses==1);
 auto merged=coalesced.schedule(wide,false,false,0,false,&s,nullptr,0x1000);
 assert(merged.translation_accesses==2&&merged.translation_timing_misses==0&&merged.translation_timing_merged_misses==2);
 // Nonpaged MSR has explicit evidence but does not allocate a DTLB entry.
 fst::TranslationMapping b;b.address_space_id=0x1000;b.kind=fst::TranslationKind::kMsr;
 b.virtual_base=0x1600000000;b.request_physical_base=b.virtual_base;b.request_virtual_base=b.virtual_base;
 s.facts={{b.virtual_base,b.virtual_base,8,b}};
 assert(!pending.schedule(load(b.virtual_base),false,false,0,false,&s,nullptr,0x1000).dtlb_access);
 // A declared dynamic source may never quietly use the old 4KiB token fallback.
 s.facts.clear();bool rejected=false;
 try {(void)pending.schedule(load(0x9000),false,false,0,false,&s,nullptr,0x1000);}catch(const std::runtime_error&){rejected=true;}
 assert(rejected);
 std::cout<<"dynamic DTLB tests passed\n";
}
