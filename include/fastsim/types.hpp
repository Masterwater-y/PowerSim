#pragma once

#include <algorithm>
#include <array>
#include <cstdint>
#include <string>
#include <vector>

namespace fastsim {

// Canonical trace op classes are gem5 OpClass values and therefore
// non-negative.  Negative values are reserved for FastSim functional event
// markers that must survive the compact binary trace without consuming one of
// the already-full 16 flag bits.
constexpr std::int16_t kSyscallOpClass = -1;
constexpr std::uint32_t kDestinationClassCountsMarker = 1u << 31;
constexpr std::size_t kTrackedRegisterClasses = 4;

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
    // `TraceRecord::reserved` contains an opaque, per-trace virtual-page
    // identity. The physical byte address remains in `address`.
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
    std::array<std::uint8_t, 4> producer_classes{
        255, 255, 255, 255};
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
    // On a syscall marker record the memory-address block is skipped
    // (`simulator.cpp` guards it with `!record.is_memory()`), so `address`
    // is unused and carries the syscall number instead.  Non-syscall callers
    // must never read this.  Zero means "no sysnum recorded" and selects the
    // scalar fallback cost.
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
            const auto producer = producers[index] == 255
                                      ? 7u
                                      : producers[index];
            producer_classes[index] = static_cast<std::uint8_t>(
                (destinations[index] << 3) | producer);
        }
        reserved |= kDestinationClassCountsMarker;
    }
};
static_assert(sizeof(TraceRecord) == 64,
              "canonical trace record must stay compact");

enum class HitLevel : std::uint8_t {
    kL1 = 0,
    kL2 = 1,
    kLlc = 2,
    kRemote = 3,
    kMemory = 4,
    kUnknown = 5,
};

struct CacheCounters {
    std::uint64_t accesses = 0;
    std::uint64_t hits = 0;
    std::uint64_t misses = 0;
    std::uint64_t evictions = 0;
    std::uint64_t writebacks = 0;

    CacheCounters& operator+=(const CacheCounters& other) {
        accesses += other.accesses;
        hits += other.hits;
        misses += other.misses;
        evictions += other.evictions;
        writebacks += other.writebacks;
        return *this;
    }
};

struct BranchCounters {
    std::uint64_t branches = 0;
    std::uint64_t conditional = 0;
    std::uint64_t direction_misses = 0;
    std::uint64_t target_misses = 0;
    std::uint64_t misses = 0;
    std::uint64_t btb_hits = 0;
    std::uint64_t ras_hits = 0;

    BranchCounters& operator+=(const BranchCounters& other) {
        branches += other.branches;
        conditional += other.conditional;
        direction_misses += other.direction_misses;
        target_misses += other.target_misses;
        misses += other.misses;
        btb_hits += other.btb_hits;
        ras_hits += other.ras_hits;
        return *this;
    }
};

struct TranslationCounters {
    std::uint64_t accesses = 0;
    std::uint64_t hits = 0;
    // New page-walk allocations. Accesses coalesced with an outstanding walk
    // are reported separately to match gem5's DTLB miss accounting.
    std::uint64_t misses = 0;
    std::uint64_t merged_misses = 0;
    std::uint64_t untracked = 0;
    std::uint64_t walk_delay_cycles = 0;

    TranslationCounters& operator+=(const TranslationCounters& other) {
        accesses += other.accesses;
        hits += other.hits;
        misses += other.misses;
        merged_misses += other.merged_misses;
        untracked += other.untracked;
        walk_delay_cycles += other.walk_delay_cycles;
        return *this;
    }
};

struct ChaCounters {
    std::uint64_t requests = 0;
    std::uint64_t reads = 0;
    std::uint64_t writes = 0;
    std::uint64_t llc_hits = 0;
    std::uint64_t llc_misses = 0;
    std::uint64_t upgrades = 0;
    std::uint64_t invalidations = 0;
    std::uint64_t remote_supplies = 0;
    std::uint64_t dram_reads = 0;
    std::uint64_t dram_writes = 0;
    // A Ruby LLC demand miss can either allocate a new memory transaction or
    // merge behind an existing transient line.  Keep both populations
    // explicit: DRAM reads count only unique fills, while merged misses retain
    // the demand-side LLC miss semantics and wait for the same fill response.
    std::uint64_t llc_unique_fills = 0;
    std::uint64_t llc_merged_misses = 0;
    std::uint64_t llc_merged_wait_cycles = 0;
    std::uint64_t llc_merged_wait_max_cycles = 0;
    std::uint64_t queue_cycles = 0;

