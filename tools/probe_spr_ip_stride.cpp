// Offline functional-input coverage probe. Program-order candidates are not
// target cache predictions and do not simulate queues, misses or prefetches.
#include "fastsim/trace.hpp"
#include "fastsim/spr_ip_stride.hpp"
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
using namespace fastsim;
int main(int argc,char** argv) {
 try {
  if(argc!=5) throw std::runtime_error("need core path warmup_records measurement_records");
  const auto core=std::stoul(argv[1]);const auto warm=std::stoull(argv[3]);const auto measured=std::stoull(argv[4]);
  BinaryTraceSource trace(argv[2]);SprIpStride predictor;TraceRecord record;
  std::uint64_t ordinal=0,loads=0,eligible=0,unknown=0,unsupported=0,missing=0,candidates=0,trained=0;
  while(trace.next(record)) {
   const bool in_measurement=ordinal>=warm && ordinal<warm+measured;
   if(record.is_memory() && !record.is_write() && has_flag(record.flags,kLoad)) {
    if(in_measurement) ++loads;
    const auto allowed=trace.current_ordinary_load_eligible();
    if(!allowed) {if(in_measurement)++unknown;}
    else if(!*allowed) {if(in_measurement)++unsupported;}
    else {
     if(in_measurement)++eligible;
     const auto& fragments=trace.current_translation_fragments();
     if(fragments.empty() || fragments[0].physical_address!=record.address ||
        fragments[0].mapping.kind!=fst::TranslationKind::kPaged ||
        !(fragments[0].mapping.flags & fst::kTranslationLeafKnown)) {if(in_measurement)++missing;}
     else {
      if(in_measurement)++trained;
      const auto& fragment=fragments[0];
      if(predictor.observe(record.pc,core,false,fragment.virtual_address,fragment.physical_address) && in_measurement)++candidates;
     }
    }
   }
   ++ordinal;
  }
  if(ordinal!=warm+measured)throw std::runtime_error("record population mismatch");
  std::cout<<"{\"core\":"<<core<<",\"records\":"<<ordinal<<",\"warmup_records\":"<<warm
   <<",\"measured_records\":"<<measured<<",\"measured_load_uops\":"<<loads
   <<",\"ordinary_loads\":"<<eligible<<",\"unknown_eligibility\":"<<unknown
   <<",\"nonordinary\":"<<unsupported<<",\"missing_translation\":"<<missing
   <<",\"trained_measurement_uops\":"<<trained<<",\"candidates_in_program_order\":"<<candidates<<"}\n";
 } catch(const std::exception& e) {std::cerr<<e.what()<<'\n';return 1;}
}
