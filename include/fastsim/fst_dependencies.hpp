#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

// Portable on-disk declarations also used by the TaoTrace producer. The main
// FST record stays 64 bytes. Distances are strictly increasing unique producer
// identities; the first four remain inline, the rest are streamed in .deps.
namespace fastsim::fst {
constexpr std::uint64_t kCompleteDependencies = 1ull << 5;
struct DependencyHeader {
    std::array<char, 8> magic{'F', 'S', 'T', 'D', 'E', 'P', '1', '\0'};
    std::uint32_t version = 1;
    std::uint32_t header_size = 48;
    std::uint32_t core_id = 0;
    std::uint32_t flags = 0;
    std::uint64_t record_count = 0;
    std::uint64_t extension_count = 0;
    std::uint64_t extra_distance_count = 0;
};
struct DependencyRow {
    std::uint64_t record_ordinal = 0;
    std::uint32_t extra_count = 0;
    std::uint32_t hot_record_hash = 0;
};
static_assert(sizeof(DependencyHeader) == 48);
static_assert(sizeof(DependencyRow) == 16);
// Binds each sparse extension to its exact hot record, including the first
// four edges. This detects accidentally mismatched companion files.
inline std::uint32_t dependency_record_hash(const void* record) {
    const auto* bytes = static_cast<const unsigned char*>(record);
    std::uint32_t hash = 2166136261u;
    for (std::size_t i = 0; i != 64; ++i) hash = (hash ^ bytes[i]) * 16777619u;
    return hash;
}
}  // namespace fastsim::fst