    ChaCounters& operator+=(const ChaCounters& other) {
        requests += other.requests;
        reads += other.reads;
        writes += other.writes;
        llc_hits += other.llc_hits;
        llc_misses += other.llc_misses;
        upgrades += other.upgrades;
        invalidations += other.invalidations;
        remote_supplies += other.remote_supplies;
        dram_reads += other.dram_reads;
        dram_writes += other.dram_writes;
        llc_unique_fills += other.llc_unique_fills;
        llc_merged_misses += other.llc_merged_misses;
        llc_merged_wait_cycles += other.llc_merged_wait_cycles;
        llc_merged_wait_max_cycles = std::max(
            llc_merged_wait_max_cycles,
            other.llc_merged_wait_max_cycles);
        queue_cycles += other.queue_cycles;
        return *this;
    }
};

struct SequencerCounters {
    std::uint64_t requests = 0;
    std::uint64_t buffer_full_stalls = 0;
    std::uint64_t stall_cycles = 0;
    std::uint64_t max_outstanding = 0;

    SequencerCounters& operator+=(const SequencerCounters& other) {
        requests += other.requests;
        buffer_full_stalls += other.buffer_full_stalls;
        stall_cycles += other.stall_cycles;
        max_outstanding = std::max(max_outstanding,
                                   other.max_outstanding);
        return *this;
    }
};

struct O3QueueCounters {
    std::uint64_t iq_full_events = 0;
    std::uint64_t iq_stall_cycles = 0;
    std::uint64_t iq_max_occupancy = 0;
    std::uint64_t rob_full_events = 0;
    std::uint64_t rob_stall_cycles = 0;
    std::uint64_t rob_max_occupancy = 0;
    std::uint64_t lq_full_events = 0;
    std::uint64_t lq_stall_cycles = 0;
    std::uint64_t lq_max_occupancy = 0;
    std::uint64_t sq_full_events = 0;
    std::uint64_t sq_stall_cycles = 0;
    std::uint64_t sq_max_occupancy = 0;
    std::uint64_t tso_store_stall_cycles = 0;

    O3QueueCounters& operator+=(const O3QueueCounters& other) {
        iq_full_events += other.iq_full_events;
        iq_stall_cycles += other.iq_stall_cycles;
        iq_max_occupancy = std::max(iq_max_occupancy,
                                    other.iq_max_occupancy);
        rob_full_events += other.rob_full_events;
        rob_stall_cycles += other.rob_stall_cycles;
        rob_max_occupancy = std::max(rob_max_occupancy,
                                     other.rob_max_occupancy);
        lq_full_events += other.lq_full_events;
        lq_stall_cycles += other.lq_stall_cycles;
        lq_max_occupancy = std::max(lq_max_occupancy,
                                    other.lq_max_occupancy);
        sq_full_events += other.sq_full_events;
        sq_stall_cycles += other.sq_stall_cycles;
        sq_max_occupancy = std::max(sq_max_occupancy,
                                    other.sq_max_occupancy);
        tso_store_stall_cycles += other.tso_store_stall_cycles;
        return *this;
    }
};

struct ResponseRenameCounters {
    std::uint64_t destination_uops = 0;
    std::array<std::uint64_t, kTrackedRegisterClasses> allocated{};
    std::array<std::uint64_t, kTrackedRegisterClasses> released{};
    std::array<std::uint64_t, kTrackedRegisterClasses> live{};
    std::array<std::uint64_t, kTrackedRegisterClasses> max_live{};
    std::uint64_t free_list_stall_uops = 0;
    std::uint64_t free_list_stall_cycles = 0;

    bool conserved() const {
        for (std::size_t index = 0; index < allocated.size(); ++index) {
            if (allocated[index] != released[index] + live[index]) {
                return false;
            }
        }
        return true;
    }

    ResponseRenameCounters& operator+=(
        const ResponseRenameCounters& other) {
        destination_uops += other.destination_uops;
        for (std::size_t index = 0; index < allocated.size(); ++index) {
            allocated[index] += other.allocated[index];
            released[index] += other.released[index];
            live[index] += other.live[index];
            max_live[index] = std::max(max_live[index], other.max_live[index]);
        }
        free_list_stall_uops += other.free_list_stall_uops;
        free_list_stall_cycles += other.free_list_stall_cycles;
        return *this;
    }
};

// Committed-path-only audit of the interval core's rename/dispatch window.
// Every delayed UOP is attributed to exactly one final lower-bound gate, but
// the per-UOP delay sums overlap in simulated time and are not an additive CPI
// decomposition. Legacy v5 traces expose only n_dst and therefore retain the
// class-agnostic threshold proxy. V6 traces additionally provide per-class
// architectural destination counts for the optional physical-register model.
struct CommittedPipelineAuditCounters {
    static constexpr std::array<std::uint32_t, 6>
        destination_thresholds{64, 96, 128, 160, 192, 256};

