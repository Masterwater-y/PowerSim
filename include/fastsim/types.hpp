#pragma once

#include <algorithm>
#include <array>
#include <cstdint>
#include <limits>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace fastsim {

// Canonical trace op classes are gem5 OpClass values and therefore
// non-negative.  Negative values are reserved for FastSim functional event
// markers that must survive the compact binary trace without consuming one of
// the already-full 16 flag bits.
constexpr std::int16_t kSyscallOpClass = -1;
// FST v7 privilege-tagged records preserve the 64-byte hot-record layout by
// encoding a kernel record's canonical gem5 OpClass N as -(N + 2).  -1 stays
// reserved for the user-side syscall transition marker.
constexpr std::int16_t kKernelOpClassBias = 2;
constexpr std::uint32_t kDestinationClassCountsMarker = 1u << 31;
constexpr std::size_t kTrackedRegisterClasses = 4;
constexpr std::size_t kMaximumSyscallArguments = 6;
constexpr std::size_t kSpeculativeProfilePoolCount = 8;
constexpr std::array<std::uint64_t, 10>
    kPageFaultAllocationRecencyUpperBounds{
        256,
        1024,
        4096,
        16384,
        65536,
        262144,
        1048576,
        4194304,
        16777216,
        UINT64_MAX,
    };

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

// Portable syscall metadata is kept in a sparse table after the 64-byte FST
// record stream.  These validity bits distinguish a captured zero/false value
// from information which the producer did not provide.
enum SyscallMetadataField : std::uint16_t {
    kSyscallArgumentsValid = 1u << 0,
    kSyscallReturnValueValid = 1u << 1,
    kSyscallFailureValid = 1u << 2,
    kSyscallErrnoValid = 1u << 3,
    kSyscallPreTimestampValid = 1u << 4,
    kSyscallPostTimestampValid = 1u << 5,
    kSyscallPreCpuValid = 1u << 6,
    kSyscallPostCpuValid = 1u << 7,
    kSyscallMaybeBlockingValid = 1u << 8,
    kSyscallThreadIdValid = 1u << 9,
};

inline bool has_syscall_field(std::uint16_t fields,
                              SyscallMetadataField field) {
    return (fields & static_cast<std::uint16_t>(field)) != 0;
}

enum class SyscallAbi : std::uint64_t {
    kUnknown = 0,
    kLinuxX86_64 = 1,
    kLinuxX86_32 = 2,
    kLinuxAArch64 = 3,
    kLinuxArm32 = 4,
};

struct SyscallMetadata {
    // Ordinals are zero-based within one source FST.  record_ordinal anchors
    // the entry to the hot stream; syscall_ordinal stays stable when a wrapper
    // skips non-syscall instructions during functional slicing.
    std::uint64_t record_ordinal = 0;
    std::uint64_t syscall_ordinal = 0;
    std::uint64_t thread_id = 0;
    std::uint64_t number = 0;
    std::array<std::uint64_t, kMaximumSyscallArguments> arguments{};
    std::uint64_t return_value_raw = 0;
    std::uint64_t pre_timestamp_us = 0;
    std::uint64_t post_timestamp_us = 0;
    std::uint32_t errno_value = 0;
    std::uint32_t pre_cpu = 0;
    std::uint32_t post_cpu = 0;
    std::uint16_t valid_fields = 0;
    std::uint8_t argument_count = 0;
    bool failed = false;
    bool maybe_blocking = false;

    bool has(SyscallMetadataField field) const {
        return has_syscall_field(valid_fields, field);
    }
};

// A sparse FST companion entry.  The address space applies to this record
// ordinal and every following record until the next transition.  Producers
// normalize gem5 CR3 roots or native PIDs into stable, non-zero IDs; zero is
// reserved for legacy streams whose process identity is unspecified.
struct AddressSpaceTransition {
    std::uint64_t record_ordinal = 0;
    std::uint64_t address_space_id = 0;
};

// Optional FST companion mapping for committed instruction fetches. A row
// takes effect before record_ordinal is decoded and remains active for the
// same (address_space_id, virtual_page) until a later row replaces it. This
// is functional translation state only: it carries no fetch tick, cache/TLB
// result, retry, speculative-path identity, or latency.
struct InstructionPageMapping {
    std::uint64_t record_ordinal = 0;
    std::uint64_t address_space_id = 0;
    std::uint64_t virtual_page = 0;
    std::uint64_t physical_page = 0;
};

// Optional FST v7 companion mapping for the opaque 31-bit token carried by
// hot memory records.  virtual_page is portable across TaoTrace and
// drmemtrace.  physical_page is present only when the producer had a physical
// translation; the hot record remains the authority for cache addressing.
struct VirtualPageMapping {
    std::uint32_t token = 0;
    std::uint64_t first_record_ordinal = 0;
    std::uint64_t virtual_page = 0;
    std::uint64_t physical_page = 0;
    bool physical_page_valid = false;
    // Optional state captured from the guest page tables before the first
    // functional user record.  A valid false `initial_pte_present` denotes
    // a non-present leaf PTE; invalid means that the producer could not prove
    // a state (for example because an upper-level page-table entry was absent).
    // These are functional initial conditions, never timing/oracle labels.
    bool initial_pte_state_valid = false;
    bool initial_pte_present = false;
    // Optional state captured at the exact functional-warmup/ROI-entry
    // boundary. This must be preferred for first touches in the measured
    // phase: kernel activity and other threads may have changed a PTE since
    // the initial snapshot even when this stream did not touch the page.
    bool roi_entry_page_state_valid = false;
    bool roi_entry_page_present = false;
    // The producer observed this stream already servicing a precise page
    // fault when the process-wide ROI-entry marker opened. The retried
    // instruction is therefore visible as a measured committed access, but
    // the fault entry itself belongs to warmup. This is boundary state, not
    // a post-measurement timing/oracle label.
    bool roi_entry_inflight_page_fault = false;
};

// ISA-decoded facts shared by a gem5 TaoTrace producer and an offline
// drmemtrace module decoder. These facts describe the executable image, not a
// particular speculative execution: no predictor outcome, timing, cache hit,
// or physical instruction address is permitted here.
enum StaticInstructionFlag : std::uint16_t {
    kStaticBranch = 1u << 0,
    kStaticConditional = 1u << 1,
    kStaticIndirect = 1u << 2,
    kStaticCall = 1u << 3,
    kStaticReturn = 1u << 4,
    kStaticDirectTargetValid = 1u << 5,
    // The decoded macro instruction may issue at least one data-memory
    // reference. This carries no dynamic address, cache result, or timing.
    kStaticMemory = 1u << 6,
};

// Register IDs in an instruction-map companion are ISA namespaced.  The
// current portable producer contract defines the x86-64 namespace; unknown
// keeps v1 maps and traces without decoded operands unambiguous.
enum class StaticInstructionIsa : std::uint32_t {
    kUnknown = 0,
    kX86_64 = 1,
};

constexpr std::size_t kStaticRegisterMaskWords = 2;
constexpr std::size_t kStaticRegisterCount =
    kStaticRegisterMaskWords * 64;

inline bool has_static_instruction_flag(
    std::uint16_t flags, StaticInstructionFlag flag) {
    return (flags & static_cast<std::uint16_t>(flag)) != 0;
}

struct StaticInstructionInfo {
    std::uint64_t pc = 0;
    std::uint64_t fallthrough_pc = 0;
    std::uint64_t direct_target = 0;
    std::uint16_t flags = 0;
    std::uint8_t size = 0;
    std::array<std::uint64_t, kStaticRegisterMaskWords>
        read_register_mask{};
    std::array<std::uint64_t, kStaticRegisterMaskWords>
        write_register_mask{};
    bool operand_semantics_valid = false;

