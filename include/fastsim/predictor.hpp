#pragma once

#include <cstdint>
#include <deque>
#include <optional>
#include <unordered_map>
#include <vector>

#include "fastsim/config.hpp"
#include "fastsim/types.hpp"

namespace fastsim {

class TraceSource;

struct BranchPredictionResult {
    std::uint64_t sequence = 0;
    bool conditional_prediction = false;
    bool predicted_taken = false;
    std::uint64_t predicted_target = 0;
    bool target_available = false;
    bool direction_miss = false;
    bool target_miss = false;
    bool miss = false;
    // Exact PCs selected from the pre-repair predictor state. Populated only
    // when a static instruction map and a non-zero budget are supplied.
    std::vector<std::uint64_t> speculative_path;
};

class BranchPredictor {
  public:
    explicit BranchPredictor(const BranchConfig& config);

    BranchPredictionResult process(const TraceRecord& record,
                                   BranchCounters& counters,
                                   const TraceSource* trace_source = nullptr,
                                   std::uint64_t speculative_path_budget = 0);

    // Fetch/commit split for source-aligned speculative histories. The caller
    // attaches the modeled ordered-retire cycle after scheduling the branch
    // and advances pending table updates before each later Fetch lookup.
    BranchPredictionResult predict_speculative(
        const TraceRecord& record, BranchCounters& counters,
        const TraceSource* trace_source = nullptr,
        std::uint64_t speculative_path_budget = 0);
    void advance_to(std::uint64_t fetch_cycle);
    void schedule_commit(std::uint64_t sequence,
                         std::uint64_t retire_cycle);
    void drain();
    std::size_t pending_commits() const { return pending_commits_.size(); }

  private:
    struct TournamentHistory {
        std::uint64_t global_history = 0;
        std::uint32_t global_index = 0;
        std::uint32_t local_history_index = 0;
        std::uint32_t local_history = 0;
        bool local_prediction = false;
        bool global_prediction = false;
        bool global_used = false;
        bool local_valid = false;
    };

    struct BtbEntry {
        std::uint64_t tag = 0;
        std::uint64_t target = 0;
        std::uint64_t last_touch = 0;
        bool valid = false;
    };

    struct RasFrame {
        std::uint64_t address_space_id = 0;
        std::uint64_t call_pc = 0;
        std::uint64_t return_target = 0;
        bool target_valid = false;
        bool valid = false;
    };

    struct RasHistory {
        bool pushed = false;
        bool popped = false;
        std::uint32_t old_tos = 0;
        RasFrame popped_frame;
    };

    struct IndirectPathEntry {
        std::uint64_t pc = 0;
        std::uint64_t target = 0;
        std::uint64_t sequence = 0;
    };

    struct IndirectEntry {
        std::uint32_t tag = 0;
        std::uint64_t target = 0;
        bool valid = false;
    };

    struct IndirectHistory {
        std::uint32_t ghr = 0;
        std::uint64_t pc = 0;
        std::uint32_t set = 0;
        std::uint32_t tag = 0;
        bool hit = false;
        bool was_indirect = false;
    };

    struct PendingCommit {
        std::uint64_t sequence = 0;
        std::uint64_t retire_cycle = 0;
        std::uint64_t pc = 0;
        std::uint64_t btb_target = 0;
        TournamentHistory tournament_history;
        bool actual_taken = false;
        bool retire_cycle_valid = false;
        bool update_btb = false;
        bool learn_return_target = false;
        std::uint64_t learned_address_space_id = 0;
        std::uint64_t learned_call_pc = 0;
        std::uint64_t learned_return_target = 0;
    };

    class GlibcRand {
      public:
        explicit GlibcRand(std::uint32_t seed = 1);
        std::uint32_t next();

      private:
        std::vector<std::uint32_t> state_;
        std::uint32_t position_ = 0;
    };

    static std::uint32_t bit_width(std::uint64_t value);
    static std::uint64_t mask_for_bits(std::uint32_t bits);
    static void update_counter(std::uint8_t& counter, bool taken,
                               std::uint32_t bits);
    static bool predicts_taken(std::uint8_t counter,
                               std::uint32_t bits);