    std::uint64_t uops = 0;
    std::uint64_t destination_uops = 0;
    std::uint64_t destination_tokens = 0;
    std::uint64_t destination_release_tokens = 0;
    std::uint64_t destination_tokens_released = 0;
    std::uint64_t destination_tokens_live_at_last_rename = 0;
    std::uint64_t max_live_destination_tokens = 0;
    std::uint64_t destination_lifetime_token_cycles = 0;
    std::array<std::uint64_t, 6> destination_threshold_events{};
    std::array<std::uint64_t, 6> destination_threshold_excess_tokens{};
    std::uint64_t destination_class_uops = 0;
    std::array<std::uint64_t, kTrackedRegisterClasses>
        destination_class_tokens{};
    std::array<std::uint64_t, kTrackedRegisterClasses>
        destination_class_release_tokens{};
    std::array<std::uint64_t, kTrackedRegisterClasses>
        destination_class_tokens_released{};
    std::array<std::uint64_t, kTrackedRegisterClasses>
        destination_class_tokens_live_at_last_rename{};
    std::array<std::uint64_t, kTrackedRegisterClasses>
        destination_class_max_live_tokens{};
    std::uint64_t rename_free_list_stall_uops = 0;
    std::uint64_t rename_free_list_stall_cycles = 0;

    std::uint64_t dispatch_delayed_uops = 0;
    std::uint64_t dispatch_delay_cycles = 0;
    std::uint64_t dispatch_bandwidth_events = 0;
    std::uint64_t dispatch_bandwidth_cycles = 0;
    std::uint64_t rob_capacity_events = 0;
    std::uint64_t rob_capacity_cycles = 0;
    std::uint64_t iq_capacity_events = 0;
    std::uint64_t iq_capacity_cycles = 0;
    std::uint64_t lq_capacity_events = 0;
    std::uint64_t lq_capacity_cycles = 0;
    std::uint64_t sq_capacity_events = 0;
    std::uint64_t sq_capacity_cycles = 0;

    std::uint64_t rob_residency_cycles = 0;
    std::uint64_t rob_max_residency_cycles = 0;
    std::uint64_t iq_residency_cycles = 0;
    std::uint64_t iq_max_residency_cycles = 0;
    std::uint64_t memory_iq_post_issue_uops = 0;
    std::uint64_t memory_iq_post_issue_cycles = 0;

    std::uint64_t classified_dispatch_delay_cycles() const {
        return dispatch_bandwidth_cycles + rob_capacity_cycles +
               iq_capacity_cycles + lq_capacity_cycles +
               sq_capacity_cycles;
    }

    bool dispatch_conserved() const {
        return dispatch_delay_cycles ==
               classified_dispatch_delay_cycles();
    }

    bool destination_conserved() const {
        return destination_tokens == destination_release_tokens &&
               destination_tokens == destination_tokens_released +
                   destination_tokens_live_at_last_rename;
    }

    bool destination_classes_conserved() const {
        for (std::size_t index = 0;
             index < destination_class_tokens.size(); ++index) {
            if (destination_class_tokens[index] !=
                    destination_class_release_tokens[index] ||
                destination_class_tokens[index] !=
                    destination_class_tokens_released[index] +
                        destination_class_tokens_live_at_last_rename[index]) {
                return false;
            }
        }
        return true;
    }

    CommittedPipelineAuditCounters& operator+=(
        const CommittedPipelineAuditCounters& other) {
        uops += other.uops;
        destination_uops += other.destination_uops;
        destination_tokens += other.destination_tokens;
        destination_release_tokens += other.destination_release_tokens;
        destination_tokens_released +=
            other.destination_tokens_released;
        destination_tokens_live_at_last_rename +=
            other.destination_tokens_live_at_last_rename;
        max_live_destination_tokens = std::max(
            max_live_destination_tokens,
            other.max_live_destination_tokens);
        destination_lifetime_token_cycles +=
            other.destination_lifetime_token_cycles;
        for (std::size_t index = 0;
             index < destination_threshold_events.size(); ++index) {
            destination_threshold_events[index] +=
                other.destination_threshold_events[index];
            destination_threshold_excess_tokens[index] +=
                other.destination_threshold_excess_tokens[index];
        }
        destination_class_uops += other.destination_class_uops;
        for (std::size_t index = 0;
             index < destination_class_tokens.size(); ++index) {
            destination_class_tokens[index] +=
                other.destination_class_tokens[index];
            destination_class_release_tokens[index] +=
                other.destination_class_release_tokens[index];
            destination_class_tokens_released[index] +=
                other.destination_class_tokens_released[index];
            destination_class_tokens_live_at_last_rename[index] +=
                other.destination_class_tokens_live_at_last_rename[index];
            destination_class_max_live_tokens[index] = std::max(
                destination_class_max_live_tokens[index],
                other.destination_class_max_live_tokens[index]);
        }
        rename_free_list_stall_uops +=
            other.rename_free_list_stall_uops;
        rename_free_list_stall_cycles +=
            other.rename_free_list_stall_cycles;
        dispatch_delayed_uops += other.dispatch_delayed_uops;
        dispatch_delay_cycles += other.dispatch_delay_cycles;
        dispatch_bandwidth_events += other.dispatch_bandwidth_events;
        dispatch_bandwidth_cycles += other.dispatch_bandwidth_cycles;
        rob_capacity_events += other.rob_capacity_events;
        rob_capacity_cycles += other.rob_capacity_cycles;
        iq_capacity_events += other.iq_capacity_events;
        iq_capacity_cycles += other.iq_capacity_cycles;
        lq_capacity_events += other.lq_capacity_events;
        lq_capacity_cycles += other.lq_capacity_cycles;
        sq_capacity_events += other.sq_capacity_events;
        sq_capacity_cycles += other.sq_capacity_cycles;
        rob_residency_cycles += other.rob_residency_cycles;
        rob_max_residency_cycles = std::max(
            rob_max_residency_cycles,
            other.rob_max_residency_cycles);
        iq_residency_cycles += other.iq_residency_cycles;
        iq_max_residency_cycles = std::max(
            iq_max_residency_cycles,
            other.iq_max_residency_cycles);
        memory_iq_post_issue_uops +=
            other.memory_iq_post_issue_uops;
        memory_iq_post_issue_cycles +=
            other.memory_iq_post_issue_cycles;
        return *this;
    }
};