    bool is_branch() const {
        return has_static_instruction_flag(flags, kStaticBranch);
    }
    bool is_conditional() const {
        return has_static_instruction_flag(flags, kStaticConditional);
    }
    bool is_indirect() const {
        return has_static_instruction_flag(flags, kStaticIndirect);
    }
    bool is_call() const {
        return has_static_instruction_flag(flags, kStaticCall);
    }
    bool is_return() const {
        return has_static_instruction_flag(flags, kStaticReturn);
    }
    bool has_direct_target() const {
        return has_static_instruction_flag(
            flags, kStaticDirectTargetValid);
    }
    bool is_memory() const {
        return has_static_instruction_flag(flags, kStaticMemory);
    }
    bool reads_register(std::size_t id) const {
        return operand_semantics_valid && id < kStaticRegisterCount &&
            (read_register_mask[id / 64] & (1ull << (id % 64))) != 0;
    }
    bool writes_register(std::size_t id) const {
        return operand_semantics_valid && id < kStaticRegisterCount &&
            (write_register_mask[id / 64] & (1ull << (id % 64))) != 0;
    }
};

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
    bool is_kernel() const { return op_class < kSyscallOpClass; }
    int canonical_op_class() const {
        return is_kernel()
                   ? -static_cast<int>(op_class) - kKernelOpClassBias
                   : static_cast<int>(op_class);
    }
    void set_kernel_mode(bool kernel) {
        if (is_syscall()) {
            if (kernel) {
                throw std::invalid_argument(
                    "syscall transition marker must remain user-scoped");
            }
            return;
        }
        const auto canonical = canonical_op_class();
        if (canonical < 0 ||
            canonical >
                std::numeric_limits<std::int16_t>::max() -
                    kKernelOpClassBias) {
            throw std::invalid_argument(
                "canonical op class cannot be privilege encoded");
        }
        op_class = kernel
            ? static_cast<std::int16_t>(
                  -canonical - kKernelOpClassBias)
            : static_cast<std::int16_t>(canonical);
    }
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
    // Legacy raw conditional-predictor disagreement. This is intentionally
    // retained because a missing target can force the final prediction to
    // fall through and mask a raw taken prediction.
    std::uint64_t direction_misses = 0;
    // Mutually exclusive committed-miss attribution. These three buckets sum
    // to misses; target_unavailable is the forced-fallthrough population that
    // the legacy direction/target counters did not expose.
    std::uint64_t direction_only_misses = 0;
    std::uint64_t target_unavailable_misses = 0;
    std::uint64_t wrong_target_misses = 0;
    std::uint64_t masked_direction_misses = 0;
    std::uint64_t target_misses = 0;
    std::uint64_t misses = 0;
    std::uint64_t btb_hits = 0;
    std::uint64_t ras_hits = 0;
    std::uint64_t history_checkpoints = 0;
    std::uint64_t history_squashes = 0;
    std::uint64_t deferred_direction_commits = 0;
    // RAS source/coverage diagnostics. Every detected call contributes to
    // exactly one return-target source bucket. A prediction is a detected
    // return that popped a valid target, independently of whether it hit.
    std::uint64_t ras_pushes = 0;
    std::uint64_t ras_pops = 0;
    std::uint64_t ras_predictions = 0;
    std::uint64_t ras_static_return_targets = 0;
    std::uint64_t ras_learned_return_targets = 0;
    std::uint64_t ras_unknown_return_targets = 0;

    bool ras_source_conserved() const {
        return ras_pushes == ras_static_return_targets +
                ras_learned_return_targets +
                ras_unknown_return_targets &&
            ras_hits <= ras_predictions && ras_predictions <= ras_pops;
    }

    bool miss_population_conserved() const {
        return misses == direction_only_misses +
                target_unavailable_misses + wrong_target_misses;
    }

    BranchCounters& operator+=(const BranchCounters& other) {
        branches += other.branches;
        conditional += other.conditional;
        direction_misses += other.direction_misses;
        direction_only_misses += other.direction_only_misses;
        target_unavailable_misses += other.target_unavailable_misses;
        wrong_target_misses += other.wrong_target_misses;
        masked_direction_misses += other.masked_direction_misses;
        target_misses += other.target_misses;
        misses += other.misses;
        btb_hits += other.btb_hits;
        ras_hits += other.ras_hits;
        history_checkpoints += other.history_checkpoints;
        history_squashes += other.history_squashes;
        deferred_direction_commits +=
            other.deferred_direction_commits;
        ras_pushes += other.ras_pushes;
        ras_pops += other.ras_pops;
        ras_predictions += other.ras_predictions;
        ras_static_return_targets += other.ras_static_return_targets;
        ras_learned_return_targets += other.ras_learned_return_targets;
        ras_unknown_return_targets += other.ras_unknown_return_targets;
        return *this;
    }
};

// Audit-only branch-miss population estimate. This ledger deliberately has no
// cycle-penalty field: it describes the wrong-path population and its
// resolution-time ROB residency without changing the committed timeline.
struct BranchPopulationAuditCounters {
    std::uint64_t miss_events = 0;
    std::uint64_t history_ready_events = 0;
    std::uint64_t history_unavailable_events = 0;
    std::uint64_t predicted_path_covered_events = 0;
    std::uint64_t predicted_path_unavailable_events = 0;
    std::uint64_t predicted_path_records = 0;
    std::uint64_t predicted_path_covered_uops = 0;
    std::uint64_t resolution_cycles = 0;
    std::uint64_t resolution_cycles_max = 0;
    std::uint64_t older_live_uops = 0;
    std::uint64_t rob_free_uops = 0;
    std::uint64_t supply_history_uops = 0;
    std::uint64_t supply_history_cycles = 0;
    // Committed Fetch-block responses observed in the same causal rolling
    // window as supply_history_uops.  These are sufficient statistics for an
    // address-free request-density/service-time prior; they never identify or
    // touch a wrong-path cache line.
    std::uint64_t supply_history_fetch_requests = 0;
    std::uint64_t supply_history_fetch_response_cycles = 0;
    std::uint64_t supply_budget_uops = 0;
    std::uint64_t estimated_squashed_uops = 0;
    std::uint64_t estimated_squashed_uops_max = 0;
    std::uint64_t estimated_rob_residency_uop_cycles = 0;
    std::uint64_t rob_limited_events = 0;
    std::uint64_t supply_limited_events = 0;
    std::uint64_t zero_window_events = 0;

    bool conserved() const {
        return miss_events ==
                   history_ready_events + history_unavailable_events &&
            miss_events == predicted_path_covered_events +
                predicted_path_unavailable_events &&
            estimated_squashed_uops <= rob_free_uops &&
            estimated_squashed_uops <= supply_budget_uops &&
            predicted_path_covered_uops <= estimated_squashed_uops;
    }

