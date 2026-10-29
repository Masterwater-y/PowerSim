#pragma once
#include <array>
#include <cstdint>
#include <tuple>
#include <vector>

namespace fastsim::fst {
// Functional translation only. Never carry TLB/cache outcomes or target time.
constexpr std::uint8_t kTranslationLeafKnown=1, kTranslationPathKnown=2,
    kTranslationGlobal=4;
constexpr std::uint64_t kDynamicTranslations=1ull<<9;
struct TranslationMapping {
    std::uint64_t address_space_id=0,walk_root=0,virtual_base=0,physical_base=0;
    std::array<std::uint64_t,8> pte_addresses{};
    std::uint16_t pcid=0;
    std::uint8_t page_bits=0,levels=0,flags=0;
    auto identity() const {
        return std::tie(address_space_id,walk_root,virtual_base,physical_base,
                        pte_addresses,pcid,page_bits,levels,flags);
    }
    bool operator<(const TranslationMapping& other) const {return identity()<other.identity();}
};
struct TranslationFragment {
    std::uint64_t virtual_address=0,physical_address=0;
    std::uint32_t size=0;
    TranslationMapping mapping;
};
}