// Mutually exclusive attribution of cycles added by response-driven interval
// feedback.  `total_cycles` must equal the sum of every named cause.  These
// counters intentionally do not decompose the lower-bound interval core; they
// answer which feedback component extended that lower bound on the critical
// path without summing overlapping raw stall counters.
struct ResponseCriticalCycleCounters {
    std::uint64_t total_cycles = 0;
    std::uint64_t rename_free_list_cycles = 0;
    std::uint64_t dispatch_bandwidth_cycles = 0;
    std::uint64_t rob_capacity_cycles = 0;
    std::uint64_t iq_capacity_cycles = 0;
    std::uint64_t lq_capacity_cycles = 0;
    std::uint64_t sq_capacity_cycles = 0;
    std::uint64_t dependency_cycles = 0;
    std::uint64_t sequencer_cycles = 0;
    std::uint64_t l1_mshr_cycles = 0;
    std::uint64_t l2_mshr_cycles = 0;
    std::uint64_t memory_response_cycles = 0;
    std::uint64_t commit_bandwidth_cycles = 0;
    std::uint64_t tso_store_cycles = 0;
    std::uint64_t unattributed_cycles = 0;

    std::uint64_t classified_cycles() const {
        return rename_free_list_cycles + dispatch_bandwidth_cycles +
               rob_capacity_cycles +
               iq_capacity_cycles + lq_capacity_cycles +
               sq_capacity_cycles + dependency_cycles +
               sequencer_cycles + l1_mshr_cycles + l2_mshr_cycles +
               memory_response_cycles + commit_bandwidth_cycles +
               tso_store_cycles + unattributed_cycles;
    }

    ResponseCriticalCycleCounters& operator+=(
        const ResponseCriticalCycleCounters& other) {
        total_cycles += other.total_cycles;
        rename_free_list_cycles += other.rename_free_list_cycles;
        dispatch_bandwidth_cycles += other.dispatch_bandwidth_cycles;
        rob_capacity_cycles += other.rob_capacity_cycles;
        iq_capacity_cycles += other.iq_capacity_cycles;
        lq_capacity_cycles += other.lq_capacity_cycles;
        sq_capacity_cycles += other.sq_capacity_cycles;
        dependency_cycles += other.dependency_cycles;
        sequencer_cycles += other.sequencer_cycles;
        l1_mshr_cycles += other.l1_mshr_cycles;
        l2_mshr_cycles += other.l2_mshr_cycles;
        memory_response_cycles += other.memory_response_cycles;
        commit_bandwidth_cycles += other.commit_bandwidth_cycles;
        tso_store_cycles += other.tso_store_cycles;
        unattributed_cycles += other.unattributed_cycles;
        return *this;
    }
};

// Audit-only stage ledger for response-driven OoO feedback.  Unlike raw
// queue-stall counters, the dependency and retirement sub-ledgers are each
// conservative: every input extension is either hidden by pre-existing OoO
// slack or propagated across the corresponding stage boundary.  The other
// counters describe how often that propagated delay moves admission,
// retirement, and later memory issue; they are intentionally not additive.
struct ResponseResidualCounters {
    std::uint64_t response_seed_events = 0;
    std::uint64_t response_seed_uops = 0;
    std::uint64_t response_seed_cycles = 0;
    std::uint64_t completion_extended_uops = 0;
    std::uint64_t completion_extension_cycles = 0;

