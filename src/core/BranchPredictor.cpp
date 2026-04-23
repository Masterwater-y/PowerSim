#include "core/BranchPredictor.h"
#include <iostream>
#include <iomanip>
#include <cmath>

namespace minesim {

std::unique_ptr<BranchPredictor> BranchPredictor::create(const MicroArchConfig& config) {
    if (config.bp_type == "one_bit") {
        return std::make_unique<OneBitBranchPredictor>(config.bp_size);
    } else if (config.bp_type == "bimodal") {
        return std::make_unique<BimodalBranchPredictor>(config.bp_size);
    } else if (config.bp_type == "perfect" || config.bp_type == "none") {
        return std::make_unique<PerfectBranchPredictor>();
    } else {
        std::cerr << "Warning: Unknown branch predictor type '" << config.bp_type 
                  << "'. Falling back to perfect predictor.\n";
        return std::make_unique<PerfectBranchPredictor>();
    }
}

void BranchPredictor::print_stats() const {
    double accuracy = stats_.predictions > 0 ? (double)stats_.correct_predictions / stats_.predictions * 100.0 : 0.0;
    std::cout << "Predictions: " << stats_.predictions 
              << " | Correct: " << stats_.correct_predictions
              << " | Mispredicts: " << stats_.mispredictions
              << " | Accuracy: " << std::fixed << std::setprecision(2) << accuracy << "%\n";
}

// ----------------- OneBitBranchPredictor -----------------

OneBitBranchPredictor::OneBitBranchPredictor(uint32_t size) : table_(size, false) {
    std::cout << "Initialized OneBitBranchPredictor | Entries: " << size << "\n";
}

bool OneBitBranchPredictor::predict(Addr pc) {
    stats_.predictions++;
    uint32_t index = (pc >> 2) % table_.size(); // Ignore lower bits since instructions are multi-byte
    return table_[index];
}

void OneBitBranchPredictor::update(Addr pc, bool actual_taken) {
    uint32_t index = (pc >> 2) % table_.size();
    bool predicted = table_[index];

    if (predicted == actual_taken) {
        stats_.correct_predictions++;
    } else {
        stats_.mispredictions++;
    }

    table_[index] = actual_taken;
}

// ----------------- BimodalBranchPredictor -----------------

BimodalBranchPredictor::BimodalBranchPredictor(uint32_t size) : table_(size, 1) { // Initialize to Weakly Not Taken (1)
    std::cout << "Initialized BimodalBranchPredictor (2-bit) | Entries: " << size << "\n";
}

bool BimodalBranchPredictor::predict(Addr pc) {
    stats_.predictions++;
    uint32_t index = (pc >> 2) % table_.size();
    
    // 2-bit counter:
    // 0 = Strongly Not Taken
    // 1 = Weakly Not Taken
    // 2 = Weakly Taken
    // 3 = Strongly Taken
    return table_[index] >= 2;
}

void BimodalBranchPredictor::update(Addr pc, bool actual_taken) {
    uint32_t index = (pc >> 2) % table_.size();
    bool predicted = (table_[index] >= 2);

    if (predicted == actual_taken) {
        stats_.correct_predictions++;
    } else {
        stats_.mispredictions++;
    }

    // Update saturating counter
    if (actual_taken) {
        if (table_[index] < 3) {
            table_[index]++;
        }
    } else {
        if (table_[index] > 0) {
            table_[index]--;
        }
    }
}

} // namespace minesim
