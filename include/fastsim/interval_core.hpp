#pragma once

#include <array>
#include <cstdint>
#include <functional>
#include <queue>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "fastsim/cache.hpp"
#include "fastsim/config.hpp"
#include "fastsim/types.hpp"

namespace fastsim {

class TraceSource;

// Stable resource classes shared by the lower-bound scheduler and the
// response repair.  They describe the configured target FUPool; they are not
// host-worker lanes and do not encode workload-specific behavior.
enum class IntervalFuPool : std::uint8_t {
    kInteger,
    kIntegerMultiply,
    kFloatSimple,
    kFloatComplex,
    kSimd,
    kPredicate,
    kMemory,
    kSystem,
    kCount,
};
static_assert(static_cast<std::size_t>(IntervalFuPool::kCount) ==
              kSpeculativeProfilePoolCount);

struct IntervalTiming {
    std::uint64_t fetch_cycle = 0;
    std::uint64_t decode_cycle = 0;
    std::uint64_t rename_cycle = 0;
    std::uint64_t dispatch_cycle = 0;
    std::uint64_t issue_cycle = 0;
    std::uint64_t execute_cycle = 0;
    std::uint64_t completion_cycle = 0;
    std::uint64_t retire_cycle = 0;
    std::uint64_t translation_ready_cycle = 0;
    std::uint64_t translation_delay_cycles = 0;
    std::uint64_t syscall_drain_cycles = 0;
    std::uint64_t syscall_service_cycles = 0;
    std::uint64_t syscall_restart_cycles = 0;
    std::uint64_t branch_shadow_uops = 0;
    std::uint64_t branch_shadow_cycles = 0;
    bool fetch_buffer_transition = false;
    std::uint64_t fetch_buffer_refill_delay_cycles = 0;
    // Timing-neutral ledger for one committed fetch-block transition. The
    // response wait is partitioned into overlap with another ready gate and
    // locally exposed wait; later resume delay is reported separately.
    std::uint64_t fetch_block_request_cycle = 0;
    std::uint64_t fetch_block_response_cycle = 0;
    std::uint64_t fetch_block_response_wait_cycles = 0;
    std::uint64_t fetch_block_response_hidden_cycles = 0;
    std::uint64_t fetch_block_response_exposed_cycles = 0;
    std::uint64_t fetch_block_response_to_resume_cycles = 0;
    std::uint64_t fetch_block_request_to_resume_cycles = 0;
    std::uint64_t l1i_miss_stall_cycles = 0;
    IntervalFuPool fu_pool = IntervalFuPool::kInteger;
    std::uint32_t fu_occupancy_cycles = 1;
    bool dtlb_access = false;
    bool dtlb_hit = false;
    bool dtlb_miss = false;
    bool dtlb_merged_miss = false;
    bool dtlb_untracked = false;
    // The committed-stream PMU view above is updated immediately, matching
    // retired architectural accesses.  These fields describe the separate
    // timing walker state, where followers may still miss while an older walk
    // to the same page is outstanding.
    bool dtlb_timing_access = false;
    bool dtlb_timing_hit = false;
    bool dtlb_timing_miss = false;
    bool dtlb_timing_merged_miss = false;
    bool dtlb_timing_untracked = false;
    bool l1i_access = false;
    bool l1i_hit = false;
    bool l1i_miss = false;
    bool l1i_eviction = false;
    bool l1i_speculative_entry_access = false;
    bool l1i_speculative_entry_hit = false;
    bool l1i_speculative_entry_miss = false;
    bool l1i_speculative_entry_eviction = false;
    bool l1i_speculative_entry_untracked = false;
    std::uint64_t l1i_speculative_path_records = 0;
    std::uint64_t l1i_speculative_path_accesses = 0;
    std::uint64_t l1i_speculative_path_hits = 0;
    std::uint64_t l1i_speculative_path_misses = 0;
    std::uint64_t l1i_speculative_path_evictions = 0;
    std::uint64_t l1i_speculative_path_static_instructions = 0;
    // Audit-only operand coverage from .fst.imap v2. These counters do not
    // allocate rename/ROB/IQ resources or add cycles.
    std::uint64_t l1i_speculative_path_operand_instructions = 0;
    std::uint64_t l1i_speculative_path_read_registers = 0;
    std::uint64_t l1i_speculative_path_write_registers = 0;
    // Dependency/rename-pressure lower bounds are enabled only when the
    // instruction-map header asserts complete operand semantics. They are
    // local to one wrong-path replay and remain audit-only.
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
    std::uint64_t speculative_dtlb_accesses = 0;
    std::uint64_t speculative_dtlb_hits = 0;
    std::uint64_t speculative_dtlb_misses = 0;
    std::uint64_t speculative_dtlb_untracked = 0;
    bool l1i_speculative_path_unknown_edge = false;
};

// A functional, lower-bound OoO window model. It consumes only operation
// classes and producer distances. Shared-cache/DRAM feedback is intentionally
// outside this class so an interval can be bound first and woven later.
class IntervalCoreModel {
  public:
    explicit IntervalCoreModel(const SimulatorConfig& config);

