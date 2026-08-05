#pragma once

#include <array>
#include <cstdint>
#include <functional>
#include <queue>
#include <unordered_map>
#include <utility>
#include <vector>

#include "fastsim/config.hpp"
#include "fastsim/types.hpp"

namespace fastsim {

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
    IntervalFuPool fu_pool = IntervalFuPool::kInteger;
    std::uint32_t fu_occupancy_cycles = 1;
    bool dtlb_access = false;
    bool dtlb_hit = false;
    bool dtlb_miss = false;
    bool dtlb_merged_miss = false;
    bool dtlb_untracked = false;
};

// A functional, lower-bound OoO window model. It consumes only operation
// classes and producer distances. Shared-cache/DRAM feedback is intentionally
// outside this class so an interval can be bound first and woven later.
class IntervalCoreModel {
  public:
    explicit IntervalCoreModel(const SimulatorConfig& config);

    IntervalTiming schedule(const TraceRecord& record, bool branch_miss);
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

    std::unordered_map<std::uint32_t, std::uint64_t> dtlb_lru_;
    std::unordered_map<std::uint32_t, std::uint64_t> pending_page_walks_;
    using PageWalk = std::pair<std::uint64_t, std::uint32_t>;
    std::priority_queue<PageWalk, std::vector<PageWalk>,
                        std::greater<PageWalk>> page_walk_completions_;
    std::vector<std::uint64_t> page_walker_ready_;
    std::uint64_t dtlb_sequence_ = 0;
};

}  // namespace fastsim
