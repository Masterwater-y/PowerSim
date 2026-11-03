#pragma once
#include "fastsim/config.hpp"
#include <limits>
#include <stdexcept>

namespace fastsim {
constexpr std::uint64_t kDramGiB = 1ull << 30;
inline bool dram_is_split(const DramConfig& d) {
    return d.memory_layout == "x86-low3g-high4g-v1";
}
inline void validate_dram_layout(const DramConfig& d, std::uint32_t line_size) {
    if (d.memory_layout == "contiguous") return;
    if (!dram_is_split(d) || !line_size || d.channels < 2 ||
        (d.channels & (d.channels - 1)) || d.size_bytes <= 3 * kDramGiB ||
        d.size_bytes > std::numeric_limits<std::uint64_t>::max() - kDramGiB)
        throw std::invalid_argument("invalid dram.memory_layout or split geometry");
    const auto stripe = std::uint64_t{line_size} * (d.channels / 2);
    if ((3 * kDramGiB) % stripe || (4 * kDramGiB) % stripe ||
        (d.size_bytes - 3 * kDramGiB) % stripe)
        throw std::invalid_argument("split DRAM ranges must align to channel stripes");
}
inline std::uint64_t dram_physical_extent(const DramConfig& d) {
    return d.size_bytes + (dram_is_split(d) ? kDramGiB : 0);
}
inline bool dram_contains_address(const DramConfig& d, std::uint64_t address) {
    if (!dram_is_split(d)) return address < d.size_bytes;
    return address < 3 * kDramGiB ||
        (address >= 4 * kDramGiB && address < dram_physical_extent(d));
}
inline bool dram_contains_span(const DramConfig& d, std::uint64_t first,
                               std::uint64_t last) {
    return first <= last && dram_contains_address(d, first) &&
        dram_contains_address(d, last) &&
        (!dram_is_split(d) || first >= 4 * kDramGiB || last < 3 * kDramGiB);
}
inline bool dram_contains_line(const DramConfig& d, std::uint64_t line,
                               std::uint32_t line_size) {
    if (!line_size || line > std::numeric_limits<std::uint64_t>::max() / line_size)
        return false;
    return dram_contains_address(d, line * line_size);
}
// Packed offsets are only for synthetic placement. Never compact cache tags.
inline std::uint64_t dram_physical_from_offset(const DramConfig& d,
                                              std::uint64_t offset) {
    if (offset >= d.size_bytes) throw std::out_of_range("DRAM packed offset");
    return offset + (dram_is_split(d) && offset >= 3 * kDramGiB ? kDramGiB : 0);
}
struct DramLineAddress { std::uint32_t channel; std::uint64_t local_line; };
inline DramLineAddress dram_decode_line(const DramConfig& d, std::uint64_t line,
                                       std::uint32_t line_size) {
    if (!dram_contains_line(d, line, line_size))
        throw std::out_of_range("physical address outside DRAM ranges");
    auto channels = d.channels;
    std::uint32_t base = 0;
    if (dram_is_split(d)) {
        channels /= 2;
        if (line >= 4 * kDramGiB / line_size) {
            base = channels;
            line -= 4 * kDramGiB / line_size;
        }
    }
    return {base + static_cast<std::uint32_t>(line % channels), line / channels};
}
} // namespace fastsim
