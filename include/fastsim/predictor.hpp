#pragma once

#include <cstdint>
#include <optional>
#include <unordered_map>
#include <vector>

#include "fastsim/config.hpp"
#include "fastsim/types.hpp"

namespace fastsim {

struct BranchPredictionResult {
    bool conditional_prediction = false;
    bool predicted_taken = false;
    std::uint64_t predicted_target = 0;
    bool target_available = false;
    bool direction_miss = false;
    bool target_miss = false;
    bool miss = false;
};

class BranchPredictor {
  public:
    explicit BranchPredictor(const BranchConfig& config);

    BranchPredictionResult process(const TraceRecord& record,
                                   BranchCounters& counters);

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
    void direction_commit(std::uint64_t pc, bool actual_taken,
                          const TournamentHistory& history);

    bool btb_lookup(std::uint64_t pc, std::uint64_t& target);
    void btb_update(std::uint64_t pc, std::uint64_t target);
    void update_btb_for_event(const TraceRecord& record);

    RasHistory ras_push(const RasFrame& frame);
    std::pair<RasFrame, RasHistory> ras_pop();
    void ras_squash(const RasHistory& history);

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
    std::unordered_map<std::uint64_t, std::uint64_t>
        learned_return_targets_;

    std::vector<IndirectEntry> indirect_cache_;
    std::vector<IndirectPathEntry> indirect_path_;
    std::uint32_t indirect_ghr_ = 0;
    GlibcRand indirect_random_;
    std::uint64_t sequence_ = 0;
};

}  // namespace fastsim

