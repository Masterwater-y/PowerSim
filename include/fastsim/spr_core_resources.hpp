#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string_view>

namespace fastsim {
enum class SprIqDomain : std::uint8_t { Execution, Memory, StoreData };
enum class SprIqRelease : std::uint8_t { Issue, Complete };
struct SprIqProfile { std::array<std::size_t, 3> capacity; };
struct SprIqHandle { std::uint16_t slot; std::uint64_t generation; };

// One instance per core. Members may share one reservation; each has its own
// stale-safe handle and releases at its own issue/completion boundary.
class SprPartitionedIq {
public:
    explicit SprPartitionedIq(SprIqProfile profile);
    static SprIqDomain domain_for(std::string_view op_class);
    std::optional<SprIqHandle> acquire(std::string_view op_class, bool fu_capable, bool memory);
    std::optional<SprIqHandle> join(SprIqHandle existing, std::string_view op_class, bool memory);
    bool release(SprIqHandle member, SprIqRelease boundary);
    bool schedule_release(SprIqHandle member, SprIqRelease boundary, std::uint64_t cycle);
    void advance(std::uint64_t cycle);
    std::size_t used(SprIqDomain domain) const;
private:
    struct Group { bool active = false; SprIqDomain domain{}; std::size_t members = 0; };
    struct Member { bool active = false; bool memory = false; std::uint16_t group = 0; std::uint64_t generation = 0; bool scheduled = false; std::uint64_t release_cycle = 0; };
    static constexpr std::size_t kGroups = 248, kMembers = 512;
    SprIqProfile profile_;
    bool advanced_ = false;
    std::uint64_t cycle_ = 0;
    std::array<std::size_t, 3> used_{};
    std::array<Group, kGroups> groups_{};
    std::array<Member, kMembers> members_{};
    std::optional<SprIqHandle> insert_member(std::size_t group, bool memory);
};

// Port occupancy and per-operation issue intervals remain the caller's
// responsibility. Eligible is the intersection of those constraints.
class SprPortRoundRobin {
public:
    int probe(unsigned family, std::uint16_t candidates, std::uint16_t eligible) const;
    bool commit(unsigned family, std::uint16_t candidates, unsigned port);
private:
    std::array<std::array<std::uint8_t, 4096>, 6> cursors_{};
};
// Keep one ring per OpClass, with a stable capability mask. attempt() is
// a genuine hardware arbitration attempt, not a speculative availability
// query: an all-busy scan advances the native FU ring by one entry.
class SprGenericFuRing {
public:
    int attempt(std::uint16_t candidates, std::uint16_t eligible);
    int probe(std::uint16_t candidates, std::uint16_t eligible) const;
    bool commit(std::uint16_t candidates, unsigned unit);
private:
    std::uint8_t cursor_ = 0;
};
struct SprLoadBankRequest {
    std::uint64_t paddr;
    bool cacheable_ordinary_load;
    bool strictly_ordered;
    bool prefetch;
};
// Call per packet/fragment only at actual memory send admission. A failed send
// never changes counters. Accesses must be processed in nondecreasing cycle.
class SprLoadBanks {
public:
    bool available(std::uint64_t cycle, const SprLoadBankRequest& request) const;
    bool note_send(std::uint64_t cycle, const SprLoadBankRequest& request, bool sent);
private:
    struct Bank { std::uint64_t cycle = 0; std::uint8_t uses = 0; bool valid = false; };
    std::array<Bank, 16> banks_{};
};
}