    std::uint64_t dependency_edges = 0;
    std::uint64_t dependency_input_cycles = 0;
    std::uint64_t dependency_absorbed_cycles = 0;
    std::uint64_t dependency_propagated_cycles = 0;

    std::uint64_t retire_seed_uops = 0;
    std::uint64_t retire_input_cycles = 0;
    std::uint64_t retire_absorbed_cycles = 0;
    std::uint64_t retire_propagated_cycles = 0;
    std::uint64_t ordered_retire_moved_uops = 0;
    std::uint64_t ordered_retire_moved_cycles = 0;

    std::uint64_t dispatch_moved_uops = 0;
    std::uint64_t dispatch_moved_cycles = 0;
    std::uint64_t memory_issue_moved_events = 0;
    std::uint64_t memory_issue_moved_cycles = 0;
    std::uint64_t escape_issue_moved_events = 0;
    std::uint64_t escape_issue_moved_cycles = 0;

    bool dependency_conserved() const {
        return dependency_input_cycles ==
               dependency_absorbed_cycles +
                   dependency_propagated_cycles;
    }

    bool retire_conserved() const {
        return retire_input_cycles ==
               retire_absorbed_cycles + retire_propagated_cycles;
    }

    ResponseResidualCounters& operator+=(
        const ResponseResidualCounters& other) {
        response_seed_events += other.response_seed_events;
        response_seed_uops += other.response_seed_uops;
        response_seed_cycles += other.response_seed_cycles;
        completion_extended_uops += other.completion_extended_uops;
        completion_extension_cycles +=
            other.completion_extension_cycles;
        dependency_edges += other.dependency_edges;
        dependency_input_cycles += other.dependency_input_cycles;
        dependency_absorbed_cycles +=
            other.dependency_absorbed_cycles;
        dependency_propagated_cycles +=
            other.dependency_propagated_cycles;
        retire_seed_uops += other.retire_seed_uops;
        retire_input_cycles += other.retire_input_cycles;
        retire_absorbed_cycles += other.retire_absorbed_cycles;
        retire_propagated_cycles += other.retire_propagated_cycles;
        ordered_retire_moved_uops +=
            other.ordered_retire_moved_uops;
        ordered_retire_moved_cycles +=
            other.ordered_retire_moved_cycles;
        dispatch_moved_uops += other.dispatch_moved_uops;
        dispatch_moved_cycles += other.dispatch_moved_cycles;
        memory_issue_moved_events += other.memory_issue_moved_events;
        memory_issue_moved_cycles += other.memory_issue_moved_cycles;
        escape_issue_moved_events += other.escape_issue_moved_events;
        escape_issue_moved_cycles += other.escape_issue_moved_cycles;
        return *this;
    }
};

struct CoreCounters {
    std::uint64_t records = 0;
    std::uint64_t retired_uops = 0;
    std::uint64_t retired_instructions = 0;
    std::uint64_t memory_accesses = 0;
    std::uint64_t mmio_escape_accesses = 0;
    std::uint64_t unknown_addresses = 0;
    std::uint64_t branches_without_outcome = 0;
    std::uint64_t serializing_uops = 0;
    std::uint64_t syscall_uops = 0;
    // Structural syscall costs charged by the interval core.  Drain cycles
    // are dynamic; service/restart cycles are configured target costs.  They
    // are raw components and may overlap other pipeline work, so they are not
    // an additive CPI decomposition.
    std::uint64_t syscall_drain_cycles = 0;
    std::uint64_t syscall_service_cycles = 0;
    std::uint64_t syscall_restart_cycles = 0;
    std::uint64_t branch_penalty_cycles = 0;
    std::uint64_t branch_shadow_uops = 0;
    std::uint64_t branch_shadow_cycles = 0;
    std::uint64_t memory_penalty_cycles = 0;
    // Lower-bound memory events are currently kept in per-core program order
    // for the canonical merge. These audit counters quantify how often that
    // conservative ordering moves an event beyond its O3 issue bound.
    std::uint64_t memory_order_clamp_events = 0;
    std::uint64_t memory_order_clamp_cycles = 0;
    std::uint64_t cycles = 0;
    CacheCounters l1d;
    CacheCounters l2;
    BranchCounters branch;
    TranslationCounters dtlb;

