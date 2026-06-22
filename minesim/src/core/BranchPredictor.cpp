#include "core/BranchPredictor.h"
#include <algorithm>
#include <iostream>
#include <iomanip>
#include <cmath>
#include <vector>

namespace minesim {

std::unique_ptr<BranchPredictor> BranchPredictor::create(const MicroArchConfig& config) {
    if (config.bp_type == "one_bit") {
        return std::make_unique<OneBitBranchPredictor>(config.bp_size);
    } else if (config.bp_type == "bimodal") {
        return std::make_unique<BimodalBranchPredictor>(config.bp_size);
    } else if (config.bp_type == "gshare") {
        // bp_size is in entries, default GHR width is log2(size). The history
        // width is capped at 14 bits (typical of TAGE/GShare implementations).
        uint32_t bits = config.bp_history_bits;
        if (bits == 0) {
            uint32_t s = config.bp_size > 1 ? config.bp_size : 2;
            while ((1u << bits) < s && bits < 31) bits++;
            if (bits > 14) bits = 14;
        }
        return std::make_unique<GShareBranchPredictor>(config.bp_size, bits);
    } else if (config.bp_type == "hybrid" || config.bp_type == "pentium_m_like") {
        uint32_t bits = config.bp_history_bits;
        if (bits == 0) {
            uint32_t s = config.bp_size > 1 ? config.bp_size : 2;
            while ((1u << bits) < s && bits < 31) bits++;
            if (bits > 15) bits = 15;
        }
        return std::make_unique<HybridBranchPredictor>(config.bp_size, bits);
    } else if (config.bp_type == "perfect" || config.bp_type == "none") {
        return std::make_unique<PerfectBranchPredictor>();
    } else {
        std::cerr << "Warning: Unknown branch predictor type '" << config.bp_type 
                  << "'. Falling back to perfect predictor.\n";
        return std::make_unique<PerfectBranchPredictor>();
    }
}

void BranchPredictor::reset_stats() {
    stats_ = Stats{};
}

void BranchPredictor::print_stats() const {
    uint64_t total_pred = stats_.cond_predictions + stats_.direct_predictions +
                          stats_.indirect_predictions + stats_.return_predictions;
    uint64_t total_mis = total_mispredictions();
    double accuracy = total_pred > 0 ? (double)(total_pred - total_mis) / total_pred * 100.0 : 0.0;

    std::cout << "Predictions: " << total_pred
              << " | Mispredicts (total): " << total_mis
              << " | Accuracy: " << std::fixed << std::setprecision(2) << accuracy << "%\n";
    std::cout << "  Cond:     pred=" << stats_.cond_predictions
              << " mispredict=" << stats_.cond_mispredictions << "\n";
    std::cout << "  Direct:   pred=" << stats_.direct_predictions
              << " mispredict=" << stats_.direct_mispredictions << "\n";
    std::cout << "  Indirect: pred=" << stats_.indirect_predictions
              << " mispredict=" << stats_.indirect_mispredictions << "\n";
    std::cout << "  Return:   pred=" << stats_.return_predictions
              << " mispredict=" << stats_.return_mispredictions << "\n";
}

bool BranchPredictor::predict_direct(Addr pc, Addr actual_target) {
    stats_.direct_predictions++;
    size_t set = static_cast<size_t>(pc >> 2) % kDirectBTBSets;
    size_t set_base = set * kDirectBTBWays;

    BTBEntry* victim = &direct_btb_[set_base];
    for (size_t way = 0; way < kDirectBTBWays; ++way) {
        BTBEntry& entry = direct_btb_[set_base + way];
        if (entry.valid && entry.tag == pc) {
            bool correct = entry.target == actual_target;
            if (!correct) {
                stats_.direct_mispredictions++;
            }
            entry.valid = true;
            entry.tag = pc;
            entry.target = actual_target;
            entry.last_used = btb_use_clock_++;
            return correct;
        }
        if (!entry.valid || entry.last_used < victim->last_used) {
            victim = &entry;
        }
    }

    stats_.direct_mispredictions++;
    victim->valid = true;
    victim->tag = pc;
    victim->target = actual_target;
    victim->last_used = btb_use_clock_++;
    return false;
}

bool BranchPredictor::predict_indirect(Addr pc, Addr actual_target) {
    stats_.indirect_predictions++;
    size_t idx = static_cast<size_t>(pc >> 2) % kIndirectBTBSize;
    BTBEntry& e = indirect_btb_[idx];
    bool correct = e.valid && e.tag == pc && e.target == actual_target;
    if (!correct) {
        stats_.indirect_mispredictions++;
    }
    e.valid = true;
    e.tag = pc;
    e.target = actual_target;
    e.last_used = btb_use_clock_++;
    return correct;
}

bool BranchPredictor::predict_return(Addr actual_target) {
    stats_.return_predictions++;
    bool correct = false;
    if (!rsb_.empty()) {
        Addr top = rsb_.back();
        rsb_.pop_back();
        correct = (top == actual_target);
    }
    if (!correct) {
        stats_.return_mispredictions++;
    }
    return correct;
}

void BranchPredictor::push_call(Addr return_target) {
    if (rsb_.size() >= kRSBSize) {
        // Drop the oldest entry (RSB is bounded; in real HW it wraps).
        rsb_.erase(rsb_.begin());
    }
    rsb_.push_back(return_target);
}

// ----------------- OneBitBranchPredictor -----------------

OneBitBranchPredictor::OneBitBranchPredictor(uint32_t size) : table_(size, false) {
    std::cout << "Initialized OneBitBranchPredictor | Entries: " << size << "\n";
}

bool OneBitBranchPredictor::predict(Addr pc) {
    stats_.predictions++;
    stats_.cond_predictions++;
    uint32_t index = (pc >> 2) % table_.size();
    return table_[index];
}

void OneBitBranchPredictor::update(Addr pc, bool actual_taken) {
    uint32_t index = (pc >> 2) % table_.size();
    bool predicted = table_[index];

    if (predicted == actual_taken) {
        stats_.correct_predictions++;
    } else {
        stats_.mispredictions++;
        stats_.cond_mispredictions++;
    }

    table_[index] = actual_taken;
}

// ----------------- BimodalBranchPredictor -----------------

BimodalBranchPredictor::BimodalBranchPredictor(uint32_t size) : table_(size, 1) {
    std::cout << "Initialized BimodalBranchPredictor (2-bit) | Entries: " << size << "\n";
}

bool BimodalBranchPredictor::predict(Addr pc) {
    stats_.predictions++;
    stats_.cond_predictions++;
    uint32_t index = (pc >> 2) % table_.size();
    return table_[index] >= 2;
}

void BimodalBranchPredictor::update(Addr pc, bool actual_taken) {
    uint32_t index = (pc >> 2) % table_.size();
    bool predicted = (table_[index] >= 2);

    if (predicted == actual_taken) {
        stats_.correct_predictions++;
    } else {
        stats_.mispredictions++;
        stats_.cond_mispredictions++;
    }

    if (actual_taken) {
        if (table_[index] < 3) table_[index]++;
    } else {
        if (table_[index] > 0) table_[index]--;
    }
}

// ----------------- GShareBranchPredictor -----------------

static uint32_t round_up_pow2(uint32_t v) {
    if (v < 2) return 2;
    uint32_t r = 1;
    while (r < v) r <<= 1;
    return r;
}

GShareBranchPredictor::GShareBranchPredictor(uint32_t size, uint32_t history_bits) {
    uint32_t pow2_size = round_up_pow2(size);
    table_.assign(pow2_size, 1);
    table_mask_ = pow2_size - 1;
    history_bits_ = history_bits;
    if (history_bits_ > 31) history_bits_ = 31;
    ghr_mask_ = (history_bits_ == 0) ? 0 : ((1ULL << history_bits_) - 1);
    ghr_ = 0;
    std::cout << "Initialized GShareBranchPredictor (2-bit) | Entries: " << pow2_size
              << " | GHR bits: " << history_bits_ << "\n";
}

uint32_t GShareBranchPredictor::index_for(Addr pc) const {
    uint64_t pc_shift = static_cast<uint64_t>(pc >> 2);
    return static_cast<uint32_t>((pc_shift ^ ghr_) & table_mask_);
}

bool GShareBranchPredictor::predict(Addr pc) {
    stats_.predictions++;
    stats_.cond_predictions++;
    return table_[index_for(pc)] >= 2;
}

void GShareBranchPredictor::update(Addr pc, bool actual_taken) {
    uint32_t index = index_for(pc);
    bool predicted = (table_[index] >= 2);

    if (predicted == actual_taken) {
        stats_.correct_predictions++;
    } else {
        stats_.mispredictions++;
        stats_.cond_mispredictions++;
    }

    if (actual_taken) {
        if (table_[index] < 3) table_[index]++;
    } else {
        if (table_[index] > 0) table_[index]--;
    }

    // Shift GHR after update so the next branch sees the resolved direction.
    if (history_bits_ > 0) {
        ghr_ = ((ghr_ << 1) | (actual_taken ? 1ULL : 0ULL)) & ghr_mask_;
    }
}

// ----------------- HybridBranchPredictor -----------------

HybridBranchPredictor::HybridBranchPredictor(uint32_t size, uint32_t history_bits) {
    uint32_t pow2_size = round_up_pow2(size);
    bimodal_table_.assign(pow2_size, 1);
    bimodal_mask_ = pow2_size - 1;
    gshare_table_.assign(pow2_size, 1);
    gshare_mask_ = pow2_size - 1;
    chooser_table_.assign(pow2_size, 1);  // start weakly favoring bimodal

    uint32_t global_entries = round_up_pow2(size < kGlobalWays ? kGlobalWays : size);
    global_sets_ = global_entries / kGlobalWays;
    if (global_sets_ < 1) global_sets_ = 1;
    global_sets_ = round_up_pow2(global_sets_);
    global_mask_ = global_sets_ - 1;
    global_table_.assign(static_cast<size_t>(global_sets_) * kGlobalWays, GlobalEntry{});

    history_bits_ = history_bits;
    if (history_bits_ > 31) history_bits_ = 31;
    ghr_mask_ = (history_bits_ == 0) ? 0 : ((1ULL << history_bits_) - 1);
    ghr_ = 0;

    loop_table_.assign(kLoopSets * kLoopWays, LoopEntry{});

    std::cout << "Initialized HybridBranchPredictor | GShare fallback entries: " << pow2_size
              << " | Bimodal entries: " << pow2_size
              << " | Tagged global entries: " << global_table_.size()
              << " | Global ways: " << kGlobalWays
              << " | GHR bits: " << history_bits_
              << " | Loop entries: " << loop_table_.size() << "\n";
}

uint32_t HybridBranchPredictor::global_index(Addr pc) const {
    uint64_t pc_shift = static_cast<uint64_t>(pc >> 4);
    uint64_t folded = ghr_ ^ (ghr_ >> 7) ^ (pc_shift >> 3);
    return static_cast<uint32_t>((pc_shift ^ folded) & global_mask_);
}

uint16_t HybridBranchPredictor::global_tag(Addr pc) const {
    uint64_t pc_shift = static_cast<uint64_t>(pc >> 2);
    uint64_t folded = ghr_ ^ (ghr_ >> 11);
    return static_cast<uint16_t>((pc_shift ^ (pc_shift >> 13) ^ folded) & 0xffff);
}

bool HybridBranchPredictor::global_lookup(Addr pc, bool& prediction, size_t& way_index) const {
    uint32_t index = global_index(pc);
    uint16_t tag = global_tag(pc);
    size_t base = static_cast<size_t>(index) * kGlobalWays;
    for (size_t way = 0; way < kGlobalWays; ++way) {
        const GlobalEntry& entry = global_table_[base + way];
        if (entry.valid && entry.tag == tag) {
            prediction = entry.counter >= 2;
            way_index = base + way;
            return true;
        }
    }
    return false;
}

void HybridBranchPredictor::global_update(Addr pc, bool actual_taken, bool allocate) {
    bool prediction = false;
    size_t hit_index = 0;
    if (global_lookup(pc, prediction, hit_index)) {
        GlobalEntry& entry = global_table_[hit_index];
        if (actual_taken) {
            if (entry.counter < 3) entry.counter++;
        } else {
            if (entry.counter > 0) entry.counter--;
        }
        entry.last_used = global_use_clock_++;
        return;
    }

    if (!allocate) return;

    uint32_t index = global_index(pc);
    uint16_t tag = global_tag(pc);
    size_t base = static_cast<size_t>(index) * kGlobalWays;
    size_t victim = base;
    for (size_t way = 0; way < kGlobalWays; ++way) {
        GlobalEntry& entry = global_table_[base + way];
        if (!entry.valid) {
            victim = base + way;
            break;
        }
        if (entry.last_used < global_table_[victim].last_used) {
            victim = base + way;
        }
    }

    GlobalEntry& entry = global_table_[victim];
    entry.valid = true;
    entry.tag = tag;
    entry.counter = actual_taken ? 2 : 1;
    entry.last_used = global_use_clock_++;
}

void HybridBranchPredictor::global_evict(Addr pc) {
    bool prediction = false;
    size_t hit_index = 0;
    if (global_lookup(pc, prediction, hit_index)) {
        global_table_[hit_index].valid = false;
    }
}

bool HybridBranchPredictor::loop_lookup(Addr pc, bool& prediction, size_t& way_index) const {
    size_t set = static_cast<size_t>(pc >> 4) % kLoopSets;
    size_t base = set * kLoopWays;
    for (size_t way = 0; way < kLoopWays; ++way) {
        const LoopEntry& entry = loop_table_[base + way];
        if (!entry.valid || entry.tag != pc || entry.confidence < 3 ||
            entry.stable_length < 2) {
            continue;
        }

        // Predict the direction flip at the learned run boundary.
        bool next = entry.last_actual;
        if (entry.run_length + 1 >= entry.stable_length) {
            next = !entry.last_actual;
        }
        prediction = next;
        way_index = base + way;
        return true;
    }
    return false;
}

void HybridBranchPredictor::loop_update(Addr pc, bool actual_taken) {
    size_t set = static_cast<size_t>(pc >> 4) % kLoopSets;
    size_t base = set * kLoopWays;
    size_t victim = base;

    for (size_t way = 0; way < kLoopWays; ++way) {
        LoopEntry& entry = loop_table_[base + way];
        if (entry.valid && entry.tag == pc) {
            if (entry.run_length == 0) {
                entry.last_actual = actual_taken;
                entry.run_length = 1;
            } else if (entry.last_actual == actual_taken) {
                if (entry.run_length < UINT32_MAX) entry.run_length++;
            } else {
                if (entry.stable_length == entry.run_length) {
                    if (entry.confidence < 3) entry.confidence++;
                } else {
                    entry.stable_length = entry.run_length;
                    if (entry.confidence > 0) entry.confidence--;
                }
                entry.last_actual = actual_taken;
                entry.run_length = 1;
            }
            entry.last_used = loop_use_clock_++;
            return;
        }
        if (!entry.valid || entry.last_used < loop_table_[victim].last_used) {
            victim = base + way;
        }
    }

    LoopEntry& entry = loop_table_[victim];
    entry.valid = true;
    entry.tag = pc;
    entry.run_length = 1;
    entry.stable_length = 0;
    entry.confidence = 0;
    entry.last_actual = actual_taken;
    entry.last_used = loop_use_clock_++;
}

uint32_t HybridBranchPredictor::bimodal_index(Addr pc) const {
    return static_cast<uint32_t>((pc >> 2) & bimodal_mask_);
}

bool HybridBranchPredictor::bimodal_predict(Addr pc) const {
    return bimodal_table_[bimodal_index(pc)] >= 2;
}

void HybridBranchPredictor::bimodal_update(Addr pc, bool actual_taken) {
    uint8_t& counter = bimodal_table_[bimodal_index(pc)];
    if (actual_taken) {
        if (counter < 3) counter++;
    } else {
        if (counter > 0) counter--;
    }
}

uint32_t HybridBranchPredictor::gshare_index(Addr pc) const {
    uint64_t pc_shift = static_cast<uint64_t>(pc >> 2);
    return static_cast<uint32_t>((pc_shift ^ ghr_) & gshare_mask_);
}

bool HybridBranchPredictor::gshare_predict(Addr pc) const {
    return gshare_table_[gshare_index(pc)] >= 2;
}

void HybridBranchPredictor::gshare_update(Addr pc, bool actual_taken) {
    uint8_t& counter = gshare_table_[gshare_index(pc)];
    if (actual_taken) {
        if (counter < 3) counter++;
    } else {
        if (counter > 0) counter--;
    }
}

void HybridBranchPredictor::update_history(bool actual_taken) {
    if (history_bits_ > 0) {
        ghr_ = ((ghr_ << 1) | (actual_taken ? 1ULL : 0ULL)) & ghr_mask_;
    }
}

void HybridBranchPredictor::print_stats() const {
    BranchPredictor::print_stats();

    uint64_t total_cond = istats_.global_hits + istats_.global_misses;
    double global_hit_pct = total_cond > 0 ? (double)istats_.global_hits / total_cond * 100.0 : 0.0;
    double loop_hit_pct = total_cond > 0 ? (double)istats_.loop_hits / total_cond * 100.0 : 0.0;

    std::cout << "  [Hybrid Internals]\n";
    std::cout << "    Global:     hit=" << istats_.global_hits
              << " miss=" << istats_.global_misses
              << " (" << std::fixed << std::setprecision(1) << global_hit_pct << "%)\n";
    std::cout << "    Loop:       hit=" << istats_.loop_hits
              << " miss=" << istats_.loop_misses
              << " (" << std::fixed << std::setprecision(1) << loop_hit_pct << "%)\n";
    std::cout << "    Chooser:    gshare=" << istats_.chooser_use_gshare
              << " bimodal=" << istats_.chooser_use_bimodal << "\n";
    std::cout << "    CorrectOnly: bimodal=" << istats_.bimodal_correct_only
              << " gshare=" << istats_.gshare_correct_only
              << " both_wrong=" << istats_.both_wrong << "\n";
    std::cout << "    Override:   global_loop=" << istats_.global_loop_override << "\n";
    std::cout << "    GlobalGating: correct_only=" << istats_.global_correct_only
              << " wrong_but_chooser_ok=" << istats_.global_wrong_but_chooser_correct
              << " differs_from_chooser=" << istats_.global_hit_differs_from_chooser
              << " differs_and_correct=" << istats_.global_hit_differs_and_global_correct << "\n";
    std::cout << "    LoopQuality: hit_and_correct=" << istats_.loop_hit_and_correct
              << " correct_only=" << istats_.loop_correct_only << "\n";
    std::cout << "    FinalSource: chooser_bimodal=" << istats_.final_from_chooser_bimodal
              << " chooser_gshare=" << istats_.final_from_chooser_gshare
              << " global_loop_override=" << istats_.final_from_global_loop_override << "\n";

    // Top-20 both_wrong PCs.
    if (!both_wrong_per_pc_.empty()) {
        std::vector<std::pair<Addr, uint64_t>> ranked(both_wrong_per_pc_.begin(), both_wrong_per_pc_.end());
        std::sort(ranked.begin(), ranked.end(),
                  [](const auto& a, const auto& b) { return a.second > b.second; });
        size_t top_n = std::min(size_t(20), ranked.size());
        std::cout << "    BothWrongTop" << top_n << ":";
        for (size_t i = 0; i < top_n; ++i) {
            uint64_t total = 0;
            auto it = cond_pred_per_pc_.find(ranked[i].first);
            if (it != cond_pred_per_pc_.end()) total = it->second;
            double pct = total > 0 ? (double)ranked[i].second / total * 100.0 : 0.0;
            std::cout << " " << std::hex << ranked[i].first << std::dec
                      << "(" << ranked[i].second << "/" << total << "="
                      << std::fixed << std::setprecision(1) << pct << "%)";
        }
        std::cout << "\n";
    }

    // Per-PC pattern details for top-6 both_wrong PCs.
    if (!both_wrong_per_pc_.empty()) {
        std::vector<std::pair<Addr, uint64_t>> ranked(both_wrong_per_pc_.begin(), both_wrong_per_pc_.end());
        std::sort(ranked.begin(), ranked.end(),
                  [](const auto& a, const auto& b) { return a.second > b.second; });
        size_t top_n = std::min(size_t(6), ranked.size());
        for (size_t i = 0; i < top_n; ++i) {
            Addr pc = ranked[i].first;
            auto it = pc_patterns_.find(pc);
            if (it == pc_patterns_.end()) continue;
            const auto& p = it->second;
            uint64_t dyn = p.dynamic_count;
            double taken_pct = dyn > 0 ? (double)p.taken_count / dyn * 100.0 : 0.0;
            double flip_pct = (dyn > 1) ? (double)p.flip_count / (dyn - 1) * 100.0 : 0.0;
            double bim_acc = dyn > 0 ? (double)p.bimodal_correct / dyn * 100.0 : 0.0;
            double gs_acc = dyn > 0 ? (double)p.gshare_correct / dyn * 100.0 : 0.0;
            double l1_acc = dyn > 0 ? (double)p.local_1bit_correct / dyn * 100.0 : 0.0;
            double l2_acc = dyn > 0 ? (double)p.local_2bit_correct / dyn * 100.0 : 0.0;
            double gl_hit_pct = dyn > 0 ? (double)p.global_hit_count / dyn * 100.0 : 0.0;
            double gl_acc = p.global_hit_count > 0 ? (double)p.global_correct / p.global_hit_count * 100.0 : 0.0;
            double gl_diff_acc = p.global_hit_differs_chooser > 0 ? (double)p.global_hit_differs_correct / p.global_hit_differs_chooser * 100.0 : 0.0;
            std::cout << "    PCPattern " << std::hex << pc << std::dec
                      << ": dyn=" << dyn
                      << " both_wrong=" << p.both_wrong_count
                      << " taken=" << std::fixed << std::setprecision(1) << taken_pct << "%"
                      << " flip=" << flip_pct << "%"
                      << " TT=" << p.tt << " TN=" << p.tn << " NT=" << p.nt << " NN=" << p.nn
                      << " maxRunT=" << p.max_run_t << " maxRunN=" << p.max_run_n
                      << " bimAcc=" << std::setprecision(1) << bim_acc << "%"
                      << " gsAcc=" << gs_acc << "%"
                      << " loc1b=" << l1_acc << "%"
                      << " loc2b=" << l2_acc << "%"
                      << " glHit=" << gl_hit_pct << "%"
                      << " glAcc=" << gl_acc << "%"
                      << " glDiff=" << p.global_hit_differs_chooser
                      << " glDiffAcc=" << gl_diff_acc << "%\n";
        }
    }
}

bool HybridBranchPredictor::predict(Addr pc) {
    stats_.predictions++;
    stats_.cond_predictions++;

    bool global_prediction = false;
    bool loop_prediction = false;
    size_t unused = 0;
    last_global_hit_ = global_lookup(pc, global_prediction, unused);
    last_loop_hit_ = loop_lookup(pc, loop_prediction, unused);
    last_bimodal_pred_ = bimodal_predict(pc);
    last_gshare_pred_ = gshare_predict(pc);

    if (last_global_hit_) istats_.global_hits++; else istats_.global_misses++;
    if (last_loop_hit_) istats_.loop_hits++; else istats_.loop_misses++;
    cond_pred_per_pc_[pc]++;

    last_global_pred_ = global_prediction;
    last_loop_pred_ = loop_prediction;

    // Chooser table: 2-bit counter per entry, >=2 picks gshare, <2 picks bimodal.
    // This lets the predictor dynamically adapt to branches that are better
    // served by bimodal (random/data-dependent) vs gshare (correlated).
    uint32_t chooser_idx = static_cast<uint32_t>((pc >> 2) & gshare_mask_);
    bool use_gshare = chooser_table_[chooser_idx] >= 2;
    last_use_gshare_ = use_gshare;
    if (use_gshare) istats_.chooser_use_gshare++; else istats_.chooser_use_bimodal++;
    bool chooser_pred = use_gshare ? last_gshare_pred_ : last_bimodal_pred_;
    last_chooser_pred_ = chooser_pred;

    last_global_differs_from_chooser_ = last_global_hit_ && (global_prediction != chooser_pred);
    if (last_global_differs_from_chooser_) {
        istats_.global_hit_differs_from_chooser++;
    }

    // Tagged global + loop agreement still overrides when both hit and agree.
    if (last_global_hit_ && last_loop_hit_ &&
        global_prediction == loop_prediction &&
        global_prediction != chooser_pred) {
        last_prediction_ = global_prediction;
        istats_.global_loop_override++;
        istats_.final_from_global_loop_override++;
    } else {
        last_prediction_ = chooser_pred;
        if (use_gshare) {
            istats_.final_from_chooser_gshare++;
        } else {
            istats_.final_from_chooser_bimodal++;
        }
    }
    return last_prediction_;
}

void HybridBranchPredictor::update(Addr pc, bool actual_taken) {
    if (last_prediction_ == actual_taken) {
        stats_.correct_predictions++;
    } else {
        stats_.mispredictions++;
        stats_.cond_mispredictions++;
    }

    loop_update(pc, actual_taken);

    // Train chooser table: if bimodal was correct and gshare was wrong, move
    // toward bimodal (decrement). If gshare was correct and bimodal was wrong,
    // move toward gshare (increment). If both correct or both wrong, no change.
    {
        uint32_t chooser_idx = static_cast<uint32_t>((pc >> 2) & gshare_mask_);
        uint8_t& chooser = chooser_table_[chooser_idx];
        bool bim_ok = last_bimodal_pred_ == actual_taken;
        bool gs_ok = last_gshare_pred_ == actual_taken;
        if (bim_ok && !gs_ok) {
            istats_.bimodal_correct_only++;
            if (chooser > 0) chooser--;
        } else if (!bim_ok && gs_ok) {
            istats_.gshare_correct_only++;
            if (chooser < 3) chooser++;
        } else if (!bim_ok && !gs_ok) {
            istats_.both_wrong++;
            both_wrong_per_pc_[pc]++;
        }
    }

    // Per-PC pattern analysis (observation-only).
    {
        auto& pat = pc_patterns_[pc];
        pat.dynamic_count++;
        if (actual_taken) pat.taken_count++;
        bool bim_ok = last_bimodal_pred_ == actual_taken;
        bool gs_ok = last_gshare_pred_ == actual_taken;
        if (bim_ok) pat.bimodal_correct++;
        if (gs_ok) pat.gshare_correct++;
        if (!bim_ok && !gs_ok) pat.both_wrong_count++;

        if (pat.has_last) {
            if (pat.last_actual != actual_taken) pat.flip_count++;
            if (pat.last_actual && actual_taken) pat.tt++;
            else if (pat.last_actual && !actual_taken) pat.tn++;
            else if (!pat.last_actual && actual_taken) pat.nt++;
            else pat.nn++;

            // local-1bit oracle: predict last outcome
            if (pat.last_actual == actual_taken) pat.local_1bit_correct++;
        }
        // local-2bit oracle: 2-bit saturating counter per PC
        bool local_2bit_pred = pat.local_2bit_counter >= 2;
        if (local_2bit_pred == actual_taken) pat.local_2bit_correct++;
        if (actual_taken) {
            if (pat.local_2bit_counter < 3) pat.local_2bit_counter++;
        } else {
            if (pat.local_2bit_counter > 0) pat.local_2bit_counter--;
        }

        // run-length tracking
        if (actual_taken) {
            pat.cur_run_t++;
            if (pat.cur_run_n > pat.max_run_n) pat.max_run_n = pat.cur_run_n;
            pat.cur_run_n = 0;
        } else {
            pat.cur_run_n++;
            if (pat.cur_run_t > pat.max_run_t) pat.max_run_t = pat.cur_run_t;
            pat.cur_run_t = 0;
        }

        // Tagged global predictor per-PC stats.
        if (last_global_hit_) {
            pat.global_hit_count++;
            if (last_global_pred_ == actual_taken) pat.global_correct++;
            if (last_global_differs_from_chooser_) {
                pat.global_hit_differs_chooser++;
                if (last_global_pred_ == actual_taken) pat.global_hit_differs_correct++;
            }
        }

        pat.last_actual = actual_taken;
        pat.has_last = true;
    }

    // Global predictor gating analysis (observation-only).
    if (last_global_hit_) {
        bool global_ok = last_global_pred_ == actual_taken;
        bool final_ok = last_prediction_ == actual_taken;
        if (global_ok && !final_ok) {
            istats_.global_correct_only++;
        }
        if (!global_ok && last_chooser_pred_ == actual_taken) {
            istats_.global_wrong_but_chooser_correct++;
        }
        if (last_global_differs_from_chooser_ && global_ok) {
            istats_.global_hit_differs_and_global_correct++;
        }
    }
    if (last_loop_hit_) {
        bool loop_ok = last_loop_pred_ == actual_taken;
        if (loop_ok) {
            istats_.loop_hit_and_correct++;
            if (last_prediction_ != actual_taken) {
                istats_.loop_correct_only++;
            }
        }
    }

    // Keep the fallback table warm even when loop/global structures are used;
    // Sniper relies on bimodal as the stable base predictor for noisy branches.
    bimodal_update(pc, actual_taken);
    gshare_update(pc, actual_taken);

    const bool loop_correct = last_loop_hit_ && (last_prediction_ == actual_taken);
    const bool bimodal_correct = last_bimodal_pred_ == actual_taken;
    const bool gshare_correct = last_gshare_pred_ == actual_taken;
    const bool fallback_correct = loop_correct || gshare_correct || bimodal_correct;
    if (last_global_hit_) {
        if (last_prediction_ != actual_taken && fallback_correct) {
            global_evict(pc);
        } else {
            global_update(pc, actual_taken, false);
        }
    } else if (last_prediction_ != actual_taken) {
        global_update(pc, actual_taken, true);
    }

    update_history(actual_taken);
}

} // namespace minesim
