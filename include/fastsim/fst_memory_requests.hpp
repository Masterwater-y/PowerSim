#pragma once
#include <array>
#include <cstddef>
#include <cstdint>

// Functional request semantics only: no observed cache outcome or native time.
// The sparse companion is mandatory when this FST feature bit is set. Absence
// of a row means Data only in a fully declared stream; legacy means unknown.
namespace fastsim::fst {
constexpr std::uint64_t kMemoryRequestRoles = 1ull << 6;
// SoftwarePrefetch is a read-only ISA hint with no architectural load
// result. QEMU/DR producers must decode this property, not infer it from PC,
// response time, or a cache outcome. Never use this role for store-prepare.
enum class MemoryRequestRole : std::uint8_t { Data = 0, NoAccess = 1, SoftwarePrefetch = 2 };
constexpr std::uint64_t kRequestStreamHashSeed = 14695981039346656037ull;
struct MemoryRequestHeader {
    std::array<char,8> magic{'F','S','T','M','R','E','Q','1'};
    std::uint32_t version=2, header_size=56, core_id=0, flags=1;
    std::uint64_t record_count=0, row_count=0, stream_hash=kRequestStreamHashSeed;
    std::uint32_t row_size=24, reserved=0;
};
struct MemoryRequestRow {
    std::uint64_t record_ordinal=0;
    std::uint32_t hot_record_hash=0, micro_pc=0;
    MemoryRequestRole role=MemoryRequestRole::NoAccess;
    std::array<std::uint8_t,7> reserved{};
};
static_assert(sizeof(MemoryRequestHeader)==56);
static_assert(sizeof(MemoryRequestRow)==24);
inline std::uint64_t memory_request_stream_hash(std::uint64_t hash, const void* record) {
    const auto* bytes=static_cast<const unsigned char*>(record);
    for (std::size_t i=0;i<64;++i) hash=(hash^bytes[i])*1099511628211ull;
    return hash;
}
}
