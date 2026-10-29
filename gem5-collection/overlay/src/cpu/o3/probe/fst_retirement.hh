#pragma once
#include <array>
#include <cstdint>

// Portable functional instruction origin, never an observed PMU/cache result.
// HardwareEntry identifies simulator-generated external interrupt/trap entry
// microcode with no decoded architectural macro instruction. Keep its work in
// the timing stream; internal memory operations are not retired x86 loads.
namespace fastsim::fst {
constexpr std::uint64_t kFunctionalRetirement = 1ull << 7;
struct RetirementHeader {
    std::array<char,8> magic{'F','S','T','R','E','T','V','1'};
    std::uint32_t version=2,header_size=56,core_id=0,flags=1;
    std::uint64_t record_count=0,row_count=0,stream_hash=14695981039346656037ull;
    std::uint32_t row_size=16,reserved=0;
};
struct RetirementRow {
    std::uint64_t record_ordinal=0;
    // bit0=hardware entry, bit1=entry follows an independently accepted
    // precise fault abandoning an incomplete macro prefix (version2 only).
    std::uint32_t hot_record_hash=0,hardware_entry=1;
};
static_assert(sizeof(RetirementHeader)==56);
static_assert(sizeof(RetirementRow)==16);
}
