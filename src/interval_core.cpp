#include "fastsim/interval_core.hpp"

#include <algorithm>
#include <limits>
#include <numeric>
#include <stdexcept>

namespace fastsim {

IntervalCoreModel::IntervalCoreModel(const SimulatorConfig& config)
    : config_(config) {
    rename_free_entries_ = {
        config_.rename_int_free_entries,
        config_.rename_float_free_entries,
        config_.rename_vec_free_entries,
        config_.rename_cc_free_entries};
    for (auto& traits : trait_table_) {
        traits = OpTraits{FuPool::kInteger,
                          config_.integer_alu_latency,
                          config_.integer_alu_pipelined};
    }
    trait_table_[2] = OpTraits{FuPool::kIntegerMultiply,
                               config_.integer_multiply_latency,
                               config_.integer_multiply_pipelined};
    trait_table_[3] = OpTraits{FuPool::kIntegerMultiply,
                               config_.integer_divide_latency,
                               config_.integer_divide_pipelined};
    for (int op = 4; op <= 6; ++op) {
        trait_table_[static_cast<std::size_t>(op)] =
            OpTraits{FuPool::kFloatSimple,
                     config_.float_simple_latency,
                     config_.float_simple_pipelined};
    }
    trait_table_[7] = OpTraits{FuPool::kFloatComplex,
                               config_.float_multiply_latency,
                               config_.float_complex_pipelined};
    trait_table_[8] = OpTraits{
        FuPool::kFloatComplex,
        config_.float_multiply_accumulate_latency,
        config_.float_complex_pipelined};
    trait_table_[9] = OpTraits{FuPool::kFloatComplex,
                               config_.float_divide_latency,
                               config_.float_divide_pipelined};
    trait_table_[10] = OpTraits{FuPool::kFloatComplex,
                                config_.float_misc_latency,
                                config_.float_complex_pipelined};
    trait_table_[11] = OpTraits{FuPool::kFloatComplex,
                                config_.float_sqrt_latency,
                                config_.float_sqrt_pipelined};
    for (int op = 12; op <= 55; ++op) {
        trait_table_[static_cast<std::size_t>(op)] =
            OpTraits{FuPool::kSimd, config_.simd_latency, true};
    }
    trait_table_[51] = OpTraits{FuPool::kPredicate,
                                config_.predicate_latency, true};
    for (int op = 77; op <= 86; ++op) {
        trait_table_[static_cast<std::size_t>(op)] =
            OpTraits{FuPool::kSimd, config_.simd_latency, true};
    }
    trait_table_[87] = OpTraits{FuPool::kFloatSimple,
                                config_.float_simple_latency,
                                config_.float_simple_pipelined};
    for (std::size_t op = 88; op < trait_table_.size(); ++op) {
        trait_table_[op] = OpTraits{FuPool::kSystem,
                                    config_.system_latency, true};
    }
    fu_ready_[static_cast<std::size_t>(FuPool::kInteger)].resize(
        config_.integer_alu_units);
    fu_ready_[static_cast<std::size_t>(FuPool::kIntegerMultiply)].resize(
        config_.integer_multiply_units);
    fu_ready_[static_cast<std::size_t>(FuPool::kFloatSimple)].resize(
        config_.float_simple_units);
    fu_ready_[static_cast<std::size_t>(FuPool::kFloatComplex)].resize(
        config_.float_complex_units);
    fu_ready_[static_cast<std::size_t>(FuPool::kSimd)].resize(
        config_.simd_units);
    fu_ready_[static_cast<std::size_t>(FuPool::kPredicate)].resize(
        config_.predicate_units);
    fu_ready_[static_cast<std::size_t>(FuPool::kMemory)].resize(
        config_.memory_units);
    fu_ready_[static_cast<std::size_t>(FuPool::kSystem)].resize(
        config_.system_units);
    for (const auto& lanes : fu_ready_) {
        if (lanes.empty()) {
            throw std::invalid_argument(
                "interval core functional-unit count must be nonzero");
        }
    }
    if (config_.dtlb.enabled &&
        config_.dtlb.miss_model == "timing_walk") {
        dtlb_lru_.reserve(config_.dtlb.entries * 2u);
        pending_page_walks_.reserve(config_.dtlb.page_walkers * 2u);
        page_walker_ready_.resize(config_.dtlb.page_walkers, 0);
    }
}

IntervalCoreModel::OpTraits IntervalCoreModel::traits(
    const TraceRecord& record) const {
    if (record.is_memory()) {
        // Stores leave the execution core after address/data generation;
        // loads and atomics wait for the private-L1 lower-bound response.
        const bool regular_store =
            record.is_write() && !has_flag(record.flags, kAtomic);
        return OpTraits{FuPool::kMemory,
                        regular_store ? 1u
                                      : config_.minimum_load_latency,
                        true};
    }
    if (record.is_syscall()) {
        return OpTraits{
            FuPool::kSystem,
            static_cast<std::uint32_t>(
                static_cast<std::uint64_t>(config_.system_latency) +
                config_.syscall_service_latency),
            true};
    }
    const auto op = static_cast<int>(record.op_class);
    if (op >= 0 &&
        static_cast<std::size_t>(op) < trait_table_.size()) {
        return trait_table_[static_cast<std::size_t>(op)];
    }
    return op >= 88
               ? OpTraits{FuPool::kSystem, config_.system_latency, true}
               : OpTraits{FuPool::kInteger,
                          config_.integer_alu_latency,
                          config_.integer_alu_pipelined};
}

std::uint64_t IntervalCoreModel::allocate_dispatch(
    std::uint64_t earliest) {
    if (earliest > dispatch_cycle_) {
        dispatch_cycle_ = earliest;
        dispatches_this_cycle_ = 0;
    }
    if (dispatches_this_cycle_ == config_.dispatch_width) {
        ++dispatch_cycle_;
        dispatches_this_cycle_ = 0;
    }
    ++dispatches_this_cycle_;
    return dispatch_cycle_;
}

std::uint64_t IntervalCoreModel::allocate_stage(
    std::uint64_t earliest, std::uint32_t width,
    std::uint64_t& cycle, std::uint32_t& used) {
    if (earliest > cycle) {
        cycle = earliest;
        used = 0;
    }
    if (used == width) {
        ++cycle;
        used = 0;
    }
    ++used;
    return cycle;
}

std::uint64_t IntervalCoreModel::allocate_issue(
    std::uint64_t earliest, const OpTraits& op,
    const TraceRecord& record) {
    auto& lanes = fu_ready_[static_cast<std::size_t>(op.pool)];
    auto cycle = earliest;
    while (true) {
        auto lane = std::min_element(lanes.begin(), lanes.end());
        cycle = std::max(cycle, *lane);
        if (cycle >= issue_slots_.size()) {
            issue_slots_.resize(static_cast<std::size_t>(cycle + 1), 0);
        }
        if (issue_slots_[cycle] >= config_.issue_width) {
            ++cycle;
            continue;
        }
        std::vector<std::uint32_t>* port_slots = nullptr;
        std::uint32_t port_count = 0;
        if (record.is_memory()) {
            if (record.is_write()) {
                port_slots = &store_port_slots_;
                port_count = config_.cache_store_ports;
            } else {
                port_slots = &load_port_slots_;
                port_count = config_.cache_load_ports;
            }
            if (cycle >= port_slots->size()) {
                port_slots->resize(static_cast<std::size_t>(cycle + 1), 0);
            }
            if ((*port_slots)[cycle] >= port_count) {
                ++cycle;
                continue;
            }
        }
        auto available = std::find_if(
            lanes.begin(), lanes.end(),
            [cycle](std::uint64_t ready) { return ready <= cycle; });
        if (available == lanes.end()) {
            ++cycle;
            continue;
        }
        ++issue_slots_[cycle];
        if (port_slots != nullptr) ++(*port_slots)[cycle];
        *available = cycle + (op.pipelined ? 1u : op.latency);
        return cycle;
    }
}

std::uint64_t IntervalCoreModel::allocate_writeback(
    std::uint64_t earliest) {
    auto cycle = earliest;
    while (true) {
        if (cycle >= writeback_slots_.size()) {
            writeback_slots_.resize(static_cast<std::size_t>(cycle + 1), 0);
        }
        if (writeback_slots_[cycle] < config_.writeback_width) {
            ++writeback_slots_[cycle];
            return cycle;
        }
        ++cycle;
    }
}

std::uint64_t IntervalCoreModel::allocate_retire(
    std::uint64_t earliest) {
    if (earliest > last_retire_cycle_) {
        last_retire_cycle_ = earliest;
        retires_this_cycle_ = 0;
    }
    if (retires_this_cycle_ == config_.commit_width) {
        ++last_retire_cycle_;
        retires_this_cycle_ = 0;
    }
    ++retires_this_cycle_;
    return last_retire_cycle_;
}

void IntervalCoreModel::audit_destination_releases_through(
    std::uint64_t cycle) {
    while (!destination_releases_.empty() &&
           destination_releases_.top().first <= cycle) {
        const auto tokens = destination_releases_.top().second;
        destination_releases_.pop();
        if (tokens > live_destination_tokens_) {
            throw std::logic_error(
                "committed destination-token audit underflow");
        }
        live_destination_tokens_ -= tokens;
        committed_pipeline_audit_.destination_tokens_released += tokens;
    }
    committed_pipeline_audit_.destination_tokens_live_at_last_rename =
        live_destination_tokens_;
}

void IntervalCoreModel::destination_class_releases_through(
    std::uint64_t cycle) {
    while (!destination_class_releases_.empty() &&
           destination_class_releases_.top().first <= cycle) {
        const auto counts = destination_class_releases_.top().second;
        destination_class_releases_.pop();
        for (std::size_t index = 0; index < counts.size(); ++index) {
            if (counts[index] > live_destination_class_tokens_[index]) {
                throw std::logic_error(
                    "committed destination-class audit underflow");
            }
            live_destination_class_tokens_[index] -= counts[index];
            committed_pipeline_audit_
                .destination_class_tokens_released[index] += counts[index];
        }
    }
    committed_pipeline_audit_
        .destination_class_tokens_live_at_last_rename =
        live_destination_class_tokens_;
}

std::uint64_t IntervalCoreModel::rename_free_list_ready(
    const DestinationClassCounts& required,
    std::uint64_t earliest) {
    for (std::size_t index = 0; index < required.size(); ++index) {
        if (required[index] > rename_free_entries_[index]) {
            throw std::runtime_error(
                "one UOP requires more destination registers than the "
                "configured committed rename free list");
        }
    }
    destination_class_releases_through(earliest);
    const auto unavailable = [&] {
        for (std::size_t index = 0; index < required.size(); ++index) {
            if (live_destination_class_tokens_[index] + required[index] >
                rename_free_entries_[index]) {
                return true;
            }
        }
        return false;
    };
    while (unavailable()) {
        if (destination_class_releases_.empty()) {
            throw std::logic_error(
                "full committed rename free list has no scheduled release");
        }
        earliest = std::max(
            earliest, destination_class_releases_.top().first);
        destination_class_releases_through(earliest);
    }
    return earliest;
}

void IntervalCoreModel::audit_dispatch_delay(
    std::uint64_t nominal, std::uint64_t actual, DispatchGate gate) {
    if (actual <= nominal) return;
    if (gate == DispatchGate::kNone) {
        throw std::logic_error(
            "delayed interval dispatch has no audit attribution");
    }
    const auto cycles = actual - nominal;
    auto* events = &committed_pipeline_audit_.dispatch_bandwidth_events;
    auto* attributed_cycles =
        &committed_pipeline_audit_.dispatch_bandwidth_cycles;
    switch (gate) {
        case DispatchGate::kBandwidth:
            break;
        case DispatchGate::kRob:
            events = &committed_pipeline_audit_.rob_capacity_events;
            attributed_cycles =
                &committed_pipeline_audit_.rob_capacity_cycles;
            break;
        case DispatchGate::kIq:
            events = &committed_pipeline_audit_.iq_capacity_events;
            attributed_cycles =
                &committed_pipeline_audit_.iq_capacity_cycles;
            break;
        case DispatchGate::kLq:
            events = &committed_pipeline_audit_.lq_capacity_events;
            attributed_cycles =
                &committed_pipeline_audit_.lq_capacity_cycles;
            break;
        case DispatchGate::kSq:
            events = &committed_pipeline_audit_.sq_capacity_events;
            attributed_cycles =
                &committed_pipeline_audit_.sq_capacity_cycles;
            break;
        case DispatchGate::kNone:
            throw std::logic_error(
                "unreachable empty dispatch audit gate");
    }
    ++committed_pipeline_audit_.dispatch_delayed_uops;
    committed_pipeline_audit_.dispatch_delay_cycles += cycles;
    ++*events;
    *attributed_cycles += cycles;
}

void IntervalCoreModel::release_iq_through(std::uint64_t cycle) {
    if (cycle < iq_release_cursor_) return;
    const auto end = std::min<std::uint64_t>(
        cycle, iq_release_slots_.empty()
                   ? 0
                   : iq_release_slots_.size() - 1);
    if (!iq_release_slots_.empty()) {
        while (iq_release_cursor_ <= end) {
            const auto releases = iq_release_slots_[iq_release_cursor_];
            if (releases > iq_occupancy_) {
                throw std::logic_error(
                    "interval IQ release calendar underflow");
            }
            iq_occupancy_ -= releases;
            ++iq_release_cursor_;
        }
    }
    if (iq_release_cursor_ <= cycle) {
        iq_release_cursor_ = cycle + 1;
    }
}

std::uint64_t IntervalCoreModel::next_iq_release_cycle() const {
    auto cycle = iq_release_cursor_;
    while (cycle < iq_release_slots_.size() &&
           iq_release_slots_[cycle] == 0) {
        ++cycle;
    }
    if (cycle == iq_release_slots_.size()) {
        throw std::logic_error(
            "nonempty interval IQ has no scheduled release");
    }
    return cycle;
}

void IntervalCoreModel::fill_dtlb(std::uint32_t token) {
    const auto found = dtlb_lru_.find(token);
    if (found != dtlb_lru_.end()) {
        found->second = ++dtlb_sequence_;
        return;
    }
    if (dtlb_lru_.size() == config_.dtlb.entries) {
        const auto victim = std::min_element(
            dtlb_lru_.begin(), dtlb_lru_.end(),
            [](const auto& left, const auto& right) {
                return left.second < right.second;
            });
        if (victim == dtlb_lru_.end()) {
            throw std::logic_error("nonempty DTLB has no LRU victim");
        }
        dtlb_lru_.erase(victim);
    }
    dtlb_lru_.emplace(token, ++dtlb_sequence_);
}

void IntervalCoreModel::retire_page_walks_through(std::uint64_t cycle) {
    while (!page_walk_completions_.empty() &&
           page_walk_completions_.top().first <= cycle) {
        const auto [ready, token] = page_walk_completions_.top();
        page_walk_completions_.pop();
        if (config_.dtlb.coalesce_misses) {
            const auto pending = pending_page_walks_.find(token);
            if (pending == pending_page_walks_.end() ||
                pending->second != ready) {
                continue;
            }
            pending_page_walks_.erase(pending);
        }
        fill_dtlb(token);
    }
}

std::uint64_t IntervalCoreModel::translate(
    const TraceRecord& record, std::uint64_t earliest,
    IntervalTiming& timing) {
    timing.translation_ready_cycle = earliest;
    if (!config_.dtlb.enabled || !record.is_memory()) return earliest;

    timing.dtlb_access = true;
    if (!has_flag(record.flags, kVirtualPageToken) ||
        record.virtual_page_token() == 0) {
        timing.dtlb_untracked = true;
        return earliest;
    }

    retire_page_walks_through(earliest);
    const auto token = record.virtual_page_token();
    const auto resident = dtlb_lru_.find(token);
    if (resident != dtlb_lru_.end()) {
        resident->second = ++dtlb_sequence_;
        timing.dtlb_hit = true;
        timing.translation_ready_cycle =
            earliest + config_.dtlb.hit_latency;
        timing.translation_delay_cycles = config_.dtlb.hit_latency;
        return timing.translation_ready_cycle;
    }

    timing.dtlb_miss = true;
    if (config_.dtlb.miss_model == "se_atomic") {
        fill_dtlb(token);
        timing.translation_ready_cycle = earliest;
        timing.translation_delay_cycles = 0;
        return earliest;
    }
    if (config_.dtlb.coalesce_misses) {
        const auto pending = pending_page_walks_.find(token);
        if (pending != pending_page_walks_.end()) {
            timing.dtlb_merged_miss = true;
            timing.translation_ready_cycle = pending->second;
            timing.translation_delay_cycles =
                pending->second > earliest ? pending->second - earliest : 0;
            return timing.translation_ready_cycle;
        }
    }

    auto walker = std::min_element(page_walker_ready_.begin(),
                                   page_walker_ready_.end());
    if (walker == page_walker_ready_.end()) {
        throw std::logic_error("enabled DTLB has no page walker");
    }
    const auto start = std::max(earliest, *walker);
    const auto ready = start + config_.dtlb.page_walk_latency;
    *walker = ready;
    if (config_.dtlb.coalesce_misses) {
        pending_page_walks_.emplace(token, ready);
    }
    page_walk_completions_.emplace(ready, token);
    timing.translation_ready_cycle = ready;
    timing.translation_delay_cycles = ready - earliest;
    return ready;
}

IntervalTiming IntervalCoreModel::schedule(
    const TraceRecord& record, bool branch_miss) {
    const auto index = completion_.size();
    IntervalTiming timing;
    auto fetch_earliest =
        std::max(frontend_ready_cycle_, serial_ready_cycle_);
    if (config_.fetch_buffer_bytes != 0) {
        const auto block = record.pc /
            static_cast<std::uint64_t>(config_.fetch_buffer_bytes);
        if (fetch_buffer_valid_ && block != fetch_buffer_block_) {
            // A new block is requested after the current fetch cycle. The
            // configured latency is the number of intervening empty cycles,
            // so even a zero-latency block switch begins next cycle.
            fetch_earliest = std::max(
                fetch_earliest,
                fetch_cycle_ + 1 +
                    config_.fetch_buffer_refill_latency);
        }
        fetch_buffer_block_ = block;
        fetch_buffer_valid_ = true;
    }
    if (index >= config_.fetch_queue_entries) {
        fetch_earliest = std::max(
            fetch_earliest,
            dispatch_history_[index - config_.fetch_queue_entries]);
    }
    timing.fetch_cycle = allocate_stage(
        fetch_earliest, config_.fetch_width,
        fetch_cycle_, fetches_this_cycle_);
    timing.decode_cycle = allocate_stage(
        timing.fetch_cycle + config_.fetch_to_decode,
        config_.decode_width, decode_cycle_, decodes_this_cycle_);
    const auto nominal_rename =
        timing.decode_cycle + config_.decode_to_rename;
    auto rename_earliest = nominal_rename;
    if (record.is_syscall() && !retirement_.empty()) {
        // gem5 SE executes a syscall as a non-speculative serializing system
        // operation.  It may be fetched/decoded speculatively, but it cannot
        // enter the OoO window until all older work has retired.
        rename_earliest = std::max(
            rename_earliest, retirement_.back() + 1);
        timing.syscall_drain_cycles =
            rename_earliest - nominal_rename;
    }
    const auto destination_classes =
        record.destination_class_counts();
    if (config_.rename_free_list &&
        !record.has_destination_class_counts()) {
        throw std::runtime_error(
            "core.rename_free_list requires FST v6 destination class counts");
    }
    if (record.has_destination_class_counts()) {
        const auto classified_destinations = std::accumulate(
            destination_classes.begin(), destination_classes.end(), 0u);
        if (classified_destinations != record.n_dst) {
            throw std::runtime_error(
                "destination class counts do not sum to n_dst");
        }
    }
    if (config_.rename_free_list) {
        const auto before_free_list =
            std::max(rename_earliest, rename_cycle_);
        const auto free_list_ready = rename_free_list_ready(
            destination_classes, before_free_list);
        if (free_list_ready > before_free_list) {
            ++committed_pipeline_audit_.rename_free_list_stall_uops;
            committed_pipeline_audit_.rename_free_list_stall_cycles +=
                free_list_ready - before_free_list;
        }
        rename_earliest = std::max(rename_earliest, free_list_ready);
    }
    timing.rename_cycle = allocate_stage(
        rename_earliest,
        config_.rename_width, rename_cycle_, renames_this_cycle_);

    if (config_.committed_pipeline_audit) {
        audit_destination_releases_through(timing.rename_cycle);
        ++committed_pipeline_audit_.uops;
        if (record.n_dst != 0) {
            ++committed_pipeline_audit_.destination_uops;
            committed_pipeline_audit_.destination_tokens += record.n_dst;
            live_destination_tokens_ += record.n_dst;
            committed_pipeline_audit_.destination_tokens_live_at_last_rename =
                live_destination_tokens_;
            committed_pipeline_audit_.max_live_destination_tokens =
                std::max(
                    committed_pipeline_audit_.max_live_destination_tokens,
                    live_destination_tokens_);
            for (std::size_t threshold = 0;
                 threshold < committed_pipeline_audit_
                                 .destination_thresholds.size();
                 ++threshold) {
                const auto limit = committed_pipeline_audit_
                                       .destination_thresholds[threshold];
                if (live_destination_tokens_ <= limit) continue;
                ++committed_pipeline_audit_
                      .destination_threshold_events[threshold];
                committed_pipeline_audit_
                    .destination_threshold_excess_tokens[threshold] +=
                    live_destination_tokens_ - limit;
            }
        }
    }
    if ((config_.committed_pipeline_audit || config_.rename_free_list) &&
        record.has_destination_class_counts()) {
        destination_class_releases_through(timing.rename_cycle);
        ++committed_pipeline_audit_.destination_class_uops;
        for (std::size_t class_index = 0;
             class_index < destination_classes.size(); ++class_index) {
            committed_pipeline_audit_
                .destination_class_tokens[class_index] +=
                destination_classes[class_index];
            live_destination_class_tokens_[class_index] +=
                destination_classes[class_index];
            if (config_.rename_free_list &&
                live_destination_class_tokens_[class_index] >
                    rename_free_entries_[class_index]) {
                throw std::logic_error(
                    "committed rename free-list capacity overflow");
            }
            committed_pipeline_audit_
                .destination_class_tokens_live_at_last_rename[class_index] =
                live_destination_class_tokens_[class_index];
            committed_pipeline_audit_
                .destination_class_max_live_tokens[class_index] = std::max(
                    committed_pipeline_audit_
                        .destination_class_max_live_tokens[class_index],
                    live_destination_class_tokens_[class_index]);
        }
    }

    const auto nominal_dispatch =
        timing.rename_cycle + config_.rename_to_dispatch;
    std::uint64_t dispatch_earliest = nominal_dispatch;
    auto dispatch_gate = DispatchGate::kNone;
    if (index >= config_.rob_entries) {
        const auto rob_ready =
            retirement_[index - config_.rob_entries];
        if (rob_ready > dispatch_earliest) {
            dispatch_earliest = rob_ready;
            dispatch_gate = DispatchGate::kRob;
        }
    }
    release_iq_through(dispatch_earliest);
    if (iq_occupancy_ >= config_.iq_entries) {
        const auto iq_ready = next_iq_release_cycle();
        if (iq_ready > dispatch_earliest) {
            dispatch_earliest = iq_ready;
            dispatch_gate = DispatchGate::kIq;
        }
        release_iq_through(dispatch_earliest);
    }
    if (record.is_memory()) {
        const auto& queue = record.is_write()
                                ? store_retirement_
                                : load_retirement_;
        const auto capacity = record.is_write()
                                  ? config_.sq_entries
                                  : config_.lq_entries;
        if (queue.size() >= capacity) {
            const auto queue_ready = queue[queue.size() - capacity];
            if (queue_ready > dispatch_earliest) {
                dispatch_earliest = queue_ready;
                dispatch_gate = record.is_write()
                                    ? DispatchGate::kSq
                                    : DispatchGate::kLq;
            }
        }
    }
    const auto before_dispatch_bandwidth = dispatch_earliest;
    timing.dispatch_cycle = allocate_dispatch(dispatch_earliest);
    if (timing.dispatch_cycle > before_dispatch_bandwidth) {
        dispatch_gate = DispatchGate::kBandwidth;
    }
    if (config_.committed_pipeline_audit) {
        audit_dispatch_delay(
            nominal_dispatch, timing.dispatch_cycle, dispatch_gate);
    }

    std::uint64_t dependency_ready =
        timing.dispatch_cycle + config_.dispatch_to_issue;
    for (const auto distance : record.producer_dists) {
        if (distance == 0 || distance > index) continue;
        dependency_ready = std::max(
            dependency_ready, completion_[index - distance]);
    }
    const auto op = traits(record);
    timing.fu_pool = op.pool;
    timing.fu_occupancy_cycles = op.pipelined ? 1u : op.latency;
    const auto translation_ready =
        translate(record, dependency_ready, timing);
    timing.issue_cycle = allocate_issue(
        std::max(dependency_ready, translation_ready), op, record);
    timing.execute_cycle =
        timing.issue_cycle + config_.issue_to_execute;
    timing.completion_cycle = allocate_writeback(
        timing.execute_cycle + op.latency);
    const auto iq_release_cycle =
        record.is_memory() ? timing.completion_cycle : timing.issue_cycle;
    if (iq_release_cycle >= iq_release_cursor_) {
        if (iq_release_cycle >= iq_release_slots_.size()) {
            iq_release_slots_.resize(
                static_cast<std::size_t>(iq_release_cycle + 1), 0);
        }
        ++iq_release_slots_[iq_release_cycle];
        ++iq_occupancy_;
    }
    timing.retire_cycle = allocate_retire(
        timing.completion_cycle + config_.execute_to_commit);

    if (config_.committed_pipeline_audit) {
        const auto rob_residency =
            timing.retire_cycle - timing.dispatch_cycle;
        const auto iq_residency =
            iq_release_cycle - timing.dispatch_cycle;
        committed_pipeline_audit_.rob_residency_cycles += rob_residency;
        committed_pipeline_audit_.rob_max_residency_cycles = std::max(
            committed_pipeline_audit_.rob_max_residency_cycles,
            rob_residency);
        committed_pipeline_audit_.iq_residency_cycles += iq_residency;
        committed_pipeline_audit_.iq_max_residency_cycles = std::max(
            committed_pipeline_audit_.iq_max_residency_cycles,
            iq_residency);
        if (record.is_memory() &&
            timing.completion_cycle > timing.issue_cycle) {
            ++committed_pipeline_audit_.memory_iq_post_issue_uops;
            committed_pipeline_audit_.memory_iq_post_issue_cycles +=
                timing.completion_cycle - timing.issue_cycle;
        }
        if (record.n_dst != 0) {
            committed_pipeline_audit_.destination_lifetime_token_cycles +=
                static_cast<std::uint64_t>(record.n_dst) *
                (timing.retire_cycle - timing.rename_cycle);
            committed_pipeline_audit_.destination_release_tokens +=
                record.n_dst;
            destination_releases_.emplace(
                timing.retire_cycle, record.n_dst);
        }
    }
    if ((config_.committed_pipeline_audit || config_.rename_free_list) &&
        record.has_destination_class_counts()) {
        for (std::size_t class_index = 0;
             class_index < destination_classes.size(); ++class_index) {
            committed_pipeline_audit_
                .destination_class_release_tokens[class_index] +=
                destination_classes[class_index];
        }
        destination_class_releases_.emplace(
            timing.retire_cycle, destination_classes);
    }

    completion_.push_back(timing.completion_cycle);
    retirement_.push_back(timing.retire_cycle);
    dispatch_history_.push_back(timing.dispatch_cycle);
    if (record.is_memory()) {
        auto& queue = record.is_write()
                          ? store_retirement_
                          : load_retirement_;
        queue.push_back(timing.retire_cycle);
    }
    if (branch_miss) {
        frontend_ready_cycle_ = std::max(
            frontend_ready_cycle_,
            timing.completion_cycle + config_.branch.mispredict_penalty);
    } else if (has_flag(record.flags, kBranch) &&
               has_flag(record.flags, kTaken)) {
        // gem5 stops the current fetch group at a predicted-taken branch.
        // A correctly predicted target may be fetched in the next cycle;
        // a different fetch-buffer block adds its refill constraint above.
        frontend_ready_cycle_ = std::max(
            frontend_ready_cycle_, timing.fetch_cycle + 1);
    }
    if (record.is_serializing()) {
        const auto restart = record.is_syscall()
            ? config_.syscall_restart_latency
            : 1u;
        serial_ready_cycle_ = timing.retire_cycle + restart;
        if (record.is_syscall()) {
            timing.syscall_service_cycles =
                config_.syscall_service_latency;
            timing.syscall_restart_cycles = restart;
        }
    }
    return timing;
}

}  // namespace fastsim
