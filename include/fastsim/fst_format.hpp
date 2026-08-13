#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace fastsim {

inline constexpr std::array<char, 8> kFstMagic{
    'F', 'S', 'T', 'R', 'C', '0', '1', '\0'};
inline constexpr std::uint32_t kFstVersion = 6;
inline constexpr std::uint64_t kFstFeatureVirtualPageTokens = 1ull << 0;
inline constexpr std::uint64_t kFstFeatureSyscallMarkers = 1ull << 1;
inline constexpr std::uint64_t kFstFeatureDestinationClassCounts = 1ull << 2;
inline constexpr std::int16_t kSyscallOpClass = -1;
inline constexpr std::uint32_t kDestinationClassCountsMarker = 1u << 31;
inline constexpr std::size_t kTrackedRegisterClasses = 4;

enum TraceFlag : std::uint16_t {
    kRetires = 1u << 0,
    kLoad = 1u << 1,
    kStore = 1u << 2,
    kAtomic = 1u << 3,
    kBranch = 1u << 4,
    kConditional = 1u << 5,
    kIndirect = 1u << 6,
    kCall = 1u << 7,
    kReturn = 1u << 8,
    kTaken = 1u << 9,
    kMicroOp = 1u << 10,
    kLastMicroOp = 1u << 11,
    kPhysicalAddress = 1u << 12,
    kSerialize = 1u << 13,
    kBranchOutcomeValid = 1u << 14,
    kVirtualPageToken = 1u << 15,
};

inline std::uint16_t operator|(std::uint16_t left, TraceFlag right) {
    return static_cast<std::uint16_t>(
        left | static_cast<std::uint16_t>(right));
}

inline std::uint16_t operator|(TraceFlag left, TraceFlag right) {
    return static_cast<std::uint16_t>(
        static_cast<std::uint16_t>(left) |
        static_cast<std::uint16_t>(right));
}

inline bool has_flag(std::uint16_t flags, TraceFlag flag) {
    return (flags & static_cast<std::uint16_t>(flag)) != 0;
}

// Stable on-disk FST record. Keep this header dependency-light: the vendored
// gem5 DR adapter includes it directly and writes the same bytes FastSim reads.
struct TraceRecord {
    std::uint64_t pc = 0;
    std::uint64_t address = 0;
    std::uint64_t target = 0;
    std::uint64_t next_pc = 0;
    std::array<std::uint32_t, 4> producer_dists{};
    std::uint16_t size = 0;
    std::uint16_t flags = kRetires;
    std::int16_t op_class = 0;
    std::uint8_t n_src = 0;
    std::uint8_t n_dst = 0;
    std::array<std::uint8_t, 4> producer_classes{255, 255, 255, 255};
    std::uint32_t reserved = 0;

    bool retires() const { return has_flag(flags, kRetires); }
    bool is_memory() const {
        return has_flag(flags, kLoad) || has_flag(flags, kStore) ||
               has_flag(flags, kAtomic);
    }
    bool is_write() const {
        return has_flag(flags, kStore) || has_flag(flags, kAtomic);
    }
    bool is_syscall() const { return op_class == kSyscallOpClass; }
    bool is_serializing() const {
        return is_syscall() || has_flag(flags, kSerialize);
    }
    std::uint64_t syscall_number() const { return address; }
    void set_syscall_number(std::uint64_t number) { address = number; }
    bool has_destination_class_counts() const {
        return (reserved & kDestinationClassCountsMarker) != 0;
    }
    std::uint32_t virtual_page_token() const {
        return reserved & ~kDestinationClassCountsMarker;
    }
    std::uint8_t producer_class(std::size_t index) const {
        const auto encoded = producer_classes.at(index);
        if (!has_destination_class_counts()) return encoded;
        const auto value = static_cast<std::uint8_t>(encoded & 0x7u);
        return value == 0x7u ? 255u : value;
    }
    std::uint8_t destination_class_count(std::size_t index) const {
        if (!has_destination_class_counts()) return 0;
        return static_cast<std::uint8_t>(producer_classes.at(index) >> 3);
    }
    std::array<std::uint8_t, kTrackedRegisterClasses>
    destination_class_counts() const {
        std::array<std::uint8_t, kTrackedRegisterClasses> counts{};
        for (std::size_t index = 0; index < counts.size(); ++index) {
            counts[index] = destination_class_count(index);
        }
        return counts;
    }
    void set_register_class_metadata(
        const std::array<std::uint8_t, kTrackedRegisterClasses>& producers,
        const std::array<std::uint8_t, kTrackedRegisterClasses>& destinations) {
        for (std::size_t index = 0; index < producer_classes.size(); ++index) {
            const auto producer = producers[index] == 255 ? 7u : producers[index];
            producer_classes[index] = static_cast<std::uint8_t>(
                (destinations[index] << 3) | producer);
        }
        reserved |= kDestinationClassCountsMarker;
    }
};
static_assert(sizeof(TraceRecord) == 64,
              "canonical FST record must remain 64 bytes");

struct FstHeader {
    std::array<char, 8> magic = kFstMagic;
    std::uint32_t version = kFstVersion;
    std::uint32_t header_size = sizeof(FstHeader);
    std::uint32_t record_size = sizeof(TraceRecord);
    std::uint32_t core_id = 0;
    std::uint64_t record_count = 0;
    std::uint64_t feature_flags = 0;
    std::array<std::uint64_t, 4> reserved{};
};
static_assert(sizeof(FstHeader) == 72,
              "canonical FST header must remain 72 bytes");

}  // namespace fastsim
