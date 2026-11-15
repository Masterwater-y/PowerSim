#pragma once
#include "fastsim/fst_store_requests.hpp"
#include "fastsim/types.hpp"
#include <cstdint>
#include <limits>
#include <optional>

namespace fastsim {
// Owned functional values for one ordinary, single-line store. This value may
// outlive TraceSource::next(); it never retains a pointer into a reader. `line`
// is a 64-byte line number and mask bit 0 is the line's first byte. It carries
// no store data, memory/cache outcomes, native readiness, or native timestamps.
struct SprStoreInput {
    std::uint64_t line, byte_mask, flags;
    std::uint32_t context, requestor;
};

// Empty means unknown/ineligible and requires the caller to preserve program
// ordering and take its conservative path. It does NOT authorize bypassing a
// special request or inventing zero-valued missing fields. Context/requestor
// zero are valid only when an explicit complete facts row certifies them.
// Context/requestor and Request::Flags have producer-specific encodings;
// callers must independently establish their equality semantics for a producer.
inline std::optional<SprStoreInput> spr_store_input(
    const TraceRecord& record, const fst::StoreRequestFacts* facts) {
    if (!has_flag(record.flags,kStore) || has_flag(record.flags,kLoad) ||
        has_flag(record.flags,kAtomic) || !has_flag(record.flags,kPhysicalAddress) || !facts)
        return std::nullopt;
    constexpr auto required=fst::kStoreFactsComplete | fst::kStoreOrdinary;
    constexpr auto excluded=fst::kStoreNoAccess | fst::kStoreSplit | fst::kStoreMasked |
        fst::kStoreSecure | fst::kStoreAtomic | fst::kStoreUncacheable |
        fst::kStoreStrictlyOrdered | fst::kStoreHtm | fst::kStoreSpecial |
        fst::kMemoryRead | fst::kOrdinaryLoad | fst::kStorePagedFragments;
    if ((facts->attributes & ~fst::kKnownStoreAttributes) ||
        (facts->attributes & required)!=required || (facts->attributes & excluded))
        return std::nullopt;
    // Same functional Request::Flags exclusions as the .sreq schema validator,
    // plus SECURE, which this intentionally narrow first slice excludes.
    constexpr std::uint64_t forbidden_flags=
        0x400ull | 0x800ull | 0x10000ull | 0x40000ull | 0x80000ull |
        0x100000ull | 0x200000ull | 0xc00000ull | 0x1000000ull |
        0x10000000ull | 0xc0000000ull | 0x300000000ull |
        0xf0000000000ull | 0xf00000000000ull | 0x1000000000000ull |
        0x2000000000000ull | 0x20000000000000ull;
    if ((facts->request_flags & forbidden_flags) || !record.size || record.size>64 ||
        facts->address!=record.address || facts->size!=record.size ||
        record.address>std::numeric_limits<std::uint64_t>::max()-(record.size-1))
        return std::nullopt;
    const auto offset=record.address & 63u;
    if (record.size>64-offset) return std::nullopt;
    // Avoid undefined 1<<64 and ensure an all-bytes-enabled request; a masked
    // or contradictory record is not silently normalized into an ordinary one.
    const auto full=record.size==64 ? ~std::uint64_t{0} :
        (std::uint64_t{1}<<record.size)-1;
    if(facts->byte_enable!=full)return std::nullopt;
    return SprStoreInput{record.address/64,full<<offset,facts->request_flags,
                         facts->context,facts->requestor};
}
} // namespace fastsim
