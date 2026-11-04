#pragma once
#include <algorithm>
#include <array>
#include <cstdint>
namespace fastsim::fst {
// Functional facts for EVERY retired memory record, including reads. The .sreq
// name is retained for the store-qualification extension. No native outcomes.
// Version 1 request_flags use the gem5-spr Request::Flags layout (request.hh).
constexpr std::uint64_t kStoreRequestFacts = 1ull << 13;
enum StoreFactAttribute : std::uint32_t {
    kStoreFactsComplete=1u<<0, kStoreOrdinary=1u<<1, kStoreNoAccess=1u<<2,
    kStoreSplit=1u<<3, kStoreMasked=1u<<4, kStoreSecure=1u<<5,
    kStoreAtomic=1u<<6, kStoreUncacheable=1u<<7, kStoreStrictlyOrdered=1u<<8,
    kStoreHtm=1u<<9, kStoreSpecial=1u<<10,
    kMemoryRead=1u<<11, kOrdinaryLoad=1u<<12,
    kStorePagedFragments=1u<<13 // v2: PA identity is per bound .xlat fragment
};
constexpr std::uint32_t kKnownStoreAttributes=(1u<<14)-1;
struct StoreRequestHeader {
    std::array<char,8> magic{'F','S','T','S','R','E','Q','1'};
    std::uint32_t version=1,header_size=56,core_id=0,flags=0;
    std::uint64_t record_count=0,row_count=0,stream_hash=14695981039346656037ull;
    std::uint32_t row_size=56,reserved=0;
};
struct StoreRequestFacts {
    std::uint64_t record_ordinal=0;
    std::uint32_t hot_record_hash=0,micro_pc=0;
    std::uint64_t address=0,request_flags=0,byte_enable=0;
    std::uint32_t size=0,attributes=0,context=0,requestor=0;
};
static_assert(sizeof(StoreRequestHeader)==56);
static_assert(sizeof(StoreRequestFacts)==56);
} // namespace fastsim::fst

namespace fastsim::fst {
// No inferred physical continuation across a virtual-page boundary. The
// dynamic translation decoder separately validates mappings and stream hashes.
template<class Fragments>
bool paged_request_fragments_match(std::uint64_t address,std::uint32_t size,const Fragments& fragments) {
    if(!size || size>64 || fragments.empty() || fragments.size()>2 || fragments.front().physical_address!=address)return false;
    std::uint64_t covered=0;const auto base=fragments.front().virtual_address;
    if(base>UINT64_MAX-(size-1))return false;
    for(const auto& f:fragments) {
        if(!f.size || f.size>size-covered || f.virtual_address!=base+covered ||
           f.physical_address>UINT64_MAX-(f.size-1) || static_cast<unsigned>(f.mapping.kind)!=0 ||
           !(f.mapping.flags&1) || f.mapping.address_space_id!=fragments.front().mapping.address_space_id ||
           f.mapping.pcid!=fragments.front().mapping.pcid)return false;
        covered+=f.size;
    }
    return covered==size;
}
template<class Fragments>
bool paged_request_packet_matches(std::uint64_t va,std::uint64_t pa,std::uint32_t size,const Fragments& fragments) {
    if(!size || va>UINT64_MAX-(size-1) || pa>UINT64_MAX-(size-1))return false;
    std::uint64_t covered=0;
    for(const auto& f:fragments) {
        if(!f.size || f.virtual_address>UINT64_MAX-(f.size-1))return false;
        const auto cursor=va+covered;
        if(cursor<f.virtual_address || cursor-f.virtual_address>=f.size)continue;
        const auto offset=cursor-f.virtual_address;
        const auto part=std::min<std::uint64_t>(size-covered,f.size-offset);
        if(f.physical_address>UINT64_MAX-offset || f.physical_address+offset!=pa+covered)return false;
        covered+=part;
        if(covered==size)return true;
    }
    return false;
}
} // namespace fastsim::fst