    IntervalTiming schedule(const TraceRecord& record, bool branch_miss,
                            bool predicted_taken = false,
                            std::uint64_t predicted_target = 0,
                            bool predicted_target_available = false,
                            const TraceSource* trace_source = nullptr,
                            const std::vector<std::uint64_t>*
                                speculative_path = nullptr);
    // Insert an active kernel interval at a retired-instruction boundary.
    // The kernel PMU is accounted by the caller; this method changes only the
    // core time line and does not create functional trace UOPs.
    void inject_kernel_pause(std::uint32_t active_cycles);
    // Start a new measurement phase after a fully drained functional warmup.
    // Target timing/history and warmed architectural state remain resident.
    // Committed-pipeline counters are cleared and fully retired warmup rename
    // allocations are drained at the common barrier.
    void reset_measurement_audit();
    std::uint64_t retired_uops() const { return completion_.size(); }
    std::uint64_t last_retire_cycle() const { return last_retire_cycle_; }
    const CommittedPipelineAuditCounters& committed_pipeline_audit() const {
        return committed_pipeline_audit_;
    }

  private:
    using FuPool = IntervalFuPool;

    enum class DispatchGate : std::uint8_t {
        kNone,
        kBandwidth,
        kRob,
        kIq,
        kLq,
        kSq,
    };

    struct OpTraits {
        FuPool pool = FuPool::kInteger;
        std::uint32_t latency = 1;
        bool pipelined = true;
    };

    OpTraits traits(const TraceRecord& record) const;
    std::uint64_t allocate_dispatch(std::uint64_t earliest);
    std::uint64_t allocate_issue(std::uint64_t earliest,
                                 const OpTraits& traits,
                                 const TraceRecord& record);
    std::uint64_t allocate_writeback(std::uint64_t earliest);
    std::uint64_t allocate_retire(std::uint64_t earliest);
    static std::uint64_t allocate_stage(std::uint64_t earliest,
                                        std::uint32_t width,
                                        std::uint64_t& cycle,
                                        std::uint32_t& used);
    void release_iq_through(std::uint64_t cycle);
    std::uint64_t next_iq_release_cycle() const;
    std::uint64_t translate(const TraceRecord& record,
                            std::uint64_t earliest,
                            IntervalTiming& timing);
    void retire_page_walks_through(std::uint64_t cycle);
    void fill_dtlb(std::uint32_t token);
    void fill_architectural_dtlb(std::uint32_t token);
    void access_speculative_dtlb(std::uint64_t pc,
                                 IntervalTiming& timing);
    void audit_destination_releases_through(std::uint64_t cycle);
    using DestinationClassCounts =
        std::array<std::uint8_t, kTrackedRegisterClasses>;
    void destination_class_releases_through(std::uint64_t cycle);
    std::uint64_t rename_free_list_ready(
        const DestinationClassCounts& required,
        std::uint64_t earliest);
    void audit_dispatch_delay(std::uint64_t nominal,
                              std::uint64_t actual,
                              DispatchGate gate);
    void observe_committed_pc(const TraceRecord& record);
    void observe_committed_uop_profile(const TraceRecord& record);
    void account_speculative_uop_profile(std::uint64_t pc,
                                         IntervalTiming& timing) const;
    void account_speculative_operands(
        const StaticInstructionInfo& instruction,
        IntervalTiming& timing) const;
    void replay_speculative_l1i_path(std::uint64_t entry_pc,
                                    std::uint64_t record_budget,
                                    IntervalTiming& timing,
                                    const TraceSource* trace_source,
                                    const std::vector<std::uint64_t>*
                                        speculative_path,
                                    std::uint64_t profile_uop_budget);