    BranchPopulationAuditCounters& operator+=(
        const BranchPopulationAuditCounters& other) {
        miss_events += other.miss_events;
        history_ready_events += other.history_ready_events;
        history_unavailable_events += other.history_unavailable_events;
        predicted_path_covered_events +=
            other.predicted_path_covered_events;
        predicted_path_unavailable_events +=
            other.predicted_path_unavailable_events;
        predicted_path_records += other.predicted_path_records;
        predicted_path_covered_uops += other.predicted_path_covered_uops;
        resolution_cycles += other.resolution_cycles;
        resolution_cycles_max = std::max(
            resolution_cycles_max, other.resolution_cycles_max);
        older_live_uops += other.older_live_uops;
        rob_free_uops += other.rob_free_uops;
        supply_history_uops += other.supply_history_uops;
        supply_history_cycles += other.supply_history_cycles;
        supply_history_fetch_requests +=
            other.supply_history_fetch_requests;
        supply_history_fetch_response_cycles +=
            other.supply_history_fetch_response_cycles;
        supply_budget_uops += other.supply_budget_uops;
        estimated_squashed_uops += other.estimated_squashed_uops;
        estimated_squashed_uops_max = std::max(
            estimated_squashed_uops_max,
            other.estimated_squashed_uops_max);
        estimated_rob_residency_uop_cycles +=
            other.estimated_rob_residency_uop_cycles;
        rob_limited_events += other.rob_limited_events;
        supply_limited_events += other.supply_limited_events;
        zero_window_events += other.zero_window_events;
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

    bool conserved() const {
        return accesses == hits + misses + merged_misses + untracked;
    }

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

// Statistical kernel contribution emitted by a synthetic event model.  It is
// intentionally separate from the functional user counters: the trace does
// not contain kernel UOPs or addresses, so these values are PMU estimates and
// must not silently participate in user cache/TLB state.  active_cycles are
// injected into the timing model; blocked_wall_cycles are report-only.
struct KernelEventCounters {
    std::uint64_t events = 0;
    std::uint64_t active_cycles = 0;
    std::uint64_t blocked_wall_cycles = 0;
    std::uint64_t retired_instructions = 0;
    std::uint64_t retired_uops = 0;
    std::uint64_t memory_uops = 0;
    std::uint64_t line_requests = 0;
    CacheCounters l1d;
    CacheCounters l2;
    CacheCounters llc;
    std::uint64_t permission_upgrades = 0;
    std::uint64_t remote_supplies = 0;
    std::uint64_t llc_merged_misses = 0;
    std::uint64_t llc_unique_fills = 0;
    std::uint64_t dram_reads = 0;
    std::uint64_t dram_writes = 0;
    BranchCounters branch;
    TranslationCounters dtlb;

    KernelEventCounters& operator+=(const KernelEventCounters& other) {
        events += other.events;
        active_cycles += other.active_cycles;
        blocked_wall_cycles += other.blocked_wall_cycles;
        retired_instructions += other.retired_instructions;
        retired_uops += other.retired_uops;
        memory_uops += other.memory_uops;
        line_requests += other.line_requests;
        l1d += other.l1d;
        l2 += other.l2;
        llc += other.llc;
        permission_upgrades += other.permission_upgrades;
        remote_supplies += other.remote_supplies;
        llc_merged_misses += other.llc_merged_misses;
        llc_unique_fills += other.llc_unique_fills;
        dram_reads += other.dram_reads;
        dram_writes += other.dram_writes;
        branch += other.branch;
        dtlb += other.dtlb;
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
    // The native Ruby oracle reports exactly one shared-cache outcome for
    // every request.  Tag hits, tag misses, permission upgrades, transient
    // merges, and remote supplies are therefore mutually exclusive here too.
    // A tag miss may allocate one unique fill; a merged request only waits on
    // that parent fill and is not also a tag miss.
    std::uint64_t llc_unique_fills = 0;
    std::uint64_t llc_merged_misses = 0;
    std::uint64_t llc_merged_wait_cycles = 0;
    std::uint64_t llc_merged_wait_max_cycles = 0;
    std::uint64_t queue_cycles = 0;

    std::uint64_t llc_outcomes() const {
        return llc_hits + llc_misses + upgrades + remote_supplies +
            llc_merged_misses;
    }

    bool llc_outcomes_conserved() const {
        return requests == llc_outcomes();
    }

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

    // Timing-neutral committed dependency census.  producer_dists[] is the
    // only dynamic RAW graph available to FastSim, so these counters make its
    // coverage and FU-pool distribution explicit instead of inferring them
    // from CPI sensitivity.  A producer is counted once when its first
    // in-measurement consumer is observed; edges retain duplicate operands,
    // matching gem5's dependency-graph wakeup accounting.
    std::uint64_t source_operands = 0;
    std::uint64_t source_uops_over_dependency_slots = 0;
    std::uint64_t source_operands_over_dependency_slots = 0;
    std::uint64_t dependency_edges = 0;
    std::uint64_t dependency_cross_boundary_edges = 0;
    std::uint64_t dependent_uops = 0;
    std::uint64_t dependency_producer_uops = 0;
    std::uint64_t dependency_distance_sum = 0;
    std::uint64_t dependency_distance_max = 0;
    std::uint64_t dependency_gated_uops = 0;
    std::uint64_t dependency_gate_cycles = 0;
    std::uint64_t dependency_gate_cycles_max = 0;
    // Macro-level architectural RAW reconstruction from `.fst.imap` v2.
    // The candidate is audit-only here: it identifies producer sequences not
    // present in the fixed four dynamic dependency slots and measures how
    // often their lower-bound completion would extend issue readiness.
    std::uint64_t static_dependency_uops = 0;
    std::uint64_t static_dependency_map_misses = 0;
    std::uint64_t static_dependency_edges = 0;
    std::uint64_t static_dependency_duplicate_edges = 0;
    std::uint64_t static_dependency_supplemental_edges = 0;
    std::uint64_t static_dependency_supplemental_uops = 0;
    std::uint64_t static_dependency_ready_extension_uops = 0;
    std::uint64_t static_dependency_ready_extension_cycles = 0;
    std::uint64_t static_dependency_ready_extension_max_cycles = 0;
    std::uint64_t static_dependency_truncated_ready_extension_uops = 0;
    std::uint64_t static_dependency_truncated_ready_extension_cycles = 0;
    std::uint64_t static_dependency_truncated_ready_extension_max_cycles = 0;
    // Target StoreSet/LFST candidate reconstructed without timing oracle.
    // Same-macro overlapping load/store UOPs train a same-PC RMW key; later
    // loads and stores wait on its latest live store. The final edge remains
    // PC based, so its address relation may intentionally be non-aliasing.
    std::uint64_t store_set_rmw_observations = 0;
    std::uint64_t store_set_rmw_pc_trainings = 0;
    std::uint64_t store_set_same_pc_load_candidates = 0;
    std::uint64_t store_set_same_pc_store_candidates = 0;
    std::uint64_t store_set_same_pc_edges = 0;
    std::uint64_t store_set_same_pc_load_edges = 0;
    std::uint64_t store_set_same_pc_store_edges = 0;
    std::uint64_t store_set_same_pc_nonoverlap_edges = 0;
    std::uint64_t store_set_same_pc_distance_sum = 0;
    std::uint64_t store_set_same_pc_distance_max = 0;
    std::uint64_t store_set_same_pc_ready_extension_uops = 0;
    std::uint64_t store_set_same_pc_ready_extension_cycles = 0;
    std::uint64_t store_set_same_pc_ready_extension_max_cycles = 0;
    std::uint64_t atomic_uops = 0;
    std::array<std::uint64_t, kSpeculativeProfilePoolCount> pool_uops{};
    std::array<std::uint64_t, kSpeculativeProfilePoolCount>
        source_uops_over_dependency_slots_by_pool{};
    std::array<std::uint64_t, kSpeculativeProfilePoolCount>
        source_operands_over_dependency_slots_by_pool{};
    std::array<std::uint64_t, kSpeculativeProfilePoolCount>
        dependent_uops_by_pool{};
    std::array<std::uint64_t, kSpeculativeProfilePoolCount>
        dependency_edges_by_pool{};
    std::array<std::uint64_t, kSpeculativeProfilePoolCount>
        dependency_gated_uops_by_pool{};
    std::array<std::uint64_t, kSpeculativeProfilePoolCount>
        dependency_gate_cycles_by_pool{};

    // Per-UOP lower-bound stage residence.  These edges are deliberately
    // separate from queue stall counters: every committed UOP contributes to
    // exactly one value on each adjacent stage edge, so their sum must equal
    // fetch-to-retire residence.  The sums overlap between UOPs and are not a
    // CPI decomposition; they provide a conserved stage contract that can be
    // compared with gem5's non-additive O3 occupancy diagnostics.
    std::uint64_t stage_fetch_to_decode_cycles = 0;
    std::uint64_t stage_decode_to_rename_cycles = 0;
    std::uint64_t stage_rename_to_dispatch_cycles = 0;
    std::uint64_t stage_dispatch_to_issue_cycles = 0;
    std::uint64_t stage_issue_to_execute_cycles = 0;
    std::uint64_t stage_execute_to_completion_cycles = 0;
    std::uint64_t stage_completion_to_retire_cycles = 0;
    std::uint64_t stage_fetch_to_retire_cycles = 0;
    std::uint64_t stage_memory_uops = 0;
    std::uint64_t stage_memory_issue_to_completion_cycles = 0;
    std::uint64_t stage_memory_completion_to_retire_cycles = 0;

    std::uint64_t classified_stage_cycles() const {
        return stage_fetch_to_decode_cycles +
               stage_decode_to_rename_cycles +
               stage_rename_to_dispatch_cycles +
               stage_dispatch_to_issue_cycles +
               stage_issue_to_execute_cycles +
               stage_execute_to_completion_cycles +
               stage_completion_to_retire_cycles;
    }

    bool stage_conserved() const {
        return stage_fetch_to_retire_cycles == classified_stage_cycles();
    }

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
        source_operands += other.source_operands;
        source_uops_over_dependency_slots +=
            other.source_uops_over_dependency_slots;
        source_operands_over_dependency_slots +=
            other.source_operands_over_dependency_slots;
        dependency_edges += other.dependency_edges;
        dependency_cross_boundary_edges +=
            other.dependency_cross_boundary_edges;
        dependent_uops += other.dependent_uops;
        dependency_producer_uops += other.dependency_producer_uops;
        dependency_distance_sum += other.dependency_distance_sum;
        dependency_distance_max = std::max(
            dependency_distance_max, other.dependency_distance_max);
        dependency_gated_uops += other.dependency_gated_uops;
        dependency_gate_cycles += other.dependency_gate_cycles;
        dependency_gate_cycles_max = std::max(
            dependency_gate_cycles_max,
            other.dependency_gate_cycles_max);
        static_dependency_uops += other.static_dependency_uops;
        static_dependency_map_misses +=
            other.static_dependency_map_misses;
        static_dependency_edges += other.static_dependency_edges;
        static_dependency_duplicate_edges +=
            other.static_dependency_duplicate_edges;
        static_dependency_supplemental_edges +=
            other.static_dependency_supplemental_edges;
        static_dependency_supplemental_uops +=
            other.static_dependency_supplemental_uops;
        static_dependency_ready_extension_uops +=
            other.static_dependency_ready_extension_uops;
        static_dependency_ready_extension_cycles +=
            other.static_dependency_ready_extension_cycles;
        static_dependency_ready_extension_max_cycles = std::max(
            static_dependency_ready_extension_max_cycles,
            other.static_dependency_ready_extension_max_cycles);
        static_dependency_truncated_ready_extension_uops +=
            other.static_dependency_truncated_ready_extension_uops;
        static_dependency_truncated_ready_extension_cycles +=
            other.static_dependency_truncated_ready_extension_cycles;
        static_dependency_truncated_ready_extension_max_cycles = std::max(
            static_dependency_truncated_ready_extension_max_cycles,
            other.static_dependency_truncated_ready_extension_max_cycles);
        store_set_rmw_observations += other.store_set_rmw_observations;
        store_set_rmw_pc_trainings += other.store_set_rmw_pc_trainings;
        store_set_same_pc_load_candidates +=
            other.store_set_same_pc_load_candidates;
        store_set_same_pc_store_candidates +=
            other.store_set_same_pc_store_candidates;
        store_set_same_pc_edges += other.store_set_same_pc_edges;
        store_set_same_pc_load_edges +=
            other.store_set_same_pc_load_edges;
        store_set_same_pc_store_edges +=
            other.store_set_same_pc_store_edges;
        store_set_same_pc_nonoverlap_edges +=
            other.store_set_same_pc_nonoverlap_edges;
        store_set_same_pc_distance_sum +=
            other.store_set_same_pc_distance_sum;
        store_set_same_pc_distance_max = std::max(
            store_set_same_pc_distance_max,
            other.store_set_same_pc_distance_max);
        store_set_same_pc_ready_extension_uops +=
            other.store_set_same_pc_ready_extension_uops;
        store_set_same_pc_ready_extension_cycles +=
            other.store_set_same_pc_ready_extension_cycles;
        store_set_same_pc_ready_extension_max_cycles = std::max(
            store_set_same_pc_ready_extension_max_cycles,
            other.store_set_same_pc_ready_extension_max_cycles);
        atomic_uops += other.atomic_uops;
        for (std::size_t index = 0; index < pool_uops.size(); ++index) {
            pool_uops[index] += other.pool_uops[index];
            source_uops_over_dependency_slots_by_pool[index] +=
                other.source_uops_over_dependency_slots_by_pool[index];
            source_operands_over_dependency_slots_by_pool[index] +=
                other.source_operands_over_dependency_slots_by_pool[index];
            dependent_uops_by_pool[index] +=
                other.dependent_uops_by_pool[index];
            dependency_edges_by_pool[index] +=
                other.dependency_edges_by_pool[index];
            dependency_gated_uops_by_pool[index] +=
                other.dependency_gated_uops_by_pool[index];
            dependency_gate_cycles_by_pool[index] +=
                other.dependency_gate_cycles_by_pool[index];
        }
        stage_fetch_to_decode_cycles +=
            other.stage_fetch_to_decode_cycles;
        stage_decode_to_rename_cycles +=
            other.stage_decode_to_rename_cycles;
        stage_rename_to_dispatch_cycles +=
            other.stage_rename_to_dispatch_cycles;
        stage_dispatch_to_issue_cycles +=
            other.stage_dispatch_to_issue_cycles;
        stage_issue_to_execute_cycles +=
            other.stage_issue_to_execute_cycles;
        stage_execute_to_completion_cycles +=
            other.stage_execute_to_completion_cycles;
        stage_completion_to_retire_cycles +=
            other.stage_completion_to_retire_cycles;
        stage_fetch_to_retire_cycles +=
            other.stage_fetch_to_retire_cycles;
        stage_memory_uops += other.stage_memory_uops;
        stage_memory_issue_to_completion_cycles +=
            other.stage_memory_issue_to_completion_cycles;
        stage_memory_completion_to_retire_cycles +=
            other.stage_memory_completion_to_retire_cycles;
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
    std::uint64_t instruction_fetch_cycles = 0;
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
               instruction_fetch_cycles + memory_response_cycles +
               commit_bandwidth_cycles +
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
        instruction_fetch_cycles += other.instruction_fetch_cycles;
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
    // Lower-hierarchy delay beyond the local L1I-miss baseline is injected at
    // the affected committed UOP's Fetch edge, not as a data-load completion.
    std::uint64_t instruction_fetch_seed_events = 0;
    std::uint64_t instruction_fetch_seed_cycles = 0;
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
    std::uint64_t fetch_queue_moved_uops = 0;
    std::uint64_t fetch_queue_moved_cycles = 0;
    std::uint64_t memory_issue_moved_events = 0;
    std::uint64_t memory_issue_moved_cycles = 0;
    std::uint64_t escape_issue_moved_events = 0;
    std::uint64_t escape_issue_moved_cycles = 0;

    // Regular-store lifecycle reconstructed by the sparse response model.
    // These intervals are deliberately adjacent and non-overlapping:
    // commit->SQ-release equals commit->TSO-send plus send->response.  The
    // hierarchy-response lead is a separate audit of the current preview
    // request and must not be added to the lifecycle intervals.
    std::uint64_t store_uops = 0;
    std::uint64_t store_address_to_commit_cycles = 0;
    std::uint64_t store_hierarchy_response_before_commit_uops = 0;
    std::uint64_t store_hierarchy_response_before_commit_cycles = 0;
    std::uint64_t store_tso_wait_uops = 0;
    std::uint64_t store_tso_wait_cycles = 0;
    std::uint64_t store_send_to_response_cycles = 0;
    std::uint64_t store_commit_to_sq_release_cycles = 0;
    std::uint64_t store_sq_release_max_cycles = 0;
    std::uint64_t store_send_retimed_uops = 0;
    std::uint64_t store_send_retimed_cycles = 0;

    // Adjacent stage residence before and after response correction.  These
    // values are collected only with cpi_attribution and keep the interval
    // gap in both schedules, so the delta describes response/queue feedback
    // rather than the absolute ROI origin.  As above, sums overlap across
    // UOPs and must not be added to core cycles.
    std::uint64_t stage_uops = 0;
    std::uint64_t stage_memory_uops = 0;
    // Stage slices used to compare the response-corrected committed schedule
    // with gem5's per-UOP fetch/issue/commit labels.  Stores are deliberately
    // excluded from the non-memory slice because their post-commit hierarchy
    // send time is not an execution-stage issue timestamp.
    std::uint64_t stage_non_memory_uops = 0;
    std::uint64_t stage_non_memory_base_fetch_to_issue_cycles = 0;
    std::uint64_t stage_non_memory_corrected_fetch_to_issue_cycles = 0;
    std::uint64_t stage_non_memory_corrected_issue_to_retire_cycles = 0;
    std::uint64_t stage_load_uops = 0;
    std::uint64_t stage_load_base_fetch_to_issue_cycles = 0;
    std::uint64_t stage_load_corrected_fetch_to_issue_cycles = 0;
    std::uint64_t stage_load_corrected_issue_to_completion_cycles = 0;
    std::uint64_t stage_load_corrected_issue_to_retire_cycles = 0;
    std::uint64_t stage_base_issue_to_completion_cycles = 0;
    std::uint64_t stage_base_completion_to_retire_cycles = 0;
    std::uint64_t stage_base_issue_to_retire_cycles = 0;
    std::uint64_t stage_corrected_issue_to_completion_cycles = 0;
    std::uint64_t stage_corrected_completion_to_retire_cycles = 0;
    std::uint64_t stage_corrected_issue_to_retire_cycles = 0;
    std::uint64_t stage_issue_delay_cycles = 0;
    std::uint64_t stage_completion_delay_cycles = 0;
    std::uint64_t stage_retire_delay_cycles = 0;
    std::uint64_t stage_memory_base_issue_to_retire_cycles = 0;
    std::uint64_t stage_memory_corrected_issue_to_retire_cycles = 0;

    // Critical head time, unlike the overlapping residence sums above. The
    // first retired UOP establishes the left edge; every later zero-commit
    // cycle is assigned to exactly one stage of the next ROB-head UOP.
    std::uint64_t head_gap_zero_commit_cycles = 0;
    std::uint64_t head_gap_not_fetched_cycles = 0;
    std::uint64_t head_gap_fetched_not_issued_cycles = 0;
    std::uint64_t head_gap_issued_not_retired_cycles = 0;
    std::uint64_t head_gap_issued_load_cycles = 0;
    std::uint64_t head_gap_issued_store_cycles = 0;
    std::uint64_t head_gap_issued_non_memory_cycles = 0;

    bool stage_conserved() const {
        return stage_base_issue_to_retire_cycles ==
                   stage_base_issue_to_completion_cycles +
                       stage_base_completion_to_retire_cycles &&
               stage_corrected_issue_to_retire_cycles ==
                   stage_corrected_issue_to_completion_cycles +
                       stage_corrected_completion_to_retire_cycles;
    }

    bool head_gap_conserved() const {
        return head_gap_zero_commit_cycles ==
                   head_gap_not_fetched_cycles +
                       head_gap_fetched_not_issued_cycles +
                       head_gap_issued_not_retired_cycles &&
               head_gap_issued_not_retired_cycles ==
                   head_gap_issued_load_cycles +
                       head_gap_issued_store_cycles +
                       head_gap_issued_non_memory_cycles;
    }

    bool dependency_conserved() const {
        return dependency_input_cycles ==
               dependency_absorbed_cycles +
                   dependency_propagated_cycles;
    }

    bool retire_conserved() const {
        return retire_input_cycles ==
               retire_absorbed_cycles + retire_propagated_cycles;
    }

    bool store_lifecycle_conserved() const {
        return store_commit_to_sq_release_cycles ==
               store_tso_wait_cycles + store_send_to_response_cycles;
    }

    ResponseResidualCounters& operator+=(
        const ResponseResidualCounters& other) {
        instruction_fetch_seed_events +=
            other.instruction_fetch_seed_events;
        instruction_fetch_seed_cycles +=
            other.instruction_fetch_seed_cycles;
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
        fetch_queue_moved_uops += other.fetch_queue_moved_uops;
        fetch_queue_moved_cycles += other.fetch_queue_moved_cycles;
        memory_issue_moved_events += other.memory_issue_moved_events;
        memory_issue_moved_cycles += other.memory_issue_moved_cycles;
        escape_issue_moved_events += other.escape_issue_moved_events;
        escape_issue_moved_cycles += other.escape_issue_moved_cycles;
        store_uops += other.store_uops;
        store_address_to_commit_cycles +=
            other.store_address_to_commit_cycles;
        store_hierarchy_response_before_commit_uops +=
            other.store_hierarchy_response_before_commit_uops;
        store_hierarchy_response_before_commit_cycles +=
            other.store_hierarchy_response_before_commit_cycles;
        store_tso_wait_uops += other.store_tso_wait_uops;
        store_tso_wait_cycles += other.store_tso_wait_cycles;
        store_send_to_response_cycles +=
            other.store_send_to_response_cycles;
        store_commit_to_sq_release_cycles +=
            other.store_commit_to_sq_release_cycles;
        store_sq_release_max_cycles = std::max(
            store_sq_release_max_cycles,
            other.store_sq_release_max_cycles);
        store_send_retimed_uops += other.store_send_retimed_uops;
        store_send_retimed_cycles += other.store_send_retimed_cycles;
        stage_uops += other.stage_uops;
        stage_memory_uops += other.stage_memory_uops;
        stage_non_memory_uops += other.stage_non_memory_uops;
        stage_non_memory_base_fetch_to_issue_cycles +=
            other.stage_non_memory_base_fetch_to_issue_cycles;
        stage_non_memory_corrected_fetch_to_issue_cycles +=
            other.stage_non_memory_corrected_fetch_to_issue_cycles;
        stage_non_memory_corrected_issue_to_retire_cycles +=
            other.stage_non_memory_corrected_issue_to_retire_cycles;
        stage_load_uops += other.stage_load_uops;
        stage_load_base_fetch_to_issue_cycles +=
            other.stage_load_base_fetch_to_issue_cycles;
        stage_load_corrected_fetch_to_issue_cycles +=
            other.stage_load_corrected_fetch_to_issue_cycles;
        stage_load_corrected_issue_to_completion_cycles +=
            other.stage_load_corrected_issue_to_completion_cycles;
        stage_load_corrected_issue_to_retire_cycles +=
            other.stage_load_corrected_issue_to_retire_cycles;
        stage_base_issue_to_completion_cycles +=
            other.stage_base_issue_to_completion_cycles;
        stage_base_completion_to_retire_cycles +=
            other.stage_base_completion_to_retire_cycles;
        stage_base_issue_to_retire_cycles +=
            other.stage_base_issue_to_retire_cycles;
        stage_corrected_issue_to_completion_cycles +=
            other.stage_corrected_issue_to_completion_cycles;
        stage_corrected_completion_to_retire_cycles +=
            other.stage_corrected_completion_to_retire_cycles;
        stage_corrected_issue_to_retire_cycles +=
            other.stage_corrected_issue_to_retire_cycles;
        stage_issue_delay_cycles += other.stage_issue_delay_cycles;
        stage_completion_delay_cycles +=
            other.stage_completion_delay_cycles;
        stage_retire_delay_cycles += other.stage_retire_delay_cycles;
        stage_memory_base_issue_to_retire_cycles +=
            other.stage_memory_base_issue_to_retire_cycles;
        stage_memory_corrected_issue_to_retire_cycles +=
            other.stage_memory_corrected_issue_to_retire_cycles;
        head_gap_zero_commit_cycles +=
            other.head_gap_zero_commit_cycles;
        head_gap_not_fetched_cycles +=
            other.head_gap_not_fetched_cycles;
        head_gap_fetched_not_issued_cycles +=
            other.head_gap_fetched_not_issued_cycles;
        head_gap_issued_not_retired_cycles +=
            other.head_gap_issued_not_retired_cycles;
        head_gap_issued_load_cycles +=
            other.head_gap_issued_load_cycles;
        head_gap_issued_store_cycles +=
            other.head_gap_issued_store_cycles;
        head_gap_issued_non_memory_cycles +=
            other.head_gap_issued_non_memory_cycles;
        return *this;
    }
};

// Per-core time-epoch population and boundary ledger.  Every accepted memory
// event is classified exactly once by its response-corrected issue time.  The
// accepted-UOP population is independently checked against the committed
// pipeline audit at report time, which catches cursor loss/duplication without
// changing the production scheduler.
struct CommittedEpochAuditCounters {
    std::uint64_t accepted_prefixes = 0;
    std::uint64_t accepted_uops = 0;
    std::uint64_t memory_events = 0;
    std::uint64_t inflight_memory_uops = 0;
    std::uint64_t corrected_horizon_violations = 0;
    std::uint64_t corrected_issue_within_horizon_events = 0;
    std::uint64_t corrected_issue_beyond_horizon_events = 0;
    std::uint64_t corrected_issue_beyond_horizon_uops = 0;
    std::uint64_t corrected_issue_beyond_horizon_cycles = 0;
    std::uint64_t corrected_issue_beyond_horizon_max_cycles = 0;
    std::uint64_t sparse_cross_epoch_edges = 0;

    bool memory_events_conserved() const {
        return memory_events == corrected_issue_within_horizon_events +
                                    corrected_issue_beyond_horizon_events;
    }

    CommittedEpochAuditCounters& operator+=(
        const CommittedEpochAuditCounters& other) {
        accepted_prefixes += other.accepted_prefixes;
        accepted_uops += other.accepted_uops;
        memory_events += other.memory_events;
        inflight_memory_uops += other.inflight_memory_uops;
        corrected_horizon_violations +=
            other.corrected_horizon_violations;
        corrected_issue_within_horizon_events +=
            other.corrected_issue_within_horizon_events;
        corrected_issue_beyond_horizon_events +=
            other.corrected_issue_beyond_horizon_events;
        corrected_issue_beyond_horizon_uops +=
            other.corrected_issue_beyond_horizon_uops;
        corrected_issue_beyond_horizon_cycles +=
            other.corrected_issue_beyond_horizon_cycles;
        corrected_issue_beyond_horizon_max_cycles = std::max(
            corrected_issue_beyond_horizon_max_cycles,
            other.corrected_issue_beyond_horizon_max_cycles);
        sparse_cross_epoch_edges += other.sparse_cross_epoch_edges;
        return *this;
    }
};

struct PageFaultAllocationCandidateCounters {
    std::array<std::uint64_t,
               kPageFaultAllocationRecencyUpperBounds.size()>
        recency_candidates{};
    std::array<std::uint64_t,
               kPageFaultAllocationRecencyUpperBounds.size()>
        recency_write_candidates{};

    PageFaultAllocationCandidateCounters& operator+=(
        const PageFaultAllocationCandidateCounters& other) {
        for (std::size_t index = 0; index < recency_candidates.size();
             ++index) {
            recency_candidates[index] += other.recency_candidates[index];
            recency_write_candidates[index] +=
                other.recency_write_candidates[index];
        }
        return *this;
    }
};

struct CoreCounters {
    std::uint64_t records = 0;
    std::uint64_t retired_uops = 0;
    std::uint64_t retired_instructions = 0;
    // Exact CPL0 records replayed from a privilege-tagged FST. These are a
    // subset of the aggregate functional counters above, not synthetic PMU.
    std::uint64_t native_kernel_records = 0;
    std::uint64_t native_kernel_retired_uops = 0;
    std::uint64_t native_kernel_retired_instructions = 0;
    std::uint64_t native_kernel_memory_uops = 0;
    std::uint64_t native_kernel_memory_accesses = 0;
    BranchCounters native_kernel_branch;
    TranslationCounters native_kernel_dtlb;
    // Retired memory-operation records. `memory_accesses` below is the
    // separately conserved cache-line expansion and may be larger for a
    // cross-line UOP.
    std::uint64_t memory_uops = 0;
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
    // First appearances of valid virtual-page tokens considered by the
    // statistical page-fault model. Missing-token accesses are explicit so a
    // configuration can never silently claim full page-fault coverage.
    std::uint64_t page_fault_first_touch_candidates = 0;
    std::uint64_t page_fault_first_touch_write_candidates = 0;
    std::uint64_t page_fault_background_candidates = 0;
    std::uint64_t page_fault_background_read_candidates = 0;
    std::uint64_t page_fault_background_write_candidates = 0;
    std::uint64_t page_fault_allocation_candidates = 0;
    // Mutually exclusive buckets for candidates after a trace-visible
    // allocation syscall. The upper bounds are fixed above so calibration can
    // select a deployable recency window without replaying each trace.
    std::array<std::uint64_t,
               kPageFaultAllocationRecencyUpperBounds.size()>
        page_fault_allocation_recency_candidates{};
    std::array<std::uint64_t,
               kPageFaultAllocationRecencyUpperBounds.size()>
        page_fault_allocation_recency_write_candidates{};
    // Same candidates as the aggregate histograms above, partitioned by the
    // most recent trace-visible allocation syscall. std::map keeps reports
    // deterministic regardless of producer scheduling.
    std::map<std::uint64_t, PageFaultAllocationCandidateCounters>
        page_fault_allocation_by_syscall;
    std::uint64_t page_fault_untracked_accesses = 0;
    std::uint64_t page_fault_syscall_semantic_candidates = 0;
    std::uint64_t page_fault_syscall_semantic_write_candidates = 0;
    std::uint64_t
        page_fault_syscall_semantic_fallback_write_candidates = 0;
    std::uint64_t
        page_fault_syscall_semantic_fallback_write_selected = 0;
    std::uint64_t page_fault_virtual_page_map_misses = 0;
    // Guest-PTE coverage is counted once per process virtual page in each
    // phase. Present pages suppress the statistical selector; non-present
    // pages select a first touch unless the producer marked its #PF as
    // already in flight at ROI entry. Unknown pages retain
    // the existing syscall/fallback path rather than being silently guessed.
    std::uint64_t page_fault_initial_pte_known_pages = 0;
    std::uint64_t page_fault_initial_pte_present_pages = 0;
    std::uint64_t page_fault_initial_pte_nonpresent_pages = 0;
    std::uint64_t page_fault_initial_pte_unknown_pages = 0;
    std::uint64_t page_fault_initial_pte_selected = 0;
    std::uint64_t page_fault_roi_entry_known_pages = 0;
    std::uint64_t page_fault_roi_entry_present_pages = 0;
    std::uint64_t page_fault_roi_entry_nonpresent_pages = 0;
    std::uint64_t page_fault_roi_entry_unknown_pages = 0;
    std::uint64_t page_fault_roi_entry_selected = 0;
    std::uint64_t page_fault_roi_entry_inflight_suppressed = 0;
    std::uint64_t page_fault_process_shared_duplicate_pages = 0;
    // State-only page fills alter cache residency but remain outside user and
    // kernel architectural PMU. These counters make that approximation
    // explicit and auditable.
    std::uint64_t page_fault_cache_state_pages = 0;
    std::uint64_t page_fault_cache_state_lines = 0;
    std::uint64_t branch_penalty_cycles = 0;
    std::uint64_t branch_shadow_uops = 0;
    std::uint64_t branch_shadow_cycles = 0;
    BranchPopulationAuditCounters branch_population;
    // Raw frontend events. Refill delay can overlap backend work and is not
    // an additive CPI decomposition.
    std::uint64_t fetch_buffer_transitions = 0;
    std::uint64_t fetch_buffer_refill_delay_cycles = 0;
    std::uint64_t fetch_block_response_wait_cycles = 0;
    std::uint64_t fetch_block_response_hidden_cycles = 0;
    std::uint64_t fetch_block_response_exposed_cycles = 0;
    std::uint64_t fetch_block_response_to_resume_cycles = 0;
    std::uint64_t fetch_block_request_to_resume_cycles = 0;
    std::uint64_t fetch_block_request_admission_delay_cycles = 0;
    std::uint64_t fetch_response_ledger_committed_requests = 0;
    std::uint64_t fetch_response_ledger_shadow_requests = 0;
    std::uint64_t fetch_response_ledger_responses = 0;
    std::uint64_t fetch_response_ledger_server_wait_cycles = 0;
    std::uint64_t speculative_fetch_shadow_uops = 0;
    std::uint64_t speculative_fetch_shadow_requests_estimated = 0;
    std::uint64_t speculative_fetch_shadow_requests_issued = 0;
    std::uint64_t speculative_fetch_shadow_response_wait_cycles = 0;
    std::uint64_t speculative_fetch_shadow_recovery_hidden_cycles = 0;
    std::uint64_t speculative_fetch_shadow_recovery_exposed_cycles = 0;
    std::uint64_t speculative_fetch_shadow_density_unavailable = 0;
    std::uint64_t fetch_supply_static_span_lookups = 0;
    std::uint64_t fetch_supply_static_span_unavailable = 0;
    std::uint64_t fetch_supply_cross_block_instructions = 0;
    std::uint64_t fetch_supply_cross_block_extra_requests = 0;
    std::uint64_t l1i_miss_stall_cycles = 0;
    // Physical request ledger generated from committed L1I misses. Exact
    // `.fst.ifmap` and built-in modeled mappings are reported separately.
    std::uint64_t instruction_page_map_lookups = 0;
    std::uint64_t instruction_page_map_hits = 0;
    std::uint64_t instruction_page_map_misses = 0;
    std::uint64_t modeled_instruction_page_lookups = 0;
    std::uint64_t physical_instruction_fetch_requests = 0;
    std::uint64_t modeled_instruction_fetch_requests = 0;
    std::uint64_t physical_kernel_instruction_fetch_requests = 0;
    std::uint64_t instruction_fetch_lower_hierarchy_requests = 0;
    std::uint64_t instruction_fetch_request_order_clamps = 0;
    std::uint64_t instruction_fetch_request_order_clamp_cycles = 0;
    std::uint64_t l1i_speculative_entry_accesses = 0;
    std::uint64_t l1i_speculative_entry_hits = 0;
    std::uint64_t l1i_speculative_entry_misses = 0;
    std::uint64_t l1i_speculative_entry_evictions = 0;
    std::uint64_t l1i_speculative_entry_untracked = 0;
    std::uint64_t l1i_speculative_path_records = 0;
    std::uint64_t l1i_speculative_path_accesses = 0;
    std::uint64_t l1i_speculative_path_hits = 0;
    std::uint64_t l1i_speculative_path_misses = 0;
    std::uint64_t l1i_speculative_path_evictions = 0;
    std::uint64_t l1i_speculative_path_static_instructions = 0;
    std::uint64_t l1i_speculative_path_operand_instructions = 0;
    std::uint64_t l1i_speculative_path_read_registers = 0;
    std::uint64_t l1i_speculative_path_write_registers = 0;
    std::uint64_t l1i_speculative_path_operand_segments = 0;
    std::uint64_t l1i_speculative_path_raw_edges = 0;
    std::uint64_t l1i_speculative_path_dependent_instructions = 0;
    std::uint64_t l1i_speculative_path_chain_depth_sum = 0;
    std::uint64_t l1i_speculative_path_chain_depth_max = 0;
    std::uint64_t l1i_speculative_path_operand_rob_prefix_uops_q16 = 0;
    std::uint64_t l1i_speculative_path_operand_rob_capped_instructions = 0;
    std::uint64_t l1i_speculative_path_operand_rob_capped_read_registers = 0;
    std::uint64_t l1i_speculative_path_operand_rob_capped_write_registers = 0;
    std::uint64_t l1i_speculative_path_operand_rob_capped_memory_instructions =
        0;
    std::uint64_t
        l1i_speculative_path_operand_rob_capped_memory_instructions_max_per_path =
            0;
    std::uint64_t
        l1i_speculative_path_operand_rob_capped_write_registers_max_per_path =
            0;
    std::uint64_t l1i_speculative_path_operand_rob_capped_raw_edges = 0;
    std::uint64_t
        l1i_speculative_path_operand_rob_capped_dependent_instructions = 0;
    std::uint64_t l1i_speculative_path_operand_rob_capped_chain_depth_sum = 0;
    std::uint64_t l1i_speculative_path_operand_rob_capped_chain_depth_max = 0;
    std::uint64_t l1i_speculative_path_memory_instructions = 0;
    std::uint64_t l1i_speculative_path_memory_page_known = 0;
    std::uint64_t l1i_speculative_path_memory_page_unstable = 0;
    std::uint64_t l1i_speculative_path_memory_page_transition_samples = 0;
    std::uint64_t l1i_speculative_path_memory_page_transition_score_ppm = 0;
    std::uint64_t l1i_speculative_path_profiled_instructions = 0;
    std::array<std::uint64_t, kSpeculativeProfilePoolCount>
        l1i_speculative_path_profile_uops_q16{};
    std::uint64_t l1i_speculative_path_profile_rob_capped_uops_q16 = 0;
    std::uint64_t l1i_speculative_path_conditional_stops = 0;
    std::uint64_t l1i_speculative_path_indirect_stops = 0;
    std::uint64_t l1i_speculative_path_static_map_misses = 0;
    std::uint64_t l1i_speculative_path_unknown_edges = 0;
    TranslationCounters speculative_dtlb;
    std::uint64_t memory_penalty_cycles = 0;
    // Lower-bound memory events are currently kept in per-core program order
    // for the canonical merge. These audit counters quantify how often that
    // conservative ordering moves an event beyond its O3 issue bound.
    std::uint64_t memory_order_clamp_events = 0;
    std::uint64_t memory_order_clamp_cycles = 0;
    std::uint64_t cycles = 0;
    CacheCounters l1i;
    CacheCounters l1d;
    // Direct committed instruction requests at the unified private L2. Keep
    // these separate from data-demand PMU while sharing the same cache state.
    CacheCounters instruction_l2;
    CacheCounters l2;
    BranchCounters branch;
    TranslationCounters dtlb;
    // Delayed page-walker activity is not an architectural PMU domain.  Keep
    // it separate so repeated followers can affect CPI without inflating the
    // retired DTLB-miss count.
    TranslationCounters dtlb_timing;
    // Separate source domains allow the reporting layer to conserve
    // user+kernel PMU without pretending that synthetic kernel events were
    // present in the functional stream.
    KernelEventCounters syscall_kernel;
    KernelEventCounters page_fault_kernel;
    KernelEventCounters irq_kernel;

    CoreCounters& operator+=(const CoreCounters& other) {
        records += other.records;
        retired_uops += other.retired_uops;
        retired_instructions += other.retired_instructions;
        native_kernel_records += other.native_kernel_records;
        native_kernel_retired_uops +=
            other.native_kernel_retired_uops;
        native_kernel_retired_instructions +=
            other.native_kernel_retired_instructions;
        native_kernel_memory_uops += other.native_kernel_memory_uops;
        native_kernel_memory_accesses +=
            other.native_kernel_memory_accesses;
        native_kernel_branch += other.native_kernel_branch;
        native_kernel_dtlb += other.native_kernel_dtlb;
        memory_uops += other.memory_uops;
        memory_accesses += other.memory_accesses;
        mmio_escape_accesses += other.mmio_escape_accesses;
        unknown_addresses += other.unknown_addresses;
        branches_without_outcome += other.branches_without_outcome;
        serializing_uops += other.serializing_uops;
        syscall_uops += other.syscall_uops;
        syscall_drain_cycles += other.syscall_drain_cycles;
        syscall_service_cycles += other.syscall_service_cycles;
        syscall_restart_cycles += other.syscall_restart_cycles;
        page_fault_first_touch_candidates +=
            other.page_fault_first_touch_candidates;
        page_fault_first_touch_write_candidates +=
            other.page_fault_first_touch_write_candidates;
        page_fault_background_candidates +=
            other.page_fault_background_candidates;
        page_fault_background_read_candidates +=
            other.page_fault_background_read_candidates;
        page_fault_background_write_candidates +=
            other.page_fault_background_write_candidates;
        page_fault_allocation_candidates +=
            other.page_fault_allocation_candidates;
        for (std::size_t index = 0;
             index < page_fault_allocation_recency_candidates.size();
             ++index) {
            page_fault_allocation_recency_candidates[index] +=
                other.page_fault_allocation_recency_candidates[index];
            page_fault_allocation_recency_write_candidates[index] +=
                other
                    .page_fault_allocation_recency_write_candidates[index];
        }
        for (const auto& [sysnum, counters] :
             other.page_fault_allocation_by_syscall) {
            page_fault_allocation_by_syscall[sysnum] += counters;
        }
        page_fault_untracked_accesses +=
            other.page_fault_untracked_accesses;
        page_fault_syscall_semantic_candidates +=
            other.page_fault_syscall_semantic_candidates;
        page_fault_syscall_semantic_write_candidates +=
            other.page_fault_syscall_semantic_write_candidates;
        page_fault_syscall_semantic_fallback_write_candidates +=
            other.page_fault_syscall_semantic_fallback_write_candidates;
        page_fault_syscall_semantic_fallback_write_selected +=
            other.page_fault_syscall_semantic_fallback_write_selected;
        page_fault_virtual_page_map_misses +=
            other.page_fault_virtual_page_map_misses;
        page_fault_initial_pte_known_pages +=
            other.page_fault_initial_pte_known_pages;
        page_fault_initial_pte_present_pages +=
            other.page_fault_initial_pte_present_pages;
        page_fault_initial_pte_nonpresent_pages +=
            other.page_fault_initial_pte_nonpresent_pages;
        page_fault_initial_pte_unknown_pages +=
            other.page_fault_initial_pte_unknown_pages;
        page_fault_initial_pte_selected +=
            other.page_fault_initial_pte_selected;
        page_fault_roi_entry_known_pages +=
            other.page_fault_roi_entry_known_pages;
        page_fault_roi_entry_present_pages +=
            other.page_fault_roi_entry_present_pages;
        page_fault_roi_entry_nonpresent_pages +=
            other.page_fault_roi_entry_nonpresent_pages;
        page_fault_roi_entry_unknown_pages +=
            other.page_fault_roi_entry_unknown_pages;
        page_fault_roi_entry_selected +=
            other.page_fault_roi_entry_selected;
        page_fault_roi_entry_inflight_suppressed +=
            other.page_fault_roi_entry_inflight_suppressed;
        page_fault_process_shared_duplicate_pages +=
            other.page_fault_process_shared_duplicate_pages;
        page_fault_cache_state_pages +=
            other.page_fault_cache_state_pages;
        page_fault_cache_state_lines +=
            other.page_fault_cache_state_lines;
        branch_penalty_cycles += other.branch_penalty_cycles;
        branch_shadow_uops += other.branch_shadow_uops;
        branch_shadow_cycles += other.branch_shadow_cycles;
        branch_population += other.branch_population;
        fetch_buffer_transitions += other.fetch_buffer_transitions;
        fetch_buffer_refill_delay_cycles +=
            other.fetch_buffer_refill_delay_cycles;
        fetch_block_response_wait_cycles +=
            other.fetch_block_response_wait_cycles;
        fetch_block_response_hidden_cycles +=
            other.fetch_block_response_hidden_cycles;
        fetch_block_response_exposed_cycles +=
            other.fetch_block_response_exposed_cycles;
        fetch_block_response_to_resume_cycles +=
            other.fetch_block_response_to_resume_cycles;
        fetch_block_request_to_resume_cycles +=
            other.fetch_block_request_to_resume_cycles;
        fetch_block_request_admission_delay_cycles +=
            other.fetch_block_request_admission_delay_cycles;
        fetch_response_ledger_committed_requests +=
            other.fetch_response_ledger_committed_requests;
        fetch_response_ledger_shadow_requests +=
            other.fetch_response_ledger_shadow_requests;
        fetch_response_ledger_responses +=
            other.fetch_response_ledger_responses;
        fetch_response_ledger_server_wait_cycles +=
            other.fetch_response_ledger_server_wait_cycles;
        speculative_fetch_shadow_uops +=
            other.speculative_fetch_shadow_uops;
        speculative_fetch_shadow_requests_estimated +=
            other.speculative_fetch_shadow_requests_estimated;
        speculative_fetch_shadow_requests_issued +=
            other.speculative_fetch_shadow_requests_issued;
        speculative_fetch_shadow_response_wait_cycles +=
            other.speculative_fetch_shadow_response_wait_cycles;
        speculative_fetch_shadow_recovery_hidden_cycles +=
            other.speculative_fetch_shadow_recovery_hidden_cycles;
        speculative_fetch_shadow_recovery_exposed_cycles +=
            other.speculative_fetch_shadow_recovery_exposed_cycles;
        speculative_fetch_shadow_density_unavailable +=
            other.speculative_fetch_shadow_density_unavailable;
        fetch_supply_static_span_lookups +=
            other.fetch_supply_static_span_lookups;
        fetch_supply_static_span_unavailable +=
            other.fetch_supply_static_span_unavailable;
        fetch_supply_cross_block_instructions +=
            other.fetch_supply_cross_block_instructions;
        fetch_supply_cross_block_extra_requests +=
            other.fetch_supply_cross_block_extra_requests;
        l1i_miss_stall_cycles += other.l1i_miss_stall_cycles;
        instruction_page_map_lookups +=
            other.instruction_page_map_lookups;
        instruction_page_map_hits += other.instruction_page_map_hits;
        instruction_page_map_misses += other.instruction_page_map_misses;
        modeled_instruction_page_lookups +=
            other.modeled_instruction_page_lookups;
        physical_instruction_fetch_requests +=
            other.physical_instruction_fetch_requests;
        modeled_instruction_fetch_requests +=
            other.modeled_instruction_fetch_requests;
        physical_kernel_instruction_fetch_requests +=
            other.physical_kernel_instruction_fetch_requests;
        instruction_fetch_lower_hierarchy_requests +=
            other.instruction_fetch_lower_hierarchy_requests;
        instruction_fetch_request_order_clamps +=
            other.instruction_fetch_request_order_clamps;
        instruction_fetch_request_order_clamp_cycles +=
            other.instruction_fetch_request_order_clamp_cycles;
        l1i_speculative_entry_accesses +=
            other.l1i_speculative_entry_accesses;
        l1i_speculative_entry_hits += other.l1i_speculative_entry_hits;
        l1i_speculative_entry_misses +=
            other.l1i_speculative_entry_misses;
        l1i_speculative_entry_evictions +=
            other.l1i_speculative_entry_evictions;
        l1i_speculative_entry_untracked +=
            other.l1i_speculative_entry_untracked;
        l1i_speculative_path_records +=
            other.l1i_speculative_path_records;
        l1i_speculative_path_accesses +=
            other.l1i_speculative_path_accesses;
        l1i_speculative_path_hits += other.l1i_speculative_path_hits;
        l1i_speculative_path_misses += other.l1i_speculative_path_misses;
        l1i_speculative_path_evictions +=
            other.l1i_speculative_path_evictions;
        l1i_speculative_path_static_instructions +=
            other.l1i_speculative_path_static_instructions;
        l1i_speculative_path_operand_instructions +=
            other.l1i_speculative_path_operand_instructions;
        l1i_speculative_path_read_registers +=
            other.l1i_speculative_path_read_registers;
        l1i_speculative_path_write_registers +=
            other.l1i_speculative_path_write_registers;
        l1i_speculative_path_operand_segments +=
            other.l1i_speculative_path_operand_segments;
        l1i_speculative_path_raw_edges +=
            other.l1i_speculative_path_raw_edges;
        l1i_speculative_path_dependent_instructions +=
            other.l1i_speculative_path_dependent_instructions;
        l1i_speculative_path_chain_depth_sum +=
            other.l1i_speculative_path_chain_depth_sum;
        l1i_speculative_path_chain_depth_max = std::max(
            l1i_speculative_path_chain_depth_max,
            other.l1i_speculative_path_chain_depth_max);
        l1i_speculative_path_operand_rob_prefix_uops_q16 +=
            other.l1i_speculative_path_operand_rob_prefix_uops_q16;
        l1i_speculative_path_operand_rob_capped_instructions +=
            other.l1i_speculative_path_operand_rob_capped_instructions;
        l1i_speculative_path_operand_rob_capped_read_registers +=
            other.l1i_speculative_path_operand_rob_capped_read_registers;
        l1i_speculative_path_operand_rob_capped_write_registers +=
            other.l1i_speculative_path_operand_rob_capped_write_registers;
        l1i_speculative_path_operand_rob_capped_memory_instructions +=
            other
                .l1i_speculative_path_operand_rob_capped_memory_instructions;
        l1i_speculative_path_operand_rob_capped_memory_instructions_max_per_path =
            std::max(
                l1i_speculative_path_operand_rob_capped_memory_instructions_max_per_path,
                other
                    .l1i_speculative_path_operand_rob_capped_memory_instructions_max_per_path);
        l1i_speculative_path_operand_rob_capped_write_registers_max_per_path =
            std::max(
                l1i_speculative_path_operand_rob_capped_write_registers_max_per_path,
                other
                    .l1i_speculative_path_operand_rob_capped_write_registers_max_per_path);
        l1i_speculative_path_operand_rob_capped_raw_edges +=
            other.l1i_speculative_path_operand_rob_capped_raw_edges;
        l1i_speculative_path_operand_rob_capped_dependent_instructions +=
            other
                .l1i_speculative_path_operand_rob_capped_dependent_instructions;
        l1i_speculative_path_operand_rob_capped_chain_depth_sum +=
            other.l1i_speculative_path_operand_rob_capped_chain_depth_sum;
        l1i_speculative_path_operand_rob_capped_chain_depth_max = std::max(
            l1i_speculative_path_operand_rob_capped_chain_depth_max,
            other.l1i_speculative_path_operand_rob_capped_chain_depth_max);
        l1i_speculative_path_memory_instructions +=
            other.l1i_speculative_path_memory_instructions;
        l1i_speculative_path_memory_page_known +=
            other.l1i_speculative_path_memory_page_known;
        l1i_speculative_path_memory_page_unstable +=
            other.l1i_speculative_path_memory_page_unstable;
        l1i_speculative_path_memory_page_transition_samples +=
            other.l1i_speculative_path_memory_page_transition_samples;
        l1i_speculative_path_memory_page_transition_score_ppm +=
            other.l1i_speculative_path_memory_page_transition_score_ppm;
        l1i_speculative_path_profiled_instructions +=
            other.l1i_speculative_path_profiled_instructions;
        for (std::size_t index = 0;
             index < l1i_speculative_path_profile_uops_q16.size();
             ++index) {
            l1i_speculative_path_profile_uops_q16[index] +=
                other.l1i_speculative_path_profile_uops_q16[index];
        }
        l1i_speculative_path_profile_rob_capped_uops_q16 +=
            other.l1i_speculative_path_profile_rob_capped_uops_q16;
        l1i_speculative_path_conditional_stops +=
            other.l1i_speculative_path_conditional_stops;
        l1i_speculative_path_indirect_stops +=
            other.l1i_speculative_path_indirect_stops;
        l1i_speculative_path_static_map_misses +=
            other.l1i_speculative_path_static_map_misses;
        l1i_speculative_path_unknown_edges +=
            other.l1i_speculative_path_unknown_edges;
        speculative_dtlb += other.speculative_dtlb;
        memory_penalty_cycles += other.memory_penalty_cycles;
        memory_order_clamp_events += other.memory_order_clamp_events;
        memory_order_clamp_cycles += other.memory_order_clamp_cycles;
        cycles += other.cycles;
        l1i += other.l1i;
        l1d += other.l1d;
        instruction_l2 += other.instruction_l2;
        l2 += other.l2;
        branch += other.branch;
        dtlb += other.dtlb;
        dtlb_timing += other.dtlb_timing;
        syscall_kernel += other.syscall_kernel;
        page_fault_kernel += other.page_fault_kernel;
        irq_kernel += other.irq_kernel;
        return *this;
    }
};

struct ThreadStats {
    std::uint32_t thread_id = 0;
    std::uint32_t initial_core = 0;
    std::uint32_t final_core = 0;
    // Legacy/static binding fallback. Dynamic traces report their measured
    // coverage in the fields below without changing this compatibility field.
    std::uint64_t address_space_id = 0;
    std::uint64_t initial_effective_address_space_id = 0;
    std::uint64_t final_effective_address_space_id = 0;
    std::uint64_t distinct_address_spaces = 0;
    std::uint64_t address_space_switches = 0;
    std::uint64_t records = 0;
    std::uint64_t retired_uops = 0;
    std::uint64_t retired_instructions = 0;
    std::uint64_t native_kernel_records = 0;
    std::uint64_t native_kernel_retired_uops = 0;
    std::uint64_t native_kernel_retired_instructions = 0;
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
    std::vector<CommittedEpochAuditCounters> committed_epoch_audit;
    // Direct I-side outcomes use the same modeled LLC/CHA/DRAM state and
    // timing resources, but are not part of the committed data-demand PMU
    // contract consumed by the validator.
    CacheCounters instruction_llc;
    std::vector<ChaCounters> instruction_cha;
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
    // Experimental regular-store request edge. Counts are cache-line
    // requests, except boundary_deferred_uops which counts excluded UOPs.
    std::uint64_t store_post_commit_request_events = 0;
    std::uint64_t store_post_commit_request_delay_cycles = 0;
    std::uint64_t store_post_commit_request_max_delay_cycles = 0;
    std::uint64_t store_post_commit_request_reordered_events = 0;
    std::uint64_t store_post_commit_request_boundary_deferred_uops = 0;
    // A cache request may be issued after the local retire lower bound of
    // its owning UOP. Time-epoch scheduling defers that UOP until every
    // attached request edge enters the global horizon.
    std::uint64_t time_epoch_request_boundary_deferred_uops = 0;
    std::uint64_t store_post_commit_request_candidate_epochs = 0;
    std::uint64_t store_post_commit_request_stable_epochs = 0;
    std::uint64_t store_post_commit_request_fallback_epochs = 0;
    std::uint64_t store_post_commit_request_horizon_fallback_epochs = 0;
    std::uint64_t store_post_commit_request_passes = 0;
    std::uint64_t store_post_commit_request_replayed_events = 0;
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
    std::uint64_t response_iq_radix_checkpoints = 0;
    std::uint64_t response_iq_radix_updates = 0;
    std::array<std::uint64_t, 3> response_causal_block_candidates{};
    std::array<std::uint64_t, 3> response_causal_block_transfers{};
    std::uint64_t response_causal_block_transferred_uops = 0;
    std::uint64_t response_materialized_fast_kernel_checkpoints = 0;
    std::uint64_t response_materialized_fast_kernel_uops = 0;
    std::uint64_t response_event_only_calibration_checkpoints = 0;
    std::uint64_t response_event_only_calibration_uops = 0;
    std::uint64_t response_event_only_calibration_estimated_cycles = 0;
    std::uint64_t response_event_only_calibration_exact_cycles = 0;
    std::uint64_t response_event_only_teacher_checkpoints = 0;
    std::uint64_t response_event_only_teacher_uops = 0;
    std::uint64_t response_event_only_teacher_estimated_cycles = 0;
    std::uint64_t response_event_only_teacher_exact_cycles = 0;
    std::uint64_t response_event_only_teacher_reference_checkpoints = 0;
    std::uint64_t response_event_only_teacher_reference_uops = 0;
    std::uint64_t response_event_only_teacher_reference_exact_cycles = 0;
    std::uint64_t response_event_only_approximation_checkpoints = 0;
    std::uint64_t response_event_only_anchor_uops = 0;
    std::uint64_t response_event_only_skipped_uops = 0;
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

    CommittedEpochAuditCounters total_committed_epoch_audit() const {
        CommittedEpochAuditCounters total;
        for (const auto& core : committed_epoch_audit) total += core;
        return total;
    }
};

std::string hit_level_name(HitLevel level);

}  // namespace fastsim
