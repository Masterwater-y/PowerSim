#include "fastsim/interval_core.hpp"
#include "fastsim/trace.hpp"

#include <algorithm>
#include <limits>
#include <numeric>
#include <stdexcept>

namespace fastsim {

namespace {

struct SpeculativeDependencyAudit {
    std::array<std::uint32_t, kStaticRegisterCount> last_writer_depth{};
    std::uint64_t instructions = 0;
    std::uint64_t read_registers = 0;
    std::uint64_t write_registers = 0;
    std::uint64_t memory_instructions = 0;
    std::uint64_t raw_edges = 0;
    std::uint64_t dependent_instructions = 0;
    std::uint64_t chain_depth_sum = 0;
    std::uint64_t chain_depth_max = 0;
};

void observe_speculative_dependencies(
    const StaticInstructionInfo& instruction,
    SpeculativeDependencyAudit& audit) {
    if (!instruction.operand_semantics_valid) return;
    ++audit.instructions;
    if (instruction.is_memory()) ++audit.memory_instructions;
    std::uint32_t predecessor_depth = 0;
    bool dependent = false;
    for (std::size_t id = 0; id < kStaticRegisterCount; ++id) {
        if (instruction.reads_register(id)) {
            ++audit.read_registers;
            if (audit.last_writer_depth[id] != 0) {
                ++audit.raw_edges;
                dependent = true;
                predecessor_depth = std::max(
                    predecessor_depth, audit.last_writer_depth[id]);
            }
        }
    }
    const auto depth = static_cast<std::uint64_t>(predecessor_depth) + 1;
    audit.chain_depth_sum += depth;
    audit.chain_depth_max = std::max(audit.chain_depth_max, depth);
    if (dependent) ++audit.dependent_instructions;
    for (std::size_t id = 0; id < kStaticRegisterCount; ++id) {
        if (!instruction.writes_register(id)) continue;
        ++audit.write_registers;
        audit.last_writer_depth[id] = static_cast<std::uint32_t>(depth);
    }
}

}  // namespace

IntervalCoreModel::IntervalCoreModel(const SimulatorConfig& config)
    : config_(config), l1i_(config.l1i) {
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
    if (config_.dtlb.enabled) {
        architectural_dtlb_lru_.reserve(config_.dtlb.entries * 2u);
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
        // Synthetic per-sysnum cost when enabled and the number is known;
        // otherwise the scalar ABI fallback.  Only on-core service cycles are
        // charged here (see docs/syscall-modeling-dual-cpi.md); blocked/idle
        // time is never represented as execution latency.
        const std::uint32_t service =
            config_.syscall_service_cycles(record.syscall_number());
        return OpTraits{
            FuPool::kSystem,
            static_cast<std::uint32_t>(
                static_cast<std::uint64_t>(config_.system_latency) + service),
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

void IntervalCoreModel::observe_committed_pc(
    const TraceRecord& record, std::uint64_t address_space_id) {
    observe_committed_uop_profile(record);
    if (record.is_memory() &&
        has_flag(record.flags, kVirtualPageToken) &&
        record.virtual_page_token() != 0) {
        const DtlbKey key{address_space_id,
                          record.virtual_page_token()};
        const auto previous = observed_memory_page_.find(record.pc);
        if (previous != observed_memory_page_.end()) {
            ++observed_memory_page_transition_opportunities_[record.pc];
            if (!(previous->second == key)) {
                observed_memory_page_unstable_.insert(record.pc);
                ++observed_memory_page_changes_[record.pc];
            }
        }
        observed_memory_page_[record.pc] = key;
    }
    if (config_.l1i_speculative_path_state &&
        previous_record_completed_macro_) {
        if (previous_macro_valid_) {
            observed_pc_successor_[previous_macro_pc_] = record.pc;
        }
        previous_macro_pc_ = record.pc;
        previous_macro_valid_ = true;
    }
    if (has_flag(record.flags, kBranch) &&
        has_flag(record.flags, kBranchOutcomeValid) &&
        !has_flag(record.flags, kTaken) && record.next_pc != 0) {
        observed_branch_fallthrough_[record.pc] = record.next_pc;
    }
    previous_record_completed_macro_ =
        !has_flag(record.flags, kMicroOp) ||
        has_flag(record.flags, kLastMicroOp);
}

void IntervalCoreModel::observe_committed_uop_profile(
    const TraceRecord& record) {
    if (!record.retires()) return;
    const auto pool = static_cast<std::size_t>(traits(record).pool);
    if (pool >= kSpeculativeProfilePoolCount) {
        throw std::logic_error("functional-unit profile index is invalid");
    }
    const bool micro_op = has_flag(record.flags, kMicroOp);
    if (!micro_op) {
        // A non-microop row is a complete one-UOP macro instruction. Drop an
        // incomplete pending group instead of merging across a malformed
        // producer boundary.
        pending_uop_profile_ = PendingUopProfile{};
        auto& profile = observed_uop_profiles_[record.pc];
        ++profile.instances;
        ++profile.pool_uops[pool];
        return;
    }
    if (!pending_uop_profile_.valid ||
        pending_uop_profile_.pc != record.pc) {
        pending_uop_profile_ = PendingUopProfile{};
        pending_uop_profile_.pc = record.pc;
        pending_uop_profile_.valid = true;
    }
    ++pending_uop_profile_.pool_uops[pool];
    if (!has_flag(record.flags, kLastMicroOp)) return;

    auto& profile = observed_uop_profiles_[pending_uop_profile_.pc];
    ++profile.instances;
    for (std::size_t index = 0; index < profile.pool_uops.size(); ++index) {
        profile.pool_uops[index] += pending_uop_profile_.pool_uops[index];
    }
    pending_uop_profile_ = PendingUopProfile{};
}

void IntervalCoreModel::account_speculative_uop_profile(
    std::uint64_t pc, IntervalTiming& timing) const {
    const auto found = observed_uop_profiles_.find(pc);
    if (found == observed_uop_profiles_.end() ||
        found->second.instances == 0) {
        return;
    }
    ++timing.l1i_speculative_path_profiled_instructions;
    const auto instances = found->second.instances;
    for (std::size_t index = 0;
         index < timing.l1i_speculative_path_profile_uops_q16.size();
         ++index) {
        // Q16 retains the causal mean UOP expansion/resource mix without
        // rounding every speculative macro to a whole UOP.
        timing.l1i_speculative_path_profile_uops_q16[index] +=
            found->second.pool_uops[index] * 65'536ull / instances;
    }
}

void IntervalCoreModel::account_speculative_operands(
    const StaticInstructionInfo& instruction,
    IntervalTiming& timing) const {
    if (!instruction.operand_semantics_valid) return;
    ++timing.l1i_speculative_path_operand_instructions;
    const auto population = [](std::uint64_t value) {
        std::uint64_t count = 0;
        while (value != 0) {
            value &= value - 1;
            ++count;
        }
        return count;
    };
    for (const auto mask : instruction.read_register_mask) {
        timing.l1i_speculative_path_read_registers += population(mask);
    }
    for (const auto mask : instruction.write_register_mask) {
        timing.l1i_speculative_path_write_registers += population(mask);
    }
}

void IntervalCoreModel::access_speculative_dtlb(
    std::uint64_t pc, IntervalTiming& timing) {
    if (!config_.dtlb.speculative_path_state) return;
    ++timing.speculative_dtlb_accesses;
    const auto mapping = observed_memory_page_.find(pc);
    if (mapping == observed_memory_page_.end()) {
        ++timing.speculative_dtlb_untracked;
        return;
    }
    const auto key = mapping->second;
    const auto resident = dtlb_lru_.find(key);
    if (resident != dtlb_lru_.end()) {
        resident->second = ++dtlb_sequence_;
        ++timing.speculative_dtlb_hits;
        return;
    }
    ++timing.speculative_dtlb_misses;
    fill_dtlb(key);
}

void IntervalCoreModel::replay_speculative_l1i_path(
    std::uint64_t entry_pc, std::uint64_t record_budget,
    IntervalTiming& timing, const TraceSource* trace_source,
    const std::vector<std::uint64_t>* speculative_path,
    std::uint64_t profile_uop_budget) {
    SpeculativeDependencyAudit operand_path;
    SpeculativeDependencyAudit operand_rob_prefix;
    const bool dependency_audit_enabled =
        trace_source != nullptr &&
        trace_source->static_instruction_operands_complete();
    constexpr std::uint64_t kOperandQ16Scale = 65'536;
    const auto operand_rob_budget_q16 = profile_uop_budget >
            std::numeric_limits<std::uint64_t>::max() /
                kOperandQ16Scale
        ? std::numeric_limits<std::uint64_t>::max()
        : profile_uop_budget * kOperandQ16Scale;
    std::uint64_t operand_rob_prefix_uops_q16 = 0;
    bool operand_rob_prefix_open = dependency_audit_enabled;
    const auto estimated_profile_uops_q16 = [
        this, kOperandQ16Scale](std::uint64_t pc) {
        const auto found = observed_uop_profiles_.find(pc);
        if (found == observed_uop_profiles_.end() ||
            found->second.instances == 0) {
            return kOperandQ16Scale;
        }
        std::uint64_t total = 0;
        for (const auto pool_uops : found->second.pool_uops) {
            total += pool_uops * kOperandQ16Scale /
                found->second.instances;
        }
        return std::max(kOperandQ16Scale, total);
    };
    const auto account_operands = [
        this, &operand_path, &operand_rob_prefix,
        &operand_rob_prefix_uops_q16, &operand_rob_prefix_open,
        operand_rob_budget_q16, dependency_audit_enabled,
        &estimated_profile_uops_q16](
            std::uint64_t pc, const StaticInstructionInfo& instruction,
            IntervalTiming& result) {
        account_speculative_operands(instruction, result);
        if (!dependency_audit_enabled) return;
        observe_speculative_dependencies(instruction, operand_path);
        if (!operand_rob_prefix_open) return;
        const auto cost_q16 = estimated_profile_uops_q16(pc);
        if (cost_q16 >
            operand_rob_budget_q16 - operand_rob_prefix_uops_q16) {
            operand_rob_prefix_open = false;
            return;
        }
        operand_rob_prefix_uops_q16 += cost_q16;
        observe_speculative_dependencies(instruction, operand_rob_prefix);
    };
    const auto finalize_operand_audit = [
        &timing, &operand_path, &operand_rob_prefix,
        &operand_rob_prefix_uops_q16, dependency_audit_enabled,
        operand_rob_budget_q16]() {
        if (!dependency_audit_enabled || operand_path.instructions == 0) {
            return;
        }
        const auto audit_valid = [](const SpeculativeDependencyAudit& audit) {
            return audit.raw_edges <= audit.read_registers &&
                audit.dependent_instructions <= audit.instructions &&
                audit.chain_depth_sum >= audit.instructions &&
                audit.chain_depth_max <= audit.chain_depth_sum;
        };
        if (!audit_valid(operand_path) ||
            !audit_valid(operand_rob_prefix) ||
            operand_path.instructions !=
                timing.l1i_speculative_path_operand_instructions ||
            operand_path.read_registers !=
                timing.l1i_speculative_path_read_registers ||
            operand_path.write_registers !=
                timing.l1i_speculative_path_write_registers ||
            operand_rob_prefix.instructions > operand_path.instructions ||
            operand_rob_prefix.read_registers >
                operand_path.read_registers ||
            operand_rob_prefix.write_registers >
                operand_path.write_registers ||
            operand_rob_prefix.memory_instructions >
                operand_path.memory_instructions ||
            operand_rob_prefix.raw_edges > operand_path.raw_edges ||
            operand_rob_prefix.dependent_instructions >
                operand_path.dependent_instructions ||
            operand_rob_prefix_uops_q16 > operand_rob_budget_q16) {
            throw std::logic_error(
                "speculative operand dependency audit failed conservation");
        }
        timing.l1i_speculative_path_operand_segments = 1;
        timing.l1i_speculative_path_raw_edges = operand_path.raw_edges;
        timing.l1i_speculative_path_dependent_instructions =
            operand_path.dependent_instructions;
        timing.l1i_speculative_path_chain_depth_sum =
            operand_path.chain_depth_sum;
        timing.l1i_speculative_path_chain_depth_max =
            operand_path.chain_depth_max;
        timing.l1i_speculative_path_operand_rob_prefix_uops_q16 =
            operand_rob_prefix_uops_q16;
        timing.l1i_speculative_path_operand_rob_capped_instructions =
            operand_rob_prefix.instructions;
        timing.l1i_speculative_path_operand_rob_capped_read_registers =
            operand_rob_prefix.read_registers;
        timing.l1i_speculative_path_operand_rob_capped_write_registers =
            operand_rob_prefix.write_registers;
        timing.l1i_speculative_path_operand_rob_capped_memory_instructions =
            operand_rob_prefix.memory_instructions;
        timing.l1i_speculative_path_operand_rob_capped_raw_edges =
            operand_rob_prefix.raw_edges;
        timing
            .l1i_speculative_path_operand_rob_capped_dependent_instructions =
            operand_rob_prefix.dependent_instructions;
        timing.l1i_speculative_path_operand_rob_capped_chain_depth_sum =
            operand_rob_prefix.chain_depth_sum;
        timing.l1i_speculative_path_operand_rob_capped_chain_depth_max =
            operand_rob_prefix.chain_depth_max;
    };
    const auto cap_profile_uops = [&timing, profile_uop_budget]() {
        std::uint64_t raw_q16 = 0;
        for (const auto value :
             timing.l1i_speculative_path_profile_uops_q16) {
            raw_q16 += value;
        }
        const std::uint64_t q16_scale = 65'536;
        const std::uint64_t budget_q16 = profile_uop_budget >
                std::numeric_limits<std::uint64_t>::max() / q16_scale
            ? std::numeric_limits<std::uint64_t>::max()
            : profile_uop_budget * q16_scale;
        timing.l1i_speculative_path_profile_rob_capped_uops_q16 =
            std::min(raw_q16, budget_q16);
    };
    if (speculative_path != nullptr && !speculative_path->empty()) {
        std::uint64_t previous_line = 0;
        bool previous_line_valid = false;
        const auto count = std::min<std::uint64_t>(
            record_budget, speculative_path->size());
        for (std::uint64_t position = 0; position < count; ++position) {
            const auto pc = (*speculative_path)[position];
            ++timing.l1i_speculative_path_records;
            const auto line = pc /
                static_cast<std::uint64_t>(config_.l1i.line_size);
            if (!previous_line_valid || line != previous_line) {
                ++timing.l1i_speculative_path_accesses;
                CacheCounters ignored;
                const auto result = l1i_.access(line, false, ignored);
                if (result.hit) {
                    ++timing.l1i_speculative_path_hits;
                } else {
                    ++timing.l1i_speculative_path_misses;
                }
                if (result.evicted) {
                    ++timing.l1i_speculative_path_evictions;
                }
                previous_line = line;
                previous_line_valid = true;
            }
            const auto* instruction = trace_source == nullptr
                ? nullptr
                : trace_source->static_instruction(pc);
            if (instruction != nullptr) {
                ++timing.l1i_speculative_path_static_instructions;
                account_operands(pc, *instruction, timing);
                account_speculative_uop_profile(pc, timing);
                if (instruction->is_memory()) {
                    ++timing.l1i_speculative_path_memory_instructions;
                    if (observed_memory_page_.count(pc) != 0) {
                        ++timing.l1i_speculative_path_memory_page_known;
                    }
                    if (observed_memory_page_unstable_.count(pc) != 0) {
                        ++timing
                              .l1i_speculative_path_memory_page_unstable;
                    }
                    const auto transitions =
                        observed_memory_page_transition_opportunities_.find(pc);
                    if (transitions !=
                            observed_memory_page_transition_opportunities_.end() &&
                        transitions->second != 0) {
                        ++timing
                              .l1i_speculative_path_memory_page_transition_samples;
                        const auto changes = observed_memory_page_changes_.find(pc);
                        const auto change_count = changes ==
                                observed_memory_page_changes_.end()
                            ? 0
                            : changes->second;
                        timing
                            .l1i_speculative_path_memory_page_transition_score_ppm +=
                            change_count * 1'000'000ull /
                            transitions->second;
                    }
                    access_speculative_dtlb(pc, timing);
                }
            }
        }
        if (count < record_budget && count == speculative_path->size()) {
            timing.l1i_speculative_path_unknown_edge = true;
            const auto last_pc = speculative_path->back();
            const auto* last = trace_source == nullptr
                ? nullptr
                : trace_source->static_instruction(last_pc);
            if (last == nullptr) {
                if (trace_source != nullptr &&
                    trace_source->static_instruction_map_complete()) {
                    ++timing.l1i_speculative_path_static_map_misses;
                }
            } else if (last->is_conditional()) {
                ++timing.l1i_speculative_path_conditional_stops;
            } else if (last->is_indirect()) {
                ++timing.l1i_speculative_path_indirect_stops;
            }
        }
        finalize_operand_audit();
        cap_profile_uops();
        return;
    }

    auto pc = entry_pc;
    std::uint64_t previous_line = 0;
    bool previous_line_valid = false;
    for (std::uint64_t record = 0; record < record_budget; ++record) {
        ++timing.l1i_speculative_path_records;
        const auto line = pc /
            static_cast<std::uint64_t>(config_.l1i.line_size);
        if (!previous_line_valid || line != previous_line) {
            ++timing.l1i_speculative_path_accesses;
            CacheCounters ignored;
            const auto result = l1i_.access(line, false, ignored);
            if (result.hit) {
                ++timing.l1i_speculative_path_hits;
            } else {
                ++timing.l1i_speculative_path_misses;
            }
            if (result.evicted) {
                ++timing.l1i_speculative_path_evictions;
            }
            previous_line = line;
            previous_line_valid = true;
        }
        const auto* instruction = trace_source == nullptr
            ? nullptr
            : trace_source->static_instruction(pc);
        if (instruction != nullptr) {
            ++timing.l1i_speculative_path_static_instructions;
            account_operands(pc, *instruction, timing);
            account_speculative_uop_profile(pc, timing);
            if (instruction->is_memory()) {
                ++timing.l1i_speculative_path_memory_instructions;
                if (observed_memory_page_.count(pc) != 0) {
                    ++timing.l1i_speculative_path_memory_page_known;
                }
                if (observed_memory_page_unstable_.count(pc) != 0) {
                    ++timing.l1i_speculative_path_memory_page_unstable;
                }
                const auto transitions =
                    observed_memory_page_transition_opportunities_.find(pc);
                if (transitions !=
                        observed_memory_page_transition_opportunities_.end() &&
                    transitions->second != 0) {
                    ++timing
                          .l1i_speculative_path_memory_page_transition_samples;
                    const auto changes = observed_memory_page_changes_.find(pc);
                    const auto change_count = changes ==
                            observed_memory_page_changes_.end()
                        ? 0
                        : changes->second;
                    timing
                        .l1i_speculative_path_memory_page_transition_score_ppm +=
                        change_count * 1'000'000ull / transitions->second;
                }
                access_speculative_dtlb(pc, timing);
            }
            if (!instruction->is_branch()) {
                pc = instruction->fallthrough_pc;
                continue;
            }
            if (!instruction->is_conditional() &&
                !instruction->is_indirect() &&
                instruction->has_direct_target()) {
                pc = instruction->direct_target;
                continue;
            }
            // A nested conditional or indirect branch needs a predictor
            // snapshot from before the resolving branch. The static map does
            // not encode an outcome, so stop instead of leaking the committed
            // direction or inventing one.
            if (instruction->is_conditional()) {
                ++timing.l1i_speculative_path_conditional_stops;
            } else if (instruction->is_indirect()) {
                ++timing.l1i_speculative_path_indirect_stops;
            }
            timing.l1i_speculative_path_unknown_edge = true;
            break;
        }
        if (trace_source != nullptr &&
            trace_source->static_instruction_map_complete()) {
            ++timing.l1i_speculative_path_static_map_misses;
        }
        const auto successor = observed_pc_successor_.find(pc);
        if (successor != observed_pc_successor_.end()) {
            pc = successor->second;
            continue;
        }
        timing.l1i_speculative_path_unknown_edge = true;
        break;
    }
    finalize_operand_audit();
    cap_profile_uops();
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

void IntervalCoreModel::fill_dtlb(const DtlbKey& key) {
    const auto found = dtlb_lru_.find(key);
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
    dtlb_lru_.emplace(key, ++dtlb_sequence_);
}

void IntervalCoreModel::fill_architectural_dtlb(const DtlbKey& key) {
    const auto found = architectural_dtlb_lru_.find(key);
    if (found != architectural_dtlb_lru_.end()) {
        found->second = ++architectural_dtlb_sequence_;
        return;
    }
    if (architectural_dtlb_lru_.size() == config_.dtlb.entries) {
        const auto victim = std::min_element(
            architectural_dtlb_lru_.begin(),
            architectural_dtlb_lru_.end(),
            [](const auto& left, const auto& right) {
                return left.second < right.second;
            });
        if (victim == architectural_dtlb_lru_.end()) {
            throw std::logic_error(
                "nonempty architectural DTLB has no LRU victim");
        }
        architectural_dtlb_lru_.erase(victim);
    }
    architectural_dtlb_lru_.emplace(
        key, ++architectural_dtlb_sequence_);
}

void IntervalCoreModel::activate_address_space(
    std::uint64_t address_space_id) {
    if (!active_address_space_valid_) {
        active_address_space_id_ = address_space_id;
        active_address_space_valid_ = true;
        return;
    }
    if (active_address_space_id_ == address_space_id) return;

    // The target gem5 x86 ISA calls flushNonGlobal() on every CR3 write.
    // FastSim does not classify global translations, so its supported subset
    // flushes all modeled DTLB entries and all not-yet-installed walk results.
    architectural_dtlb_lru_.clear();
    dtlb_lru_.clear();
    pending_page_walks_.clear();
    page_walk_completions_ = decltype(page_walk_completions_){};

    // These committed-stream maps are speculative-path diagnostics keyed by
    // virtual PC.  Clearing avoids borrowing a prior process's PC/page or
    // static successor facts until the instruction-map format is AS-scoped.
    observed_pc_successor_.clear();
    observed_branch_fallthrough_.clear();
    observed_memory_page_.clear();
    observed_memory_page_unstable_.clear();
    observed_memory_page_transition_opportunities_.clear();
    observed_memory_page_changes_.clear();
    observed_uop_profiles_.clear();
    pending_uop_profile_ = PendingUopProfile{};
    previous_macro_valid_ = false;
    previous_record_completed_macro_ = true;
    active_address_space_id_ = address_space_id;
}

void IntervalCoreModel::retire_page_walks_through(std::uint64_t cycle) {
    while (!page_walk_completions_.empty() &&
           page_walk_completions_.top().first <= cycle) {
        const auto [ready, key] = page_walk_completions_.top();
        page_walk_completions_.pop();
        if (config_.dtlb.coalesce_misses) {
            const auto pending = pending_page_walks_.find(key);
            if (pending == pending_page_walks_.end() ||
                pending->second != ready) {
                continue;
            }
            pending_page_walks_.erase(pending);
        }
        fill_dtlb(key);
    }
}

std::uint64_t IntervalCoreModel::translate(
    const TraceRecord& record, std::uint64_t earliest,
    IntervalTiming& timing, std::uint64_t address_space_id) {
    timing.translation_ready_cycle = earliest;
    if (!config_.dtlb.enabled || !record.is_memory()) return earliest;

    timing.dtlb_access = true;
    timing.dtlb_timing_access = true;
    if (!has_flag(record.flags, kVirtualPageToken) ||
        record.virtual_page_token() == 0) {
        timing.dtlb_untracked = true;
        timing.dtlb_timing_untracked = true;
        return earliest;
    }

    const DtlbKey key{address_space_id,
                      record.virtual_page_token()};
    const auto architectural_resident =
        architectural_dtlb_lru_.find(key);
    if (architectural_resident != architectural_dtlb_lru_.end()) {
        architectural_resident->second = ++architectural_dtlb_sequence_;
        timing.dtlb_hit = true;
    } else {
        timing.dtlb_miss = true;
        fill_architectural_dtlb(key);
    }

    if (config_.dtlb.miss_model == "se_atomic") {
        // Preserve the historical SE contract exactly: architectural hits
        // pay the configured lookup latency; misses fill functionally and do
        // not synthesize an unavailable full-system page walk.
        timing.dtlb_timing_hit = timing.dtlb_hit;
        timing.dtlb_timing_miss = timing.dtlb_miss;
        if (timing.dtlb_hit) {
            timing.translation_ready_cycle =
                earliest + config_.dtlb.hit_latency;
            timing.translation_delay_cycles = config_.dtlb.hit_latency;
        }
        return timing.translation_ready_cycle;
    }

    retire_page_walks_through(earliest);
    const auto resident = dtlb_lru_.find(key);
    if (resident != dtlb_lru_.end()) {
        resident->second = ++dtlb_sequence_;
        timing.dtlb_timing_hit = true;
        timing.translation_ready_cycle =
            earliest + config_.dtlb.hit_latency;
        timing.translation_delay_cycles = config_.dtlb.hit_latency;
        return timing.translation_ready_cycle;
    }

    timing.dtlb_timing_miss = true;
    if (config_.dtlb.coalesce_misses) {
        const auto pending = pending_page_walks_.find(key);
        if (pending != pending_page_walks_.end()) {
            timing.dtlb_timing_merged_miss = true;
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
        pending_page_walks_.emplace(key, ready);
    }
    page_walk_completions_.emplace(ready, key);
    timing.translation_ready_cycle = ready;
    timing.translation_delay_cycles = ready - earliest;
    return ready;
}

IntervalTiming IntervalCoreModel::schedule(
    const TraceRecord& record, bool branch_miss,
    bool predicted_taken, std::uint64_t predicted_target,
    bool predicted_target_available, const TraceSource* trace_source,
    const std::vector<std::uint64_t>* speculative_path,
    std::uint64_t address_space_id) {
    activate_address_space(address_space_id);
    const auto index = completion_.size();
    IntervalTiming timing;
    if (config_.l1i_enabled &&
        (config_.l1i_speculative_entry_state ||
         config_.l1i_speculative_path_state)) {
        observe_committed_pc(record, address_space_id);
    }
    auto fetch_earliest =
        std::max(frontend_ready_cycle_, serial_ready_cycle_);
    std::uint64_t fetch_queue_ready = 0;
    if (index >= config_.fetch_queue_entries) {
        fetch_queue_ready =
            dispatch_history_[index - config_.fetch_queue_entries];
    }
    if (config_.fetch_buffer_bytes != 0) {
        const auto block = record.pc /
            static_cast<std::uint64_t>(config_.fetch_buffer_bytes);
        const bool new_block =
            !fetch_buffer_valid_ || block != fetch_buffer_block_;
        if (new_block && config_.l1i_enabled) {
            timing.l1i_access = true;
            CacheCounters ignored;
            const auto result = l1i_.access(block, false, ignored);
            timing.l1i_hit = result.hit;
            timing.l1i_miss = !result.hit;
            timing.l1i_eviction = result.evicted;
            if (!result.hit) {
                timing.l1i_miss_stall_cycles =
                    config_.l1i_miss_penalty;
            }
        }
        if (fetch_buffer_valid_ && new_block) {
            // A new block is requested after the current fetch cycle. The
            // configured latency is the number of intervening empty cycles,
            // so even a zero-latency block switch begins next cycle.
            const auto refill_latency = config_.l1i_enabled
                ? config_.l1i.hit_latency
                : config_.fetch_buffer_refill_latency;
            timing.fetch_buffer_transition = true;
            timing.fetch_buffer_refill_delay_cycles = refill_latency;
            timing.fetch_block_request_cycle = fetch_cycle_ + 1;
            timing.fetch_block_response_wait_cycles =
                refill_latency + timing.l1i_miss_stall_cycles;
            timing.fetch_block_response_cycle =
                timing.fetch_block_request_cycle +
                timing.fetch_block_response_wait_cycles;
            const auto fetch_bandwidth_ready =
                fetches_this_cycle_ == config_.fetch_width
                    ? fetch_cycle_ + 1
                    : fetch_cycle_;
            const auto other_ready = std::max(
                {frontend_ready_cycle_, serial_ready_cycle_,
                 fetch_queue_ready, fetch_bandwidth_ready});
            const auto exposed_begin = std::max(
                timing.fetch_block_request_cycle, other_ready);
            timing.fetch_block_response_exposed_cycles =
                timing.fetch_block_response_cycle > exposed_begin
                    ? timing.fetch_block_response_cycle - exposed_begin
                    : 0;
            timing.fetch_block_response_hidden_cycles =
                timing.fetch_block_response_wait_cycles -
                timing.fetch_block_response_exposed_cycles;
            fetch_earliest = std::max(
                fetch_earliest,
                timing.fetch_block_response_cycle);
        } else if (!fetch_buffer_valid_ && config_.l1i_enabled) {
            // The first request has no preceding fetch group, but still
            // waits for the target-visible L1I response. Functional warmup
            // normally removes this cold-start latency from measurement.
            fetch_earliest += config_.l1i.hit_latency +
                timing.l1i_miss_stall_cycles;
        }
        fetch_buffer_block_ = block;
        fetch_buffer_valid_ = true;
    }
    fetch_earliest = std::max(fetch_earliest, fetch_queue_ready);
    timing.fetch_cycle = allocate_stage(
        fetch_earliest, config_.fetch_width,
        fetch_cycle_, fetches_this_cycle_);
    if (timing.fetch_buffer_transition) {
        timing.fetch_block_response_to_resume_cycles =
            timing.fetch_cycle > timing.fetch_block_response_cycle
                ? timing.fetch_cycle - timing.fetch_block_response_cycle
                : 0;
        timing.fetch_block_request_to_resume_cycles =
            timing.fetch_cycle - timing.fetch_block_request_cycle;
    }
    timing.decode_cycle = allocate_stage(
        timing.fetch_cycle + config_.fetch_to_decode,
        config_.decode_width, decode_cycle_, decodes_this_cycle_);
    const auto nominal_rename =
        timing.decode_cycle + config_.decode_to_rename;
    auto rename_earliest = std::max(
        nominal_rename, branch_shadow_rename_ready_cycle_);
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
            "core.rename_free_list requires FST destination class counts");
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
        const auto rob_ready = retirement_[
            index - config_.rob_entries] +
            config_.commit_to_rename +
            config_.rename_to_dispatch;
        if (rob_ready > dispatch_earliest) {
            dispatch_earliest = rob_ready;
            dispatch_gate = DispatchGate::kRob;
        }
    }
    release_iq_through(dispatch_earliest);
    if (iq_occupancy_ >= config_.iq_entries) {
        const auto iq_ready = next_iq_release_cycle() +
            config_.iew_to_rename +
            config_.rename_to_dispatch;
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
            const auto visible_queue_ready = queue_ready +
                config_.iew_to_rename +
                config_.rename_to_dispatch;
            if (visible_queue_ready > dispatch_earliest) {
                dispatch_earliest = visible_queue_ready;
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
        translate(record, dependency_ready, timing, address_space_id);
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
        if (config_.branch.shadow_rob &&
            config_.branch.squash_width != 0) {
            const auto frontend_width = std::min(
                {config_.fetch_width, config_.decode_width,
                 config_.rename_width});
            const auto speculative_cycles =
                timing.completion_cycle > timing.fetch_cycle
                    ? timing.completion_cycle - timing.fetch_cycle
                    : 0;
            std::uint64_t occupied_rob = 1;
            const auto rob_history_begin =
                retirement_.size() > config_.rob_entries
                    ? retirement_.size() - config_.rob_entries
                    : 0;
            for (auto older = rob_history_begin;
                 older < retirement_.size(); ++older) {
                if (dispatch_history_[older] <=
                        timing.completion_cycle &&
                    retirement_[older] > timing.completion_cycle) {
                    ++occupied_rob;
                }
            }
            const auto available_rob =
                occupied_rob < config_.rob_entries
                    ? config_.rob_entries - occupied_rob
                    : 0;
            const auto shadow_uops = std::min<std::uint64_t>(
                available_rob,
                speculative_cycles *
                    static_cast<std::uint64_t>(frontend_width));
            const auto shadow_cycles =
                (shadow_uops + config_.branch.squash_width - 1) /
                config_.branch.squash_width;
            const auto correct_path_decode =
                frontend_ready_cycle_ +
                static_cast<std::uint64_t>(config_.fetch_to_decode) +
                config_.decode_to_rename;
            branch_shadow_rename_ready_cycle_ = std::max(
                branch_shadow_rename_ready_cycle_,
                correct_path_decode + shadow_cycles);
            timing.branch_shadow_uops = shadow_uops;
            timing.branch_shadow_cycles = shadow_cycles;
        }
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
            // Mirror the service cycles actually charged in traits() so the
            // audit total matches the synthetic cost model when enabled.
            const std::uint32_t service =
                config_.syscall_service_cycles(record.syscall_number());
            timing.syscall_service_cycles = service;
            timing.syscall_restart_cycles = restart;
        }
    }
    if (branch_miss && config_.l1i_enabled &&
        (config_.l1i_speculative_entry_state ||
         config_.l1i_speculative_path_state)) {
        std::uint64_t speculative_entry = 0;
        bool speculative_entry_valid = false;
        // The predictor constructs this path before squash repair from the
        // same direction/target snapshot used for branch_miss.  Its first PC
        // is therefore the exact speculative entry, including a statically
        // decoded fallthrough that has not appeared in committed history yet.
        // Prefer that causal handoff instead of independently requiring the
        // interval model to have observed the fallthrough already.
        if (speculative_path != nullptr && !speculative_path->empty()) {
            speculative_entry = speculative_path->front();
            speculative_entry_valid = true;
        } else if (predicted_taken && predicted_target_available) {
            speculative_entry = predicted_target;
            speculative_entry_valid = true;
        } else if (!predicted_taken) {
            const auto fallthrough =
                observed_branch_fallthrough_.find(record.pc);
            if (fallthrough != observed_branch_fallthrough_.end()) {
                speculative_entry = fallthrough->second;
                speculative_entry_valid = true;
            }
        }
        if (speculative_entry_valid &&
            config_.l1i_speculative_path_state) {
            const auto resolution_cycles =
                timing.completion_cycle > timing.fetch_cycle
                    ? timing.completion_cycle - timing.fetch_cycle
                    : 1;
            const auto fetch_budget = resolution_cycles >
                    std::numeric_limits<std::uint64_t>::max() /
                        config_.fetch_width
                ? std::numeric_limits<std::uint64_t>::max()
                : resolution_cycles * config_.fetch_width;
            // Older committed UOPs still alive at branch resolution plus
            // the branch itself occupy ROB slots. Remaining slots are a
            // causal upper bound on wrong-path UOPs present at squash. The
            // operand prefix is also capped by the resolution-time fetch UOP
            // budget below; neither UOP budget may be reinterpreted as a
            // macro-instruction count. This is diagnostic only and does not
            // allocate resources or cycles.
            std::uint64_t occupied_rob = 1;
            const auto rob_history_begin = index >
                    config_.rob_entries
                ? index - config_.rob_entries
                : 0;
            for (auto older = rob_history_begin;
                 older < index; ++older) {
                if (dispatch_history_[older] <= timing.completion_cycle &&
                    retirement_[older] > timing.completion_cycle) {
                    ++occupied_rob;
                }
            }
            const auto available_rob = occupied_rob < config_.rob_entries
                ? config_.rob_entries - occupied_rob
                : 0;
            replay_speculative_l1i_path(
                speculative_entry,
                std::min<std::uint64_t>(
                    config_.rob_entries,
                    std::max<std::uint64_t>(1, fetch_budget)),
                timing, trace_source, speculative_path,
                std::min(available_rob, fetch_budget));
        } else if (speculative_entry_valid) {
            timing.l1i_speculative_entry_access = true;
            CacheCounters ignored;
            const auto predicted_line = speculative_entry /
                static_cast<std::uint64_t>(config_.l1i.line_size);
            const auto result = l1i_.access(
                predicted_line, false, ignored);
            timing.l1i_speculative_entry_hit = result.hit;
            timing.l1i_speculative_entry_miss = !result.hit;
            timing.l1i_speculative_entry_eviction = result.evicted;
        } else {
            // A not-taken prediction needs a causally observed exact x86
            // fallthrough. Fail closed instead of assuming pc+4.
            timing.l1i_speculative_entry_untracked = true;
        }
    }
    return timing;
}

void IntervalCoreModel::inject_kernel_pause(
    std::uint32_t active_cycles) {
    if (active_cycles == 0) return;
    const auto anchor = std::max(
        {last_retire_cycle_, frontend_ready_cycle_, serial_ready_cycle_});
    if (active_cycles >
        std::numeric_limits<std::uint64_t>::max() - anchor) {
        throw std::overflow_error("kernel pause exceeds cycle range");
    }
    const auto ready = anchor + active_cycles;
    frontend_ready_cycle_ = ready;
    serial_ready_cycle_ = ready;
    branch_shadow_rename_ready_cycle_ = std::max(
        branch_shadow_rename_ready_cycle_, ready);
}

void IntervalCoreModel::reset_measurement_audit() {
    if (!config_.committed_pipeline_audit &&
        !config_.rename_free_list) return;

    // The warmup phase is run to completion before the common measurement
    // barrier. Release records are normally consumed lazily by the next
    // rename, so drain them explicitly here before zeroing their counters.
    // Keeping the completion/retirement/dependency histories is intentional:
    // a producer distance at the first measurement UOP may still cross the
    // marker and cache/predictor/resource state must remain warm. The finite
    // rename free list itself is empty at this fully retired barrier, so its
    // warmup allocations must not leak into measurement.
    audit_destination_releases_through(last_retire_cycle_);
    destination_class_releases_through(last_retire_cycle_);
    if (!destination_releases_.empty() ||
        !destination_class_releases_.empty() ||
        live_destination_tokens_ != 0 ||
        std::any_of(
            live_destination_class_tokens_.begin(),
            live_destination_class_tokens_.end(),
            [](std::uint64_t count) { return count != 0; })) {
        throw std::logic_error(
            "functional warmup ended with live committed destinations");
    }
    committed_pipeline_audit_ = CommittedPipelineAuditCounters{};
}

}  // namespace fastsim