    CoreCounters& operator+=(const CoreCounters& other) {
        records += other.records;
        retired_uops += other.retired_uops;
        retired_instructions += other.retired_instructions;
        memory_accesses += other.memory_accesses;
        mmio_escape_accesses += other.mmio_escape_accesses;
        unknown_addresses += other.unknown_addresses;
        branches_without_outcome += other.branches_without_outcome;
        serializing_uops += other.serializing_uops;
        syscall_uops += other.syscall_uops;
        syscall_drain_cycles += other.syscall_drain_cycles;
        syscall_service_cycles += other.syscall_service_cycles;
        syscall_restart_cycles += other.syscall_restart_cycles;
        branch_penalty_cycles += other.branch_penalty_cycles;
        branch_shadow_uops += other.branch_shadow_uops;
        branch_shadow_cycles += other.branch_shadow_cycles;
        memory_penalty_cycles += other.memory_penalty_cycles;
        memory_order_clamp_events += other.memory_order_clamp_events;
        memory_order_clamp_cycles += other.memory_order_clamp_cycles;
        cycles += other.cycles;
        l1d += other.l1d;
        l2 += other.l2;
        branch += other.branch;
        dtlb += other.dtlb;
        return *this;
    }
};

struct ThreadStats {
    std::uint32_t thread_id = 0;
    std::uint32_t initial_core = 0;
    std::uint32_t final_core = 0;
    std::uint64_t address_space_id = 0;
    std::uint64_t records = 0;
    std::uint64_t retired_uops = 0;
    std::uint64_t retired_instructions = 0;
    std::uint64_t serializing_uops = 0;
    std::uint64_t syscall_uops = 0;
    std::uint64_t cycles = 0;
};

