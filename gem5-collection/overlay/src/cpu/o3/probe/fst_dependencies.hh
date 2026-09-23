#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

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

inline std::uint32_t
dependencyRecordHash(const void *record)
{
    const auto *bytes = static_cast<const unsigned char *>(record);
    std::uint32_t hash = 2166136261u;
    for (std::size_t index = 0; index != 64; ++index) {
        hash = (hash ^ bytes[index]) * 16777619u;
    }
    return hash;
}
} // namespace fastsim::fst