    const SimulatorConfig& config_;
    std::vector<std::uint64_t> completion_;
    std::vector<std::uint64_t> retirement_;
    std::vector<std::uint64_t> dispatch_history_;
    // IQ releases have their own compact calendar. Non-memory UOPs leave at
    // issue; memory UOPs leave after the core-side execution completion in
    // the lower-bound pass. Shared-response extension is applied by the
    // interval feedback path.
    std::uint64_t iq_release_cursor_ = 0;
    std::uint64_t iq_occupancy_ = 0;
    std::vector<std::uint64_t> load_retirement_;
    std::vector<std::uint64_t> store_retirement_;
    using DestinationRelease =
        std::pair<std::uint64_t, std::uint32_t>;
    std::priority_queue<DestinationRelease,
                        std::vector<DestinationRelease>,
                        std::greater<DestinationRelease>>
        destination_releases_;
    std::uint64_t live_destination_tokens_ = 0;
    using DestinationClassRelease =
        std::pair<std::uint64_t, DestinationClassCounts>;
    std::priority_queue<DestinationClassRelease,
                        std::vector<DestinationClassRelease>,
                        std::greater<DestinationClassRelease>>
        destination_class_releases_;
    std::array<std::uint64_t, kTrackedRegisterClasses>
        live_destination_class_tokens_{};
    std::array<std::uint32_t, kTrackedRegisterClasses>
        rename_free_entries_{};
    CommittedPipelineAuditCounters committed_pipeline_audit_;
    std::vector<std::uint32_t> issue_slots_;
    std::vector<std::uint32_t> iq_release_slots_;
    std::vector<std::uint32_t> writeback_slots_;
    std::vector<std::uint32_t> load_port_slots_;
    std::vector<std::uint32_t> store_port_slots_;
    std::array<std::vector<std::uint64_t>,
               static_cast<std::size_t>(FuPool::kCount)> fu_ready_;
    std::array<OpTraits, 128> trait_table_{};

    std::uint64_t fetch_cycle_ = 0;
    std::uint32_t fetches_this_cycle_ = 0;
    std::uint64_t fetch_buffer_block_ = 0;
    bool fetch_buffer_valid_ = false;
    SetAssociativeCache l1i_;
    std::unordered_map<std::uint64_t, std::uint64_t>
        observed_pc_successor_;
    std::unordered_map<std::uint64_t, std::uint64_t>
        observed_branch_fallthrough_;
    std::unordered_map<std::uint64_t, std::uint32_t>
        observed_memory_page_;
    // Causal diagnostic only: a PC becomes unstable after the committed
    // stream has shown it touching more than one virtual page token.
    std::unordered_set<std::uint64_t> observed_memory_page_unstable_;
    std::unordered_map<std::uint64_t, std::uint64_t>
        observed_memory_page_transition_opportunities_;
    std::unordered_map<std::uint64_t, std::uint64_t>
        observed_memory_page_changes_;
    struct ObservedUopProfile {
        std::uint64_t instances = 0;
        std::array<std::uint64_t, kSpeculativeProfilePoolCount>
            pool_uops{};
    };
    struct PendingUopProfile {
        std::uint64_t pc = 0;
        std::array<std::uint32_t, kSpeculativeProfilePoolCount>
            pool_uops{};
        bool valid = false;
    };
    std::unordered_map<std::uint64_t, ObservedUopProfile>
        observed_uop_profiles_;
    PendingUopProfile pending_uop_profile_;
    std::uint64_t previous_macro_pc_ = 0;
    bool previous_macro_valid_ = false;
    bool previous_record_completed_macro_ = true;
    std::uint64_t decode_cycle_ = 0;
    std::uint32_t decodes_this_cycle_ = 0;
    std::uint64_t rename_cycle_ = 0;
    std::uint32_t renames_this_cycle_ = 0;
    std::uint64_t dispatch_cycle_ = 0;
    std::uint32_t dispatches_this_cycle_ = 0;
    std::uint64_t last_retire_cycle_ = 0;
    std::uint32_t retires_this_cycle_ = 0;
    std::uint64_t frontend_ready_cycle_ = 0;
    std::uint64_t serial_ready_cycle_ = 0;
    std::uint64_t branch_shadow_rename_ready_cycle_ = 0;

    // Architectural/retired PMU state is deliberately separate from the
    // delayed timing-walker state.  A younger committed access can be an
    // architectural hit while it still queues behind an outstanding gem5
    // page walk.
    std::unordered_map<std::uint32_t, std::uint64_t>
        architectural_dtlb_lru_;
    std::uint64_t architectural_dtlb_sequence_ = 0;
    std::unordered_map<std::uint32_t, std::uint64_t> dtlb_lru_;
    std::unordered_map<std::uint32_t, std::uint64_t> pending_page_walks_;
    using PageWalk = std::pair<std::uint64_t, std::uint32_t>;
    std::priority_queue<PageWalk, std::vector<PageWalk>,
                        std::greater<PageWalk>> page_walk_completions_;
    std::vector<std::uint64_t> page_walker_ready_;
    std::uint64_t dtlb_sequence_ = 0;
};

}  // namespace fastsim