struct SimulationStats {
    std::vector<CoreCounters> cores;
    std::vector<ThreadStats> threads;
    std::vector<O3QueueCounters> o3;
    std::vector<ResponseRenameCounters> response_rename;
    std::vector<CommittedPipelineAuditCounters> committed_pipeline_audit;
    std::vector<SequencerCounters> sequencer;
    std::vector<ResponseCriticalCycleCounters> response_critical_cycles;
    std::vector<ResponseResidualCounters> response_residuals;
    CacheCounters llc;
    std::vector<ChaCounters> cha;
    bool functional_warmup_enabled = false;
    std::uint64_t functional_warmup_records = 0;
    std::uint64_t functional_warmup_uops = 0;
    std::uint64_t functional_warmup_instructions = 0;
    std::uint64_t functional_warmup_memory_events = 0;
    std::uint64_t functional_warmup_barrier_cycles = 0;
    std::uint64_t chunks_consumed = 0;
    std::uint64_t frontier_waits = 0;
    std::uint64_t max_resident_chunks = 0;
    std::uint64_t interval_steps = 0;
    std::uint64_t interval_zero_progress_steps = 0;
    std::uint64_t interval_accepted_uops = 0;
    std::uint64_t interval_active_prefixes = 0;
    std::uint64_t max_interval_accepted_uops = 0;
    std::uint64_t epoch_lookahead_chunks = 0;
    std::uint64_t epoch_inflight_memory_uops = 0;
    std::uint64_t epoch_corrected_horizon_violations = 0;
    // Memory requests are selected with the interval-core lower bound. These
    // audit counters quantify requests whose response-corrected issue moves
    // beyond the current time-epoch horizon.  They are the exact candidate
    // set for a future sparse boundary repair; no timing is changed here.
    std::uint64_t epoch_corrected_issue_horizon_events = 0;
    std::uint64_t epoch_corrected_issue_horizon_uops = 0;
    std::uint64_t epoch_corrected_issue_horizon_cycles = 0;
    std::uint64_t epoch_corrected_issue_horizon_max_cycles = 0;
    std::uint64_t corrected_suffix_candidate_epochs = 0;
    std::uint64_t corrected_suffix_stable_epochs = 0;
    std::uint64_t corrected_suffix_passes = 0;
    std::uint64_t corrected_suffix_preflight_epochs = 0;
    std::uint64_t corrected_suffix_preflight_passes = 0;
    std::uint64_t corrected_suffix_deferred_uops = 0;
    std::uint64_t corrected_suffix_deferred_memory_events = 0;
    std::uint64_t corrected_suffix_conservative_epochs = 0;
    std::uint64_t epoch_advanced_cycles = 0;
    std::uint64_t batch_memory_events = 0;
    std::uint64_t interval_private_memory_events = 0;
    std::uint64_t interval_escape_memory_events = 0;
    std::uint64_t private_preview_epochs = 0;
    std::uint64_t private_preview_partial_epochs = 0;
    std::uint64_t private_preview_events = 0;
    std::uint64_t private_preview_unsafe_events = 0;
    std::uint64_t private_preview_safe_cores = 0;
    std::uint64_t private_preview_unsafe_cores = 0;
    std::uint64_t private_preview_bypass_epochs = 0;
    std::uint64_t private_preview_bypass_events = 0;
    std::uint64_t materialized_escape_events = 0;
    std::uint64_t state_certificate_failures = 0;
    std::uint64_t state_certificate_wall_ns = 0;
    std::uint64_t timing_certificate_failures = 0;
    std::uint64_t timing_reweave_passes = 0;
    std::uint64_t replayed_shared_events = 0;
    std::uint64_t canonical_fallback_epochs = 0;
    std::uint64_t corrected_arrival_candidate_epochs = 0;
    std::uint64_t corrected_arrival_conflict_components = 0;
    std::uint64_t corrected_arrival_component_events = 0;
    std::uint64_t corrected_arrival_max_component_events = 0;
    std::uint64_t corrected_arrival_replay_epochs = 0;
    std::uint64_t corrected_arrival_replayed_events = 0;
    std::uint64_t corrected_arrival_stable_epochs = 0;
    std::uint64_t corrected_arrival_fallback_epochs = 0;
    std::uint64_t causal_timing_candidate_epochs = 0;
    std::uint64_t causal_timing_noop_epochs = 0;
    std::uint64_t causal_timing_stable_epochs = 0;
    std::uint64_t causal_timing_fallback_epochs = 0;
    std::uint64_t causal_timing_deferred_epochs = 0;
    std::uint64_t causal_closure_components = 0;
    std::uint64_t causal_closure_events = 0;
    std::uint64_t causal_max_closure_events = 0;
    std::uint64_t causal_timing_passes = 0;
    std::uint64_t causal_timing_replayed_events = 0;
    std::uint64_t causal_timing_wall_ns = 0;
    std::uint64_t response_retime_candidate_epochs = 0;
    std::uint64_t response_retime_noop_epochs = 0;
    std::uint64_t response_retime_stable_epochs = 0;
    std::uint64_t response_retime_fallback_epochs = 0;
    std::uint64_t response_retime_moved_shared_events = 0;
    std::uint64_t response_retime_replayed_events = 0;
    std::uint64_t response_retime_wall_ns = 0;
    std::uint64_t dram_frfcfs_candidate_epochs = 0;
    std::uint64_t dram_frfcfs_bypass_epochs = 0;
    std::uint64_t dram_frfcfs_bypass_requests = 0;
    std::uint64_t dram_frfcfs_candidate_queue_cycles = 0;
    std::uint64_t dram_frfcfs_bypass_queue_cycles = 0;
    std::uint64_t dram_frfcfs_selection_window_sum = 0;
    std::uint64_t dram_frfcfs_selection_window_max = 0;
    std::uint64_t dram_frfcfs_effective_selection_window = 0;
    std::uint64_t dram_frfcfs_stable_epochs = 0;
    std::uint64_t dram_frfcfs_fallback_epochs = 0;
    std::uint64_t dram_frfcfs_requests = 0;
    std::uint64_t dram_frfcfs_passes = 0;
    std::uint64_t dram_frfcfs_reordered_requests = 0;
    std::uint64_t dram_frfcfs_row_hits = 0;
    std::uint64_t dram_frfcfs_row_misses = 0;
    // `max_pending` is the bounded service-candidate frontier retained for
    // compatibility. `max_admitted_pending` is the full reconstructed
    // controller queue visible to page-policy decisions.
    std::uint64_t dram_frfcfs_max_pending = 0;
    std::uint64_t dram_frfcfs_max_admitted_pending = 0;
    std::uint64_t dram_frfcfs_saturated_selections = 0;
    std::uint64_t dram_frfcfs_page_policy_scanned_requests = 0;
    std::uint64_t dram_frfcfs_outside_window_row_hits = 0;
    std::uint64_t dram_frfcfs_outside_window_bank_conflicts = 0;
    std::uint64_t dram_frfcfs_row_cap_precharges = 0;
    std::uint64_t dram_frfcfs_adaptive_precharges = 0;
    std::uint64_t dram_frfcfs_wall_ns = 0;
    // Source-aligned DRAM write-controller audit. These counters are global
    // across channels and remain zero while separate_write_queue is disabled.
    std::uint64_t dram_write_queue_enqueues = 0;
    std::uint64_t dram_write_queue_drained = 0;
    std::uint64_t dram_write_queue_read_bypasses = 0;
    std::uint64_t dram_write_queue_high_watermark_switches = 0;
    std::uint64_t dram_write_queue_forced_capacity_drains = 0;
    std::uint64_t dram_write_queue_turnarounds = 0;
    std::uint64_t dram_write_queue_row_hits = 0;
    std::uint64_t dram_write_queue_row_misses = 0;
    std::uint64_t dram_write_queue_wait_cycles = 0;
    std::uint64_t dram_write_queue_max_pending = 0;
    std::uint64_t dram_write_queue_pending_initial = 0;
    std::uint64_t dram_write_queue_pending_final = 0;
    std::uint64_t max_batch_memory_events = 0;
    std::uint64_t reordered_memory_event_pairs = 0;
    std::uint64_t same_line_reordered_pairs = 0;
    std::uint64_t timing_feedback_calls = 0;
    std::uint64_t timing_feedback_parallel_calls = 0;
    std::uint64_t timing_feedback_core_tasks = 0;
    std::uint64_t timing_feedback_wall_ns = 0;
    std::uint64_t sparse_scoreboard_seeds = 0;
    std::uint64_t sparse_scoreboard_materialized_uops = 0;
    std::uint64_t sparse_scoreboard_absorbed_edges = 0;
    std::uint64_t sparse_scoreboard_cross_epoch_edges = 0;
    std::uint64_t sparse_scoreboard_rob_crossings = 0;
    std::uint64_t sparse_scoreboard_lq_crossings = 0;
    std::uint64_t sparse_scoreboard_sq_crossings = 0;
    std::uint64_t response_block_summary_checkpoints = 0;
    std::uint64_t response_block_summary_uops = 0;
    std::uint64_t response_block_summary_rob_writes = 0;
    std::uint64_t response_block_summary_rob_writes_avoided = 0;
    std::uint64_t sparse_resource_candidates = 0;
    std::uint64_t sparse_resource_issue_moves = 0;
    std::uint64_t sparse_resource_issue_collision_cycles = 0;
    std::uint64_t sparse_resource_writeback_moves = 0;
    std::uint64_t sparse_resource_writeback_collision_cycles = 0;
    std::uint64_t rob_head_suffix_anchors = 0;
    std::uint64_t rob_head_suffix_recoveries = 0;
    std::uint64_t rob_head_suffix_uops = 0;
    std::uint64_t rob_head_suffix_open_checkpoints = 0;
    std::uint64_t response_activity_candidates = 0;
    std::uint64_t response_activity_certified_segments = 0;
    std::uint64_t response_activity_certified_uops = 0;
    std::uint64_t response_activity_fallback_segments = 0;
    std::uint64_t rob_head_suffix_candidate_epochs = 0;
    std::uint64_t rob_head_suffix_noop_epochs = 0;
    std::uint64_t rob_head_suffix_stable_epochs = 0;
    std::uint64_t rob_head_suffix_fallback_epochs = 0;
    std::uint64_t rob_head_suffix_moved_shared_events = 0;
    std::uint64_t rob_head_suffix_boundary_clipped_events = 0;
    std::uint64_t rob_head_suffix_replayed_events = 0;
    std::uint64_t rob_head_suffix_wall_ns = 0;
    // Audit-only Ruby Sequencer issue-to-completion latency, sampled from the
    // final committed replay of each memory event when cpi_attribution=true.
    std::uint64_t response_latency_samples = 0;
    std::uint64_t response_latency_cycles = 0;
    std::uint64_t response_l1_samples = 0;
    std::uint64_t response_l1_latency_cycles = 0;
    std::uint64_t response_l2_samples = 0;
    std::uint64_t response_l2_latency_cycles = 0;
    std::uint64_t response_escape_samples = 0;
    std::uint64_t response_escape_latency_cycles = 0;
    std::uint64_t interval_schedule_batch_wall_ns = 0;
    std::uint64_t interval_weave_wall_ns = 0;
    std::uint64_t interval_commit_wall_ns = 0;
    std::uint64_t domain_worker_threads = 0;
    std::uint64_t trace_worker_threads = 0;
    std::uint64_t domain_phase_calls = 0;
    std::uint64_t domain_phase_wall_ns = 0;
    // The legacy total wall time spans both phases.  Keep explicit phase
    // timers so a two-phase FS replay never divides ROI-only UOPs by a
    // warmup+ROI denominator without making that mixed scope visible.
    std::uint64_t functional_warmup_wall_ns = 0;
    std::uint64_t measurement_wall_ns = 0;
    std::uint64_t wall_time_ns = 0;

    CoreCounters total_core() const {
        CoreCounters total;
        for (const auto& core : cores) total += core;
        return total;
    }

    SequencerCounters total_sequencer() const {
        SequencerCounters total;
        for (const auto& core : sequencer) total += core;
        return total;
    }

    O3QueueCounters total_o3() const {
        O3QueueCounters total;
        for (const auto& core : o3) total += core;
        return total;
    }

    ResponseRenameCounters total_response_rename() const {
        ResponseRenameCounters total;
        for (const auto& core : response_rename) total += core;
        return total;
    }

    CommittedPipelineAuditCounters total_committed_pipeline_audit() const {
        CommittedPipelineAuditCounters total;
        for (const auto& core : committed_pipeline_audit) total += core;
        return total;
    }

    ResponseCriticalCycleCounters total_response_critical_cycles() const {
        ResponseCriticalCycleCounters total;
        for (const auto& core : response_critical_cycles) total += core;
        return total;
    }

    ResponseResidualCounters total_response_residuals() const {
        ResponseResidualCounters total;
        for (const auto& core : response_residuals) total += core;
        return total;
    }
};

std::string hit_level_name(HitLevel level);

}  // namespace fastsim
