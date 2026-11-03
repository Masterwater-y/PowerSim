#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>

namespace fastsim {

// Local strict-max winner in dispatch evaluation order; carry is deliberately
// unresolved rather than attributed to a guessed earlier resource owner.
enum class IntervalTailDispatchGate : std::uint32_t {
    kBase, kRename, kRob, kLq, kSq, kIq, kCarry, kWidth, kRenameReadmission,
};

// Samples identify an endpoint and its largest retirement advance. They are
// not a root-cause assignment: an issue/completion edge can inherit older work.
struct IntervalTailAuditSample {
    std::uint64_t begin_sequence = 0, end_sequence = 0;
    std::uint64_t base_end_cycle = 0, entry_commit_cycle = 0;
    std::uint64_t interval_gap_cycles = 0;
    std::uint64_t final_retire_cycle = 0, charged_cycles = 0;
    std::uint64_t witness_sequence = 0, witness_advance_cycles = 0;
    std::uint64_t witness_issue_cycle = 0, witness_completion_cycle = 0;
    std::uint64_t witness_load_ready_cycle = 0;
    std::uint64_t witness_dependency_extra = 0;
    std::uint32_t witness_issue_gate = 0;
    std::uint32_t witness_dispatch_gate = 0;
};

// Disjoint advances of the ordered retirement clock, clipped above the final
// lower-bound retirement of this checkpoint. Summing per-UOP waiting times
// would double-count overlap; these intervals telescope to the charged tail.
struct IntervalTailAuditCounters {
    std::uint64_t checkpoints = 0, accepted_uops = 0, charged_cycles = 0;
    std::uint64_t certified_zero_checkpoints = 0, certified_zero_uops = 0;
    std::uint64_t nonzero_checkpoints = 0, max_extra_cycles = 0;
    std::uint64_t entry_carry_cycles = 0, base_advance_cycles = 0;
    std::uint64_t completion_advance_cycles = 0, commit_width_cycles = 0;
    std::uint64_t completion_load_cycles = 0, completion_store_cycles = 0;
    std::uint64_t completion_other_cycles = 0;
    std::uint64_t pre_issue_cycles = 0, post_issue_cycles = 0;
    std::array<std::uint64_t, 15> completion_by_issue_gate{};
    std::array<std::uint64_t, 15> pre_issue_by_issue_gate{};
    std::array<std::uint64_t, 9> dispatch_pre_issue_by_gate{};
    std::array<IntervalTailAuditSample, 8> largest_checkpoints{};
    std::uint32_t sample_count = 0;

    static std::uint64_t span_above(std::uint64_t before,
                                    std::uint64_t after,
                                    std::uint64_t floor) {
        const auto start = std::max(before, floor);
        return after > start ? after - start : 0;
    }

    bool conserved() const {
        std::uint64_t gate_sum = 0;
        for (auto cycles : completion_by_issue_gate) gate_sum += cycles;
        std::uint64_t pre_issue_sum = 0, dispatch_sum = 0;
        for (auto cycles : pre_issue_by_issue_gate) pre_issue_sum += cycles;
        for (auto cycles : dispatch_pre_issue_by_gate) dispatch_sum += cycles;
        return charged_cycles == entry_carry_cycles + base_advance_cycles +
                   completion_advance_cycles + commit_width_cycles &&
            completion_advance_cycles == completion_load_cycles +
                   completion_store_cycles + completion_other_cycles &&
            completion_advance_cycles == gate_sum &&
            completion_advance_cycles == pre_issue_cycles + post_issue_cycles &&
            pre_issue_cycles == pre_issue_sum &&
            pre_issue_by_issue_gate[1] == dispatch_sum;
    }

    void retain(const IntervalTailAuditSample& sample) {
        if (sample.charged_cycles == 0) return;
        auto position = sample_count;
        if (position == largest_checkpoints.size()) {
            position -= 1;
            if (sample.charged_cycles <=
                largest_checkpoints[position].charged_cycles) return;
        } else {
            ++sample_count;
        }
        while (position != 0 && sample.charged_cycles >
               largest_checkpoints[position - 1].charged_cycles) {
            largest_checkpoints[position] = largest_checkpoints[position - 1];
            --position;
        }
        largest_checkpoints[position] = sample;
    }

    IntervalTailAuditCounters& operator+=(const IntervalTailAuditCounters& other) {
        checkpoints += other.checkpoints;
        accepted_uops += other.accepted_uops;
        charged_cycles += other.charged_cycles;
        certified_zero_checkpoints += other.certified_zero_checkpoints;
        certified_zero_uops += other.certified_zero_uops;
        nonzero_checkpoints += other.nonzero_checkpoints;
        max_extra_cycles = std::max(max_extra_cycles, other.max_extra_cycles);
        entry_carry_cycles += other.entry_carry_cycles;
        base_advance_cycles += other.base_advance_cycles;
        completion_advance_cycles += other.completion_advance_cycles;
        commit_width_cycles += other.commit_width_cycles;
        completion_load_cycles += other.completion_load_cycles;
        completion_store_cycles += other.completion_store_cycles;
        completion_other_cycles += other.completion_other_cycles;
        pre_issue_cycles += other.pre_issue_cycles;
        post_issue_cycles += other.post_issue_cycles;
        for (std::size_t i = 0; i < completion_by_issue_gate.size(); ++i)
            completion_by_issue_gate[i] += other.completion_by_issue_gate[i];
        for (std::size_t i = 0; i < pre_issue_by_issue_gate.size(); ++i)
            pre_issue_by_issue_gate[i] += other.pre_issue_by_issue_gate[i];
        for (std::size_t i = 0; i < dispatch_pre_issue_by_gate.size(); ++i)
            dispatch_pre_issue_by_gate[i] += other.dispatch_pre_issue_by_gate[i];
        for (std::uint32_t i = 0; i < other.sample_count; ++i)
            retain(other.largest_checkpoints[i]);
        return *this;
    }
};

}  // namespace fastsim
