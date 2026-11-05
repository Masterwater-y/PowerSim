#pragma once

#include <cstdint>
#include <memory>
#include <vector>

namespace fastsim {

enum class SprFrontendSource : std::uint8_t { Mite, Dsb, Microcode };

struct SprFrontendConfig {
    std::uint32_t byte_buffer_bytes = 64;
    std::uint32_t byte_service_bytes = 32;
    std::uint32_t mite_instructions_per_batch = 6;
    std::uint32_t mite_width = 8;
    std::uint64_t mite_latency = 1;
    std::uint32_t dsb_sets = 64;
    std::uint32_t dsb_ways = 8;
    std::uint32_t dsb_slots_per_entry = 8;
    std::uint32_t dsb_width = 8;
    std::uint64_t dsb_latency = 1;
    std::uint32_t ms_width = 4;
    std::uint64_t ms_latency = 1;
    std::uint32_t batch_uops = 64;
    std::uint32_t idq_capacity = 144;
    std::uint32_t rename_width = 6;
    std::uint64_t dsb_to_mite_delay = 0;
};

// Facts only: identities and classifications must be supplied by the functional
// decoder companion. Partial observed macros are delivered but never cached.
// Explicit pseudo records skip byte traffic and DSB; normal macros require bytes.
struct SprFrontendMacro {
    std::uint64_t pc = 0;
    std::uint64_t address_space_id = 0;
    std::uint64_t code_identity = 0;
    std::uint32_t instruction_bytes = 0;
    std::uint32_t uops = 0;
    bool shared_allocation = false;
    bool microcode = false;
    bool pseudo = false;
    bool complete = true;
    bool unconditional_branch = false;
    bool dsb_eligible = true;
};

struct SprFrontendDelivery {
    std::uint64_t cycle = 0;
    std::uint32_t slot_cost = 0;
    SprFrontendSource source = SprFrontendSource::Mite;
    bool macro_begin = false;
    bool macro_end = false;
};

struct SprFrontendCounters {
    std::uint64_t macros = 0, uops = 0, allocation_slots = 0;
    std::uint64_t mite_macros = 0, dsb_macros = 0, ms_macros = 0;
    std::uint64_t byte_service_blocks = 0, byte_buffer_fills = 0;
    std::uint64_t dsb_lookups = 0, dsb_hits = 0, dsb_fills = 0;
    std::uint64_t dsb_evictions = 0, dsb_invalidations = 0;
    std::uint64_t dsb_oversize = 0, dsb_branch_limited_blocks = 0;
    std::uint64_t source_transitions = 0, switch_delay_cycles = 0;
    std::uint64_t enqueued_uops = 0, accepted_uops = 0;
    std::uint64_t idq_high_water_slots = 0, idq_credit_stalls = 0;
};

// Ordered, retired-path-only event calendar. Exactly one macro is active and
// one delivered record may await acknowledgement. accepted() must acknowledge
// actual Rename admission before the next delivery. Rename width is owned by
// the caller, which passes its reservation lower bound to deliver_next().
//
// begin_macro's earliest bounds source submission, not Rename. Consecutive
// macros may share that bound to preserve MITE acceptance batches. The model
// cannot look ahead into later macros while the caller is processing this one;
// batching across backend stalls therefore remains a retired-stream proxy.
// The decoded-cache branch prior uses bounded per-set LRU rejection tombstones.
// No speculative or wrong-path state, native source outcomes or timing is input.
class SprFrontend {
public:
    explicit SprFrontend(const SprFrontendConfig &config = {});
    ~SprFrontend();
    SprFrontend(SprFrontend &&) noexcept;
    SprFrontend &operator=(SprFrontend &&) noexcept;
    SprFrontend(const SprFrontend &);
    SprFrontend &operator=(const SprFrontend &);

    SprFrontendSource source_for(const SprFrontendMacro &) const;
    // Pure query against the same context/code identity and resident byte buffer
    // used by begin_macro. Returns aligned buffer BYTE addresses, in demand order.
    std::vector<std::uint64_t> instruction_blocks(const SprFrontendMacro &) const;
    void invalidate_code(); // stale byte response must not seed reusable state
    void begin_macro(const SprFrontendMacro &, std::uint64_t earliest,
                     std::uint64_t byte_ready, bool reusable_bytes=true);
    SprFrontendDelivery deliver_next(std::uint64_t earliest);
    void accepted(std::uint64_t actual_rename_cycle);
    bool active() const;
    std::uint32_t idq_occupancy() const;
    const SprFrontendCounters &counters() const;
    void reset();
    // Measurement-only reset: preserve templates, bytes and queued credits.
    void reset_counters();

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace fastsim
