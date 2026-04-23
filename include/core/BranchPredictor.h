#pragma once

#include "common/Types.h"
#include "core/Config.h"
#include <memory>
#include <vector>

namespace minesim {

class BranchPredictor {
public:
    BranchPredictor() = default;
    virtual ~BranchPredictor() = default;

    // Factory method
    static std::unique_ptr<BranchPredictor> create(const MicroArchConfig& config);

    // Predict if a branch is taken or not
    virtual bool predict(Addr pc) = 0;

    // Update the predictor with the actual outcome
    virtual void update(Addr pc, bool actual_taken) = 0;

    // Statistics
    struct Stats {
        uint64_t predictions = 0;
        uint64_t correct_predictions = 0;
        uint64_t mispredictions = 0;
    };
    
    const Stats& get_stats() const { return stats_; }
    virtual void print_stats() const;

protected:
    Stats stats_;
};

class OneBitBranchPredictor : public BranchPredictor {
public:
    OneBitBranchPredictor(uint32_t size);

    bool predict(Addr pc) override;
    void update(Addr pc, bool actual_taken) override;

private:
    std::vector<bool> table_;
};

class BimodalBranchPredictor : public BranchPredictor {
public:
    BimodalBranchPredictor(uint32_t size);

    bool predict(Addr pc) override;
    void update(Addr pc, bool actual_taken) override;

private:
    std::vector<uint8_t> table_; // 2-bit saturating counters (0-3)
};

class PerfectBranchPredictor : public BranchPredictor {
public:
    bool predict(Addr pc) override { return false; /* Actual doesn't matter for perfect, we just override logic later */ }
    void update(Addr pc, bool actual_taken) override {
        stats_.predictions++;
        stats_.correct_predictions++;
    }
};

} // namespace minesim
