#include "fastsim/predictor.hpp"

#include <algorithm>
#include <limits>
#include <stdexcept>

#include "fastsim/trace.hpp"

namespace fastsim {

BranchPredictor::GlibcRand::GlibcRand(std::uint32_t seed) {
    seed &= 0x7fffffffu;
    if (seed == 0) seed = 1;
    std::vector<std::uint32_t> state;
    state.reserve(344);
    state.push_back(seed);
    for (std::uint32_t index = 1; index < 31; ++index) {
        state.push_back(static_cast<std::uint32_t>(
            (16807ull * state[index - 1]) % 2147483647ull));
    }
    for (std::uint32_t index = 31; index < 34; ++index) {
        state.push_back(state[index - 31]);
    }
    for (std::uint32_t index = 34; index < 344; ++index) {
        state.push_back(state[index - 31] + state[index - 3]);
    }
    state_.assign(state.end() - 31, state.end());
}

std::uint32_t BranchPredictor::GlibcRand::next() {
    const auto value =
        state_[position_] + state_[(position_ + 28) % 31];
    state_[position_] = value;
    position_ = (position_ + 1) % 31;
    return value >> 1;
}

std::uint32_t BranchPredictor::bit_width(std::uint64_t value) {
    std::uint32_t bits = 0;
    while (value != 0) {
        ++bits;
        value >>= 1;
    }
    return bits;
}

std::uint64_t BranchPredictor::mask_for_bits(std::uint32_t bits) {
    if (bits >= 64) return std::numeric_limits<std::uint64_t>::max();
    return bits == 0 ? 0 : (1ull << bits) - 1;
}

void BranchPredictor::update_counter(std::uint8_t& counter, bool taken,
                                     std::uint32_t bits) {
    const auto maximum =
        bits == 8 ? std::numeric_limits<std::uint8_t>::max()
                  : static_cast<std::uint8_t>((1u << bits) - 1);
    if (taken) {
        if (counter < maximum) ++counter;
    } else if (counter != 0) {
        --counter;
    }
}

bool BranchPredictor::predicts_taken(std::uint8_t counter,
                                     std::uint32_t bits) {
    const auto threshold =
        static_cast<std::uint8_t>((1u << (bits - 1)) - 1);
    return counter > threshold;
}

BranchPredictor::BranchPredictor(const BranchConfig& config)
    : config_(config),
      local_counters_(config.local_entries, 0),
      global_counters_(config.global_entries, 0),
      choice_counters_(config.choice_entries, 0),
      local_history_table_(config.local_history_entries, 0),
      local_history_mask_(static_cast<std::uint32_t>(
          mask_for_bits(bit_width(config.local_entries - 1)))),
      global_history_mask_(mask_for_bits(std::max(
          bit_width(config.global_entries - 1),
          bit_width(config.choice_entries - 1)))),
      btb_(config.btb_entries),
      btb_sets_(config.btb_entries / config.btb_associativity),
      btb_tag_shift_(config.btb_set_shift +
                     bit_width(btb_sets_ - 1)),
      btb_tag_mask_(mask_for_bits(config.btb_tag_bits)),
      ras_entries_(config.ras_entries),
      indirect_cache_(
          static_cast<std::size_t>(config.indirect_sets) *
          config.indirect_ways),
      indirect_random_(1) {
    if (config.type != "tournament" && config.type != "gshare") {
        throw std::invalid_argument("unsupported branch predictor: " +
                                    config.type);
    }
}

bool BranchPredictor::direction_lookup(
    std::uint64_t pc, TournamentHistory& history) {
    history.global_history = global_history_;
    if (config_.type == "gshare") {
        history.global_index = static_cast<std::uint32_t>(
            ((pc >> config_.inst_shift) ^ global_history_) &
            (config_.global_entries - 1));
        history.global_prediction = predicts_taken(
            global_counters_[history.global_index],
            config_.global_counter_bits);
        return history.global_prediction;
    }

    history.local_valid = true;
    history.local_history_index = static_cast<std::uint32_t>(
        (pc >> config_.inst_shift) &
        (config_.local_history_entries - 1));
    history.local_history =
        local_history_table_[history.local_history_index] &
        local_history_mask_;
    history.local_prediction = predicts_taken(
        local_counters_[history.local_history],
        config_.local_counter_bits);
    history.global_index = static_cast<std::uint32_t>(
        global_history_ & (config_.global_entries - 1));
    history.global_prediction = predicts_taken(
        global_counters_[history.global_index],
        config_.global_counter_bits);
    const auto choice_index = static_cast<std::uint32_t>(
        global_history_ & (config_.choice_entries - 1));
    history.global_used = predicts_taken(
        choice_counters_[choice_index], config_.choice_counter_bits);
    return history.global_used ? history.global_prediction
                               : history.local_prediction;
}

bool BranchPredictor::direction_lookup_at_history(
    std::uint64_t pc, std::uint64_t global_history) const {
    if (config_.type == "gshare") {
        const auto index = static_cast<std::uint32_t>(
            ((pc >> config_.inst_shift) ^ global_history) &
            (config_.global_entries - 1));
        return predicts_taken(
            global_counters_[index], config_.global_counter_bits);
    }

    const auto local_history_index = static_cast<std::uint32_t>(
        (pc >> config_.inst_shift) &
        (config_.local_history_entries - 1));
    const auto local_history =
        local_history_table_[local_history_index] & local_history_mask_;
    const auto local_prediction = predicts_taken(
        local_counters_[local_history], config_.local_counter_bits);
    const auto global_index = static_cast<std::uint32_t>(
        global_history & (config_.global_entries - 1));
    const auto global_prediction = predicts_taken(
        global_counters_[global_index], config_.global_counter_bits);
    const auto choice_index = static_cast<std::uint32_t>(
        global_history & (config_.choice_entries - 1));
    const auto global_used = predicts_taken(
        choice_counters_[choice_index], config_.choice_counter_bits);
    return global_used ? global_prediction : local_prediction;
}

void BranchPredictor::build_speculative_path(
    const TraceRecord& resolving_record,
    const BranchPredictionResult& result,
    const TraceSource& trace_source,
    std::uint64_t budget,
    std::vector<std::uint64_t>& path) const {
    if (!result.miss || budget == 0) return;

    std::uint64_t pc = 0;
    if (result.predicted_taken && result.target_available) {
        pc = result.predicted_target;
    } else if (!result.predicted_taken) {
        const auto* resolving =
            trace_source.static_instruction(resolving_record.pc);
        if (resolving == nullptr) return;
        pc = resolving->fallthrough_pc;
    } else {
        return;
    }

    auto speculative_global_history =
        ((global_history_ << 1) |
         static_cast<std::uint64_t>(result.predicted_taken)) &
        global_history_mask_;
    path.reserve(static_cast<std::size_t>(budget));
    for (std::uint64_t position = 0; position < budget; ++position) {
        path.push_back(pc);
        const auto* instruction = trace_source.static_instruction(pc);
        if (instruction == nullptr) break;
        if (!instruction->is_branch()) {
            pc = instruction->fallthrough_pc;
            continue;
        }
        if (instruction->is_indirect()) {
            // RAS and indirect-target snapshots need separate state cursors.
            // Stop without consulting the committed outcome.
            break;
        }

        bool predicted_taken = true;
        if (instruction->is_conditional()) {
            predicted_taken = direction_lookup_at_history(
                pc, speculative_global_history);
        }
        if (predicted_taken) {
            if (!instruction->has_direct_target()) break;
            pc = instruction->direct_target;
        } else {
            pc = instruction->fallthrough_pc;
        }
        speculative_global_history =
            ((speculative_global_history << 1) |
             static_cast<std::uint64_t>(predicted_taken)) &
            global_history_mask_;
    }
}

void BranchPredictor::direction_update_histories(
    bool taken, const TournamentHistory& history) {
    global_history_ =
        ((history.global_history << 1) |
         static_cast<std::uint64_t>(taken)) &
        global_history_mask_;
    if (history.local_valid) {
        local_history_table_[history.local_history_index] =
            static_cast<std::uint32_t>(
                ((static_cast<std::uint64_t>(history.local_history) << 1) |
                 static_cast<std::uint64_t>(taken)) &
                0xffffffffull);
    }
}

void BranchPredictor::direction_train(
    bool actual_taken, const TournamentHistory& history) {
    if (history.local_valid &&
        history.local_prediction != history.global_prediction) {
        const auto choice_index = static_cast<std::uint32_t>(
            history.global_history & (config_.choice_entries - 1));
        if (history.local_prediction == actual_taken) {
            update_counter(choice_counters_[choice_index], false,
                           config_.choice_counter_bits);
        } else if (history.global_prediction == actual_taken) {
            update_counter(choice_counters_[choice_index], true,
                           config_.choice_counter_bits);
        }
    }
    update_counter(global_counters_[history.global_index], actual_taken,
                   config_.global_counter_bits);
    if (history.local_valid) {
        update_counter(local_counters_[history.local_history], actual_taken,
                       config_.local_counter_bits);
    }
}

void BranchPredictor::direction_commit(
    std::uint64_t pc, bool actual_taken,
    const TournamentHistory& history) {
    direction_train(actual_taken, history);
    direction_update_histories(actual_taken, history);
    (void)pc;
}

bool BranchPredictor::btb_lookup(std::uint64_t pc,
                                 std::uint64_t& target) {
    ++btb_clock_;
    const auto set = static_cast<std::uint32_t>(
        (pc >> config_.btb_set_shift) & (btb_sets_ - 1));
    const auto tag = (pc >> btb_tag_shift_) & btb_tag_mask_;
    auto* base = btb_.data() +
                 static_cast<std::size_t>(set) *
                     config_.btb_associativity;
    for (std::uint32_t way = 0; way < config_.btb_associativity; ++way) {
        if (base[way].valid && base[way].tag == tag) {
            base[way].last_touch = btb_clock_;
            target = base[way].target;
            return true;
        }
    }
    return false;
}

void BranchPredictor::btb_update(std::uint64_t pc,
                                 std::uint64_t target) {
    ++btb_clock_;
    const auto set = static_cast<std::uint32_t>(
        (pc >> config_.btb_set_shift) & (btb_sets_ - 1));
    const auto tag = (pc >> btb_tag_shift_) & btb_tag_mask_;
    auto* base = btb_.data() +
                 static_cast<std::size_t>(set) *
                     config_.btb_associativity;
    std::uint32_t victim = 0;
    for (std::uint32_t way = 1; way < config_.btb_associativity; ++way) {
        if (base[way].last_touch < base[victim].last_touch) victim = way;
    }
    base[victim] = BtbEntry{tag, target, btb_clock_, true};
}

void BranchPredictor::update_btb_for_event(
    const TraceRecord& record) {
    if (!config_.requires_btb_hit &&
        (has_flag(record.flags, kReturn) ||
         has_flag(record.flags, kIndirect))) {
        return;
    }
    btb_update(record.pc, record.next_pc);
}

BranchPredictor::RasHistory BranchPredictor::ras_push(
    const RasFrame& frame) {
    RasHistory history;
    history.pushed = true;
    ras_tos_ = (ras_tos_ + 1) % config_.ras_entries;
    ras_entries_[ras_tos_] = frame;
    ras_used_ = std::min(config_.ras_entries, ras_used_ + 1);
    return history;
}

std::pair<BranchPredictor::RasFrame, BranchPredictor::RasHistory>
BranchPredictor::ras_pop() {
    RasHistory history;
    history.popped = true;
    history.old_tos = ras_tos_;
    history.popped_frame = ras_entries_[ras_tos_];
    const auto frame = ras_entries_[ras_tos_];
    if (ras_used_ != 0) --ras_used_;
    ras_tos_ =
        (ras_tos_ + config_.ras_entries - 1) % config_.ras_entries;
    return {frame, history};
}

void BranchPredictor::ras_squash(const RasHistory& history) {
    if (history.pushed) {
        if (ras_used_ != 0) --ras_used_;
        ras_tos_ =
            (ras_tos_ + config_.ras_entries - 1) %
            config_.ras_entries;
    }
    if (history.popped) {
        ras_tos_ = history.old_tos;
        ras_entries_[ras_tos_] = history.popped_frame;
        ras_used_ = std::min(config_.ras_entries, ras_used_ + 1);
    }
}

BranchPredictor::RasFrame BranchPredictor::ras_frame_for_call(
    const TraceRecord& record, const TraceSource* trace_source,
    BranchCounters& counters) {
    RasFrame frame;
    frame.address_space_id = trace_source == nullptr
        ? 0
        : trace_source->current_address_space_id();
    frame.call_pc = record.pc;
    frame.valid = true;
    ++counters.ras_pushes;

    if (config_.ras_static_return_target && trace_source != nullptr) {
        const auto* instruction =
            trace_source->static_instruction(record.pc);
        if (instruction != nullptr && instruction->is_call() &&
            instruction->size != 0 &&
            instruction->fallthrough_pc ==
                record.pc + instruction->size) {
            frame.return_target = instruction->fallthrough_pc;
            frame.target_valid = true;
            ++counters.ras_static_return_targets;
            return frame;
        }
    }

    const RasCallSite site{frame.address_space_id, record.pc};
    const auto learned = learned_return_targets_.find(site);
    if (learned != learned_return_targets_.end()) {
        frame.return_target = learned->second;
        frame.target_valid = true;
        ++counters.ras_learned_return_targets;
    } else {
        ++counters.ras_unknown_return_targets;
    }
    return frame;
}

std::uint32_t BranchPredictor::indirect_set(std::uint64_t pc) const {
    auto value = pc >> config_.inst_shift;
    if (config_.indirect_hash_ghr) value ^= indirect_ghr_;
    if (config_.indirect_hash_targets) {
        const auto set_bits = bit_width(config_.indirect_sets - 1);
        const auto shift = set_bits / config_.indirect_path_length;
        const auto count = std::min<std::size_t>(
            config_.indirect_path_length, indirect_path_.size());
        for (std::size_t position = 0; position < count; ++position) {
            const auto& item =
                indirect_path_[indirect_path_.size() - 1 - position];
            const auto target_shift =
                config_.inst_shift +
                static_cast<std::uint32_t>(position) * shift;
            if (target_shift < 64) {
                value ^= item.target >> target_shift;
            }
        }
    }
    return static_cast<std::uint32_t>(
        value & (config_.indirect_sets - 1));
}

std::uint32_t BranchPredictor::indirect_tag(std::uint64_t pc) const {
    return static_cast<std::uint32_t>(
        (pc >> config_.inst_shift) &
        mask_for_bits(config_.indirect_tag_bits));
}

std::optional<std::uint64_t> BranchPredictor::indirect_lookup(
    std::uint64_t pc, IndirectHistory& history) {
    history.ghr = indirect_ghr_;
    history.pc = pc;
    history.was_indirect = true;
    history.set = indirect_set(pc);
    history.tag = indirect_tag(pc);
    auto* base = indirect_cache_.data() +
                 static_cast<std::size_t>(history.set) *
                     config_.indirect_ways;
    for (std::uint32_t way = 0; way < config_.indirect_ways; ++way) {
        if (base[way].valid && base[way].tag == history.tag) {
            history.hit = true;
            return base[way].target;
        }
    }
    return std::nullopt;
}

void BranchPredictor::indirect_speculative_update(
    std::uint64_t sequence, bool predicted_taken,
    std::uint64_t predicted_target, bool indirect_no_return,
    IndirectHistory& history) {
    history.was_indirect = indirect_no_return;
    if (indirect_no_return) {
        indirect_path_.push_back(
            IndirectPathEntry{history.pc, predicted_target, sequence});
    }
    indirect_ghr_ =
        static_cast<std::uint32_t>(
            ((static_cast<std::uint64_t>(indirect_ghr_) << 1) |
             static_cast<std::uint64_t>(predicted_taken)) &
            mask_for_bits(config_.indirect_ghr_bits));
}

void BranchPredictor::indirect_repair(
    std::uint64_t sequence, bool actual_taken,
    std::uint64_t actual_target, bool indirect_no_return,
    IndirectHistory& history) {
    indirect_ghr_ = history.ghr;
    history.was_indirect = indirect_no_return;
    if (indirect_no_return) {
        if (!indirect_path_.empty()) indirect_path_.pop_back();
        history.set = indirect_set(history.pc);
        history.tag = indirect_tag(history.pc);
        indirect_path_.push_back(
            IndirectPathEntry{history.pc, actual_target, sequence});
    }
    indirect_ghr_ =
        static_cast<std::uint32_t>(
            ((static_cast<std::uint64_t>(indirect_ghr_) << 1) |
             static_cast<std::uint64_t>(actual_taken)) &
            mask_for_bits(config_.indirect_ghr_bits));
    if (!indirect_no_return || !actual_taken) return;

    auto* base = indirect_cache_.data() +
                 static_cast<std::size_t>(history.set) *
                     config_.indirect_ways;
    for (std::uint32_t way = 0; way < config_.indirect_ways; ++way) {
        if (base[way].tag == history.tag) {
            base[way].target = actual_target;
            base[way].valid = true;
            return;
        }
    }
    auto& victim =
        base[indirect_random_.next() % config_.indirect_ways];
    victim.tag = history.tag;
    victim.target = actual_target;
    victim.valid = true;
}

void BranchPredictor::indirect_commit() {
    const auto limit =
        static_cast<std::size_t>(config_.indirect_path_length) +
        config_.indirect_speculative_path_length;
    while (limit != 0 && indirect_path_.size() >= limit) {
        indirect_path_.erase(indirect_path_.begin());
    }
}

void BranchPredictor::commit_pending(PendingCommit& pending) {
    direction_train(
        pending.actual_taken, pending.tournament_history);
    indirect_commit();
    if (pending.update_btb) {
        btb_update(pending.pc, pending.btb_target);
    }
    if (pending.learn_return_target) {
        learned_return_targets_[RasCallSite{
            pending.learned_address_space_id,
            pending.learned_call_pc}] = pending.learned_return_target;
    }
}

void BranchPredictor::advance_to(std::uint64_t fetch_cycle) {
    if (advanced_ && fetch_cycle < last_advance_cycle_) {
        throw std::logic_error(
            "branch predictor Fetch cycle moved backwards");
    }
    advanced_ = true;
    last_advance_cycle_ = fetch_cycle;
    while (!pending_commits_.empty()) {
        auto& pending = pending_commits_.front();
        // gem5 ticks Fetch before Commit in one CPU cycle. A table update at
        // retire cycle N is therefore first visible to Fetch in cycle N+1.
        if (!pending.retire_cycle_valid ||
            pending.retire_cycle >= fetch_cycle) {
            break;
        }
        commit_pending(pending);
        pending_commits_.pop_front();
    }
}

void BranchPredictor::schedule_commit(
    std::uint64_t sequence, std::uint64_t retire_cycle) {
    if (pending_commits_.empty() ||
        pending_commits_.back().sequence != sequence ||
        pending_commits_.back().retire_cycle_valid) {
        throw std::logic_error(
            "branch predictor commit does not match latest checkpoint");
    }
    pending_commits_.back().retire_cycle = retire_cycle;
    pending_commits_.back().retire_cycle_valid = true;
    if (advanced_ && retire_cycle < last_advance_cycle_) {
        advance_to(last_advance_cycle_);
    }
}

void BranchPredictor::drain() {
    while (!pending_commits_.empty()) {
        auto& pending = pending_commits_.front();
        if (!pending.retire_cycle_valid) {
            throw std::logic_error(
                "branch predictor drained an unscheduled checkpoint");
        }
        commit_pending(pending);
        pending_commits_.pop_front();
    }
}

BranchPredictionResult BranchPredictor::process(
    const TraceRecord& record, BranchCounters& counters,
    const TraceSource* trace_source,
    std::uint64_t speculative_path_budget) {
    return process_impl(
        record, counters, trace_source, speculative_path_budget, false);
}

BranchPredictionResult BranchPredictor::predict_speculative(
    const TraceRecord& record, BranchCounters& counters,
    const TraceSource* trace_source,
    std::uint64_t speculative_path_budget) {
    return process_impl(
        record, counters, trace_source, speculative_path_budget, true);
}

BranchPredictionResult BranchPredictor::process_impl(
    const TraceRecord& record, BranchCounters& counters,
    const TraceSource* trace_source,
    std::uint64_t speculative_path_budget,
    bool defer_commit) {
    BranchPredictionResult result;
    if (!has_flag(record.flags, kBranch)) return result;
    ++counters.branches;
    ++sequence_;
    result.sequence = sequence_;

    const bool conditional = has_flag(record.flags, kConditional);
    const bool actual_taken = has_flag(record.flags, kTaken);
    const bool is_call = has_flag(record.flags, kCall);
    const bool is_return = has_flag(record.flags, kReturn);
    const bool is_indirect = has_flag(record.flags, kIndirect);
    const bool indirect_no_return = is_indirect && !is_return;
    if (conditional) ++counters.conditional;

    TournamentHistory tournament_history;
    if (conditional) {
        result.conditional_prediction =
            direction_lookup(record.pc, tournament_history);
    } else {
        result.conditional_prediction = true;
        tournament_history.global_history = global_history_;
        tournament_history.global_index = static_cast<std::uint32_t>(
            global_history_ & (config_.global_entries - 1));
    }
    result.predicted_taken = result.conditional_prediction;

    std::uint64_t btb_target = 0;
    const bool btb_hit = btb_lookup(record.pc, btb_target);
    if (btb_hit) ++counters.btb_hits;
    if (btb_hit && result.predicted_taken) {
        result.predicted_target = btb_target;
        result.target_available = true;
    }
    const bool branch_detected = btb_hit || !config_.requires_btb_hit;

    std::optional<RasHistory> ras_history;
    if (branch_detected && is_call) {
        ras_history = ras_push(
            ras_frame_for_call(record, trace_source, counters));
    } else if (branch_detected && is_return) {
        ++counters.ras_pops;
        auto popped = ras_pop();
        ras_history = popped.second;
        if (popped.first.valid && popped.first.target_valid) {
            ++counters.ras_predictions;
            result.predicted_target = popped.first.return_target;
            result.target_available = true;
            if (result.predicted_target == record.next_pc) {
                ++counters.ras_hits;
            }
        }
    }

    IndirectHistory indirect_history;
    indirect_history.ghr = indirect_ghr_;
    if (result.predicted_taken && branch_detected &&
        indirect_no_return) {
        const auto target =
            indirect_lookup(record.pc, indirect_history);
        if (target.has_value()) {
            result.predicted_target = *target;
            result.target_available = true;
        }
    }

    if (!result.target_available) result.predicted_taken = false;
    const auto speculative_target =
        result.target_available
            ? result.predicted_target
            : (actual_taken ? record.pc + 1 : record.next_pc);
    indirect_speculative_update(
        sequence_, result.predicted_taken, speculative_target,
        indirect_no_return, indirect_history);

    result.direction_miss =
        conditional &&
        result.conditional_prediction != actual_taken;
    const bool final_direction_miss =
        result.predicted_taken != actual_taken;
    result.target_miss =
        result.predicted_taken && actual_taken &&
        (!result.target_available ||
         result.predicted_target != record.next_pc);
    result.miss = final_direction_miss || result.target_miss;
    counters.direction_misses += result.direction_miss;
    counters.target_misses += result.target_miss;
    counters.misses += result.miss;
    if (result.miss && result.direction_miss) {
        ++counters.direction_only_misses;
    } else if (result.miss && actual_taken &&
               result.conditional_prediction &&
               !result.target_available) {
        ++counters.target_unavailable_misses;
    } else if (result.target_miss) {
        ++counters.wrong_target_misses;
    }
    if (result.direction_miss && !result.miss) {
        ++counters.masked_direction_misses;
    }
    if (!counters.miss_population_conserved()) {
        throw std::logic_error(
            "branch miss population attribution did not conserve");
    }

    // Capture wrong-path PCs before squash repair, BTB correction, and the
    // committed direction-history update. Nested choices therefore see only
    // predictor state available when the resolving branch was fetched.
    if (result.miss && trace_source != nullptr &&
        speculative_path_budget != 0) {
        build_speculative_path(
            record, result, *trace_source, speculative_path_budget,
            result.speculative_path);
    }

    if (result.miss) {
        indirect_repair(sequence_, actual_taken, record.next_pc,
                        indirect_no_return, indirect_history);
        if (actual_taken && !ras_history.has_value()) {
            if (is_return) {
                ++counters.ras_pops;
                ras_history = ras_pop().second;
            } else if (is_call) {
                ras_history = ras_push(
                    ras_frame_for_call(record, trace_source, counters));
            }
        } else if (!actual_taken && ras_history.has_value()) {
            ras_squash(*ras_history);
            ras_history.reset();
        }
        if (actual_taken && config_.update_btb_at_squash) {
            update_btb_for_event(record);
        }
    }

    bool learn_return_target = false;
    std::uint64_t learned_address_space_id = 0;
    std::uint64_t learned_call_pc = 0;
    if (is_return && actual_taken && ras_history.has_value() &&
        ras_history->popped_frame.valid) {
        const auto& frame = ras_history->popped_frame;
        learn_return_target = true;
        learned_address_space_id = frame.address_space_id;
        learned_call_pc = frame.call_pc;
    } else if (is_call && !actual_taken) {
        learn_return_target = true;
        learned_address_space_id = trace_source == nullptr
            ? 0
            : trace_source->current_address_space_id();
        learned_call_pc = record.pc;
    }

    if (defer_commit) {
        ++counters.history_checkpoints;
        ++counters.deferred_direction_commits;
        direction_update_histories(
            result.predicted_taken, tournament_history);
        if (result.miss) {
            ++counters.history_squashes;
            direction_update_histories(actual_taken, tournament_history);
        }

        PendingCommit pending;
        pending.sequence = sequence_;
        pending.pc = record.pc;
        pending.btb_target = record.next_pc;
        pending.tournament_history = tournament_history;
        pending.actual_taken = actual_taken;
        pending.update_btb =
            actual_taken && !config_.update_btb_at_squash;
        pending.learn_return_target = learn_return_target;
        pending.learned_address_space_id = learned_address_space_id;
        pending.learned_call_pc = learned_call_pc;
        pending.learned_return_target = record.next_pc;
        pending_commits_.push_back(std::move(pending));
    } else {
        direction_commit(record.pc, actual_taken, tournament_history);
        indirect_commit();
        if (actual_taken && !config_.update_btb_at_squash) {
            update_btb_for_event(record);
        }
        if (learn_return_target) {
            learned_return_targets_[RasCallSite{
                learned_address_space_id, learned_call_pc}] = record.next_pc;
        }
    }
    return result;
}

}  // namespace fastsim