    bool direction_lookup(std::uint64_t pc, TournamentHistory& history);
    bool direction_lookup_at_history(std::uint64_t pc,
                                     std::uint64_t global_history) const;
    void direction_update_histories(bool taken,
                                    const TournamentHistory& history);
    void direction_train(bool actual_taken,
                         const TournamentHistory& history);
    void direction_commit(std::uint64_t pc, bool actual_taken,
                          const TournamentHistory& history);

    BranchPredictionResult process_impl(
        const TraceRecord& record, BranchCounters& counters,
        const TraceSource* trace_source,
        std::uint64_t speculative_path_budget,
        bool defer_commit);

    void build_speculative_path(
        const TraceRecord& resolving_record,
        const BranchPredictionResult& result,
        const TraceSource& trace_source,
        std::uint64_t budget,
        std::vector<std::uint64_t>& path) const;

    bool btb_lookup(std::uint64_t pc, std::uint64_t& target);
    void btb_update(std::uint64_t pc, std::uint64_t target);
    void update_btb_for_event(const TraceRecord& record);

    RasHistory ras_push(const RasFrame& frame);
    std::pair<RasFrame, RasHistory> ras_pop();
    void ras_squash(const RasHistory& history);
    RasFrame ras_frame_for_call(const TraceRecord& record,
                                const TraceSource* trace_source,
                                BranchCounters& counters);

    std::uint32_t indirect_set(std::uint64_t pc) const;
    std::uint32_t indirect_tag(std::uint64_t pc) const;
    std::optional<std::uint64_t> indirect_lookup(
        std::uint64_t pc, IndirectHistory& history);
    void indirect_speculative_update(std::uint64_t sequence,
                                     bool predicted_taken,
                                     std::uint64_t predicted_target,
                                     bool indirect_no_return,
                                     IndirectHistory& history);
    void indirect_repair(std::uint64_t sequence, bool actual_taken,
                         std::uint64_t actual_target,
                         bool indirect_no_return,
                         IndirectHistory& history);
    void indirect_commit();
    void commit_pending(PendingCommit& pending);

    BranchConfig config_;

    std::vector<std::uint8_t> local_counters_;
    std::vector<std::uint8_t> global_counters_;
    std::vector<std::uint8_t> choice_counters_;
    std::vector<std::uint32_t> local_history_table_;
    std::uint64_t global_history_ = 0;
    std::uint32_t local_history_mask_ = 0;
    std::uint64_t global_history_mask_ = 0;

    std::vector<BtbEntry> btb_;
    std::uint32_t btb_sets_ = 0;
    std::uint32_t btb_tag_shift_ = 0;
    std::uint64_t btb_tag_mask_ = 0;
    std::uint64_t btb_clock_ = 0;

    std::vector<RasFrame> ras_entries_;
    std::uint32_t ras_used_ = 0;
    std::uint32_t ras_tos_ = 0;
    struct RasCallSite {
        std::uint64_t address_space_id = 0;
        std::uint64_t pc = 0;

        bool operator==(const RasCallSite& other) const {
            return address_space_id == other.address_space_id &&
                pc == other.pc;
        }
    };

    struct RasCallSiteHash {
        std::size_t operator()(const RasCallSite& site) const {
            const auto mixed = site.address_space_id ^
                (site.pc + 0x9e3779b97f4a7c15ull +
                 (site.address_space_id << 6) +
                 (site.address_space_id >> 2));
            return static_cast<std::size_t>(mixed);
        }
    };

    std::unordered_map<RasCallSite, std::uint64_t, RasCallSiteHash>
        learned_return_targets_;

    std::vector<IndirectEntry> indirect_cache_;
    std::vector<IndirectPathEntry> indirect_path_;
    std::uint32_t indirect_ghr_ = 0;
    GlibcRand indirect_random_;
    std::uint64_t sequence_ = 0;
    std::deque<PendingCommit> pending_commits_;
    std::uint64_t last_advance_cycle_ = 0;
    bool advanced_ = false;
};

}  // namespace fastsim
