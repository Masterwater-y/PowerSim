#pragma once
#include "fastsim/fst_translation_facts.hpp"
#include "fastsim/fst_store_requests.hpp"
#include "fastsim/types.hpp"
#include <cstdint>
#include <optional>
#include <vector>

namespace fastsim {
// Only functional CPU-stream metadata is accepted here. This is the narrow
// first subdomain for L1 IP stride; split/special requests remain explicitly
// unsupported, and no guessed virtual address or context enters the model.
enum class SprPrefetchInputStatus {
    kEligible, kNotDemandLoad, kNotEligible, kUnknownEligibility,
    kUnknownContext, kUnknownTranslation, kSplitUnsupported
};
struct SprPrefetchInput {
    SprPrefetchInputStatus status=SprPrefetchInputStatus::kNotDemandLoad;
    std::uint64_t pc=0,context=0,virtual_address=0,physical_address=0;
};
inline SprPrefetchInput spr_prefetch_input(
    const TraceRecord& record,std::optional<bool> ordinary_load,
    std::optional<std::uint64_t> cpu_context,
    const std::vector<fst::TranslationFragment>& fragments,
    const fst::StoreRequestFacts* request) {
    using S=SprPrefetchInputStatus;
    if(!has_flag(record.flags,kLoad) || record.is_write() || has_flag(record.flags,kAtomic))
        return {S::kNotDemandLoad};
    if(!ordinary_load)return {S::kUnknownEligibility};
    if(!*ordinary_load)return {S::kNotEligible};
    if(!request || !(request->attributes & fst::kStoreFactsComplete) ||
       !(request->attributes & fst::kMemoryRead))return {S::kUnknownEligibility};
    if(request->attributes & fst::kStoreSecure)return {S::kNotEligible};
    if(request->attributes & fst::kStoreSplit)return {S::kSplitUnsupported};
    if(!cpu_context)return {S::kUnknownContext};
    if(!has_flag(record.flags,kPhysicalAddress) || !record.size || fragments.empty())
        return {S::kUnknownTranslation};
    if(fragments.size()!=1 || (record.address & 63u)+record.size>64)
        return {S::kSplitUnsupported};
    const auto& f=fragments.front();
    if(f.mapping.kind!=fst::TranslationKind::kPaged)return {S::kNotEligible};
    if(!(f.mapping.flags & fst::kTranslationLeafKnown) || f.mapping.page_bits<12 ||
       f.mapping.page_bits>63 || f.physical_address!=record.address ||
       f.size<record.size || ((f.virtual_address^f.physical_address)&4095u))
        return {S::kUnknownTranslation};
    return {S::kEligible,record.pc,*cpu_context,f.virtual_address,f.physical_address};
}
}
