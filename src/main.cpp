#include <algorithm>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <utility>

#include "fastsim/config.hpp"
#include "fastsim/simulator.hpp"
#include "fastsim/trace.hpp"

namespace {

using Args = std::unordered_map<std::string, std::string>;

constexpr std::uint64_t kSyscallTransitionExtraUserUops = 24;
constexpr const char* kPmuContractId =
    "perf-gem5-fastsim-x86-fs-v1";

std::uint64_t speculative_profile_uops_q16(
    const fastsim::CoreCounters& counters) {
    std::uint64_t total = 0;
    for (const auto value :
         counters.l1i_speculative_path_profile_uops_q16) {
        total += value;
    }
    return total;
}

Args parse_args(int argc, char** argv, int start) {
    Args result;
    for (int index = start; index < argc; ++index) {
        const std::string key(argv[index]);
        if (key.rfind("--", 0) != 0) {
            throw std::invalid_argument("unexpected positional argument: " +
                                        key);
        }
        if (index + 1 >= argc ||
            std::string(argv[index + 1]).rfind("--", 0) == 0) {
            result[key.substr(2)] = "true";
        } else {
            result[key.substr(2)] = argv[++index];
        }
    }
    return result;
}

std::string require(const Args& args, const std::string& key) {
    const auto it = args.find(key);
    if (it == args.end()) {
        throw std::invalid_argument("missing required option --" + key);
    }
    return it->second;
}

std::uint64_t u64(const Args& args, const std::string& key,
                  std::uint64_t fallback) {
    const auto it = args.find(key);
    if (it == args.end()) return fallback;
    std::size_t consumed = 0;
    const auto value = std::stoull(it->second, &consumed, 0);
    if (consumed != it->second.size()) {
        throw std::invalid_argument("invalid integer for --" + key);
    }
    return value;
}

std::uint32_t u32(const Args& args, const std::string& key,
                  std::uint32_t fallback) {
    const auto value = u64(args, key, fallback);
    if (value > 0xffffffffull) {
        throw std::invalid_argument("--" + key + " exceeds uint32");
    }
    return static_cast<std::uint32_t>(value);
}

double floating(const Args& args, const std::string& key,
                double fallback) {
    const auto it = args.find(key);
    if (it == args.end()) return fallback;
    std::size_t consumed = 0;
    const auto value = std::stod(it->second, &consumed);
    if (consumed != it->second.size()) {
        throw std::invalid_argument("invalid floating point for --" + key);
    }
    return value;
}

bool boolean(const Args& args, const std::string& key, bool fallback) {
    const auto it = args.find(key);
    if (it == args.end()) return fallback;
    if (it->second == "true" || it->second == "1" ||
        it->second == "yes" || it->second == "on") {
        return true;
    }
    if (it->second == "false" || it->second == "0" ||
        it->second == "no" || it->second == "off") {
        return false;
    }
    throw std::invalid_argument("invalid boolean for --" + key);
}

std::string text_option(const Args& args, const std::string& key,
                        std::string fallback) {
    const auto it = args.find(key);
    return it == args.end() ? std::move(fallback) : it->second;
}

fastsim::MeasurementScope measurement_scope_option(
    const Args& args, fastsim::MeasurementScope fallback) {
    const auto it = args.find("measurement-scope");
    return it == args.end()
               ? fallback
               : fastsim::parse_measurement_scope(it->second);
}

void require_concrete_measurement_scope(
    fastsim::MeasurementScope scope) {
    if (scope == fastsim::MeasurementScope::kUnspecified) {
        throw std::invalid_argument(
            "missing required --measurement-scope "
            "(user or user-plus-kernel); alternatively set "
            "measurement.scope in the config");
    }
}

double ratio(std::uint64_t numerator, std::uint64_t denominator) {
    return denominator == 0
               ? 0.0
               : static_cast<double>(numerator) /
                     static_cast<double>(denominator);
}

bool has_p0_kernel_profile_contract(const fastsim::SimulatorConfig& config) {
    bool has_enabled_profile = false;
    if (config.syscall_kernel_event_model) {
        has_enabled_profile = true;
        if (!config.syscall_kernel_event_default_profile_enabled ||
            config.syscall_kernel_event_default_profile.encoding_fields != 22) {
            return false;
        }
        for (const auto& [sysnum, profile] :
             config.syscall_kernel_event_table) {
            (void)sysnum;
            if (profile.encoding_fields != 22) return false;
        }
    } else if (config.syscall_cost_model ||
               config.syscall_service_latency != 0) {
        return false;
    }
    if (config.page_fault_event_model) {
        has_enabled_profile = true;
        if (config.page_fault_event_profile.encoding_fields != 22) {
            return false;
        }
    }
    if (config.irq_event_model) {
        has_enabled_profile = true;
        if (config.irq_event_profile.encoding_fields != 22) return false;
    }
    return has_enabled_profile;
}

const char* replacement_name(fastsim::ReplacementPolicy policy) {
    return policy == fastsim::ReplacementPolicy::kLru
               ? "lru"
               : "tree_plru";
}

void write_cache_config(
    std::ostream& out, const fastsim::CacheConfig& cache) {
    out << "{\"size_bytes\": " << cache.size_bytes
        << ", \"associativity\": " << cache.associativity
        << ", \"line_size\": " << cache.line_size
        << ", \"hit_latency\": " << cache.hit_latency
        << ", \"replacement\": \"" << replacement_name(cache.replacement)
        << "\"}";
}

void write_kernel_event_counters(
    std::ostream& out, const fastsim::KernelEventCounters& counters) {
    out << "{\"events\": " << counters.events
        << ", \"active_cycles\": " << counters.active_cycles
        << ", \"blocked_wall_cycles\": "
        << counters.blocked_wall_cycles
        << ", \"retired_instructions\": "
        << counters.retired_instructions
        << ", \"retired_uops\": " << counters.retired_uops
        << ", \"memory_uops\": " << counters.memory_uops
        << ", \"line_requests\": " << counters.line_requests
        << ", \"branches\": " << counters.branch.branches
        << ", \"branch_misses\": " << counters.branch.misses
        << ", \"l1d_accesses\": " << counters.l1d.accesses
        << ", \"l1d_hits\": " << counters.l1d.hits
        << ", \"l1d_misses\": " << counters.l1d.misses
        << ", \"l2_accesses\": " << counters.l2.accesses
        << ", \"l2_hits\": " << counters.l2.hits
        << ", \"l2_misses\": " << counters.l2.misses
        << ", \"llc_accesses\": " << counters.llc.accesses
        << ", \"llc_hits\": " << counters.llc.hits
        << ", \"llc_misses\": " << counters.llc.misses
        << ", \"permission_upgrades\": "
        << counters.permission_upgrades
        << ", \"remote_supplies\": " << counters.remote_supplies
        << ", \"llc_merged_misses\": "
        << counters.llc_merged_misses
        << ", \"llc_unique_fills\": " << counters.llc_unique_fills
        << ", \"dram_reads\": " << counters.dram_reads
        << ", \"dram_writes\": " << counters.dram_writes
        << ", \"dtlb_accesses\": " << counters.dtlb.accesses
        << ", \"dtlb_hits\": " << counters.dtlb.hits
        << ", \"dtlb_misses\": " << counters.dtlb.misses << "}";
}

void write_branch_population_audit(
    std::ostream& out,
    const fastsim::BranchPopulationAuditCounters& counters) {
    out << "{\"conserved\": "
        << (counters.conserved() ? "true" : "false")
        << ", \"miss_events\": " << counters.miss_events
        << ", \"history_ready_events\": "
        << counters.history_ready_events
        << ", \"history_unavailable_events\": "
        << counters.history_unavailable_events
        << ", \"predicted_path_covered_events\": "
        << counters.predicted_path_covered_events
        << ", \"predicted_path_unavailable_events\": "
        << counters.predicted_path_unavailable_events
        << ", \"predicted_path_records\": "
        << counters.predicted_path_records
        << ", \"predicted_path_covered_uops\": "
        << counters.predicted_path_covered_uops
        << ", \"resolution_cycles\": "
        << counters.resolution_cycles
        << ", \"resolution_cycles_max\": "
        << counters.resolution_cycles_max
        << ", \"older_live_uops\": " << counters.older_live_uops
        << ", \"rob_free_uops\": " << counters.rob_free_uops
        << ", \"supply_history_uops\": "
        << counters.supply_history_uops
        << ", \"supply_history_cycles\": "
        << counters.supply_history_cycles
        << ", \"supply_history_fetch_requests\": "
        << counters.supply_history_fetch_requests
        << ", \"supply_history_fetch_response_cycles\": "
        << counters.supply_history_fetch_response_cycles
        << ", \"supply_budget_uops\": "
        << counters.supply_budget_uops
        << ", \"estimated_squashed_uops\": "
        << counters.estimated_squashed_uops
        << ", \"estimated_squashed_uops_max\": "
        << counters.estimated_squashed_uops_max
        << ", \"estimated_rob_residency_uop_cycles\": "
        << counters.estimated_rob_residency_uop_cycles
        << ", \"rob_limited_events\": "
        << counters.rob_limited_events
        << ", \"supply_limited_events\": "
        << counters.supply_limited_events
        << ", \"zero_window_events\": "
        << counters.zero_window_events << "}";
}

void write_page_fault_allocation_by_syscall(
    std::ostream& out,
    const std::map<std::uint64_t,
                   fastsim::PageFaultAllocationCandidateCounters>& table) {
    out << "[";
    bool first = true;
    for (const auto& [sysnum, counters] : table) {
        if (!first) out << ", ";
        first = false;
        out << "{\"sysnum\": " << sysnum
            << ", \"recency_candidates\": [";
        for (std::size_t index = 0;
             index < counters.recency_candidates.size(); ++index) {
            if (index != 0) out << ", ";
            out << counters.recency_candidates[index];
        }
        out << "], \"recency_write_candidates\": [";
        for (std::size_t index = 0;
             index < counters.recency_write_candidates.size(); ++index) {
            if (index != 0) out << ", ";
            out << counters.recency_write_candidates[index];
        }
        out << "]}";
    }
    out << "]";
}

void write_user_functional_pmu(
    std::ostream& out, const fastsim::CoreCounters& counters,
    const fastsim::CacheCounters& llc, const fastsim::ChaCounters& cha) {
    // The deployable FS trace collapses gem5/x86's 25 committed user-decoded
    // syscall transition UOPs into one serial marker. Restore that fixed PMU
    // footprint without changing the one-marker CPI denominator or replay
    // work. The transition macro is also one user-scoped control operation.
    const auto pmu_retired_uops =
        counters.retired_uops +
        counters.syscall_uops * kSyscallTransitionExtraUserUops;
    const auto pmu_branches =
        counters.branch.branches + counters.syscall_uops;
    out << "{\"retired_instructions\": "
        << counters.retired_instructions
        << ", \"retired_uops\": " << pmu_retired_uops
        << ", \"memory_uops\": " << counters.memory_uops
        << ", \"line_requests\": " << counters.memory_accesses
        << ", \"branches\": " << pmu_branches
        << ", \"branch_misses\": " << counters.branch.misses
        << ", \"l1d_accesses\": " << counters.l1d.accesses
        << ", \"l1d_hits\": " << counters.l1d.hits
        << ", \"l1d_misses\": " << counters.l1d.misses
        << ", \"l1d_tag_accesses\": " << counters.l1d.accesses
        << ", \"l1d_tag_hits\": " << counters.l1d.hits
        << ", \"l1d_tag_misses\": " << counters.l1d.misses
        << ", \"l2_accesses\": " << counters.l2.accesses
        << ", \"l2_hits\": " << counters.l2.hits
        << ", \"l2_misses\": " << counters.l2.misses
        << ", \"private_l2_tag_accesses\": " << counters.l2.accesses
        << ", \"private_l2_tag_hits\": " << counters.l2.hits
        << ", \"private_l2_tag_misses\": " << counters.l2.misses
        << ", \"llc_accesses\": " << llc.accesses
        << ", \"llc_hits\": " << llc.hits
        << ", \"llc_misses\": " << llc.misses
        << ", \"llc_tag_accesses\": " << llc.accesses
        << ", \"llc_tag_hits\": " << llc.hits
        << ", \"llc_tag_misses\": " << llc.misses
        << ", \"permission_upgrades\": " << cha.upgrades
        << ", \"remote_supplies\": " << cha.remote_supplies
        << ", \"llc_merged_misses\": " << cha.llc_merged_misses
        << ", \"llc_unique_fills\": " << cha.llc_unique_fills
        << ", \"dram_reads\": " << cha.dram_reads
        << ", \"dram_writes\": " << cha.dram_writes
        << ", \"dtlb_accesses\": " << counters.dtlb.accesses
        << ", \"dtlb_hits\": " << counters.dtlb.hits
        << ", \"dtlb_misses\": " << counters.dtlb.misses << "}";
}

void write_user_plus_kernel_pmu(
    std::ostream& out, const fastsim::CoreCounters& user,
    const fastsim::CacheCounters& user_llc,
    const fastsim::ChaCounters& user_cha,
    const fastsim::KernelEventCounters& kernel,
    bool include_timing_diagnostics) {
    out << "{";
    if (include_timing_diagnostics) {
        out << "\"sum_core_cycles\": " << user.cycles
            << ", \"synthetic_kernel_active_cycles\": "
            << kernel.active_cycles
            << ", \"blocked_wall_cycles\": "
            << kernel.blocked_wall_cycles << ", ";
    }
    out << "\"retired_instructions\": "
        << user.retired_instructions + kernel.retired_instructions
        << ", \"retired_uops\": "
        << user.retired_uops +
               user.syscall_uops * kSyscallTransitionExtraUserUops +
               kernel.retired_uops
        << ", \"memory_uops\": "
        << user.memory_uops + kernel.memory_uops
        << ", \"line_requests\": "
        << user.memory_accesses + kernel.line_requests
        << ", \"branches\": "
        << user.branch.branches + user.syscall_uops +
               kernel.branch.branches
        << ", \"branch_misses\": "
        << user.branch.misses + kernel.branch.misses
        << ", \"l1d_accesses\": "
        << user.l1d.accesses + kernel.l1d.accesses
        << ", \"l1d_hits\": " << user.l1d.hits + kernel.l1d.hits
        << ", \"l1d_misses\": "
        << user.l1d.misses + kernel.l1d.misses
        << ", \"l1d_tag_accesses\": "
        << user.l1d.accesses + kernel.l1d.accesses
        << ", \"l1d_tag_hits\": "
        << user.l1d.hits + kernel.l1d.hits
        << ", \"l1d_tag_misses\": "
        << user.l1d.misses + kernel.l1d.misses
        << ", \"l2_accesses\": "
        << user.l2.accesses + kernel.l2.accesses
        << ", \"l2_hits\": " << user.l2.hits + kernel.l2.hits
        << ", \"l2_misses\": "
        << user.l2.misses + kernel.l2.misses
        << ", \"private_l2_tag_accesses\": "
        << user.l2.accesses + kernel.l2.accesses
        << ", \"private_l2_tag_hits\": "
        << user.l2.hits + kernel.l2.hits
        << ", \"private_l2_tag_misses\": "
        << user.l2.misses + kernel.l2.misses
        << ", \"llc_accesses\": "
        << user_llc.accesses + kernel.llc.accesses
        << ", \"llc_hits\": " << user_llc.hits + kernel.llc.hits
        << ", \"llc_misses\": "
        << user_llc.misses + kernel.llc.misses
        << ", \"llc_tag_accesses\": "
        << user_llc.accesses + kernel.llc.accesses
        << ", \"llc_tag_hits\": "
        << user_llc.hits + kernel.llc.hits
        << ", \"llc_tag_misses\": "
        << user_llc.misses + kernel.llc.misses
        << ", \"permission_upgrades\": "
        << user_cha.upgrades + kernel.permission_upgrades
        << ", \"remote_supplies\": "
        << user_cha.remote_supplies + kernel.remote_supplies
        << ", \"llc_merged_misses\": "
        << user_cha.llc_merged_misses + kernel.llc_merged_misses
        << ", \"llc_unique_fills\": "
        << user_cha.llc_unique_fills + kernel.llc_unique_fills
        << ", \"dram_reads\": "
        << user_cha.dram_reads + kernel.dram_reads
        << ", \"dram_writes\": "
        << user_cha.dram_writes + kernel.dram_writes
        << ", \"dtlb_accesses\": "
        << user.dtlb.accesses + kernel.dtlb.accesses
        << ", \"dtlb_hits\": " << user.dtlb.hits + kernel.dtlb.hits
        << ", \"dtlb_misses\": "
        << user.dtlb.misses + kernel.dtlb.misses << "}";
}

void write_user_memory_hierarchy(
    std::ostream& out, const fastsim::CacheCounters& llc,
    const fastsim::ChaCounters& cha) {
    // These names deliberately distinguish demand/tag events from protocol
    // requests and unique memory transactions. This diagnostic object is
    // explicitly the functional user stream; scope_metrics.pmu separately
    // adds the synthetic kernel hierarchy profile for the combined scope.
    out << "{\"coverage_scope\":\"user\""
        << ",\"shared_requests\":" << cha.requests
        << ",\"llc_outcomes_conserved\":"
        << (cha.llc_outcomes_conserved() &&
                    llc.accesses == cha.requests &&
                    llc.hits == cha.llc_hits &&
                    llc.misses == cha.llc_misses
                ? "true"
                : "false")
        << ",\"permission_upgrades\":" << cha.upgrades
        << ",\"remote_supplies\":" << cha.remote_supplies
        << ",\"llc_tag_accesses\":" << llc.accesses
        << ",\"llc_tag_hits\":" << llc.hits
        << ",\"llc_tag_misses\":" << llc.misses
        << ",\"llc_merged_misses\":" << cha.llc_merged_misses
        << ",\"llc_unique_fills\":" << cha.llc_unique_fills
        << ",\"dram_reads\":" << cha.dram_reads
        << ",\"dram_writes\":" << cha.dram_writes
        << "}";
}

void write_committed_pipeline_audit(
    std::ostream& out,
    const fastsim::CommittedPipelineAuditCounters& audit) {
    out << "{\"uops\": " << audit.uops
        << ", \"destination_uops\": " << audit.destination_uops
        << ", \"destination_tokens\": " << audit.destination_tokens
        << ", \"destination_release_tokens\": "
        << audit.destination_release_tokens
        << ", \"destination_tokens_released\": "
        << audit.destination_tokens_released
        << ", \"destination_tokens_live_at_last_rename\": "
        << audit.destination_tokens_live_at_last_rename
        << ", \"max_live_destination_tokens\": "
        << audit.max_live_destination_tokens
        << ", \"destination_lifetime_token_cycles\": "
        << audit.destination_lifetime_token_cycles
        << ", \"destination_conserved\": "
        << (audit.destination_conserved() ? "true" : "false")
        << ", \"destination_thresholds\": [";
    for (std::size_t index = 0;
         index < audit.destination_thresholds.size(); ++index) {
        if (index != 0) out << ", ";
        out << "{\"tokens\": " << audit.destination_thresholds[index]
            << ", \"events\": "
            << audit.destination_threshold_events[index]
            << ", \"excess_tokens\": "
            << audit.destination_threshold_excess_tokens[index] << "}";
    }
    out << "]"
        << ", \"destination_class_uops\": "
        << audit.destination_class_uops
        << ", \"destination_classes_conserved\": "
        << (audit.destination_classes_conserved() ? "true" : "false")
        << ", \"destination_classes\": [";
    static constexpr std::array<const char*, 4> class_names{
        "int", "float", "vec", "cc"};
    for (std::size_t index = 0; index < class_names.size(); ++index) {
        if (index != 0) out << ", ";
        out << "{\"class\": \"" << class_names[index]
            << "\", \"tokens\": "
            << audit.destination_class_tokens[index]
            << ", \"release_tokens\": "
            << audit.destination_class_release_tokens[index]
            << ", \"tokens_released\": "
            << audit.destination_class_tokens_released[index]
            << ", \"live_at_last_rename\": "
            << audit.destination_class_tokens_live_at_last_rename[index]
            << ", \"max_live_tokens\": "
            << audit.destination_class_max_live_tokens[index] << "}";
    }
    out << "]"
        << ", \"rename_free_list_stall_uops\": "
        << audit.rename_free_list_stall_uops
        << ", \"rename_free_list_stall_cycles\": "
        << audit.rename_free_list_stall_cycles
        << ", \"dispatch_delayed_uops\": "
        << audit.dispatch_delayed_uops
        << ", \"dispatch_delay_cycles\": "
        << audit.dispatch_delay_cycles
        << ", \"dispatch_bandwidth_events\": "
        << audit.dispatch_bandwidth_events
        << ", \"dispatch_bandwidth_cycles\": "
        << audit.dispatch_bandwidth_cycles
        << ", \"rob_capacity_events\": "
        << audit.rob_capacity_events
        << ", \"rob_capacity_cycles\": "
        << audit.rob_capacity_cycles
        << ", \"iq_capacity_events\": "
        << audit.iq_capacity_events
        << ", \"iq_capacity_cycles\": "
        << audit.iq_capacity_cycles
        << ", \"lq_capacity_events\": "
        << audit.lq_capacity_events
        << ", \"lq_capacity_cycles\": "
        << audit.lq_capacity_cycles
        << ", \"sq_capacity_events\": "
        << audit.sq_capacity_events
        << ", \"sq_capacity_cycles\": "
        << audit.sq_capacity_cycles
        << ", \"dispatch_conserved\": "
        << (audit.dispatch_conserved() ? "true" : "false")
        << ", \"rob_residency_cycles\": "
        << audit.rob_residency_cycles
        << ", \"rob_max_residency_cycles\": "
        << audit.rob_max_residency_cycles
        << ", \"iq_residency_cycles\": "
        << audit.iq_residency_cycles
        << ", \"iq_max_residency_cycles\": "
        << audit.iq_max_residency_cycles
        << ", \"memory_iq_post_issue_uops\": "
        << audit.memory_iq_post_issue_uops
        << ", \"memory_iq_post_issue_cycles\": "
        << audit.memory_iq_post_issue_cycles
        << ", \"source_operands\": "
        << audit.source_operands
        << ", \"source_uops_over_dependency_slots\": "
        << audit.source_uops_over_dependency_slots
        << ", \"source_operands_over_dependency_slots\": "
        << audit.source_operands_over_dependency_slots
        << ", \"dependency_edges\": "
        << audit.dependency_edges
        << ", \"dependency_cross_boundary_edges\": "
        << audit.dependency_cross_boundary_edges
        << ", \"dependent_uops\": "
        << audit.dependent_uops
        << ", \"dependency_producer_uops\": "
        << audit.dependency_producer_uops
        << ", \"dependency_distance_sum\": "
        << audit.dependency_distance_sum
        << ", \"dependency_distance_max\": "
        << audit.dependency_distance_max
        << ", \"dependency_gated_uops\": "
        << audit.dependency_gated_uops
        << ", \"dependency_gate_cycles\": "
        << audit.dependency_gate_cycles
        << ", \"dependency_gate_cycles_max\": "
        << audit.dependency_gate_cycles_max
        << ", \"static_dependency_uops\": "
        << audit.static_dependency_uops
        << ", \"static_dependency_map_misses\": "
        << audit.static_dependency_map_misses
        << ", \"static_dependency_edges\": "
        << audit.static_dependency_edges
        << ", \"static_dependency_duplicate_edges\": "
        << audit.static_dependency_duplicate_edges
        << ", \"static_dependency_supplemental_edges\": "
        << audit.static_dependency_supplemental_edges
        << ", \"static_dependency_supplemental_uops\": "
        << audit.static_dependency_supplemental_uops
        << ", \"static_dependency_ready_extension_uops\": "
        << audit.static_dependency_ready_extension_uops
        << ", \"static_dependency_ready_extension_cycles\": "
        << audit.static_dependency_ready_extension_cycles
        << ", \"static_dependency_ready_extension_max_cycles\": "
        << audit.static_dependency_ready_extension_max_cycles
        << ", \"static_dependency_truncated_ready_extension_uops\": "
        << audit.static_dependency_truncated_ready_extension_uops
        << ", \"static_dependency_truncated_ready_extension_cycles\": "
        << audit.static_dependency_truncated_ready_extension_cycles
        << ", \"static_dependency_truncated_ready_extension_max_cycles\": "
        << audit.static_dependency_truncated_ready_extension_max_cycles
        << ", \"store_set_rmw_observations\": "
        << audit.store_set_rmw_observations
        << ", \"store_set_rmw_pc_trainings\": "
        << audit.store_set_rmw_pc_trainings
        << ", \"store_set_same_pc_load_candidates\": "
        << audit.store_set_same_pc_load_candidates
        << ", \"store_set_same_pc_store_candidates\": "
        << audit.store_set_same_pc_store_candidates
        << ", \"store_set_same_pc_edges\": "
        << audit.store_set_same_pc_edges
        << ", \"store_set_same_pc_load_edges\": "
        << audit.store_set_same_pc_load_edges
        << ", \"store_set_same_pc_store_edges\": "
        << audit.store_set_same_pc_store_edges
        << ", \"store_set_same_pc_nonoverlap_edges\": "
        << audit.store_set_same_pc_nonoverlap_edges
        << ", \"store_set_same_pc_distance_sum\": "
        << audit.store_set_same_pc_distance_sum
        << ", \"store_set_same_pc_distance_max\": "
        << audit.store_set_same_pc_distance_max
        << ", \"store_set_same_pc_ready_extension_uops\": "
        << audit.store_set_same_pc_ready_extension_uops
        << ", \"store_set_same_pc_ready_extension_cycles\": "
        << audit.store_set_same_pc_ready_extension_cycles
        << ", \"store_set_same_pc_ready_extension_max_cycles\": "
        << audit.store_set_same_pc_ready_extension_max_cycles
        << ", \"atomic_uops\": "
        << audit.atomic_uops
        << ", \"execution_pools\": [";
    static constexpr std::array<const char*, 8> pool_names{
        "integer", "integer_multiply", "float_simple", "float_complex",
        "simd", "predicate", "memory", "system"};
    for (std::size_t index = 0; index < pool_names.size(); ++index) {
        if (index != 0) out << ", ";
        out << "{\"pool\": \"" << pool_names[index]
            << "\", \"uops\": " << audit.pool_uops[index]
            << ", \"source_uops_over_dependency_slots\": "
            << audit.source_uops_over_dependency_slots_by_pool[index]
            << ", \"source_operands_over_dependency_slots\": "
            << audit.source_operands_over_dependency_slots_by_pool[index]
            << ", \"dependent_uops\": "
            << audit.dependent_uops_by_pool[index]
            << ", \"dependency_edges\": "
            << audit.dependency_edges_by_pool[index]
            << ", \"dependency_gated_uops\": "
            << audit.dependency_gated_uops_by_pool[index]
            << ", \"dependency_gate_cycles\": "
            << audit.dependency_gate_cycles_by_pool[index] << "}";
    }
    out << "]"
        << ", \"stage_fetch_to_decode_cycles\": "
        << audit.stage_fetch_to_decode_cycles
        << ", \"stage_decode_to_rename_cycles\": "
        << audit.stage_decode_to_rename_cycles
        << ", \"stage_rename_to_dispatch_cycles\": "
        << audit.stage_rename_to_dispatch_cycles
        << ", \"stage_dispatch_to_issue_cycles\": "
        << audit.stage_dispatch_to_issue_cycles
        << ", \"stage_issue_to_execute_cycles\": "
        << audit.stage_issue_to_execute_cycles
        << ", \"stage_execute_to_completion_cycles\": "
        << audit.stage_execute_to_completion_cycles
        << ", \"stage_completion_to_retire_cycles\": "
        << audit.stage_completion_to_retire_cycles
        << ", \"stage_fetch_to_retire_cycles\": "
        << audit.stage_fetch_to_retire_cycles
        << ", \"stage_memory_uops\": "
        << audit.stage_memory_uops
        << ", \"stage_memory_issue_to_completion_cycles\": "
        << audit.stage_memory_issue_to_completion_cycles
        << ", \"stage_memory_completion_to_retire_cycles\": "
        << audit.stage_memory_completion_to_retire_cycles
        << ", \"stage_conserved\": "
        << (audit.stage_conserved() ? "true" : "false") << "}";
}

void write_committed_epoch_audit(
    std::ostream& out,
    const fastsim::CommittedEpochAuditCounters& audit,
    std::uint64_t committed_uops) {
    out << "{\"accepted_prefixes\": " << audit.accepted_prefixes
        << ", \"accepted_uops\": " << audit.accepted_uops
        << ", \"committed_uops\": " << committed_uops
        << ", \"accepted_uops_conserved\": "
        << (audit.accepted_uops == committed_uops ? "true" : "false")
        << ", \"memory_events\": " << audit.memory_events
        << ", \"inflight_memory_uops\": "
        << audit.inflight_memory_uops
        << ", \"corrected_horizon_violations\": "
        << audit.corrected_horizon_violations
        << ", \"corrected_issue_within_horizon_events\": "
        << audit.corrected_issue_within_horizon_events
        << ", \"corrected_issue_beyond_horizon_events\": "
        << audit.corrected_issue_beyond_horizon_events
        << ", \"corrected_issue_beyond_horizon_uops\": "
        << audit.corrected_issue_beyond_horizon_uops
        << ", \"corrected_issue_beyond_horizon_cycles\": "
        << audit.corrected_issue_beyond_horizon_cycles
        << ", \"corrected_issue_beyond_horizon_max_cycles\": "
        << audit.corrected_issue_beyond_horizon_max_cycles
        << ", \"sparse_cross_epoch_edges\": "
        << audit.sparse_cross_epoch_edges
        << ", \"memory_events_conserved\": "
        << (audit.memory_events_conserved() ? "true" : "false")
        << "}";
}

std::string stats_json(
    const fastsim::SimulationStats& stats,
    const fastsim::SimulatorConfig& config) {
    const auto total = stats.total_core();
    const auto o3 = stats.total_o3();
    const auto response_rename = stats.total_response_rename();
    const auto committed_pipeline_audit =
        stats.total_committed_pipeline_audit();
    const auto sequencer = stats.total_sequencer();
    fastsim::ChaCounters cha_total;
    for (const auto& cha : stats.cha) cha_total += cha;
    fastsim::ChaCounters instruction_cha_total;
    for (const auto& cha : stats.instruction_cha) {
        instruction_cha_total += cha;
    }
    const auto response_critical =
        stats.total_response_critical_cycles();
    const auto response_residual =
        stats.total_response_residuals();
    const auto committed_epoch_audit =
        stats.total_committed_epoch_audit();
    fastsim::KernelEventCounters synthetic_kernel_total;
    synthetic_kernel_total += total.syscall_kernel;
    synthetic_kernel_total += total.page_fault_kernel;
    synthetic_kernel_total += total.irq_kernel;
    if (config.measurement_scope ==
        fastsim::MeasurementScope::kUnspecified) {
        throw std::invalid_argument(
            "stats output requires measurement.scope");
    }
    const bool user_plus_kernel =
        config.measurement_scope ==
        fastsim::MeasurementScope::kUserPlusKernel;
    const bool native_kernel_trace = config.native_kernel_trace;
    if (total.native_kernel_retired_uops > total.retired_uops ||
        total.native_kernel_retired_instructions >
            total.retired_instructions) {
        throw std::logic_error(
            "native-kernel counters exceed aggregate functional counters");
    }
    const auto user_trace_uops =
        total.retired_uops - total.native_kernel_retired_uops;
    const auto user_trace_instructions =
        total.retired_instructions -
        total.native_kernel_retired_instructions;
    const auto seconds =
        static_cast<double>(stats.wall_time_ns) / 1'000'000'000.0;
    const auto warmup_seconds = static_cast<double>(
        stats.functional_warmup_wall_ns) / 1'000'000'000.0;
    const auto measurement_seconds = static_cast<double>(
        stats.measurement_wall_ns) / 1'000'000'000.0;
    const auto instructions_per_second =
        seconds == 0.0
            ? 0.0
            : static_cast<double>(total.retired_instructions) / seconds;
    const auto uops_per_second =
        seconds == 0.0
            ? 0.0
            : static_cast<double>(total.retired_uops) / seconds;
    const auto measurement_instructions_per_second =
        measurement_seconds == 0.0
            ? 0.0
            : static_cast<double>(user_trace_instructions) /
                  measurement_seconds;
    const auto measurement_uops_per_second =
        measurement_seconds == 0.0
            ? 0.0
            : static_cast<double>(user_trace_uops) /
                  measurement_seconds;
    const auto end_to_end_instructions_per_second =
        seconds == 0.0
            ? 0.0
            : static_cast<double>(
                  total.retired_instructions +
                  stats.functional_warmup_instructions) / seconds;
    const auto end_to_end_uops_per_second =
        seconds == 0.0
            ? 0.0
            : static_cast<double>(
                  total.retired_uops + stats.functional_warmup_uops) /
                  seconds;
    std::uint64_t makespan_cycles = 0;
    for (const auto& core : stats.cores) {
        makespan_cycles = std::max(makespan_cycles, core.cycles);
    }

    std::ostringstream out;
    out << std::setprecision(10);
    out << "{\n";
    out << "  \"schema\": \"fastsim-stats-v5\",\n";
    out << "  \"measurement_scope\": \""
        << fastsim::measurement_scope_name(config.measurement_scope)
        << "\",\n";
    out << "  \"scope_metrics\": {\n";
    out << "    \"user_trace_uops\": " << user_trace_uops << ",\n";
    out << "    \"user_trace_instructions\": "
        << user_trace_instructions << ",\n";
    out << "    \"native_kernel_trace_uops\": "
        << total.native_kernel_retired_uops << ",\n";
    out << "    \"native_kernel_trace_instructions\": "
        << total.native_kernel_retired_instructions << ",\n";
    out << "    \"sum_core_cycles\": " << total.cycles << ",\n";
    out << "    \"cycles_per_user_uop\": "
        << ratio(total.cycles, user_trace_uops) << ",\n";
    // `cpi` remains an additive compatibility alias in fastsim-stats-v5.
    // New consumers must use cycles_per_user_uop or perf_like_cpi explicitly.
    out << "    \"cpi\": "
        << ratio(total.cycles, user_trace_uops) << ",\n";
    const auto perf_like_denominator = total.retired_instructions +
        (user_plus_kernel && !native_kernel_trace
             ? synthetic_kernel_total.retired_instructions
             : 0);
    out << "    \"perf_like_cpi\": "
        << ratio(total.cycles, perf_like_denominator) << ",\n";
    out << "    \"perf_like_cpi_denominator_instructions\": "
        << perf_like_denominator << ",\n";
    out << "    \"perf_like_cpi_status\": \""
        << (native_kernel_trace
                ? "strict-native-user-plus-kernel-trace"
                : (user_plus_kernel ? "profile-derived-proxy"
                                    : "strict-user-trace"))
        << "\",\n";
    out << "    \"pmu_contract_id\": \"" << kPmuContractId << "\",\n";
    out << "    \"pmu_source\": \""
        << (native_kernel_trace
                ? "fastsim-functional-native-user-plus-kernel-v1"
                : (user_plus_kernel
                       ? "fastsim-functional-plus-synthetic-kernel-profile-v1"
                       : "fastsim-functional-committed-v1"))
        << "\",\n";
    out << "    \"kernel_profile_contract\": \""
        << (native_kernel_trace
                ? "not-applicable-native-trace"
                : (!user_plus_kernel
                ? "not-applicable"
                : (has_p0_kernel_profile_contract(config)
                       ? "p0-22-field"
                       : "legacy-nonformal")))
        << "\",\n";
    out << "    \"synthetic_kernel_active_cycles\": "
        << synthetic_kernel_total.active_cycles << ",\n";
    out << "    \"blocked_wall_cycles\": "
        << synthetic_kernel_total.blocked_wall_cycles << ",\n";
    out << "    \"pmu\": ";
    if (native_kernel_trace) {
        write_user_functional_pmu(out, total, stats.llc, cha_total);
    } else if (user_plus_kernel) {
        write_user_plus_kernel_pmu(
            out, total, stats.llc, cha_total, synthetic_kernel_total, false);
    } else {
        write_user_functional_pmu(out, total, stats.llc, cha_total);
    }
    out << ",\n";
    out << "    \"memory_hierarchy_user\": ";
    if (native_kernel_trace) {
        out << "{\"status\": \"unavailable\", "
               "\"reason\": \"native combined replay does not yet "
               "privilege-partition cache/coherence events\"}";
    } else {
        write_user_memory_hierarchy(out, stats.llc, cha_total);
    }
    out << ",\n";
    out << "    \"throughput\": {"
        << "\"user_uops_per_second\": "
        << measurement_uops_per_second;
    if (native_kernel_trace) {
        out << ", \"end_to_end_user_uops_per_second\": null, "
               "\"end_to_end_user_uops_status\": "
               "\"unavailable-native-warmup-not-privilege-partitioned\"}";
    } else {
        out << ", \"end_to_end_user_uops_per_second\": "
            << end_to_end_uops_per_second << "}";
    }
    out << "\n";
    out << "  },\n";
    out << "  \"configuration\": {\n";
    out << "    \"measurement_scope\": \""
        << fastsim::measurement_scope_name(config.measurement_scope)
        << "\",\n";
    out << "    \"native_kernel_trace\": "
        << (config.native_kernel_trace ? "true" : "false") << ",\n";
    out << "    \"cores\": " << config.cores << ",\n";
    out << "    \"chunk_instructions\": "
        << config.chunk_instructions << ",\n";
    out << "    \"lookahead_chunks\": "
        << config.lookahead_chunks << ",\n";
    out << "    \"interval_target_uops\": "
        << config.interval_target_uops << ",\n";
    out << "    \"interval_max_cycles\": "
        << config.interval_max_cycles << ",\n";
    out << "    \"interval_scheduler\": \""
        << config.interval_scheduler << "\",\n";
    out << "    \"interval_full_order_audit\": "
        << (config.interval_full_order_audit ? "true" : "false")
        << ",\n";
    out << "    \"interval_same_line_order_audit\": "
        << (config.interval_same_line_order_audit ? "true" : "false")
        << ",\n";
    out << "    \"cpi_attribution\": "
        << (config.cpi_attribution ? "true" : "false") << ",\n";
    out << "    \"interval_private_preview\": "
        << (config.interval_private_preview ? "true" : "false")
        << ",\n";
    out << "    \"interval_reweave_passes\": "
        << config.interval_reweave_passes << ",\n";
    out << "    \"interval_causal_timing\": "
        << (config.interval_causal_timing ? "true" : "false")
        << ",\n";
    out << "    \"interval_response_retime\": "
        << (config.interval_response_retime ? "true" : "false")
        << ",\n";
    out << "    \"interval_rob_head_suffix_replay\": "
        << (config.interval_rob_head_suffix_replay ? "true" : "false")
        << ",\n";
    out << "    \"interval_causal_passes\": "
        << config.interval_causal_passes << ",\n";
    out << "    \"interval_causal_max_closure_events\": "
        << config.interval_causal_max_closure_events << ",\n";
    out << "    \"interval_corrected_suffix_carry\": "
        << (config.interval_corrected_suffix_carry ? "true" : "false")
        << ",\n";
    out << "    \"interval_parallel_feedback\": "
        << (config.interval_parallel_feedback ? "true" : "false")
        << ",\n";
    out << "    \"domain_workers\": "
        << config.domain_workers << ",\n";
    out << "    \"domain_min_events\": "
        << config.domain_min_events << ",\n";
    out << "    \"core_model\": \"" << config.core_model << "\",\n";
    out << "    \"fetch_width\": " << config.fetch_width << ",\n";
    out << "    \"fetch_buffer_bytes\": "
        << config.fetch_buffer_bytes << ",\n";
    out << "    \"fetch_buffer_refill_latency\": "
        << config.fetch_buffer_refill_latency << ",\n";
    out << "    \"fetch_supply_model\": "
        << (config.fetch_supply_model ? "true" : "false") << ",\n";
    out << "    \"fetch_supply_static_instruction_span\": "
        << (config.fetch_supply_static_instruction_span ? "true" : "false")
        << ",\n";
    out << "    \"fetch_supply_speculative_shadow\": "
        << (config.fetch_supply_speculative_shadow ? "true" : "false")
        << ",\n";
    out << "    \"l1i_enabled\": "
        << (config.l1i_enabled ? "true" : "false") << ",\n";
    out << "    \"fetch_supply_physical_request_ledger\": "
        << (config.fetch_supply_physical_request_ledger ? "true" : "false")
        << ",\n";
    out << "    \"fetch_supply_lower_hierarchy\": "
        << (config.fetch_supply_lower_hierarchy ? "true" : "false")
        << ",\n";
    out << "    \"instruction_address_mode\": \""
        << config.instruction_address_mode << "\",\n";
    out << "    \"instruction_physical_address_bits\": "
        << config.instruction_physical_address_bits << ",\n";
    out << "    \"instruction_page_bits\": "
        << config.instruction_page_bits << ",\n";
    out << "    \"instruction_mapping_seed\": "
        << config.instruction_mapping_seed << ",\n";
    out << "    \"l1i_miss_penalty\": "
        << config.l1i_miss_penalty << ",\n";
    out << "    \"l1i_speculative_entry_state\": "
        << (config.l1i_speculative_entry_state ? "true" : "false")
        << ",\n";
    out << "    \"l1i_speculative_path_state\": "
        << (config.l1i_speculative_path_state ? "true" : "false")
        << ",\n";
    out << "    \"decode_width\": " << config.decode_width << ",\n";
    out << "    \"rename_width\": " << config.rename_width << ",\n";
    out << "    \"issue_width\": " << config.issue_width << ",\n";
    out << "    \"dispatch_width\": " << config.dispatch_width << ",\n";
    out << "    \"writeback_width\": " << config.writeback_width << ",\n";
    out << "    \"commit_width\": " << config.commit_width << ",\n";
    out << "    \"fetch_queue_entries\": "
        << config.fetch_queue_entries << ",\n";
    out << "    \"rob_entries\": " << config.rob_entries << ",\n";
    out << "    \"iq_entries\": " << config.iq_entries << ",\n";
    out << "    \"lq_entries\": " << config.lq_entries << ",\n";
    out << "    \"sq_entries\": " << config.sq_entries << ",\n";
    out << "    \"committed_pipeline_audit\": "
        << (config.committed_pipeline_audit ? "true" : "false")
        << ",\n";
    out << "    \"committed_static_dependency_feedback\": "
        << (config.committed_static_dependency_feedback ? "true" : "false")
        << ",\n";
    out << "    \"store_set_same_pc_feedback\": "
        << (config.store_set_same_pc_feedback ? "true" : "false")
        << ",\n";
    out << "    \"rename_free_list\": "
        << (config.rename_free_list ? "true" : "false") << ",\n";
    out << "    \"response_rename_feedback\": "
        << (config.response_rename_feedback ? "true" : "false")
        << ",\n";
    out << "    \"rename_int_free_entries\": "
        << config.rename_int_free_entries << ",\n";
    out << "    \"rename_float_free_entries\": "
        << config.rename_float_free_entries << ",\n";
    out << "    \"rename_vec_free_entries\": "
        << config.rename_vec_free_entries << ",\n";
    out << "    \"rename_cc_free_entries\": "
        << config.rename_cc_free_entries << ",\n";
    out << "    \"dispatch_to_issue\": "
        << config.dispatch_to_issue << ",\n";
    out << "    \"fetch_to_decode\": "
        << config.fetch_to_decode << ",\n";
    out << "    \"decode_to_rename\": "
        << config.decode_to_rename << ",\n";
    out << "    \"rename_to_dispatch\": "
        << config.rename_to_dispatch << ",\n";
    out << "    \"iew_to_rename\": "
        << config.iew_to_rename << ",\n";
    out << "    \"commit_to_rename\": "
        << config.commit_to_rename << ",\n";
    out << "    \"issue_to_execute\": "
        << config.issue_to_execute << ",\n";
    out << "    \"execute_to_commit\": "
        << config.execute_to_commit << ",\n";
    out << "    \"minimum_load_latency\": "
        << config.minimum_load_latency << ",\n";
    out << "    \"response_queue_feedback\": "
        << (config.response_queue_feedback ? "true" : "false")
        << ",\n";
    out << "    \"response_fetch_queue_feedback\": "
        << (config.response_fetch_queue_feedback ? "true" : "false")
        << ",\n";
    out << "    \"response_rob_lsq_feedback\": "
        << (config.response_rob_lsq_feedback ? "true" : "false")
        << ",\n";
    out << "    \"response_sparse_scoreboard\": "
        << (config.response_sparse_scoreboard ? "true" : "false")
        << ",\n";
    out << "    \"response_block_summary\": "
        << (config.response_block_summary ? "true" : "false")
        << ",\n";
    out << "    \"response_memory_descriptor\": "
        << (config.response_memory_descriptor ? "true" : "false")
        << ",\n";
    out << "    \"response_batch_timing_encode\": "
        << (config.response_batch_timing_encode ? "true" : "false")
        << ",\n";
    out << "    \"response_sparse_resource_repair\": "
        << (config.response_sparse_resource_repair ? "true" : "false")
        << ",\n";
    out << "    \"response_activity_certificate\": "
        << (config.response_activity_certificate ? "true" : "false")
        << ",\n";
    out << "    \"response_retire_exposure\": "
        << config.response_retire_exposure << ",\n";
    out << "    \"store_post_commit_request\": "
        << (config.store_post_commit_request ? "true" : "false")
        << ",\n";
    out << "    \"needs_tso\": "
        << (config.needs_tso ? "true" : "false") << ",\n";
    out << "    \"integer_alu_units\": "
        << config.integer_alu_units << ",\n";
    out << "    \"integer_multiply_units\": "
        << config.integer_multiply_units << ",\n";
    out << "    \"float_simple_units\": "
        << config.float_simple_units << ",\n";
    out << "    \"float_complex_units\": "
        << config.float_complex_units << ",\n";
    out << "    \"simd_units\": " << config.simd_units << ",\n";
    out << "    \"predicate_units\": "
        << config.predicate_units << ",\n";
    out << "    \"memory_units\": " << config.memory_units << ",\n";
    out << "    \"system_units\": " << config.system_units << ",\n";
    out << "    \"cache_load_ports\": "
        << config.cache_load_ports << ",\n";
    out << "    \"cache_store_ports\": "
        << config.cache_store_ports << ",\n";
    out << "    \"integer_alu_latency\": "
        << config.integer_alu_latency << ",\n";
    out << "    \"integer_multiply_latency\": "
        << config.integer_multiply_latency << ",\n";
    out << "    \"integer_divide_latency\": "
        << config.integer_divide_latency << ",\n";
    out << "    \"integer_alu_pipelined\": "
        << (config.integer_alu_pipelined ? "true" : "false") << ",\n";
    out << "    \"integer_multiply_pipelined\": "
        << (config.integer_multiply_pipelined ? "true" : "false")
        << ",\n";
    out << "    \"integer_divide_pipelined\": "
        << (config.integer_divide_pipelined ? "true" : "false")
        << ",\n";
    out << "    \"float_simple_latency\": "
        << config.float_simple_latency << ",\n";
    out << "    \"float_multiply_latency\": "
        << config.float_multiply_latency << ",\n";
    out << "    \"float_multiply_accumulate_latency\": "
        << config.float_multiply_accumulate_latency << ",\n";
    out << "    \"float_misc_latency\": "
        << config.float_misc_latency << ",\n";
    out << "    \"float_divide_latency\": "
        << config.float_divide_latency << ",\n";
    out << "    \"float_sqrt_latency\": "
        << config.float_sqrt_latency << ",\n";
    out << "    \"float_simple_pipelined\": "
        << (config.float_simple_pipelined ? "true" : "false")
        << ",\n";
    out << "    \"float_complex_pipelined\": "
        << (config.float_complex_pipelined ? "true" : "false")
        << ",\n";
    out << "    \"float_divide_pipelined\": "
        << (config.float_divide_pipelined ? "true" : "false")
        << ",\n";
    out << "    \"float_sqrt_pipelined\": "
        << (config.float_sqrt_pipelined ? "true" : "false")
        << ",\n";
    out << "    \"simd_latency\": " << config.simd_latency << ",\n";
    out << "    \"predicate_latency\": "
        << config.predicate_latency << ",\n";
    out << "    \"system_latency\": "
        << config.system_latency << ",\n";
    out << "    \"syscall_service_latency\": "
        << config.syscall_service_latency << ",\n";
    out << "    \"syscall_restart_latency\": "
        << config.syscall_restart_latency << ",\n";
    out << "    \"syscall_cost_model\": "
        << (config.syscall_cost_model ? "true" : "false") << ",\n";
    out << "    \"syscall_cost_table_entries\": "
        << config.syscall_cost_table.size() << ",\n";
    out << "    \"syscall_kernel_event_model\": "
        << (config.syscall_kernel_event_model ? "true" : "false")
        << ",\n";
    out << "    \"syscall_kernel_event_table_entries\": "
        << config.syscall_kernel_event_table.size() << ",\n";
    out << "    \"syscall_kernel_event_default_profile\": "
        << (config.syscall_kernel_event_default_profile_enabled
                ? "true"
                : "false")
        << ",\n";
    out << "    \"page_fault_event_model\": "
        << (config.page_fault_event_model ? "true" : "false")
        << ",\n";
    out << "    \"page_fault_cache_state_model\": "
        << (config.page_fault_cache_state_model ? "true" : "false")
        << ",\n";
    out << "    \"page_fault_syscall_semantic_model\": "
        << (config.page_fault_syscall_semantic_model ? "true" : "false")
        << ",\n";
    out << "    \"page_fault_roi_entry_page_state_model\": "
        << (config.page_fault_roi_entry_page_state_model ? "true" : "false")
        << ",\n";
    out << "    \"page_fault_syscall_semantic_fallback_write_probability_ppm\": "
        << config
               .page_fault_syscall_semantic_fallback_write_probability_ppm
        << ",\n";
    out << "    \"page_fault_allocation_syscalls\": "
        << config.page_fault_allocation_syscalls.size() << ",\n";
    out << "    \"page_fault_allocation_window_records\": "
        << config.page_fault_allocation_window_records << ",\n";
    out << "    \"page_fault_probability_ppm\": "
        << config.page_fault_probability_ppm << ",\n";
    out << "    \"page_fault_background_write_probability_ppm\": "
        << config.page_fault_background_write_probability_ppm << ",\n";
    out << "    \"page_fault_allocation_probability_ppm\": "
        << config.page_fault_allocation_probability_ppm << ",\n";
    out << "    \"page_fault_allocation_write_probability_ppm\": "
        << config.page_fault_allocation_write_probability_ppm << ",\n";
    out << "    \"page_fault_allocation_probability_table_entries\": "
        << config.page_fault_allocation_probability_table.size() << ",\n";
    out << "    \"irq_event_model\": "
        << (config.irq_event_model ? "true" : "false") << ",\n";
    out << "    \"irq_period_cycles\": "
        << config.irq_period_cycles << ",\n";
    out << "    \"l1d_mshrs\": " << config.l1d_mshrs << ",\n";
    out << "    \"l2_mshrs\": " << config.l2_mshrs << ",\n";
    out << "    \"llc_mshrs\": " << config.llc_mshrs << ",\n";
    out << "    \"ruby_sequencer_max_outstanding\": "
        << config.ruby_sequencer_max_outstanding << ",\n";
    out << "    \"memory_exposure\": " << config.memory_exposure << ",\n";
    out << "    \"strict_physical_address\": "
        << (config.strict_physical_address ? "true" : "false") << ",\n";
    out << "    \"require_virtual_page_token\": "
        << (config.require_virtual_page_token ? "true" : "false")
        << ",\n";
    out << "    \"require_instruction_page_map\": "
        << (config.require_instruction_page_map ? "true" : "false")
        << ",\n";
    out << "    \"allow_cross_page_without_virtual_token\": "
        << (config.allow_cross_page_without_virtual_token ? "true" : "false")
        << ",\n";
    out << "    \"allow_mmio_escape\": "
        << (config.allow_mmio_escape ? "true" : "false") << ",\n";
    out << "    \"dtlb\": {\"enabled\": "
        << (config.dtlb.enabled ? "true" : "false")
        << ", \"speculative_path_state\": "
        << (config.dtlb.speculative_path_state ? "true" : "false")
        << ", \"entries\": " << config.dtlb.entries
        << ", \"hit_latency\": " << config.dtlb.hit_latency
        << ", \"miss_model\": \"" << config.dtlb.miss_model << "\""
        << ", \"page_walk_latency\": "
        << config.dtlb.page_walk_latency
        << ", \"page_walkers\": " << config.dtlb.page_walkers
        << ", \"coalesce_misses\": "
        << (config.dtlb.coalesce_misses ? "true" : "false")
        << "},\n";
    out << "    \"coherence\": "
        << (config.coherence ? "true" : "false") << ",\n";
    out << "    \"inclusive_llc\": "
        << (config.inclusive_llc ? "true" : "false") << ",\n";
    out << "    \"cha_count\": " << config.cha_count << ",\n";
    out << "    \"cha_xor_hash\": "
        << (config.cha_xor_hash ? "true" : "false") << ",\n";
    out << "    \"noc_one_way_latency\": "
        << config.noc_one_way_latency << ",\n";
    out << "    \"llc_service_cycles\": "
        << config.llc_service_cycles << ",\n";
    out << "    \"directory_memory_latency\": "
        << config.directory_memory_latency << ",\n";
    out << "    \"llc_fill_response_latency\": "
        << config.llc_fill_response_latency << ",\n";
    out << "    \"l1i\": ";
    write_cache_config(out, config.l1i);
    out << ",\n";
    out << "    \"l1d\": ";
    write_cache_config(out, config.l1d);
    out << ",\n";
    out << "    \"l2\": ";
    write_cache_config(out, config.l2);
    out << ",\n";
    out << "    \"llc\": ";
    write_cache_config(out, config.llc);
    out << ",\n";
    out << "    \"branch\": {\"type\": \"" << config.branch.type
        << "\", \"local_counter_bits\": "
        << config.branch.local_counter_bits
        << ", \"global_counter_bits\": "
        << config.branch.global_counter_bits
        << ", \"choice_counter_bits\": "
        << config.branch.choice_counter_bits
        << ", \"local_history_entries\": "
        << config.branch.local_history_entries
        << ", \"local_entries\": " << config.branch.local_entries
        << ", \"global_entries\": " << config.branch.global_entries
        << ", \"choice_entries\": " << config.branch.choice_entries
        << ", \"inst_shift\": " << config.branch.inst_shift
        << ", \"btb_entries\": " << config.branch.btb_entries
        << ", \"btb_associativity\": "
        << config.branch.btb_associativity
        << ", \"btb_tag_bits\": " << config.branch.btb_tag_bits
        << ", \"btb_set_shift\": " << config.branch.btb_set_shift
        << ", \"ras_entries\": " << config.branch.ras_entries
        << ", \"ras_static_return_target\": "
        << (config.branch.ras_static_return_target ? "true" : "false")
        << ", \"indirect_sets\": " << config.branch.indirect_sets
        << ", \"indirect_ways\": " << config.branch.indirect_ways
        << ", \"indirect_tag_bits\": "
        << config.branch.indirect_tag_bits
        << ", \"indirect_path_length\": "
        << config.branch.indirect_path_length
        << ", \"indirect_speculative_path_length\": "
        << config.branch.indirect_speculative_path_length
        << ", \"indirect_ghr_bits\": "
        << config.branch.indirect_ghr_bits
        << ", \"indirect_hash_ghr\": "
        << (config.branch.indirect_hash_ghr ? "true" : "false")
        << ", \"indirect_hash_targets\": "
        << (config.branch.indirect_hash_targets ? "true" : "false")
        << ", \"requires_btb_hit\": "
        << (config.branch.requires_btb_hit ? "true" : "false")
        << ", \"update_btb_at_squash\": "
        << (config.branch.update_btb_at_squash ? "true" : "false")
        << ", \"speculative_history\": "
        << (config.branch.speculative_history ? "true" : "false")
        << ", \"mispredict_penalty\": "
        << config.branch.mispredict_penalty
        << ", \"squash_width\": "
        << config.branch.squash_width
        << ", \"shadow_rob\": "
        << (config.branch.shadow_rob ? "true" : "false")
        << ", \"population_audit\": "
        << (config.branch.population_audit ? "true" : "false")
        << ", \"population_history_cycles\": "
        << config.branch.population_history_cycles << "},\n";
    out << "    \"dram\": {\"size_bytes\": " << config.dram.size_bytes
        << ", \"channels\": " << config.dram.channels
        << ", \"banks_per_channel\": "
        << config.dram.banks_per_channel
        << ", \"ranks_per_channel\": "
        << config.dram.ranks_per_channel
        << ", \"bank_groups_per_rank\": "
        << config.dram.bank_groups_per_rank
        << ", \"row_bytes\": " << config.dram.row_bytes
        << ", \"t_cl\": " << config.dram.t_cl
        << ", \"t_rcd\": " << config.dram.t_rcd
        << ", \"t_rp\": " << config.dram.t_rp
        << ", \"t_ras\": " << config.dram.t_ras
        << ", \"t_rtp\": " << config.dram.t_rtp
        << ", \"t_rrd\": " << config.dram.t_rrd
        << ", \"t_rrd_l\": " << config.dram.t_rrd_l
        << ", \"t_xaw\": " << config.dram.t_xaw
        << ", \"activation_limit\": "
        << config.dram.activation_limit
        << ", \"burst_cycles\": " << config.dram.burst_cycles
        << ", \"t_ccd_l\": " << config.dram.t_ccd_l
        << ", \"t_cs\": " << config.dram.t_cs
        << ", \"frontend_latency\": "
        << config.dram.frontend_latency
        << ", \"backend_latency\": "
        << config.dram.backend_latency
        << ", \"scheduler\": \"" << config.dram.scheduler << "\""
        << ", \"read_buffer_size\": "
        << config.dram.read_buffer_size
        << ", \"separate_write_queue\": "
        << (config.dram.separate_write_queue ? "true" : "false")
        << ", \"write_buffer_size\": "
        << config.dram.write_buffer_size
        << ", \"write_high_threshold_percent\": "
        << config.dram.write_high_threshold_percent
        << ", \"write_low_threshold_percent\": "
        << config.dram.write_low_threshold_percent
        << ", \"min_reads_per_switch\": "
        << config.dram.min_reads_per_switch
        << ", \"min_writes_per_switch\": "
        << config.dram.min_writes_per_switch
        << ", \"frfcfs_selection_window\": "
        << config.dram.frfcfs_selection_window
        << ", \"frfcfs_topology_scaled_window\": "
        << (config.dram.frfcfs_topology_scaled_window
                ? "true" : "false")
        << ", \"frfcfs_full_queue_page_policy\": "
        << (config.dram.frfcfs_full_queue_page_policy
                ? "true" : "false")
        << ", \"frfcfs_row_cap_single_precharge\": "
        << (config.dram.frfcfs_row_cap_single_precharge
                ? "true" : "false")
        << ", \"frfcfs_passes\": "
        << config.dram.frfcfs_passes
        << ", \"frfcfs_arrival_bucket_cycles\": "
        << config.dram.frfcfs_arrival_bucket_cycles
        << ", \"max_accesses_per_row\": "
        << config.dram.max_accesses_per_row
        << "}\n";
    out << "  },\n";
    out << "  \"wall_time_seconds\": " << seconds << ",\n";
    out << "  \"wall_time_breakdown\": {\"functional_warmup_seconds\": "
        << warmup_seconds << ", \"measurement_seconds\": "
        << measurement_seconds << "},\n";
    out << "  \"throughput\": {\n";
    out << "    \"instructions_per_second\": " << instructions_per_second
        << ",\n";
    out << "    \"uops_per_second\": " << uops_per_second << ",\n";
    out << "    \"measurement_instructions_per_second\": "
        << measurement_instructions_per_second << ",\n";
    out << "    \"measurement_uops_per_second\": "
        << measurement_uops_per_second << ",\n";
    out << "    \"end_to_end_instructions_per_second\": "
        << end_to_end_instructions_per_second << ",\n";
    out << "    \"end_to_end_uops_per_second\": "
        << end_to_end_uops_per_second << ",\n";
    out << "    \"mips\": " << instructions_per_second / 1'000'000.0
        << "\n";
    out << "  },\n";
    out << "  \"totals\": {\n";
    out << "    \"functional_warmup_enabled\": "
        << (stats.functional_warmup_enabled ? "true" : "false")
        << ",\n";
    out << "    \"functional_warmup_records\": "
        << stats.functional_warmup_records << ",\n";
    out << "    \"functional_warmup_uops\": "
        << stats.functional_warmup_uops << ",\n";
    out << "    \"functional_warmup_instructions\": "
        << stats.functional_warmup_instructions << ",\n";
    out << "    \"functional_warmup_memory_events\": "
        << stats.functional_warmup_memory_events << ",\n";
    out << "    \"functional_warmup_barrier_cycles\": "
        << stats.functional_warmup_barrier_cycles << ",\n";
    out << "    \"records\": " << total.records << ",\n";
    out << "    \"retired_uops\": " << total.retired_uops << ",\n";
    out << "    \"retired_instructions\": "
        << total.retired_instructions << ",\n";
    out << "    \"memory_uops\": " << total.memory_uops << ",\n";
    out << "    \"memory_accesses\": " << total.memory_accesses << ",\n";
    out << "    \"mmio_escape_accesses\": "
        << total.mmio_escape_accesses << ",\n";
    out << "    \"unknown_addresses\": " << total.unknown_addresses << ",\n";
    out << "    \"branches_without_outcome\": "
        << total.branches_without_outcome << ",\n";
    out << "    \"serializing_uops\": "
        << total.serializing_uops << ",\n";
    out << "    \"syscall_uops\": " << total.syscall_uops << ",\n";
    out << "    \"syscall_drain_cycles\": "
        << total.syscall_drain_cycles << ",\n";
    out << "    \"syscall_service_cycles\": "
        << total.syscall_service_cycles << ",\n";
    out << "    \"syscall_restart_cycles\": "
        << total.syscall_restart_cycles << ",\n";
    out << "    \"page_fault_first_touch_candidates\": "
        << total.page_fault_first_touch_candidates << ",\n";
    out << "    \"page_fault_first_touch_write_candidates\": "
        << total.page_fault_first_touch_write_candidates << ",\n";
    out << "    \"page_fault_background_candidates\": "
        << total.page_fault_background_candidates << ",\n";
    out << "    \"page_fault_background_read_candidates\": "
        << total.page_fault_background_read_candidates << ",\n";
    out << "    \"page_fault_background_write_candidates\": "
        << total.page_fault_background_write_candidates << ",\n";
    out << "    \"page_fault_allocation_candidates\": "
        << total.page_fault_allocation_candidates << ",\n";
    out << "    \"page_fault_allocation_recency_upper_bounds\": [";
    for (std::size_t index = 0;
         index < fastsim::kPageFaultAllocationRecencyUpperBounds.size();
         ++index) {
        if (index != 0) out << ", ";
        out << fastsim::kPageFaultAllocationRecencyUpperBounds[index];
    }
    out << "],\n";
    out << "    \"page_fault_allocation_recency_candidates\": [";
    for (std::size_t index = 0;
         index < total.page_fault_allocation_recency_candidates.size();
         ++index) {
        if (index != 0) out << ", ";
        out << total.page_fault_allocation_recency_candidates[index];
    }
    out << "],\n";
    out << "    \"page_fault_allocation_recency_write_candidates\": [";
    for (std::size_t index = 0;
         index < total.page_fault_allocation_recency_write_candidates.size();
         ++index) {
        if (index != 0) out << ", ";
        out << total.page_fault_allocation_recency_write_candidates[index];
    }
    out << "],\n";
    out << "    \"page_fault_allocation_by_syscall\": ";
    write_page_fault_allocation_by_syscall(
        out, total.page_fault_allocation_by_syscall);
    out << ",\n";
    out << "    \"page_fault_untracked_accesses\": "
        << total.page_fault_untracked_accesses << ",\n";
    out << "    \"page_fault_syscall_semantic_candidates\": "
        << total.page_fault_syscall_semantic_candidates << ",\n";
    out << "    \"page_fault_syscall_semantic_write_candidates\": "
        << total.page_fault_syscall_semantic_write_candidates << ",\n";
    out << "    \"page_fault_syscall_semantic_fallback_write_candidates\": "
        << total.page_fault_syscall_semantic_fallback_write_candidates
        << ",\n";
    out << "    \"page_fault_syscall_semantic_fallback_write_selected\": "
        << total.page_fault_syscall_semantic_fallback_write_selected
        << ",\n";
    out << "    \"page_fault_virtual_page_map_misses\": "
        << total.page_fault_virtual_page_map_misses << ",\n";
    out << "    \"page_fault_initial_pte_known_pages\": "
        << total.page_fault_initial_pte_known_pages << ",\n";
    out << "    \"page_fault_initial_pte_present_pages\": "
        << total.page_fault_initial_pte_present_pages << ",\n";
    out << "    \"page_fault_initial_pte_nonpresent_pages\": "
        << total.page_fault_initial_pte_nonpresent_pages << ",\n";
    out << "    \"page_fault_initial_pte_unknown_pages\": "
        << total.page_fault_initial_pte_unknown_pages << ",\n";
    out << "    \"page_fault_initial_pte_selected\": "
        << total.page_fault_initial_pte_selected << ",\n";
    out << "    \"page_fault_roi_entry_known_pages\": "
        << total.page_fault_roi_entry_known_pages << ",\n";
    out << "    \"page_fault_roi_entry_present_pages\": "
        << total.page_fault_roi_entry_present_pages << ",\n";
    out << "    \"page_fault_roi_entry_nonpresent_pages\": "
        << total.page_fault_roi_entry_nonpresent_pages << ",\n";
    out << "    \"page_fault_roi_entry_unknown_pages\": "
        << total.page_fault_roi_entry_unknown_pages << ",\n";
    out << "    \"page_fault_roi_entry_selected\": "
        << total.page_fault_roi_entry_selected << ",\n";
    out << "    \"page_fault_roi_entry_inflight_suppressed\": "
        << total.page_fault_roi_entry_inflight_suppressed
        << ",\n";
    out << "    \"page_fault_process_shared_duplicate_pages\": "
        << total.page_fault_process_shared_duplicate_pages << ",\n";
    out << "    \"page_fault_cache_state_pages\": "
        << total.page_fault_cache_state_pages << ",\n";
    out << "    \"page_fault_cache_state_lines\": "
        << total.page_fault_cache_state_lines << ",\n";
    out << "    \"fetch_buffer_transitions\": "
        << total.fetch_buffer_transitions << ",\n";
    out << "    \"fetch_buffer_refill_delay_cycles\": "
        << total.fetch_buffer_refill_delay_cycles << ",\n";
    out << "    \"fetch_block_response_wait_cycles\": "
        << total.fetch_block_response_wait_cycles << ",\n";
    out << "    \"fetch_block_response_hidden_cycles\": "
        << total.fetch_block_response_hidden_cycles << ",\n";
    out << "    \"fetch_block_response_exposed_cycles\": "
        << total.fetch_block_response_exposed_cycles << ",\n";
    out << "    \"fetch_block_response_to_resume_cycles\": "
        << total.fetch_block_response_to_resume_cycles << ",\n";
    out << "    \"fetch_block_request_to_resume_cycles\": "
        << total.fetch_block_request_to_resume_cycles << ",\n";
    out << "    \"fetch_block_request_admission_delay_cycles\": "
        << total.fetch_block_request_admission_delay_cycles << ",\n";
    out << "    \"fetch_response_ledger_committed_requests\": "
        << total.fetch_response_ledger_committed_requests << ",\n";
    out << "    \"fetch_response_ledger_shadow_requests\": "
        << total.fetch_response_ledger_shadow_requests << ",\n";
    out << "    \"fetch_response_ledger_responses\": "
        << total.fetch_response_ledger_responses << ",\n";
    out << "    \"fetch_response_ledger_server_wait_cycles\": "
        << total.fetch_response_ledger_server_wait_cycles << ",\n";
    out << "    \"speculative_fetch_shadow_uops\": "
        << total.speculative_fetch_shadow_uops << ",\n";
    out << "    \"speculative_fetch_shadow_requests_estimated\": "
        << total.speculative_fetch_shadow_requests_estimated << ",\n";
    out << "    \"speculative_fetch_shadow_requests_issued\": "
        << total.speculative_fetch_shadow_requests_issued << ",\n";
    out << "    \"speculative_fetch_shadow_response_wait_cycles\": "
        << total.speculative_fetch_shadow_response_wait_cycles << ",\n";
    out << "    \"speculative_fetch_shadow_recovery_hidden_cycles\": "
        << total.speculative_fetch_shadow_recovery_hidden_cycles << ",\n";
    out << "    \"speculative_fetch_shadow_recovery_exposed_cycles\": "
        << total.speculative_fetch_shadow_recovery_exposed_cycles << ",\n";
    out << "    \"speculative_fetch_shadow_density_unavailable\": "
        << total.speculative_fetch_shadow_density_unavailable << ",\n";
    out << "    \"fetch_supply_static_span_lookups\": "
        << total.fetch_supply_static_span_lookups << ",\n";
    out << "    \"fetch_supply_static_span_unavailable\": "
        << total.fetch_supply_static_span_unavailable << ",\n";
    out << "    \"fetch_supply_cross_block_instructions\": "
        << total.fetch_supply_cross_block_instructions << ",\n";
    out << "    \"fetch_supply_cross_block_extra_requests\": "
        << total.fetch_supply_cross_block_extra_requests << ",\n";
    out << "    \"speculative_fetch_shadow_response_conserved\": "
        << (total.speculative_fetch_shadow_response_wait_cycles ==
                    total.speculative_fetch_shadow_recovery_hidden_cycles +
                        total.speculative_fetch_shadow_recovery_exposed_cycles
                ? "true"
                : "false")
        << ",\n";
    out << "    \"fetch_block_response_conserved\": "
        << (total.fetch_block_response_wait_cycles ==
                    total.fetch_block_response_hidden_cycles +
                        total.fetch_block_response_exposed_cycles
                ? "true"
                : "false")
        << ",\n";
    out << "    \"fetch_response_ledger_conserved\": "
        << (total.fetch_response_ledger_committed_requests +
                        total.fetch_response_ledger_shadow_requests ==
                    total.fetch_response_ledger_responses
                ? "true"
                : "false")
        << ",\n";
    out << "    \"l1i_accesses\": " << total.l1i.accesses << ",\n";
    out << "    \"l1i_hits\": " << total.l1i.hits << ",\n";
    out << "    \"l1i_misses\": " << total.l1i.misses << ",\n";
    out << "    \"l1i_evictions\": " << total.l1i.evictions << ",\n";
    out << "    \"l1i_miss_stall_cycles\": "
        << total.l1i_miss_stall_cycles << ",\n";
    out << "    \"instruction_page_map_lookups\": "
        << total.instruction_page_map_lookups << ",\n";
    out << "    \"instruction_page_map_hits\": "
        << total.instruction_page_map_hits << ",\n";
    out << "    \"instruction_page_map_misses\": "
        << total.instruction_page_map_misses << ",\n";
    out << "    \"modeled_instruction_page_lookups\": "
        << total.modeled_instruction_page_lookups << ",\n";
    out << "    \"physical_instruction_fetch_requests\": "
        << total.physical_instruction_fetch_requests << ",\n";
    out << "    \"modeled_instruction_fetch_requests\": "
        << total.modeled_instruction_fetch_requests << ",\n";
    out << "    \"physical_kernel_instruction_fetch_requests\": "
        << total.physical_kernel_instruction_fetch_requests << ",\n";
    out << "    \"instruction_fetch_lower_hierarchy_requests\": "
        << total.instruction_fetch_lower_hierarchy_requests << ",\n";
    out << "    \"instruction_fetch_request_order_clamps\": "
        << total.instruction_fetch_request_order_clamps << ",\n";
    out << "    \"instruction_fetch_request_order_clamp_cycles\": "
        << total.instruction_fetch_request_order_clamp_cycles << ",\n";
    out << "    \"l1i_speculative_entry_accesses\": "
        << total.l1i_speculative_entry_accesses << ",\n";
    out << "    \"l1i_speculative_entry_hits\": "
        << total.l1i_speculative_entry_hits << ",\n";
    out << "    \"l1i_speculative_entry_misses\": "
        << total.l1i_speculative_entry_misses << ",\n";
    out << "    \"l1i_speculative_entry_evictions\": "
        << total.l1i_speculative_entry_evictions << ",\n";
    out << "    \"l1i_speculative_entry_untracked\": "
        << total.l1i_speculative_entry_untracked << ",\n";
    out << "    \"l1i_speculative_path_records\": "
        << total.l1i_speculative_path_records << ",\n";
    out << "    \"l1i_speculative_path_accesses\": "
        << total.l1i_speculative_path_accesses << ",\n";
    out << "    \"l1i_speculative_path_hits\": "
        << total.l1i_speculative_path_hits << ",\n";
    out << "    \"l1i_speculative_path_misses\": "
        << total.l1i_speculative_path_misses << ",\n";
    out << "    \"l1i_speculative_path_evictions\": "
        << total.l1i_speculative_path_evictions << ",\n";
    out << "    \"l1i_speculative_path_static_instructions\": "
        << total.l1i_speculative_path_static_instructions << ",\n";
    out << "    \"l1i_speculative_path_operand_instructions\": "
        << total.l1i_speculative_path_operand_instructions << ",\n";
    out << "    \"l1i_speculative_path_read_registers\": "
        << total.l1i_speculative_path_read_registers << ",\n";
    out << "    \"l1i_speculative_path_write_registers\": "
        << total.l1i_speculative_path_write_registers << ",\n";
    out << "    \"l1i_speculative_path_operand_segments\": "
        << total.l1i_speculative_path_operand_segments << ",\n";
    out << "    \"l1i_speculative_path_raw_edges\": "
        << total.l1i_speculative_path_raw_edges << ",\n";
    out << "    \"l1i_speculative_path_dependent_instructions\": "
        << total.l1i_speculative_path_dependent_instructions << ",\n";
    out << "    \"l1i_speculative_path_chain_depth_sum\": "
        << total.l1i_speculative_path_chain_depth_sum << ",\n";
    out << "    \"l1i_speculative_path_chain_depth_max\": "
        << total.l1i_speculative_path_chain_depth_max << ",\n";
    out << "    \"l1i_speculative_path_operand_rob_prefix_uops_q16\": "
        << total.l1i_speculative_path_operand_rob_prefix_uops_q16 << ",\n";
    out << "    \"l1i_speculative_path_operand_rob_capped_instructions\": "
        << total.l1i_speculative_path_operand_rob_capped_instructions
        << ",\n";
    out << "    \"l1i_speculative_path_operand_rob_capped_read_registers\": "
        << total.l1i_speculative_path_operand_rob_capped_read_registers
        << ",\n";
    out << "    \"l1i_speculative_path_operand_rob_capped_write_registers\": "
        << total.l1i_speculative_path_operand_rob_capped_write_registers
        << ",\n";
    out << "    \"l1i_speculative_path_operand_rob_capped_memory_instructions\": "
        << total.l1i_speculative_path_operand_rob_capped_memory_instructions
        << ",\n";
    out << "    \"l1i_speculative_path_operand_rob_capped_memory_instructions_max_per_path\": "
        << total
               .l1i_speculative_path_operand_rob_capped_memory_instructions_max_per_path
        << ",\n";
    out << "    \"l1i_speculative_path_operand_rob_capped_write_registers_max_per_path\": "
        << total
               .l1i_speculative_path_operand_rob_capped_write_registers_max_per_path
        << ",\n";
    out << "    \"l1i_speculative_path_operand_rob_capped_raw_edges\": "
        << total.l1i_speculative_path_operand_rob_capped_raw_edges << ",\n";
    out << "    \"l1i_speculative_path_operand_rob_capped_dependent_instructions\": "
        << total
               .l1i_speculative_path_operand_rob_capped_dependent_instructions
        << ",\n";
    out << "    \"l1i_speculative_path_operand_rob_capped_chain_depth_sum\": "
        << total.l1i_speculative_path_operand_rob_capped_chain_depth_sum
        << ",\n";
    out << "    \"l1i_speculative_path_operand_rob_capped_chain_depth_max\": "
        << total.l1i_speculative_path_operand_rob_capped_chain_depth_max
        << ",\n";
    out << "    \"l1i_speculative_path_memory_instructions\": "
        << total.l1i_speculative_path_memory_instructions << ",\n";
    out << "    \"l1i_speculative_path_memory_page_known\": "
        << total.l1i_speculative_path_memory_page_known << ",\n";
    out << "    \"l1i_speculative_path_memory_page_unstable\": "
        << total.l1i_speculative_path_memory_page_unstable << ",\n";
    out << "    \"l1i_speculative_path_memory_page_transition_samples\": "
        << total.l1i_speculative_path_memory_page_transition_samples << ",\n";
    out << "    \"l1i_speculative_path_memory_page_transition_score_ppm\": "
        << total.l1i_speculative_path_memory_page_transition_score_ppm
        << ",\n";
    out << "    \"l1i_speculative_path_profiled_instructions\": "
        << total.l1i_speculative_path_profiled_instructions << ",\n";
    out << "    \"l1i_speculative_path_profile_uops_q16\": "
        << speculative_profile_uops_q16(total) << ",\n";
    out << "    \"l1i_speculative_path_profile_rob_capped_uops_q16\": "
        << total.l1i_speculative_path_profile_rob_capped_uops_q16
        << ",\n";
    out << "    \"l1i_speculative_path_profile_integer_uops_q16\": "
        << total.l1i_speculative_path_profile_uops_q16[0] << ",\n";
    out << "    \"l1i_speculative_path_profile_integer_multiply_uops_q16\": "
        << total.l1i_speculative_path_profile_uops_q16[1] << ",\n";
    out << "    \"l1i_speculative_path_profile_float_simple_uops_q16\": "
        << total.l1i_speculative_path_profile_uops_q16[2] << ",\n";
    out << "    \"l1i_speculative_path_profile_float_complex_uops_q16\": "
        << total.l1i_speculative_path_profile_uops_q16[3] << ",\n";
    out << "    \"l1i_speculative_path_profile_simd_uops_q16\": "
        << total.l1i_speculative_path_profile_uops_q16[4] << ",\n";
    out << "    \"l1i_speculative_path_profile_predicate_uops_q16\": "
        << total.l1i_speculative_path_profile_uops_q16[5] << ",\n";
    out << "    \"l1i_speculative_path_profile_memory_uops_q16\": "
        << total.l1i_speculative_path_profile_uops_q16[6] << ",\n";
    out << "    \"l1i_speculative_path_profile_system_uops_q16\": "
        << total.l1i_speculative_path_profile_uops_q16[7] << ",\n";
    out << "    \"l1i_speculative_path_conditional_stops\": "
        << total.l1i_speculative_path_conditional_stops << ",\n";
    out << "    \"l1i_speculative_path_indirect_stops\": "
        << total.l1i_speculative_path_indirect_stops << ",\n";
    out << "    \"l1i_speculative_path_static_map_misses\": "
        << total.l1i_speculative_path_static_map_misses << ",\n";
    out << "    \"l1i_speculative_path_unknown_edges\": "
        << total.l1i_speculative_path_unknown_edges << ",\n";
    out << "    \"speculative_dtlb_accesses\": "
        << total.speculative_dtlb.accesses << ",\n";
    out << "    \"speculative_dtlb_hits\": "
        << total.speculative_dtlb.hits << ",\n";
    out << "    \"speculative_dtlb_misses\": "
        << total.speculative_dtlb.misses << ",\n";
    out << "    \"speculative_dtlb_untracked\": "
        << total.speculative_dtlb.untracked << ",\n";
    out << "    \"user_functional_pmu\": ";
    if (native_kernel_trace) {
        out << "{\"status\": \"unavailable\", "
               "\"reason\": \"native combined replay exposes exact "
               "user instruction counts but not a privilege-partitioned "
               "cache PMU\"}";
    } else {
        write_user_functional_pmu(out, total, stats.llc, cha_total);
    }
    out << ",\n";
    out << "    \"native_user_plus_kernel_functional_pmu\": ";
    if (native_kernel_trace) {
        write_user_functional_pmu(out, total, stats.llc, cha_total);
    } else {
        out << "{\"status\": \"not-enabled\"}";
    }
    out << ",\n";
    out << "    \"native_kernel_trace\": {"
        << "\"records\": " << total.native_kernel_records
        << ", \"retired_uops\": "
        << total.native_kernel_retired_uops
        << ", \"retired_instructions\": "
        << total.native_kernel_retired_instructions
        << ", \"memory_uops\": "
        << total.native_kernel_memory_uops
        << ", \"line_requests\": "
        << total.native_kernel_memory_accesses
        << ", \"branches\": "
        << total.native_kernel_branch.branches
        << ", \"branch_misses\": "
        << total.native_kernel_branch.misses
        << ", \"branch_direction_only_misses\": "
        << total.native_kernel_branch.direction_only_misses
        << ", \"branch_target_unavailable_misses\": "
        << total.native_kernel_branch.target_unavailable_misses
        << ", \"branch_wrong_target_misses\": "
        << total.native_kernel_branch.wrong_target_misses
        << ", \"branch_miss_population_conserved\": "
        << (total.native_kernel_branch.miss_population_conserved()
                ? "true"
                : "false")
        << ", \"ras_pushes\": "
        << total.native_kernel_branch.ras_pushes
        << ", \"ras_pops\": "
        << total.native_kernel_branch.ras_pops
        << ", \"ras_predictions\": "
        << total.native_kernel_branch.ras_predictions
        << ", \"ras_hits\": "
        << total.native_kernel_branch.ras_hits
        << ", \"ras_static_return_targets\": "
        << total.native_kernel_branch.ras_static_return_targets
        << ", \"ras_learned_return_targets\": "
        << total.native_kernel_branch.ras_learned_return_targets
        << ", \"ras_unknown_return_targets\": "
        << total.native_kernel_branch.ras_unknown_return_targets
        << ", \"ras_source_conserved\": "
        << (total.native_kernel_branch.ras_source_conserved()
                ? "true"
                : "false")
        << ", \"dtlb_accesses\": "
        << total.native_kernel_dtlb.accesses
        << ", \"dtlb_misses\": "
        << total.native_kernel_dtlb.misses << "},\n";
    out << "    \"synthetic_syscall_kernel\": ";
    write_kernel_event_counters(out, total.syscall_kernel);
    out << ",\n";
    out << "    \"synthetic_page_fault_kernel\": ";
    write_kernel_event_counters(out, total.page_fault_kernel);
    out << ",\n";
    out << "    \"synthetic_irq_kernel\": ";
    write_kernel_event_counters(out, total.irq_kernel);
    out << ",\n";
    out << "    \"synthetic_kernel_total\": ";
    write_kernel_event_counters(out, synthetic_kernel_total);
    out << ",\n";
    out << "    \"user_plus_synthetic_kernel_pmu\": ";
    if (native_kernel_trace) {
        out << "{\"status\": \"not-applicable-native-trace\"}";
    } else {
        write_user_plus_kernel_pmu(
            out, total, stats.llc, cha_total, synthetic_kernel_total, true);
    }
    out << ",\n";
    out << "    \"sum_core_cycles\": " << total.cycles << ",\n";
    out << "    \"simulated_makespan_cycles\": "
        << makespan_cycles << ",\n";
    out << "    \"aggregate_ipc\": "
        << ratio(total.retired_instructions, makespan_cycles) << ",\n";
    out << "    \"branch_penalty_cycles\": "
        << total.branch_penalty_cycles << ",\n";
    out << "    \"branch_shadow_uops\": "
        << total.branch_shadow_uops << ",\n";
    out << "    \"branch_shadow_cycles\": "
        << total.branch_shadow_cycles << ",\n";
    out << "    \"branch_population_audit\": ";
    write_branch_population_audit(out, total.branch_population);
    out << ",\n";
    out << "    \"exposed_memory_penalty_cycles\": "
        << total.memory_penalty_cycles << ",\n";
    out << "    \"memory_order_clamp_events\": "
        << total.memory_order_clamp_events << ",\n";
    out << "    \"memory_order_clamp_cycles\": "
        << total.memory_order_clamp_cycles << ",\n";
    out << "    \"response_latency_samples\": "
        << stats.response_latency_samples << ",\n";
    out << "    \"response_rename_conserved\": "
        << (response_rename.conserved() ? "true" : "false") << ",\n";
    out << "    \"response_rename_destination_uops\": "
        << response_rename.destination_uops << ",\n";
    out << "    \"response_rename_free_list_stall_uops\": "
        << response_rename.free_list_stall_uops << ",\n";
    out << "    \"response_rename_free_list_stall_cycles\": "
        << response_rename.free_list_stall_cycles << ",\n";
    out << "    \"response_rename_classes\": [";
    static constexpr std::array<const char*, 4> response_class_names{
        "int", "float", "vec", "cc"};
    for (std::size_t index = 0;
         index < response_class_names.size(); ++index) {
        if (index != 0) out << ", ";
        out << "{\"class\": \"" << response_class_names[index]
            << "\", \"allocated\": "
            << response_rename.allocated[index]
            << ", \"released\": "
            << response_rename.released[index]
            << ", \"live\": "
            << response_rename.live[index]
            << ", \"max_live\": "
            << response_rename.max_live[index] << "}";
    }
    out << "],\n";
    out << "    \"response_latency_cycles\": "
        << stats.response_latency_cycles << ",\n";
    out << "    \"response_l1_samples\": "
        << stats.response_l1_samples << ",\n";
    out << "    \"response_l1_latency_cycles\": "
        << stats.response_l1_latency_cycles << ",\n";
    out << "    \"response_l2_samples\": "
        << stats.response_l2_samples << ",\n";
    out << "    \"response_l2_latency_cycles\": "
        << stats.response_l2_latency_cycles << ",\n";
    out << "    \"response_escape_samples\": "
        << stats.response_escape_samples << ",\n";
    out << "    \"response_escape_latency_cycles\": "
        << stats.response_escape_latency_cycles << ",\n";
    out << "    \"response_critical_total_cycles\": "
        << response_critical.total_cycles << ",\n";
    out << "    \"response_critical_rename_free_list_cycles\": "
        << response_critical.rename_free_list_cycles << ",\n";
    out << "    \"response_critical_dispatch_bandwidth_cycles\": "
        << response_critical.dispatch_bandwidth_cycles << ",\n";
    out << "    \"response_critical_rob_capacity_cycles\": "
        << response_critical.rob_capacity_cycles << ",\n";
    out << "    \"response_critical_iq_capacity_cycles\": "
        << response_critical.iq_capacity_cycles << ",\n";
    out << "    \"response_critical_lq_capacity_cycles\": "
        << response_critical.lq_capacity_cycles << ",\n";
    out << "    \"response_critical_sq_capacity_cycles\": "
        << response_critical.sq_capacity_cycles << ",\n";
    out << "    \"response_critical_dependency_cycles\": "
        << response_critical.dependency_cycles << ",\n";
    out << "    \"response_critical_sequencer_cycles\": "
        << response_critical.sequencer_cycles << ",\n";
    out << "    \"response_critical_l1_mshr_cycles\": "
        << response_critical.l1_mshr_cycles << ",\n";
    out << "    \"response_critical_l2_mshr_cycles\": "
        << response_critical.l2_mshr_cycles << ",\n";
    out << "    \"response_critical_instruction_fetch_cycles\": "
        << response_critical.instruction_fetch_cycles << ",\n";
    out << "    \"response_critical_memory_response_cycles\": "
        << response_critical.memory_response_cycles << ",\n";
    out << "    \"response_critical_commit_bandwidth_cycles\": "
        << response_critical.commit_bandwidth_cycles << ",\n";
    out << "    \"response_critical_tso_store_cycles\": "
        << response_critical.tso_store_cycles << ",\n";
    out << "    \"response_critical_unattributed_cycles\": "
        << response_critical.unattributed_cycles << ",\n";
    out << "    \"response_critical_conserved\": "
        << (response_critical.total_cycles ==
                    response_critical.classified_cycles()
                ? "true"
                : "false")
        << ",\n";
    out << "    \"response_residual_instruction_fetch_seed_events\": "
        << response_residual.instruction_fetch_seed_events << ",\n";
    out << "    \"response_residual_instruction_fetch_seed_cycles\": "
        << response_residual.instruction_fetch_seed_cycles << ",\n";
    out << "    \"response_residual_seed_events\": "
        << response_residual.response_seed_events << ",\n";
    out << "    \"response_residual_seed_uops\": "
        << response_residual.response_seed_uops << ",\n";
    out << "    \"response_residual_seed_cycles\": "
        << response_residual.response_seed_cycles << ",\n";
    out << "    \"response_residual_completion_extended_uops\": "
        << response_residual.completion_extended_uops << ",\n";
    out << "    \"response_residual_completion_extension_cycles\": "
        << response_residual.completion_extension_cycles << ",\n";
    out << "    \"response_residual_dependency_edges\": "
        << response_residual.dependency_edges << ",\n";
    out << "    \"response_residual_dependency_input_cycles\": "
        << response_residual.dependency_input_cycles << ",\n";
    out << "    \"response_residual_dependency_absorbed_cycles\": "
        << response_residual.dependency_absorbed_cycles << ",\n";
    out << "    \"response_residual_dependency_propagated_cycles\": "
        << response_residual.dependency_propagated_cycles << ",\n";
    out << "    \"response_residual_dependency_conserved\": "
        << (response_residual.dependency_conserved() ? "true" : "false")
        << ",\n";
    out << "    \"response_residual_retire_seed_uops\": "
        << response_residual.retire_seed_uops << ",\n";
    out << "    \"response_residual_retire_input_cycles\": "
        << response_residual.retire_input_cycles << ",\n";
    out << "    \"response_residual_retire_absorbed_cycles\": "
        << response_residual.retire_absorbed_cycles << ",\n";
    out << "    \"response_residual_retire_propagated_cycles\": "
        << response_residual.retire_propagated_cycles << ",\n";
    out << "    \"response_residual_retire_conserved\": "
        << (response_residual.retire_conserved() ? "true" : "false")
        << ",\n";
    out << "    \"response_residual_ordered_retire_moved_uops\": "
        << response_residual.ordered_retire_moved_uops << ",\n";
    out << "    \"response_residual_ordered_retire_moved_cycles\": "
        << response_residual.ordered_retire_moved_cycles << ",\n";
    out << "    \"response_residual_dispatch_moved_uops\": "
        << response_residual.dispatch_moved_uops << ",\n";
    out << "    \"response_residual_dispatch_moved_cycles\": "
        << response_residual.dispatch_moved_cycles << ",\n";
    out << "    \"response_residual_fetch_queue_moved_uops\": "
        << response_residual.fetch_queue_moved_uops << ",\n";
    out << "    \"response_residual_fetch_queue_moved_cycles\": "
        << response_residual.fetch_queue_moved_cycles << ",\n";
    out << "    \"response_residual_memory_issue_moved_events\": "
        << response_residual.memory_issue_moved_events << ",\n";
    out << "    \"response_residual_memory_issue_moved_cycles\": "
        << response_residual.memory_issue_moved_cycles << ",\n";
    out << "    \"response_residual_stage_uops\": "
        << response_residual.stage_uops << ",\n";
    out << "    \"response_residual_stage_memory_uops\": "
        << response_residual.stage_memory_uops << ",\n";
    out << "    \"response_residual_stage_non_memory_uops\": "
        << response_residual.stage_non_memory_uops << ",\n";
    out << "    \"response_residual_stage_non_memory_base_fetch_to_issue_cycles\": "
        << response_residual.stage_non_memory_base_fetch_to_issue_cycles
        << ",\n";
    out << "    \"response_residual_stage_non_memory_corrected_fetch_to_issue_cycles\": "
        << response_residual.stage_non_memory_corrected_fetch_to_issue_cycles
        << ",\n";
    out << "    \"response_residual_stage_non_memory_corrected_issue_to_retire_cycles\": "
        << response_residual.stage_non_memory_corrected_issue_to_retire_cycles
        << ",\n";
    out << "    \"response_residual_stage_load_uops\": "
        << response_residual.stage_load_uops << ",\n";
    out << "    \"response_residual_stage_load_base_fetch_to_issue_cycles\": "
        << response_residual.stage_load_base_fetch_to_issue_cycles << ",\n";
    out << "    \"response_residual_stage_load_corrected_fetch_to_issue_cycles\": "
        << response_residual.stage_load_corrected_fetch_to_issue_cycles
        << ",\n";
    out << "    \"response_residual_stage_load_corrected_issue_to_completion_cycles\": "
        << response_residual
               .stage_load_corrected_issue_to_completion_cycles
        << ",\n";
    out << "    \"response_residual_stage_load_corrected_issue_to_retire_cycles\": "
        << response_residual.stage_load_corrected_issue_to_retire_cycles
        << ",\n";
    out << "    \"response_residual_stage_base_issue_to_completion_cycles\": "
        << response_residual.stage_base_issue_to_completion_cycles
        << ",\n";
    out << "    \"response_residual_stage_base_completion_to_retire_cycles\": "
        << response_residual.stage_base_completion_to_retire_cycles
        << ",\n";
    out << "    \"response_residual_stage_base_issue_to_retire_cycles\": "
        << response_residual.stage_base_issue_to_retire_cycles
        << ",\n";
    out << "    \"response_residual_stage_corrected_issue_to_completion_cycles\": "
        << response_residual.stage_corrected_issue_to_completion_cycles
        << ",\n";
    out << "    \"response_residual_stage_corrected_completion_to_retire_cycles\": "
        << response_residual.stage_corrected_completion_to_retire_cycles
        << ",\n";
    out << "    \"response_residual_stage_corrected_issue_to_retire_cycles\": "
        << response_residual.stage_corrected_issue_to_retire_cycles
        << ",\n";
    out << "    \"response_residual_stage_issue_delay_cycles\": "
        << response_residual.stage_issue_delay_cycles << ",\n";
    out << "    \"response_residual_stage_completion_delay_cycles\": "
        << response_residual.stage_completion_delay_cycles << ",\n";
    out << "    \"response_residual_stage_retire_delay_cycles\": "
        << response_residual.stage_retire_delay_cycles << ",\n";
    out << "    \"response_residual_stage_memory_base_issue_to_retire_cycles\": "
        << response_residual.stage_memory_base_issue_to_retire_cycles
        << ",\n";
    out << "    \"response_residual_stage_memory_corrected_issue_to_retire_cycles\": "
        << response_residual.stage_memory_corrected_issue_to_retire_cycles
        << ",\n";
    out << "    \"response_residual_head_gap_zero_commit_cycles\": "
        << response_residual.head_gap_zero_commit_cycles << ",\n";
    out << "    \"response_residual_head_gap_not_fetched_cycles\": "
        << response_residual.head_gap_not_fetched_cycles << ",\n";
    out << "    \"response_residual_head_gap_fetched_not_issued_cycles\": "
        << response_residual.head_gap_fetched_not_issued_cycles << ",\n";
    out << "    \"response_residual_head_gap_issued_not_retired_cycles\": "
        << response_residual.head_gap_issued_not_retired_cycles << ",\n";
    out << "    \"response_residual_head_gap_issued_load_cycles\": "
        << response_residual.head_gap_issued_load_cycles << ",\n";
    out << "    \"response_residual_head_gap_issued_store_cycles\": "
        << response_residual.head_gap_issued_store_cycles << ",\n";
    out << "    \"response_residual_head_gap_issued_non_memory_cycles\": "
        << response_residual.head_gap_issued_non_memory_cycles << ",\n";
    out << "    \"response_residual_head_gap_conserved\": "
        << (response_residual.head_gap_conserved() ? "true" : "false")
        << ",\n";
    out << "    \"response_residual_stage_conserved\": "
        << (response_residual.stage_conserved() ? "true" : "false")
        << ",\n";
    out << "    \"response_residual_escape_issue_moved_events\": "
        << response_residual.escape_issue_moved_events << ",\n";
    out << "    \"response_residual_escape_issue_moved_cycles\": "
        << response_residual.escape_issue_moved_cycles << ",\n";
    out << "    \"response_store_uops\": "
        << response_residual.store_uops << ",\n";
    out << "    \"response_store_address_to_commit_cycles\": "
        << response_residual.store_address_to_commit_cycles << ",\n";
    out << "    \"response_store_hierarchy_response_before_commit_uops\": "
        << response_residual.store_hierarchy_response_before_commit_uops
        << ",\n";
    out << "    \"response_store_hierarchy_response_before_commit_cycles\": "
        << response_residual.store_hierarchy_response_before_commit_cycles
        << ",\n";
    out << "    \"response_store_tso_wait_uops\": "
        << response_residual.store_tso_wait_uops << ",\n";
    out << "    \"response_store_tso_wait_cycles\": "
        << response_residual.store_tso_wait_cycles << ",\n";
    out << "    \"response_store_send_to_response_cycles\": "
        << response_residual.store_send_to_response_cycles << ",\n";
    out << "    \"response_store_commit_to_sq_release_cycles\": "
        << response_residual.store_commit_to_sq_release_cycles << ",\n";
    out << "    \"response_store_sq_release_max_cycles\": "
        << response_residual.store_sq_release_max_cycles << ",\n";
    out << "    \"response_store_send_retimed_uops\": "
        << response_residual.store_send_retimed_uops << ",\n";
    out << "    \"response_store_send_retimed_cycles\": "
        << response_residual.store_send_retimed_cycles << ",\n";
    out << "    \"response_store_lifecycle_conserved\": "
        << (response_residual.store_lifecycle_conserved()
                ? "true"
                : "false")
        << ",\n";
    out << "    \"l1d_accesses\": " << total.l1d.accesses << ",\n";
    out << "    \"l1d_hits\": " << total.l1d.hits << ",\n";
    out << "    \"l1d_misses\": " << total.l1d.misses << ",\n";
    out << "    \"l1d_evictions\": " << total.l1d.evictions << ",\n";
    out << "    \"l1d_writebacks\": " << total.l1d.writebacks << ",\n";
    out << "    \"instruction_l2_accesses\": "
        << total.instruction_l2.accesses << ",\n";
    out << "    \"instruction_l2_hits\": "
        << total.instruction_l2.hits << ",\n";
    out << "    \"instruction_l2_misses\": "
        << total.instruction_l2.misses << ",\n";
    out << "    \"instruction_l2_evictions\": "
        << total.instruction_l2.evictions << ",\n";
    out << "    \"instruction_l2_writebacks\": "
        << total.instruction_l2.writebacks << ",\n";
    out << "    \"l2_accesses\": " << total.l2.accesses << ",\n";
    out << "    \"l2_hits\": " << total.l2.hits << ",\n";
    out << "    \"l2_misses\": " << total.l2.misses << ",\n";
    out << "    \"l2_evictions\": " << total.l2.evictions << ",\n";
    out << "    \"l2_writebacks\": " << total.l2.writebacks << ",\n";
    out << "    \"dtlb_accesses\": " << total.dtlb.accesses << ",\n";
    out << "    \"dtlb_hits\": " << total.dtlb.hits << ",\n";
    out << "    \"dtlb_misses\": " << total.dtlb.misses << ",\n";
    out << "    \"dtlb_merged_misses\": "
        << total.dtlb.merged_misses << ",\n";
    out << "    \"dtlb_untracked\": " << total.dtlb.untracked << ",\n";
    out << "    \"dtlb_conserved\": "
        << (total.dtlb.conserved() ? "true" : "false") << ",\n";
    out << "    \"dtlb_timing_accesses\": "
        << total.dtlb_timing.accesses << ",\n";
    out << "    \"dtlb_timing_hits\": "
        << total.dtlb_timing.hits << ",\n";
    out << "    \"dtlb_timing_misses\": "
        << total.dtlb_timing.misses << ",\n";
    out << "    \"dtlb_timing_merged_misses\": "
        << total.dtlb_timing.merged_misses << ",\n";
    out << "    \"dtlb_timing_untracked\": "
        << total.dtlb_timing.untracked << ",\n";
    out << "    \"dtlb_timing_conserved\": "
        << (total.dtlb_timing.conserved() ? "true" : "false") << ",\n";
    out << "    \"dtlb_timing_walk_delay_cycles\": "
        << total.dtlb_timing.walk_delay_cycles << ",\n";
    // Backward-compatible alias. Counts above remain architectural; only the
    // historical delay field names the timing-walker quantity.
    out << "    \"dtlb_walk_delay_cycles\": "
        << total.dtlb_timing.walk_delay_cycles << ",\n";
    out << "    \"o3_iq_full_events\": "
        << o3.iq_full_events << ",\n";
    out << "    \"o3_iq_stall_cycles\": "
        << o3.iq_stall_cycles << ",\n";
    out << "    \"o3_iq_max_occupancy\": "
        << o3.iq_max_occupancy << ",\n";
    out << "    \"o3_rob_full_events\": "
        << o3.rob_full_events << ",\n";
    out << "    \"o3_rob_stall_cycles\": "
        << o3.rob_stall_cycles << ",\n";
    out << "    \"o3_rob_max_occupancy\": "
        << o3.rob_max_occupancy << ",\n";
    out << "    \"o3_lq_full_events\": "
        << o3.lq_full_events << ",\n";
    out << "    \"o3_lq_stall_cycles\": "
        << o3.lq_stall_cycles << ",\n";
    out << "    \"o3_lq_max_occupancy\": "
        << o3.lq_max_occupancy << ",\n";
    out << "    \"o3_sq_full_events\": "
        << o3.sq_full_events << ",\n";
    out << "    \"o3_sq_stall_cycles\": "
        << o3.sq_stall_cycles << ",\n";
    out << "    \"o3_sq_max_occupancy\": "
        << o3.sq_max_occupancy << ",\n";
    out << "    \"o3_tso_store_stall_cycles\": "
        << o3.tso_store_stall_cycles << ",\n";
    out << "    \"committed_pipeline_audit\": ";
    write_committed_pipeline_audit(out, committed_pipeline_audit);
    out << ",\n";
    out << "    \"committed_epoch_audit\": ";
    write_committed_epoch_audit(
        out, committed_epoch_audit, total.retired_uops);
    out << ",\n";
    out << "    \"ruby_sequencer_requests\": "
        << sequencer.requests << ",\n";
    out << "    \"ruby_sequencer_buffer_full_stalls\": "
        << sequencer.buffer_full_stalls << ",\n";
    out << "    \"ruby_sequencer_stall_cycles\": "
        << sequencer.stall_cycles << ",\n";
    out << "    \"ruby_sequencer_max_outstanding\": "
        << sequencer.max_outstanding << ",\n";
    out << "    \"llc_accesses\": " << stats.llc.accesses << ",\n";
    out << "    \"llc_hits\": " << stats.llc.hits << ",\n";
    out << "    \"llc_misses\": " << stats.llc.misses << ",\n";
    out << "    \"llc_outcomes_conserved\": "
        << (cha_total.llc_outcomes_conserved() &&
                    stats.llc.accesses == cha_total.requests &&
                    stats.llc.hits == cha_total.llc_hits &&
                    stats.llc.misses == cha_total.llc_misses
                ? "true"
                : "false")
        << ",\n";
    out << "    \"llc_evictions\": " << stats.llc.evictions << ",\n";
    out << "    \"llc_writebacks\": " << stats.llc.writebacks << ",\n";
    out << "    \"instruction_llc_accesses\": "
        << stats.instruction_llc.accesses << ",\n";
    out << "    \"instruction_llc_hits\": "
        << stats.instruction_llc.hits << ",\n";
    out << "    \"instruction_llc_misses\": "
        << stats.instruction_llc.misses << ",\n";
    out << "    \"instruction_llc_outcomes_conserved\": "
        << (instruction_cha_total.llc_outcomes_conserved() &&
                    stats.instruction_llc.accesses ==
                        instruction_cha_total.requests &&
                    stats.instruction_llc.hits ==
                        instruction_cha_total.llc_hits &&
                    stats.instruction_llc.misses ==
                        instruction_cha_total.llc_misses
                ? "true"
                : "false")
        << ",\n";
    out << "    \"instruction_llc_evictions\": "
        << stats.instruction_llc.evictions << ",\n";
    out << "    \"instruction_llc_writebacks\": "
        << stats.instruction_llc.writebacks << ",\n";
    out << "    \"instruction_llc_unique_fills\": "
        << instruction_cha_total.llc_unique_fills << ",\n";
    out << "    \"instruction_llc_merged_misses\": "
        << instruction_cha_total.llc_merged_misses << ",\n";
    out << "    \"instruction_dram_reads\": "
        << instruction_cha_total.dram_reads << ",\n";
    out << "    \"instruction_dram_writes\": "
        << instruction_cha_total.dram_writes << ",\n";
    out << "    \"llc_unique_fills\": "
        << cha_total.llc_unique_fills << ",\n";
    out << "    \"llc_merged_misses\": "
        << cha_total.llc_merged_misses << ",\n";
    out << "    \"llc_merged_wait_cycles\": "
        << cha_total.llc_merged_wait_cycles << ",\n";
    out << "    \"llc_merged_wait_max_cycles\": "
        << cha_total.llc_merged_wait_max_cycles << ",\n";
    out << "    \"branches\": " << total.branch.branches << ",\n";
    out << "    \"conditional_branches\": "
        << total.branch.conditional << ",\n";
    out << "    \"branch_direction_misses\": "
        << total.branch.direction_misses << ",\n";
    out << "    \"branch_direction_only_misses\": "
        << total.branch.direction_only_misses << ",\n";
    out << "    \"branch_target_unavailable_misses\": "
        << total.branch.target_unavailable_misses << ",\n";
    out << "    \"branch_wrong_target_misses\": "
        << total.branch.wrong_target_misses << ",\n";
    out << "    \"branch_masked_direction_misses\": "
        << total.branch.masked_direction_misses << ",\n";
    out << "    \"branch_target_misses\": "
        << total.branch.target_misses << ",\n";
    out << "    \"branch_misses\": " << total.branch.misses << ",\n";
    out << "    \"branch_miss_population_conserved\": "
        << (total.branch.miss_population_conserved() ? "true" : "false")
        << ",\n";
    out << "    \"branch_history_checkpoints\": "
        << total.branch.history_checkpoints << ",\n";
    out << "    \"branch_history_squashes\": "
        << total.branch.history_squashes << ",\n";
    out << "    \"branch_deferred_direction_commits\": "
        << total.branch.deferred_direction_commits << ",\n";
    out << "    \"btb_hits\": " << total.branch.btb_hits << ",\n";
    out << "    \"ras_hits\": " << total.branch.ras_hits << ",\n";
    out << "    \"ras_pushes\": " << total.branch.ras_pushes << ",\n";
    out << "    \"ras_pops\": " << total.branch.ras_pops << ",\n";
    out << "    \"ras_predictions\": "
        << total.branch.ras_predictions << ",\n";
    out << "    \"ras_static_return_targets\": "
        << total.branch.ras_static_return_targets << ",\n";
    out << "    \"ras_learned_return_targets\": "
        << total.branch.ras_learned_return_targets << ",\n";
    out << "    \"ras_unknown_return_targets\": "
        << total.branch.ras_unknown_return_targets << ",\n";
    out << "    \"ras_static_return_target_coverage\": "
        << ratio(total.branch.ras_static_return_targets,
                 total.branch.ras_pushes)
        << ",\n";
    out << "    \"ras_source_conserved\": "
        << (total.branch.ras_source_conserved() ? "true" : "false")
        << ",\n";
    out << "    \"branch_miss_rate\": "
        << ratio(total.branch.misses, total.branch.branches) << "\n";
    out << "  },\n";
    out << "  \"causal_frontier\": {\n";
    out << "    \"chunks_consumed\": " << stats.chunks_consumed << ",\n";
    out << "    \"frontier_waits\": " << stats.frontier_waits << ",\n";
    out << "    \"max_resident_chunks\": "
        << stats.max_resident_chunks << ",\n";
    out << "    \"interval_steps\": " << stats.interval_steps << ",\n";
    out << "    \"interval_zero_progress_steps\": "
        << stats.interval_zero_progress_steps << ",\n";
    out << "    \"interval_accepted_uops\": "
        << stats.interval_accepted_uops << ",\n";
    out << "    \"interval_active_prefixes\": "
        << stats.interval_active_prefixes << ",\n";
    out << "    \"max_interval_accepted_uops\": "
        << stats.max_interval_accepted_uops << ",\n";
    out << "    \"epoch_lookahead_chunks\": "
        << stats.epoch_lookahead_chunks << ",\n";
    out << "    \"epoch_inflight_memory_uops\": "
        << stats.epoch_inflight_memory_uops << ",\n";
    out << "    \"epoch_corrected_horizon_violations\": "
        << stats.epoch_corrected_horizon_violations << ",\n";
    out << "    \"epoch_corrected_issue_horizon_events\": "
        << stats.epoch_corrected_issue_horizon_events << ",\n";
    out << "    \"epoch_corrected_issue_horizon_uops\": "
        << stats.epoch_corrected_issue_horizon_uops << ",\n";
    out << "    \"epoch_corrected_issue_horizon_cycles\": "
        << stats.epoch_corrected_issue_horizon_cycles << ",\n";
    out << "    \"epoch_corrected_issue_horizon_max_cycles\": "
        << stats.epoch_corrected_issue_horizon_max_cycles << ",\n";
    out << "    \"corrected_suffix_candidate_epochs\": "
        << stats.corrected_suffix_candidate_epochs << ",\n";
    out << "    \"corrected_suffix_stable_epochs\": "
        << stats.corrected_suffix_stable_epochs << ",\n";
    out << "    \"corrected_suffix_passes\": "
        << stats.corrected_suffix_passes << ",\n";
    out << "    \"corrected_suffix_preflight_epochs\": "
        << stats.corrected_suffix_preflight_epochs << ",\n";
    out << "    \"corrected_suffix_preflight_passes\": "
        << stats.corrected_suffix_preflight_passes << ",\n";
    out << "    \"corrected_suffix_deferred_uops\": "
        << stats.corrected_suffix_deferred_uops << ",\n";
    out << "    \"corrected_suffix_deferred_memory_events\": "
        << stats.corrected_suffix_deferred_memory_events << ",\n";
    out << "    \"corrected_suffix_conservative_epochs\": "
        << stats.corrected_suffix_conservative_epochs << ",\n";
    out << "    \"epoch_advanced_cycles\": "
        << stats.epoch_advanced_cycles << ",\n";
    out << "    \"batch_memory_events\": "
        << stats.batch_memory_events << ",\n";
    out << "    \"interval_private_memory_events\": "
        << stats.interval_private_memory_events << ",\n";
    out << "    \"interval_escape_memory_events\": "
        << stats.interval_escape_memory_events << ",\n";
    out << "    \"private_preview_epochs\": "
        << stats.private_preview_epochs << ",\n";
    out << "    \"private_preview_partial_epochs\": "
        << stats.private_preview_partial_epochs << ",\n";
    out << "    \"private_preview_events\": "
        << stats.private_preview_events << ",\n";
    out << "    \"private_preview_unsafe_events\": "
        << stats.private_preview_unsafe_events << ",\n";
    out << "    \"private_preview_safe_cores\": "
        << stats.private_preview_safe_cores << ",\n";
    out << "    \"private_preview_unsafe_cores\": "
        << stats.private_preview_unsafe_cores << ",\n";
    out << "    \"private_preview_bypass_epochs\": "
        << stats.private_preview_bypass_epochs << ",\n";
    out << "    \"private_preview_bypass_events\": "
        << stats.private_preview_bypass_events << ",\n";
    out << "    \"materialized_escape_events\": "
        << stats.materialized_escape_events << ",\n";
    out << "    \"state_certificate_failures\": "
        << stats.state_certificate_failures << ",\n";
    out << "    \"state_certificate_wall_ns\": "
        << stats.state_certificate_wall_ns << ",\n";
    out << "    \"timing_certificate_failures\": "
        << stats.timing_certificate_failures << ",\n";
    out << "    \"timing_reweave_passes\": "
        << stats.timing_reweave_passes << ",\n";
    out << "    \"replayed_shared_events\": "
        << stats.replayed_shared_events << ",\n";
    out << "    \"canonical_fallback_epochs\": "
        << stats.canonical_fallback_epochs << ",\n";
    out << "    \"corrected_arrival_candidate_epochs\": "
        << stats.corrected_arrival_candidate_epochs << ",\n";
    out << "    \"corrected_arrival_conflict_components\": "
        << stats.corrected_arrival_conflict_components << ",\n";
    out << "    \"corrected_arrival_component_events\": "
        << stats.corrected_arrival_component_events << ",\n";
    out << "    \"corrected_arrival_max_component_events\": "
        << stats.corrected_arrival_max_component_events << ",\n";
    out << "    \"corrected_arrival_replay_epochs\": "
        << stats.corrected_arrival_replay_epochs << ",\n";
    out << "    \"corrected_arrival_replayed_events\": "
        << stats.corrected_arrival_replayed_events << ",\n";
    out << "    \"corrected_arrival_stable_epochs\": "
        << stats.corrected_arrival_stable_epochs << ",\n";
    out << "    \"corrected_arrival_fallback_epochs\": "
        << stats.corrected_arrival_fallback_epochs << ",\n";
    out << "    \"causal_timing_candidate_epochs\": "
        << stats.causal_timing_candidate_epochs << ",\n";
    out << "    \"causal_timing_noop_epochs\": "
        << stats.causal_timing_noop_epochs << ",\n";
    out << "    \"causal_timing_stable_epochs\": "
        << stats.causal_timing_stable_epochs << ",\n";
    out << "    \"causal_timing_fallback_epochs\": "
        << stats.causal_timing_fallback_epochs << ",\n";
    out << "    \"causal_timing_deferred_epochs\": "
        << stats.causal_timing_deferred_epochs << ",\n";
    out << "    \"causal_closure_components\": "
        << stats.causal_closure_components << ",\n";
    out << "    \"causal_closure_events\": "
        << stats.causal_closure_events << ",\n";
    out << "    \"causal_max_closure_events\": "
        << stats.causal_max_closure_events << ",\n";
    out << "    \"causal_timing_passes\": "
        << stats.causal_timing_passes << ",\n";
    out << "    \"causal_timing_replayed_events\": "
        << stats.causal_timing_replayed_events << ",\n";
    out << "    \"causal_timing_wall_ns\": "
        << stats.causal_timing_wall_ns << ",\n";
    out << "    \"response_retime_candidate_epochs\": "
        << stats.response_retime_candidate_epochs << ",\n";
    out << "    \"response_retime_noop_epochs\": "
        << stats.response_retime_noop_epochs << ",\n";
    out << "    \"response_retime_stable_epochs\": "
        << stats.response_retime_stable_epochs << ",\n";
    out << "    \"response_retime_fallback_epochs\": "
        << stats.response_retime_fallback_epochs << ",\n";
    out << "    \"response_retime_moved_shared_events\": "
        << stats.response_retime_moved_shared_events << ",\n";
    out << "    \"response_retime_replayed_events\": "
        << stats.response_retime_replayed_events << ",\n";
    out << "    \"response_retime_wall_ns\": "
        << stats.response_retime_wall_ns << ",\n";
    out << "    \"store_post_commit_request_events\": "
        << stats.store_post_commit_request_events << ",\n";
    out << "    \"store_post_commit_request_delay_cycles\": "
        << stats.store_post_commit_request_delay_cycles << ",\n";
    out << "    \"store_post_commit_request_max_delay_cycles\": "
        << stats.store_post_commit_request_max_delay_cycles << ",\n";
    out << "    \"store_post_commit_request_reordered_events\": "
        << stats.store_post_commit_request_reordered_events << ",\n";
    out << "    \"store_post_commit_request_boundary_deferred_uops\": "
        << stats.store_post_commit_request_boundary_deferred_uops
        << ",\n";
    out << "    \"time_epoch_request_boundary_deferred_uops\": "
        << stats.time_epoch_request_boundary_deferred_uops
        << ",\n";
    out << "    \"store_post_commit_request_candidate_epochs\": "
        << stats.store_post_commit_request_candidate_epochs << ",\n";
    out << "    \"store_post_commit_request_stable_epochs\": "
        << stats.store_post_commit_request_stable_epochs << ",\n";
    out << "    \"store_post_commit_request_fallback_epochs\": "
        << stats.store_post_commit_request_fallback_epochs << ",\n";
    out << "    \"store_post_commit_request_horizon_fallback_epochs\": "
        << stats.store_post_commit_request_horizon_fallback_epochs
        << ",\n";
    out << "    \"store_post_commit_request_passes\": "
        << stats.store_post_commit_request_passes << ",\n";
    out << "    \"store_post_commit_request_replayed_events\": "
        << stats.store_post_commit_request_replayed_events << ",\n";
    out << "    \"dram_frfcfs_candidate_epochs\": "
        << stats.dram_frfcfs_candidate_epochs << ",\n";
    out << "    \"dram_frfcfs_bypass_epochs\": "
        << stats.dram_frfcfs_bypass_epochs << ",\n";
    out << "    \"dram_frfcfs_bypass_requests\": "
        << stats.dram_frfcfs_bypass_requests << ",\n";
    out << "    \"dram_frfcfs_candidate_queue_cycles\": "
        << stats.dram_frfcfs_candidate_queue_cycles << ",\n";
    out << "    \"dram_frfcfs_bypass_queue_cycles\": "
        << stats.dram_frfcfs_bypass_queue_cycles << ",\n";
    out << "    \"dram_frfcfs_selection_window_sum\": "
        << stats.dram_frfcfs_selection_window_sum << ",\n";
    out << "    \"dram_frfcfs_selection_window_max\": "
        << stats.dram_frfcfs_selection_window_max << ",\n";
    out << "    \"dram_frfcfs_effective_selection_window\": "
        << stats.dram_frfcfs_effective_selection_window << ",\n";
    out << "    \"dram_frfcfs_stable_epochs\": "
        << stats.dram_frfcfs_stable_epochs << ",\n";
    out << "    \"dram_frfcfs_fallback_epochs\": "
        << stats.dram_frfcfs_fallback_epochs << ",\n";
    out << "    \"dram_frfcfs_requests\": "
        << stats.dram_frfcfs_requests << ",\n";
    out << "    \"dram_frfcfs_passes\": "
        << stats.dram_frfcfs_passes << ",\n";
    out << "    \"dram_frfcfs_reordered_requests\": "
        << stats.dram_frfcfs_reordered_requests << ",\n";
    out << "    \"dram_frfcfs_row_hits\": "
        << stats.dram_frfcfs_row_hits << ",\n";
    out << "    \"dram_frfcfs_row_misses\": "
        << stats.dram_frfcfs_row_misses << ",\n";
    out << "    \"dram_frfcfs_max_pending\": "
        << stats.dram_frfcfs_max_pending << ",\n";
    out << "    \"dram_frfcfs_max_admitted_pending\": "
        << stats.dram_frfcfs_max_admitted_pending << ",\n";
    out << "    \"dram_frfcfs_saturated_selections\": "
        << stats.dram_frfcfs_saturated_selections << ",\n";
    out << "    \"dram_frfcfs_page_policy_scanned_requests\": "
        << stats.dram_frfcfs_page_policy_scanned_requests << ",\n";
    out << "    \"dram_frfcfs_outside_window_row_hits\": "
        << stats.dram_frfcfs_outside_window_row_hits << ",\n";
    out << "    \"dram_frfcfs_outside_window_bank_conflicts\": "
        << stats.dram_frfcfs_outside_window_bank_conflicts << ",\n";
    out << "    \"dram_frfcfs_row_cap_precharges\": "
        << stats.dram_frfcfs_row_cap_precharges << ",\n";
    out << "    \"dram_frfcfs_adaptive_precharges\": "
        << stats.dram_frfcfs_adaptive_precharges << ",\n";
    out << "    \"dram_frfcfs_wall_ns\": "
        << stats.dram_frfcfs_wall_ns << ",\n";
    out << "    \"dram_write_queue_enqueues\": "
        << stats.dram_write_queue_enqueues << ",\n";
    out << "    \"dram_write_queue_drained\": "
        << stats.dram_write_queue_drained << ",\n";
    out << "    \"dram_write_queue_read_bypasses\": "
        << stats.dram_write_queue_read_bypasses << ",\n";
    out << "    \"dram_write_queue_high_watermark_switches\": "
        << stats.dram_write_queue_high_watermark_switches << ",\n";
    out << "    \"dram_write_queue_forced_capacity_drains\": "
        << stats.dram_write_queue_forced_capacity_drains << ",\n";
    out << "    \"dram_write_queue_turnarounds\": "
        << stats.dram_write_queue_turnarounds << ",\n";
    out << "    \"dram_write_queue_row_hits\": "
        << stats.dram_write_queue_row_hits << ",\n";
    out << "    \"dram_write_queue_row_misses\": "
        << stats.dram_write_queue_row_misses << ",\n";
    out << "    \"dram_write_queue_wait_cycles\": "
        << stats.dram_write_queue_wait_cycles << ",\n";
    out << "    \"dram_write_queue_max_pending\": "
        << stats.dram_write_queue_max_pending << ",\n";
    out << "    \"dram_write_queue_pending_initial\": "
        << stats.dram_write_queue_pending_initial << ",\n";
    out << "    \"dram_write_queue_pending_final\": "
        << stats.dram_write_queue_pending_final << ",\n";
    out << "    \"max_batch_memory_events\": "
        << stats.max_batch_memory_events << ",\n";
    out << "    \"reordered_memory_event_pairs\": "
        << stats.reordered_memory_event_pairs << ",\n";
    out << "    \"same_line_reordered_pairs\": "
        << stats.same_line_reordered_pairs << ",\n";
    out << "    \"timing_feedback_calls\": "
        << stats.timing_feedback_calls << ",\n";
    out << "    \"timing_feedback_parallel_calls\": "
        << stats.timing_feedback_parallel_calls << ",\n";
    out << "    \"timing_feedback_core_tasks\": "
        << stats.timing_feedback_core_tasks << ",\n";
    out << "    \"timing_feedback_wall_ns\": "
        << stats.timing_feedback_wall_ns << ",\n";
    out << "    \"sparse_scoreboard_seeds\": "
        << stats.sparse_scoreboard_seeds << ",\n";
    out << "    \"sparse_scoreboard_materialized_uops\": "
        << stats.sparse_scoreboard_materialized_uops << ",\n";
    out << "    \"sparse_scoreboard_absorbed_edges\": "
        << stats.sparse_scoreboard_absorbed_edges << ",\n";
    out << "    \"sparse_scoreboard_cross_epoch_edges\": "
        << stats.sparse_scoreboard_cross_epoch_edges << ",\n";
    out << "    \"sparse_scoreboard_rob_crossings\": "
        << stats.sparse_scoreboard_rob_crossings << ",\n";
    out << "    \"sparse_scoreboard_lq_crossings\": "
        << stats.sparse_scoreboard_lq_crossings << ",\n";
    out << "    \"sparse_scoreboard_sq_crossings\": "
        << stats.sparse_scoreboard_sq_crossings << ",\n";
    out << "    \"response_block_summary_checkpoints\": "
        << stats.response_block_summary_checkpoints << ",\n";
    out << "    \"response_block_summary_uops\": "
        << stats.response_block_summary_uops << ",\n";
    out << "    \"response_block_summary_rob_writes\": "
        << stats.response_block_summary_rob_writes << ",\n";
    out << "    \"response_block_summary_rob_writes_avoided\": "
        << stats.response_block_summary_rob_writes_avoided << ",\n";
    out << "    \"sparse_resource_candidates\": "
        << stats.sparse_resource_candidates << ",\n";
    out << "    \"sparse_resource_issue_moves\": "
        << stats.sparse_resource_issue_moves << ",\n";
    out << "    \"sparse_resource_issue_collision_cycles\": "
        << stats.sparse_resource_issue_collision_cycles << ",\n";
    out << "    \"sparse_resource_writeback_moves\": "
        << stats.sparse_resource_writeback_moves << ",\n";
    out << "    \"sparse_resource_writeback_collision_cycles\": "
        << stats.sparse_resource_writeback_collision_cycles << ",\n";
    out << "    \"rob_head_suffix_anchors\": "
        << stats.rob_head_suffix_anchors << ",\n";
    out << "    \"rob_head_suffix_recoveries\": "
        << stats.rob_head_suffix_recoveries << ",\n";
    out << "    \"rob_head_suffix_uops\": "
        << stats.rob_head_suffix_uops << ",\n";
    out << "    \"rob_head_suffix_open_checkpoints\": "
        << stats.rob_head_suffix_open_checkpoints << ",\n";
    out << "    \"rob_head_suffix_candidate_epochs\": "
        << stats.rob_head_suffix_candidate_epochs << ",\n";
    out << "    \"rob_head_suffix_noop_epochs\": "
        << stats.rob_head_suffix_noop_epochs << ",\n";
    out << "    \"rob_head_suffix_stable_epochs\": "
        << stats.rob_head_suffix_stable_epochs << ",\n";
    out << "    \"rob_head_suffix_fallback_epochs\": "
        << stats.rob_head_suffix_fallback_epochs << ",\n";
    out << "    \"rob_head_suffix_moved_shared_events\": "
        << stats.rob_head_suffix_moved_shared_events << ",\n";
    out << "    \"rob_head_suffix_boundary_clipped_events\": "
        << stats.rob_head_suffix_boundary_clipped_events << ",\n";
    out << "    \"rob_head_suffix_replayed_events\": "
        << stats.rob_head_suffix_replayed_events << ",\n";
    out << "    \"rob_head_suffix_wall_ns\": "
        << stats.rob_head_suffix_wall_ns << ",\n";
    out << "    \"response_activity_candidates\": "
        << stats.response_activity_candidates << ",\n";
    out << "    \"response_activity_certified_segments\": "
        << stats.response_activity_certified_segments << ",\n";
    out << "    \"response_activity_certified_uops\": "
        << stats.response_activity_certified_uops << ",\n";
    out << "    \"response_activity_fallback_segments\": "
        << stats.response_activity_fallback_segments << ",\n";
    out << "    \"interval_schedule_batch_wall_ns\": "
        << stats.interval_schedule_batch_wall_ns << ",\n";
    out << "    \"interval_weave_wall_ns\": "
        << stats.interval_weave_wall_ns << ",\n";
    out << "    \"interval_commit_wall_ns\": "
        << stats.interval_commit_wall_ns << ",\n";
    out << "    \"domain_worker_threads\": "
        << stats.domain_worker_threads << ",\n";
    out << "    \"trace_worker_threads\": "
        << stats.trace_worker_threads << ",\n";
    out << "    \"domain_phase_calls\": "
        << stats.domain_phase_calls << ",\n";
    out << "    \"domain_phase_wall_ns\": "
        << stats.domain_phase_wall_ns << "\n";
    out << "  },\n";
    out << "  \"threads\": [\n";
    for (std::size_t index = 0; index < stats.threads.size(); ++index) {
        const auto& thread = stats.threads[index];
        out << "    {\"thread\": " << thread.thread_id
            << ", \"address_space\": " << thread.address_space_id
            << ", \"initial_effective_address_space\": "
            << thread.initial_effective_address_space_id
            << ", \"final_effective_address_space\": "
            << thread.final_effective_address_space_id
            << ", \"distinct_address_spaces\": "
            << thread.distinct_address_spaces
            << ", \"address_space_switches\": "
            << thread.address_space_switches
            << ", \"initial_core\": " << thread.initial_core
            << ", \"final_core\": " << thread.final_core
            << ", \"records\": " << thread.records
            << ", \"instructions\": " << thread.retired_instructions
            << ", \"uops\": " << thread.retired_uops
            << ", \"native_kernel_records\": "
            << thread.native_kernel_records
            << ", \"native_kernel_instructions\": "
            << thread.native_kernel_retired_instructions
            << ", \"native_kernel_uops\": "
            << thread.native_kernel_retired_uops
            << ", \"serializing_uops\": " << thread.serializing_uops
            << ", \"syscall_uops\": " << thread.syscall_uops
            << ", \"cycles\": " << thread.cycles << "}";
        if (index + 1 != stats.threads.size()) out << ',';
        out << '\n';
    }
    out << "  ],\n";
    out << "  \"cores\": [\n";
    for (std::size_t core = 0; core < stats.cores.size(); ++core) {
        const auto& c = stats.cores[core];
        const auto& q = stats.o3[core];
        const auto& rename = stats.response_rename[core];
        const auto& pipeline_audit =
            stats.committed_pipeline_audit[core];
        const auto& s = stats.sequencer[core];
        const auto& critical = stats.response_critical_cycles[core];
        const auto& residual = stats.response_residuals[core];
        const auto& epoch = stats.committed_epoch_audit[core];
        out << "    {\"core\": " << core
            << ", \"instructions\": " << c.retired_instructions
            << ", \"uops\": " << c.retired_uops
            << ", \"cycles\": " << c.cycles
            << ", \"ipc\": " << ratio(c.retired_instructions, c.cycles)
            << ", \"memory_accesses\": " << c.memory_accesses
            << ", \"mmio_escape_accesses\": "
            << c.mmio_escape_accesses
            << ", \"branches_without_outcome\": "
            << c.branches_without_outcome
            << ", \"serializing_uops\": " << c.serializing_uops
            << ", \"syscall_uops\": " << c.syscall_uops
            << ", \"syscall_drain_cycles\": "
            << c.syscall_drain_cycles
            << ", \"syscall_service_cycles\": "
            << c.syscall_service_cycles
            << ", \"syscall_restart_cycles\": "
            << c.syscall_restart_cycles
            << ", \"page_fault_first_touch_candidates\": "
            << c.page_fault_first_touch_candidates
            << ", \"page_fault_first_touch_write_candidates\": "
            << c.page_fault_first_touch_write_candidates
            << ", \"page_fault_background_candidates\": "
            << c.page_fault_background_candidates
            << ", \"page_fault_background_read_candidates\": "
            << c.page_fault_background_read_candidates
            << ", \"page_fault_background_write_candidates\": "
            << c.page_fault_background_write_candidates
            << ", \"page_fault_allocation_candidates\": "
            << c.page_fault_allocation_candidates
            << ", \"page_fault_allocation_recency_candidates\": [";
        for (std::size_t index = 0;
             index < c.page_fault_allocation_recency_candidates.size();
             ++index) {
            if (index != 0) out << ", ";
            out << c.page_fault_allocation_recency_candidates[index];
        }
        out << "]"
            << ", \"page_fault_allocation_recency_write_candidates\": [";
        for (std::size_t index = 0;
             index <
             c.page_fault_allocation_recency_write_candidates.size();
             ++index) {
            if (index != 0) out << ", ";
            out << c.page_fault_allocation_recency_write_candidates[index];
        }
        out << "]"
            << ", \"page_fault_allocation_by_syscall\": ";
        write_page_fault_allocation_by_syscall(
            out, c.page_fault_allocation_by_syscall);
        out
            << ", \"page_fault_untracked_accesses\": "
            << c.page_fault_untracked_accesses
            << ", \"page_fault_syscall_semantic_candidates\": "
            << c.page_fault_syscall_semantic_candidates
            << ", \"page_fault_syscall_semantic_write_candidates\": "
            << c.page_fault_syscall_semantic_write_candidates
            << ", \"page_fault_syscall_semantic_fallback_write_candidates\": "
            << c.page_fault_syscall_semantic_fallback_write_candidates
            << ", \"page_fault_syscall_semantic_fallback_write_selected\": "
            << c.page_fault_syscall_semantic_fallback_write_selected
            << ", \"page_fault_virtual_page_map_misses\": "
            << c.page_fault_virtual_page_map_misses
            << ", \"page_fault_initial_pte_known_pages\": "
            << c.page_fault_initial_pte_known_pages
            << ", \"page_fault_initial_pte_present_pages\": "
            << c.page_fault_initial_pte_present_pages
            << ", \"page_fault_initial_pte_nonpresent_pages\": "
            << c.page_fault_initial_pte_nonpresent_pages
            << ", \"page_fault_initial_pte_unknown_pages\": "
            << c.page_fault_initial_pte_unknown_pages
            << ", \"page_fault_initial_pte_selected\": "
            << c.page_fault_initial_pte_selected
            << ", \"page_fault_roi_entry_known_pages\": "
            << c.page_fault_roi_entry_known_pages
            << ", \"page_fault_roi_entry_present_pages\": "
            << c.page_fault_roi_entry_present_pages
            << ", \"page_fault_roi_entry_nonpresent_pages\": "
            << c.page_fault_roi_entry_nonpresent_pages
            << ", \"page_fault_roi_entry_unknown_pages\": "
            << c.page_fault_roi_entry_unknown_pages
            << ", \"page_fault_roi_entry_selected\": "
            << c.page_fault_roi_entry_selected
            << ", \"page_fault_roi_entry_inflight_suppressed\": "
            << c.page_fault_roi_entry_inflight_suppressed
            << ", \"page_fault_process_shared_duplicate_pages\": "
            << c.page_fault_process_shared_duplicate_pages
            << ", \"page_fault_cache_state_pages\": "
            << c.page_fault_cache_state_pages
            << ", \"page_fault_cache_state_lines\": "
            << c.page_fault_cache_state_lines
            << ", \"synthetic_syscall_kernel\": ";
        write_kernel_event_counters(out, c.syscall_kernel);
        out << ", \"synthetic_page_fault_kernel\": ";
        write_kernel_event_counters(out, c.page_fault_kernel);
        out << ", \"synthetic_irq_kernel\": ";
        write_kernel_event_counters(out, c.irq_kernel);
        out
            << ", \"branch_penalty_cycles\": "
            << c.branch_penalty_cycles
            << ", \"branch_shadow_uops\": "
            << c.branch_shadow_uops
            << ", \"branch_shadow_cycles\": "
            << c.branch_shadow_cycles
            << ", \"branch_population_audit\": ";
        write_branch_population_audit(out, c.branch_population);
        out
            << ", \"fetch_buffer_transitions\": "
            << c.fetch_buffer_transitions
            << ", \"fetch_buffer_refill_delay_cycles\": "
            << c.fetch_buffer_refill_delay_cycles
            << ", \"fetch_block_response_wait_cycles\": "
            << c.fetch_block_response_wait_cycles
            << ", \"fetch_block_response_hidden_cycles\": "
            << c.fetch_block_response_hidden_cycles
            << ", \"fetch_block_response_exposed_cycles\": "
            << c.fetch_block_response_exposed_cycles
            << ", \"fetch_block_response_to_resume_cycles\": "
            << c.fetch_block_response_to_resume_cycles
            << ", \"fetch_block_request_to_resume_cycles\": "
            << c.fetch_block_request_to_resume_cycles
            << ", \"fetch_block_request_admission_delay_cycles\": "
            << c.fetch_block_request_admission_delay_cycles
            << ", \"fetch_response_ledger_committed_requests\": "
            << c.fetch_response_ledger_committed_requests
            << ", \"fetch_response_ledger_shadow_requests\": "
            << c.fetch_response_ledger_shadow_requests
            << ", \"fetch_response_ledger_responses\": "
            << c.fetch_response_ledger_responses
            << ", \"fetch_response_ledger_server_wait_cycles\": "
            << c.fetch_response_ledger_server_wait_cycles
            << ", \"speculative_fetch_shadow_uops\": "
            << c.speculative_fetch_shadow_uops
            << ", \"speculative_fetch_shadow_requests_estimated\": "
            << c.speculative_fetch_shadow_requests_estimated
            << ", \"speculative_fetch_shadow_requests_issued\": "
            << c.speculative_fetch_shadow_requests_issued
            << ", \"speculative_fetch_shadow_response_wait_cycles\": "
            << c.speculative_fetch_shadow_response_wait_cycles
            << ", \"speculative_fetch_shadow_recovery_hidden_cycles\": "
            << c.speculative_fetch_shadow_recovery_hidden_cycles
            << ", \"speculative_fetch_shadow_recovery_exposed_cycles\": "
            << c.speculative_fetch_shadow_recovery_exposed_cycles
            << ", \"speculative_fetch_shadow_density_unavailable\": "
            << c.speculative_fetch_shadow_density_unavailable
            << ", \"fetch_supply_static_span_lookups\": "
            << c.fetch_supply_static_span_lookups
            << ", \"fetch_supply_static_span_unavailable\": "
            << c.fetch_supply_static_span_unavailable
            << ", \"fetch_supply_cross_block_instructions\": "
            << c.fetch_supply_cross_block_instructions
            << ", \"fetch_supply_cross_block_extra_requests\": "
            << c.fetch_supply_cross_block_extra_requests
            << ", \"speculative_fetch_shadow_response_conserved\": "
            << (c.speculative_fetch_shadow_response_wait_cycles ==
                        c.speculative_fetch_shadow_recovery_hidden_cycles +
                            c.speculative_fetch_shadow_recovery_exposed_cycles
                    ? "true"
                    : "false")
            << ", \"fetch_block_response_conserved\": "
            << (c.fetch_block_response_wait_cycles ==
                        c.fetch_block_response_hidden_cycles +
                            c.fetch_block_response_exposed_cycles
                    ? "true"
                    : "false")
            << ", \"fetch_response_ledger_conserved\": "
            << (c.fetch_response_ledger_committed_requests +
                            c.fetch_response_ledger_shadow_requests ==
                        c.fetch_response_ledger_responses
                    ? "true"
                    : "false")
            << ", \"l1i_accesses\": " << c.l1i.accesses
            << ", \"l1i_hits\": " << c.l1i.hits
            << ", \"l1i_misses\": " << c.l1i.misses
            << ", \"l1i_evictions\": " << c.l1i.evictions
            << ", \"l1i_miss_stall_cycles\": "
            << c.l1i_miss_stall_cycles
            << ", \"instruction_page_map_lookups\": "
            << c.instruction_page_map_lookups
            << ", \"instruction_page_map_hits\": "
            << c.instruction_page_map_hits
            << ", \"instruction_page_map_misses\": "
            << c.instruction_page_map_misses
            << ", \"modeled_instruction_page_lookups\": "
            << c.modeled_instruction_page_lookups
            << ", \"physical_instruction_fetch_requests\": "
            << c.physical_instruction_fetch_requests
            << ", \"modeled_instruction_fetch_requests\": "
            << c.modeled_instruction_fetch_requests
            << ", \"physical_kernel_instruction_fetch_requests\": "
            << c.physical_kernel_instruction_fetch_requests
            << ", \"instruction_fetch_lower_hierarchy_requests\": "
            << c.instruction_fetch_lower_hierarchy_requests
            << ", \"instruction_fetch_request_order_clamps\": "
            << c.instruction_fetch_request_order_clamps
            << ", \"instruction_fetch_request_order_clamp_cycles\": "
            << c.instruction_fetch_request_order_clamp_cycles
            << ", \"l1i_speculative_entry_accesses\": "
            << c.l1i_speculative_entry_accesses
            << ", \"l1i_speculative_entry_hits\": "
            << c.l1i_speculative_entry_hits
            << ", \"l1i_speculative_entry_misses\": "
            << c.l1i_speculative_entry_misses
            << ", \"l1i_speculative_entry_evictions\": "
            << c.l1i_speculative_entry_evictions
            << ", \"l1i_speculative_entry_untracked\": "
            << c.l1i_speculative_entry_untracked
            << ", \"l1i_speculative_path_records\": "
            << c.l1i_speculative_path_records
            << ", \"l1i_speculative_path_accesses\": "
            << c.l1i_speculative_path_accesses
            << ", \"l1i_speculative_path_hits\": "
            << c.l1i_speculative_path_hits
            << ", \"l1i_speculative_path_misses\": "
            << c.l1i_speculative_path_misses
            << ", \"l1i_speculative_path_evictions\": "
            << c.l1i_speculative_path_evictions
            << ", \"l1i_speculative_path_static_instructions\": "
            << c.l1i_speculative_path_static_instructions
            << ", \"l1i_speculative_path_operand_instructions\": "
            << c.l1i_speculative_path_operand_instructions
            << ", \"l1i_speculative_path_read_registers\": "
            << c.l1i_speculative_path_read_registers
            << ", \"l1i_speculative_path_write_registers\": "
            << c.l1i_speculative_path_write_registers
            << ", \"l1i_speculative_path_operand_segments\": "
            << c.l1i_speculative_path_operand_segments
            << ", \"l1i_speculative_path_raw_edges\": "
            << c.l1i_speculative_path_raw_edges
            << ", \"l1i_speculative_path_dependent_instructions\": "
            << c.l1i_speculative_path_dependent_instructions
            << ", \"l1i_speculative_path_chain_depth_sum\": "
            << c.l1i_speculative_path_chain_depth_sum
            << ", \"l1i_speculative_path_chain_depth_max\": "
            << c.l1i_speculative_path_chain_depth_max
            << ", \"l1i_speculative_path_operand_rob_prefix_uops_q16\": "
            << c.l1i_speculative_path_operand_rob_prefix_uops_q16
            << ", \"l1i_speculative_path_operand_rob_capped_instructions\": "
            << c.l1i_speculative_path_operand_rob_capped_instructions
            << ", \"l1i_speculative_path_operand_rob_capped_read_registers\": "
            << c.l1i_speculative_path_operand_rob_capped_read_registers
            << ", \"l1i_speculative_path_operand_rob_capped_write_registers\": "
            << c.l1i_speculative_path_operand_rob_capped_write_registers
            << ", \"l1i_speculative_path_operand_rob_capped_memory_instructions\": "
            << c.l1i_speculative_path_operand_rob_capped_memory_instructions
            << ", \"l1i_speculative_path_operand_rob_capped_memory_instructions_max_per_path\": "
            << c
                   .l1i_speculative_path_operand_rob_capped_memory_instructions_max_per_path
            << ", \"l1i_speculative_path_operand_rob_capped_write_registers_max_per_path\": "
            << c
                   .l1i_speculative_path_operand_rob_capped_write_registers_max_per_path
            << ", \"l1i_speculative_path_operand_rob_capped_raw_edges\": "
            << c.l1i_speculative_path_operand_rob_capped_raw_edges
            << ", \"l1i_speculative_path_operand_rob_capped_dependent_instructions\": "
            << c
                   .l1i_speculative_path_operand_rob_capped_dependent_instructions
            << ", \"l1i_speculative_path_operand_rob_capped_chain_depth_sum\": "
            << c.l1i_speculative_path_operand_rob_capped_chain_depth_sum
            << ", \"l1i_speculative_path_operand_rob_capped_chain_depth_max\": "
            << c.l1i_speculative_path_operand_rob_capped_chain_depth_max
            << ", \"l1i_speculative_path_memory_instructions\": "
            << c.l1i_speculative_path_memory_instructions
            << ", \"l1i_speculative_path_memory_page_known\": "
            << c.l1i_speculative_path_memory_page_known
            << ", \"l1i_speculative_path_memory_page_unstable\": "
            << c.l1i_speculative_path_memory_page_unstable
            << ", \"l1i_speculative_path_memory_page_transition_samples\": "
            << c.l1i_speculative_path_memory_page_transition_samples
            << ", \"l1i_speculative_path_memory_page_transition_score_ppm\": "
            << c.l1i_speculative_path_memory_page_transition_score_ppm
            << ", \"l1i_speculative_path_profiled_instructions\": "
            << c.l1i_speculative_path_profiled_instructions
            << ", \"l1i_speculative_path_profile_uops_q16\": "
            << speculative_profile_uops_q16(c)
            << ", \"l1i_speculative_path_profile_rob_capped_uops_q16\": "
            << c.l1i_speculative_path_profile_rob_capped_uops_q16
            << ", \"l1i_speculative_path_profile_integer_uops_q16\": "
            << c.l1i_speculative_path_profile_uops_q16[0]
            << ", \"l1i_speculative_path_profile_integer_multiply_uops_q16\": "
            << c.l1i_speculative_path_profile_uops_q16[1]
            << ", \"l1i_speculative_path_profile_float_simple_uops_q16\": "
            << c.l1i_speculative_path_profile_uops_q16[2]
            << ", \"l1i_speculative_path_profile_float_complex_uops_q16\": "
            << c.l1i_speculative_path_profile_uops_q16[3]
            << ", \"l1i_speculative_path_profile_simd_uops_q16\": "
            << c.l1i_speculative_path_profile_uops_q16[4]
            << ", \"l1i_speculative_path_profile_predicate_uops_q16\": "
            << c.l1i_speculative_path_profile_uops_q16[5]
            << ", \"l1i_speculative_path_profile_memory_uops_q16\": "
            << c.l1i_speculative_path_profile_uops_q16[6]
            << ", \"l1i_speculative_path_profile_system_uops_q16\": "
            << c.l1i_speculative_path_profile_uops_q16[7]
            << ", \"l1i_speculative_path_conditional_stops\": "
            << c.l1i_speculative_path_conditional_stops
            << ", \"l1i_speculative_path_indirect_stops\": "
            << c.l1i_speculative_path_indirect_stops
            << ", \"l1i_speculative_path_static_map_misses\": "
            << c.l1i_speculative_path_static_map_misses
            << ", \"l1i_speculative_path_unknown_edges\": "
            << c.l1i_speculative_path_unknown_edges
            << ", \"speculative_dtlb_accesses\": "
            << c.speculative_dtlb.accesses
            << ", \"speculative_dtlb_hits\": "
            << c.speculative_dtlb.hits
            << ", \"speculative_dtlb_misses\": "
            << c.speculative_dtlb.misses
            << ", \"speculative_dtlb_untracked\": "
            << c.speculative_dtlb.untracked
            << ", \"exposed_memory_penalty_cycles\": "
            << c.memory_penalty_cycles
            << ", \"response_critical_total_cycles\": "
            << critical.total_cycles
            << ", \"response_critical_rename_free_list_cycles\": "
            << critical.rename_free_list_cycles
            << ", \"response_critical_dispatch_bandwidth_cycles\": "
            << critical.dispatch_bandwidth_cycles
            << ", \"response_critical_rob_capacity_cycles\": "
            << critical.rob_capacity_cycles
            << ", \"response_critical_iq_capacity_cycles\": "
            << critical.iq_capacity_cycles
            << ", \"response_critical_lq_capacity_cycles\": "
            << critical.lq_capacity_cycles
            << ", \"response_critical_sq_capacity_cycles\": "
            << critical.sq_capacity_cycles
            << ", \"response_critical_dependency_cycles\": "
            << critical.dependency_cycles
            << ", \"response_critical_sequencer_cycles\": "
            << critical.sequencer_cycles
            << ", \"response_critical_l1_mshr_cycles\": "
            << critical.l1_mshr_cycles
            << ", \"response_critical_l2_mshr_cycles\": "
            << critical.l2_mshr_cycles
            << ", \"response_critical_instruction_fetch_cycles\": "
            << critical.instruction_fetch_cycles
            << ", \"response_critical_memory_response_cycles\": "
            << critical.memory_response_cycles
            << ", \"response_critical_commit_bandwidth_cycles\": "
            << critical.commit_bandwidth_cycles
            << ", \"response_critical_tso_store_cycles\": "
            << critical.tso_store_cycles
            << ", \"response_critical_unattributed_cycles\": "
            << critical.unattributed_cycles
            << ", \"response_residual_instruction_fetch_seed_events\": "
            << residual.instruction_fetch_seed_events
            << ", \"response_residual_instruction_fetch_seed_cycles\": "
            << residual.instruction_fetch_seed_cycles
            << ", \"response_residual_seed_events\": "
            << residual.response_seed_events
            << ", \"response_residual_seed_uops\": "
            << residual.response_seed_uops
            << ", \"response_residual_seed_cycles\": "
            << residual.response_seed_cycles
            << ", \"response_residual_completion_extended_uops\": "
            << residual.completion_extended_uops
            << ", \"response_residual_completion_extension_cycles\": "
            << residual.completion_extension_cycles
            << ", \"response_residual_dependency_edges\": "
            << residual.dependency_edges
            << ", \"response_residual_dependency_input_cycles\": "
            << residual.dependency_input_cycles
            << ", \"response_residual_dependency_absorbed_cycles\": "
            << residual.dependency_absorbed_cycles
            << ", \"response_residual_dependency_propagated_cycles\": "
            << residual.dependency_propagated_cycles
            << ", \"response_residual_dependency_conserved\": "
            << (residual.dependency_conserved() ? "true" : "false")
            << ", \"response_residual_retire_seed_uops\": "
            << residual.retire_seed_uops
            << ", \"response_residual_retire_input_cycles\": "
            << residual.retire_input_cycles
            << ", \"response_residual_retire_absorbed_cycles\": "
            << residual.retire_absorbed_cycles
            << ", \"response_residual_retire_propagated_cycles\": "
            << residual.retire_propagated_cycles
            << ", \"response_residual_retire_conserved\": "
            << (residual.retire_conserved() ? "true" : "false")
            << ", \"response_residual_ordered_retire_moved_uops\": "
            << residual.ordered_retire_moved_uops
            << ", \"response_residual_ordered_retire_moved_cycles\": "
            << residual.ordered_retire_moved_cycles
            << ", \"response_residual_dispatch_moved_uops\": "
            << residual.dispatch_moved_uops
            << ", \"response_residual_dispatch_moved_cycles\": "
            << residual.dispatch_moved_cycles
            << ", \"response_residual_fetch_queue_moved_uops\": "
            << residual.fetch_queue_moved_uops
            << ", \"response_residual_fetch_queue_moved_cycles\": "
            << residual.fetch_queue_moved_cycles
            << ", \"response_residual_memory_issue_moved_events\": "
            << residual.memory_issue_moved_events
            << ", \"response_residual_memory_issue_moved_cycles\": "
            << residual.memory_issue_moved_cycles
            << ", \"response_residual_escape_issue_moved_events\": "
            << residual.escape_issue_moved_events
            << ", \"response_residual_escape_issue_moved_cycles\": "
            << residual.escape_issue_moved_cycles
            << ", \"response_residual_stage_uops\": "
            << residual.stage_uops
            << ", \"response_residual_stage_memory_uops\": "
            << residual.stage_memory_uops
            << ", \"response_residual_stage_non_memory_uops\": "
            << residual.stage_non_memory_uops
            << ", \"response_residual_stage_non_memory_base_fetch_to_issue_cycles\": "
            << residual.stage_non_memory_base_fetch_to_issue_cycles
            << ", \"response_residual_stage_non_memory_corrected_fetch_to_issue_cycles\": "
            << residual.stage_non_memory_corrected_fetch_to_issue_cycles
            << ", \"response_residual_stage_non_memory_corrected_issue_to_retire_cycles\": "
            << residual.stage_non_memory_corrected_issue_to_retire_cycles
            << ", \"response_residual_stage_load_uops\": "
            << residual.stage_load_uops
            << ", \"response_residual_stage_load_base_fetch_to_issue_cycles\": "
            << residual.stage_load_base_fetch_to_issue_cycles
            << ", \"response_residual_stage_load_corrected_fetch_to_issue_cycles\": "
            << residual.stage_load_corrected_fetch_to_issue_cycles
            << ", \"response_residual_stage_load_corrected_issue_to_completion_cycles\": "
            << residual.stage_load_corrected_issue_to_completion_cycles
            << ", \"response_residual_stage_load_corrected_issue_to_retire_cycles\": "
            << residual.stage_load_corrected_issue_to_retire_cycles
            << ", \"response_residual_stage_base_issue_to_completion_cycles\": "
            << residual.stage_base_issue_to_completion_cycles
            << ", \"response_residual_stage_base_completion_to_retire_cycles\": "
            << residual.stage_base_completion_to_retire_cycles
            << ", \"response_residual_stage_base_issue_to_retire_cycles\": "
            << residual.stage_base_issue_to_retire_cycles
            << ", \"response_residual_stage_corrected_issue_to_completion_cycles\": "
            << residual.stage_corrected_issue_to_completion_cycles
            << ", \"response_residual_stage_corrected_completion_to_retire_cycles\": "
            << residual.stage_corrected_completion_to_retire_cycles
            << ", \"response_residual_stage_corrected_issue_to_retire_cycles\": "
            << residual.stage_corrected_issue_to_retire_cycles
            << ", \"response_residual_stage_issue_delay_cycles\": "
            << residual.stage_issue_delay_cycles
            << ", \"response_residual_stage_completion_delay_cycles\": "
            << residual.stage_completion_delay_cycles
            << ", \"response_residual_stage_retire_delay_cycles\": "
            << residual.stage_retire_delay_cycles
            << ", \"response_residual_stage_memory_base_issue_to_retire_cycles\": "
            << residual.stage_memory_base_issue_to_retire_cycles
            << ", \"response_residual_stage_memory_corrected_issue_to_retire_cycles\": "
            << residual.stage_memory_corrected_issue_to_retire_cycles
            << ", \"response_residual_head_gap_zero_commit_cycles\": "
            << residual.head_gap_zero_commit_cycles
            << ", \"response_residual_head_gap_not_fetched_cycles\": "
            << residual.head_gap_not_fetched_cycles
            << ", \"response_residual_head_gap_fetched_not_issued_cycles\": "
            << residual.head_gap_fetched_not_issued_cycles
            << ", \"response_residual_head_gap_issued_not_retired_cycles\": "
            << residual.head_gap_issued_not_retired_cycles
            << ", \"response_residual_head_gap_issued_load_cycles\": "
            << residual.head_gap_issued_load_cycles
            << ", \"response_residual_head_gap_issued_store_cycles\": "
            << residual.head_gap_issued_store_cycles
            << ", \"response_residual_head_gap_issued_non_memory_cycles\": "
            << residual.head_gap_issued_non_memory_cycles
            << ", \"response_residual_head_gap_conserved\": "
            << (residual.head_gap_conserved() ? "true" : "false")
            << ", \"response_residual_stage_conserved\": "
            << (residual.stage_conserved() ? "true" : "false")
            << ", \"response_store_uops\": "
            << residual.store_uops
            << ", \"response_store_address_to_commit_cycles\": "
            << residual.store_address_to_commit_cycles
            << ", \"response_store_hierarchy_response_before_commit_uops\": "
            << residual.store_hierarchy_response_before_commit_uops
            << ", \"response_store_hierarchy_response_before_commit_cycles\": "
            << residual.store_hierarchy_response_before_commit_cycles
            << ", \"response_store_tso_wait_uops\": "
            << residual.store_tso_wait_uops
            << ", \"response_store_tso_wait_cycles\": "
            << residual.store_tso_wait_cycles
            << ", \"response_store_send_to_response_cycles\": "
            << residual.store_send_to_response_cycles
            << ", \"response_store_commit_to_sq_release_cycles\": "
            << residual.store_commit_to_sq_release_cycles
            << ", \"response_store_sq_release_max_cycles\": "
            << residual.store_sq_release_max_cycles
            << ", \"response_store_send_retimed_uops\": "
            << residual.store_send_retimed_uops
            << ", \"response_store_send_retimed_cycles\": "
            << residual.store_send_retimed_cycles
            << ", \"response_store_lifecycle_conserved\": "
            << (residual.store_lifecycle_conserved() ? "true" : "false")
            << ", \"response_rename_conserved\": "
            << (rename.conserved() ? "true" : "false")
            << ", \"response_rename_destination_uops\": "
            << rename.destination_uops
            << ", \"response_rename_free_list_stall_uops\": "
            << rename.free_list_stall_uops
            << ", \"response_rename_free_list_stall_cycles\": "
            << rename.free_list_stall_cycles
            << ", \"l1d_accesses\": " << c.l1d.accesses
            << ", \"l1d_misses\": " << c.l1d.misses
            << ", \"instruction_l2_accesses\": "
            << c.instruction_l2.accesses
            << ", \"instruction_l2_hits\": "
            << c.instruction_l2.hits
            << ", \"instruction_l2_misses\": "
            << c.instruction_l2.misses
            << ", \"l2_accesses\": " << c.l2.accesses
            << ", \"l2_misses\": " << c.l2.misses
            << ", \"dtlb_accesses\": " << c.dtlb.accesses
            << ", \"dtlb_hits\": " << c.dtlb.hits
            << ", \"dtlb_misses\": " << c.dtlb.misses
            << ", \"dtlb_merged_misses\": "
            << c.dtlb.merged_misses
            << ", \"dtlb_untracked\": " << c.dtlb.untracked
            << ", \"dtlb_conserved\": "
            << (c.dtlb.conserved() ? "true" : "false")
            << ", \"dtlb_timing_accesses\": "
            << c.dtlb_timing.accesses
            << ", \"dtlb_timing_hits\": " << c.dtlb_timing.hits
            << ", \"dtlb_timing_misses\": " << c.dtlb_timing.misses
            << ", \"dtlb_timing_merged_misses\": "
            << c.dtlb_timing.merged_misses
            << ", \"dtlb_timing_untracked\": "
            << c.dtlb_timing.untracked
            << ", \"dtlb_timing_conserved\": "
            << (c.dtlb_timing.conserved() ? "true" : "false")
            << ", \"dtlb_timing_walk_delay_cycles\": "
            << c.dtlb_timing.walk_delay_cycles
            << ", \"dtlb_walk_delay_cycles\": "
            << c.dtlb_timing.walk_delay_cycles
            << ", \"o3_iq_full_events\": " << q.iq_full_events
            << ", \"o3_iq_stall_cycles\": " << q.iq_stall_cycles
            << ", \"o3_iq_max_occupancy\": "
            << q.iq_max_occupancy
            << ", \"o3_rob_full_events\": "
            << q.rob_full_events
            << ", \"o3_rob_stall_cycles\": "
            << q.rob_stall_cycles
            << ", \"o3_rob_max_occupancy\": "
            << q.rob_max_occupancy
            << ", \"o3_lq_full_events\": "
            << q.lq_full_events
            << ", \"o3_lq_stall_cycles\": "
            << q.lq_stall_cycles
            << ", \"o3_lq_max_occupancy\": "
            << q.lq_max_occupancy
            << ", \"o3_sq_full_events\": "
            << q.sq_full_events
            << ", \"o3_sq_stall_cycles\": "
            << q.sq_stall_cycles
            << ", \"o3_sq_max_occupancy\": "
            << q.sq_max_occupancy
            << ", \"o3_tso_store_stall_cycles\": "
            << q.tso_store_stall_cycles
            << ", \"committed_pipeline_audit\": ";
        write_committed_pipeline_audit(out, pipeline_audit);
        out << ", \"committed_epoch_audit\": ";
        write_committed_epoch_audit(out, epoch, c.retired_uops);
        out
            << ", \"ruby_sequencer_requests\": " << s.requests
            << ", \"ruby_sequencer_buffer_full_stalls\": "
            << s.buffer_full_stalls
            << ", \"ruby_sequencer_stall_cycles\": "
            << s.stall_cycles
            << ", \"ruby_sequencer_max_outstanding\": "
            << s.max_outstanding
            << ", \"branches\": " << c.branch.branches
            << ", \"branch_misses\": " << c.branch.misses
            << ", \"branch_direction_only_misses\": "
            << c.branch.direction_only_misses
            << ", \"branch_target_unavailable_misses\": "
            << c.branch.target_unavailable_misses
            << ", \"branch_wrong_target_misses\": "
            << c.branch.wrong_target_misses
            << ", \"branch_miss_population_conserved\": "
            << (c.branch.miss_population_conserved() ? "true" : "false")
            << ", \"branch_history_checkpoints\": "
            << c.branch.history_checkpoints
            << ", \"branch_history_squashes\": "
            << c.branch.history_squashes
            << ", \"ras_pushes\": " << c.branch.ras_pushes
            << ", \"ras_pops\": " << c.branch.ras_pops
            << ", \"ras_predictions\": "
            << c.branch.ras_predictions
            << ", \"ras_hits\": " << c.branch.ras_hits
            << ", \"ras_static_return_targets\": "
            << c.branch.ras_static_return_targets
            << ", \"ras_learned_return_targets\": "
            << c.branch.ras_learned_return_targets
            << ", \"ras_unknown_return_targets\": "
            << c.branch.ras_unknown_return_targets
            << ", \"ras_source_conserved\": "
            << (c.branch.ras_source_conserved() ? "true" : "false")
            << "}";
        if (core + 1 != stats.cores.size()) out << ',';
        out << '\n';
    }
    out << "  ],\n";
    out << "  \"cha\": [\n";
    for (std::size_t cha = 0; cha < stats.cha.size(); ++cha) {
        const auto& c = stats.cha[cha];
        out << "    {\"cha\": " << cha
            << ", \"requests\": " << c.requests
            << ", \"reads\": " << c.reads
            << ", \"writes\": " << c.writes
            << ", \"llc_hits\": " << c.llc_hits
            << ", \"llc_misses\": " << c.llc_misses
            << ", \"llc_outcomes_conserved\": "
            << (c.llc_outcomes_conserved() ? "true" : "false")
            << ", \"upgrades\": " << c.upgrades
            << ", \"invalidations\": " << c.invalidations
            << ", \"remote_supplies\": " << c.remote_supplies
            << ", \"dram_reads\": " << c.dram_reads
            << ", \"dram_writes\": " << c.dram_writes
            << ", \"llc_unique_fills\": " << c.llc_unique_fills
            << ", \"llc_merged_misses\": " << c.llc_merged_misses
            << ", \"llc_merged_wait_cycles\": "
            << c.llc_merged_wait_cycles
            << ", \"llc_merged_wait_max_cycles\": "
            << c.llc_merged_wait_max_cycles
            << ", \"queue_cycles\": " << c.queue_cycles << "}";
        if (cha + 1 != stats.cha.size()) out << ',';
        out << '\n';
    }
    out << "  ],\n";
    out << "  \"instruction_cha\": [\n";
    for (std::size_t cha = 0; cha < stats.instruction_cha.size(); ++cha) {
        const auto& c = stats.instruction_cha[cha];
        out << "    {\"cha\": " << cha
            << ", \"requests\": " << c.requests
            << ", \"reads\": " << c.reads
            << ", \"writes\": " << c.writes
            << ", \"llc_hits\": " << c.llc_hits
            << ", \"llc_misses\": " << c.llc_misses
            << ", \"llc_outcomes_conserved\": "
            << (c.llc_outcomes_conserved() ? "true" : "false")
            << ", \"upgrades\": " << c.upgrades
            << ", \"invalidations\": " << c.invalidations
            << ", \"remote_supplies\": " << c.remote_supplies
            << ", \"dram_reads\": " << c.dram_reads
            << ", \"dram_writes\": " << c.dram_writes
            << ", \"llc_unique_fills\": " << c.llc_unique_fills
            << ", \"llc_merged_misses\": " << c.llc_merged_misses
            << ", \"llc_merged_wait_cycles\": "
            << c.llc_merged_wait_cycles
            << ", \"llc_merged_wait_max_cycles\": "
            << c.llc_merged_wait_max_cycles << "}";
        if (cha + 1 != stats.instruction_cha.size()) out << ',';
        out << '\n';
    }
    out << "  ]\n";
    out << "}\n";
    return out.str();
}

void emit_stats(
    const fastsim::SimulationStats& stats, const Args& args,
    const fastsim::SimulatorConfig& config) {
    const auto json = stats_json(stats, config);
    const auto it = args.find("output");
    if (it == args.end() || it->second == "-") {
        std::cout << json;
        return;
    }
    std::ofstream output(it->second);
    if (!output) {
        throw std::runtime_error("cannot create output: " + it->second);
    }
    output << json;
    std::cerr << "wrote " << it->second << '\n';
}

int simulate(const Args& args) {
    const auto config_path = require(args, "config");
    const auto manifest_path = require(args, "manifest");
    auto config = fastsim::load_simulator_config(config_path);
    config.measurement_scope = measurement_scope_option(
        args, config.measurement_scope);
    config.native_kernel_trace = boolean(
        args, "native-kernel-trace", config.native_kernel_trace);
    config.cores = u32(args, "cores", config.cores);
    config.chunk_instructions = u32(
        args, "chunk-instructions", config.chunk_instructions);
    config.interval_max_cycles = u32(
        args, "interval-max-cycles", config.interval_max_cycles);
    config.interval_same_line_order_audit = boolean(
        args, "interval-same-line-order-audit",
        config.interval_same_line_order_audit);
    config.cpi_attribution = boolean(
        args, "cpi-attribution", config.cpi_attribution);
    config.interval_reweave_passes = u32(
        args, "interval-reweave-passes",
        config.interval_reweave_passes);
    config.interval_causal_timing = boolean(
        args, "interval-causal-timing",
        config.interval_causal_timing);
    config.interval_response_retime = boolean(
        args, "interval-response-retime",
        config.interval_response_retime);
    config.interval_rob_head_suffix_replay = boolean(
        args, "interval-rob-head-suffix-replay",
        config.interval_rob_head_suffix_replay);
    config.interval_causal_passes = u32(
        args, "interval-causal-passes",
        config.interval_causal_passes);
    config.interval_causal_max_closure_events = u32(
        args, "interval-causal-max-closure-events",
        config.interval_causal_max_closure_events);
    config.interval_corrected_suffix_carry = boolean(
        args, "interval-corrected-suffix-carry",
        config.interval_corrected_suffix_carry);
    config.interval_parallel_feedback = boolean(
        args, "interval-parallel-feedback",
        config.interval_parallel_feedback);
    config.interval_private_preview = boolean(
        args, "interval-private-preview",
        config.interval_private_preview);
    config.response_rob_lsq_feedback = boolean(
        args, "response-rob-lsq-feedback",
        config.response_rob_lsq_feedback);
    config.response_fetch_queue_feedback = boolean(
        args, "response-fetch-queue-feedback",
        config.response_fetch_queue_feedback);
    config.committed_static_dependency_feedback = boolean(
        args, "committed-static-dependency-feedback",
        config.committed_static_dependency_feedback);
    config.store_set_same_pc_feedback = boolean(
        args, "store-set-same-pc-feedback",
        config.store_set_same_pc_feedback);
    config.response_sparse_scoreboard = boolean(
        args, "response-sparse-scoreboard",
        config.response_sparse_scoreboard);
    config.response_block_summary = boolean(
        args, "response-block-summary",
        config.response_block_summary);
    config.response_memory_descriptor = boolean(
        args, "response-memory-descriptor",
        config.response_memory_descriptor);
    config.response_batch_timing_encode = boolean(
        args, "response-batch-timing-encode",
        config.response_batch_timing_encode);
    config.response_sparse_resource_repair = boolean(
        args, "response-sparse-resource-repair",
        config.response_sparse_resource_repair);
    config.response_activity_certificate = boolean(
        args, "response-activity-certificate",
        config.response_activity_certificate);
    config.committed_pipeline_audit = boolean(
        args, "committed-pipeline-audit",
        config.committed_pipeline_audit);
    config.rename_free_list = boolean(
        args, "rename-free-list", config.rename_free_list);
    config.response_rename_feedback = boolean(
        args, "response-rename-feedback",
        config.response_rename_feedback);
    config.branch.shadow_rob = boolean(
        args, "branch-shadow-rob", config.branch.shadow_rob);
    config.branch.ras_static_return_target = boolean(
        args, "branch-ras-static-return-target",
        config.branch.ras_static_return_target);
    config.branch.speculative_history = boolean(
        args, "branch-speculative-history",
        config.branch.speculative_history);
    config.branch.squash_width = u32(
        args, "branch-squash-width", config.branch.squash_width);
    config.branch.population_audit = boolean(
        args, "branch-population-audit",
        config.branch.population_audit);
    config.branch.population_history_cycles = u32(
        args, "branch-population-history-cycles",
        config.branch.population_history_cycles);
    config.fetch_buffer_refill_latency = u32(
        args, "fetch-buffer-refill-latency",
        config.fetch_buffer_refill_latency);
    config.fetch_supply_model = boolean(
        args, "fetch-supply-model", config.fetch_supply_model);
    config.fetch_supply_static_instruction_span = boolean(
        args, "fetch-supply-static-instruction-span",
        config.fetch_supply_static_instruction_span);
    config.fetch_supply_speculative_shadow = boolean(
        args, "fetch-supply-speculative-shadow",
        config.fetch_supply_speculative_shadow);
    config.l1i_enabled = boolean(
        args, "l1i-enabled", config.l1i_enabled);
    config.fetch_supply_physical_request_ledger = boolean(
        args, "fetch-supply-physical-request-ledger",
        config.fetch_supply_physical_request_ledger);
    config.fetch_supply_lower_hierarchy = boolean(
        args, "fetch-supply-lower-hierarchy",
        config.fetch_supply_lower_hierarchy);
    config.l1i_miss_penalty = u32(
        args, "l1i-miss-penalty", config.l1i_miss_penalty);
    config.l1i_speculative_entry_state = boolean(
        args, "l1i-speculative-entry-state",
        config.l1i_speculative_entry_state);
    config.l1i_speculative_path_state = boolean(
        args, "l1i-speculative-path-state",
        config.l1i_speculative_path_state);
    config.response_retire_exposure = floating(
        args, "response-retire-exposure",
        config.response_retire_exposure);
    config.store_post_commit_request = boolean(
        args, "store-post-commit-request",
        config.store_post_commit_request);
    config.llc_fill_response_latency = u32(
        args, "llc-fill-response-latency",
        config.llc_fill_response_latency);
    config.needs_tso = boolean(
        args, "needs-tso", config.needs_tso);
    config.syscall_service_latency = u32(
        args, "syscall-service-latency",
        config.syscall_service_latency);
    config.syscall_restart_latency = u32(
        args, "syscall-restart-latency",
        config.syscall_restart_latency);
    config.syscall_cost_model = boolean(
        args, "syscall-cost-model", config.syscall_cost_model);
    config.syscall_kernel_event_model = boolean(
        args, "syscall-event-model",
        config.syscall_kernel_event_model);
    config.page_fault_event_model = boolean(
        args, "page-fault-event-model",
        config.page_fault_event_model);
    config.page_fault_cache_state_model = boolean(
        args, "page-fault-cache-state-model",
        config.page_fault_cache_state_model);
    config.page_fault_syscall_semantic_model = boolean(
        args, "page-fault-syscall-semantic-model",
        config.page_fault_syscall_semantic_model);
    config.page_fault_roi_entry_page_state_model = boolean(
        args, "page-fault-initial-pte-state-model",
        config.page_fault_roi_entry_page_state_model);
    config.page_fault_roi_entry_page_state_model = boolean(
        args, "page-fault-roi-entry-page-state-model",
        config.page_fault_roi_entry_page_state_model);
    config.page_fault_syscall_semantic_fallback_write_probability_ppm = u32(
        args, "page-fault-syscall-semantic-fallback-write-probability-ppm",
        config
            .page_fault_syscall_semantic_fallback_write_probability_ppm);
    config.page_fault_probability_ppm = u32(
        args, "page-fault-probability-ppm",
        config.page_fault_probability_ppm);
    config.irq_event_model = boolean(
        args, "irq-event-model", config.irq_event_model);
    config.irq_period_cycles = u64(
        args, "irq-period-cycles", config.irq_period_cycles);
    config.domain_workers = u32(
        args, "domain-workers", config.domain_workers);
    config.domain_min_events = u32(
        args, "domain-min-events", config.domain_min_events);
    config.dtlb.page_walk_latency = u32(
        args, "dtlb-page-walk-latency",
        config.dtlb.page_walk_latency);
    config.dtlb.speculative_path_state = boolean(
        args, "dtlb-speculative-path-state",
        config.dtlb.speculative_path_state);
    config.dtlb.miss_model = text_option(
        args, "dtlb-miss-model", config.dtlb.miss_model);
    config.allow_mmio_escape = boolean(
        args, "allow-mmio-escape", config.allow_mmio_escape);
    config.require_instruction_page_map = boolean(
        args, "require-instruction-page-map",
        config.require_instruction_page_map);
    config.instruction_address_mode = text_option(
        args, "instruction-address-mode",
        config.instruction_address_mode);
    config.instruction_physical_address_bits = u32(
        args, "instruction-physical-address-bits",
        config.instruction_physical_address_bits);
    config.instruction_page_bits = u32(
        args, "instruction-page-bits",
        config.instruction_page_bits);
    config.instruction_mapping_seed = u64(
        args, "instruction-mapping-seed",
        config.instruction_mapping_seed);
    config.allow_cross_page_without_virtual_token = boolean(
        args, "allow-cross-page-without-virtual-token",
        config.allow_cross_page_without_virtual_token);
    config.dram.size_bytes = u64(
        args, "dram-size", config.dram.size_bytes);
    config.dram.scheduler = text_option(
        args, "dram-scheduler", config.dram.scheduler);
    config.dram.read_buffer_size = u32(
        args, "dram-read-buffer-size", config.dram.read_buffer_size);
    config.dram.separate_write_queue = boolean(
        args, "dram-separate-write-queue",
        config.dram.separate_write_queue);
    config.dram.write_buffer_size = u32(
        args, "dram-write-buffer-size", config.dram.write_buffer_size);
    config.dram.write_high_threshold_percent = u32(
        args, "dram-write-high-threshold-percent",
        config.dram.write_high_threshold_percent);
    config.dram.write_low_threshold_percent = u32(
        args, "dram-write-low-threshold-percent",
        config.dram.write_low_threshold_percent);
    config.dram.min_reads_per_switch = u32(
        args, "dram-min-reads-per-switch",
        config.dram.min_reads_per_switch);
    config.dram.min_writes_per_switch = u32(
        args, "dram-min-writes-per-switch",
        config.dram.min_writes_per_switch);
    config.dram.frfcfs_selection_window = u32(
        args, "dram-frfcfs-selection-window",
        config.dram.frfcfs_selection_window);
    config.dram.frfcfs_topology_scaled_window = boolean(
        args, "dram-frfcfs-topology-scaled-window",
        config.dram.frfcfs_topology_scaled_window);
    config.dram.frfcfs_full_queue_page_policy = boolean(
        args, "dram-frfcfs-full-queue-page-policy",
        config.dram.frfcfs_full_queue_page_policy);
    config.dram.frfcfs_row_cap_single_precharge = boolean(
        args, "dram-frfcfs-row-cap-single-precharge",
        config.dram.frfcfs_row_cap_single_precharge);
    config.dram.frfcfs_passes = u32(
        args, "dram-frfcfs-passes", config.dram.frfcfs_passes);
    config.dram.frfcfs_arrival_bucket_cycles = u32(
        args, "dram-frfcfs-arrival-bucket-cycles",
        config.dram.frfcfs_arrival_bucket_cycles);
    require_concrete_measurement_scope(config.measurement_scope);
    config.validate();
    auto traces =
        fastsim::open_trace_manifest(manifest_path, config.cores);
    fastsim::Simulator simulator(config, std::move(traces));
    emit_stats(simulator.run(), args, config);
    return 0;
}

int benchmark(const Args& args) {
    fastsim::SimulatorConfig config;
    const auto config_it = args.find("config");
    if (config_it != args.end()) {
        config = fastsim::load_simulator_config(config_it->second);
    }
    config.measurement_scope = measurement_scope_option(
        args, config.measurement_scope);
    config.native_kernel_trace = boolean(
        args, "native-kernel-trace", config.native_kernel_trace);
    config.cores = u32(args, "cores", config.cores);
    config.chunk_instructions = u32(
        args, "chunk-instructions", config.chunk_instructions);
    config.interval_max_cycles = u32(
        args, "interval-max-cycles", config.interval_max_cycles);
    config.interval_same_line_order_audit = boolean(
        args, "interval-same-line-order-audit",
        config.interval_same_line_order_audit);
    config.cpi_attribution = boolean(
        args, "cpi-attribution", config.cpi_attribution);
    config.interval_reweave_passes = u32(
        args, "interval-reweave-passes",
        config.interval_reweave_passes);
    config.interval_causal_timing = boolean(
        args, "interval-causal-timing",
        config.interval_causal_timing);
    config.interval_response_retime = boolean(
        args, "interval-response-retime",
        config.interval_response_retime);
    config.interval_rob_head_suffix_replay = boolean(
        args, "interval-rob-head-suffix-replay",
        config.interval_rob_head_suffix_replay);
    config.interval_causal_passes = u32(
        args, "interval-causal-passes",
        config.interval_causal_passes);
    config.interval_causal_max_closure_events = u32(
        args, "interval-causal-max-closure-events",
        config.interval_causal_max_closure_events);
    config.interval_corrected_suffix_carry = boolean(
        args, "interval-corrected-suffix-carry",
        config.interval_corrected_suffix_carry);
    config.interval_parallel_feedback = boolean(
        args, "interval-parallel-feedback",
        config.interval_parallel_feedback);
    config.interval_private_preview = boolean(
        args, "interval-private-preview",
        config.interval_private_preview);
    config.response_rob_lsq_feedback = boolean(
        args, "response-rob-lsq-feedback",
        config.response_rob_lsq_feedback);
    config.response_fetch_queue_feedback = boolean(
        args, "response-fetch-queue-feedback",
        config.response_fetch_queue_feedback);
    config.committed_static_dependency_feedback = boolean(
        args, "committed-static-dependency-feedback",
        config.committed_static_dependency_feedback);
    config.store_set_same_pc_feedback = boolean(
        args, "store-set-same-pc-feedback",
        config.store_set_same_pc_feedback);
    config.response_sparse_scoreboard = boolean(
        args, "response-sparse-scoreboard",
        config.response_sparse_scoreboard);
    config.response_block_summary = boolean(
        args, "response-block-summary",
        config.response_block_summary);
    config.response_memory_descriptor = boolean(
        args, "response-memory-descriptor",
        config.response_memory_descriptor);
    config.response_batch_timing_encode = boolean(
        args, "response-batch-timing-encode",
        config.response_batch_timing_encode);
    config.response_sparse_resource_repair = boolean(
        args, "response-sparse-resource-repair",
        config.response_sparse_resource_repair);
    config.response_activity_certificate = boolean(
        args, "response-activity-certificate",
        config.response_activity_certificate);
    config.committed_pipeline_audit = boolean(
        args, "committed-pipeline-audit",
        config.committed_pipeline_audit);
    config.rename_free_list = boolean(
        args, "rename-free-list", config.rename_free_list);
    config.response_rename_feedback = boolean(
        args, "response-rename-feedback",
        config.response_rename_feedback);
    config.branch.shadow_rob = boolean(
        args, "branch-shadow-rob", config.branch.shadow_rob);
    config.branch.ras_static_return_target = boolean(
        args, "branch-ras-static-return-target",
        config.branch.ras_static_return_target);
    config.branch.speculative_history = boolean(
        args, "branch-speculative-history",
        config.branch.speculative_history);
    config.branch.squash_width = u32(
        args, "branch-squash-width", config.branch.squash_width);
    config.branch.population_audit = boolean(
        args, "branch-population-audit",
        config.branch.population_audit);
    config.branch.population_history_cycles = u32(
        args, "branch-population-history-cycles",
        config.branch.population_history_cycles);
    config.fetch_buffer_refill_latency = u32(
        args, "fetch-buffer-refill-latency",
        config.fetch_buffer_refill_latency);
    config.fetch_supply_model = boolean(
        args, "fetch-supply-model", config.fetch_supply_model);
    config.fetch_supply_static_instruction_span = boolean(
        args, "fetch-supply-static-instruction-span",
        config.fetch_supply_static_instruction_span);
    config.fetch_supply_speculative_shadow = boolean(
        args, "fetch-supply-speculative-shadow",
        config.fetch_supply_speculative_shadow);
    config.l1i_enabled = boolean(
        args, "l1i-enabled", config.l1i_enabled);
    config.fetch_supply_physical_request_ledger = boolean(
        args, "fetch-supply-physical-request-ledger",
        config.fetch_supply_physical_request_ledger);
    config.fetch_supply_lower_hierarchy = boolean(
        args, "fetch-supply-lower-hierarchy",
        config.fetch_supply_lower_hierarchy);
    config.l1i_miss_penalty = u32(
        args, "l1i-miss-penalty", config.l1i_miss_penalty);
    config.l1i_speculative_entry_state = boolean(
        args, "l1i-speculative-entry-state",
        config.l1i_speculative_entry_state);
    config.l1i_speculative_path_state = boolean(
        args, "l1i-speculative-path-state",
        config.l1i_speculative_path_state);
    config.response_retire_exposure = floating(
        args, "response-retire-exposure",
        config.response_retire_exposure);
    config.store_post_commit_request = boolean(
        args, "store-post-commit-request",
        config.store_post_commit_request);
    config.llc_fill_response_latency = u32(
        args, "llc-fill-response-latency",
        config.llc_fill_response_latency);
    config.needs_tso = boolean(
        args, "needs-tso", config.needs_tso);
    config.syscall_service_latency = u32(
        args, "syscall-service-latency",
        config.syscall_service_latency);
    config.syscall_restart_latency = u32(
        args, "syscall-restart-latency",
        config.syscall_restart_latency);
    config.syscall_cost_model = boolean(
        args, "syscall-cost-model", config.syscall_cost_model);
    config.syscall_kernel_event_model = boolean(
        args, "syscall-event-model",
        config.syscall_kernel_event_model);
    config.page_fault_event_model = boolean(
        args, "page-fault-event-model",
        config.page_fault_event_model);
    config.page_fault_cache_state_model = boolean(
        args, "page-fault-cache-state-model",
        config.page_fault_cache_state_model);
    config.page_fault_syscall_semantic_model = boolean(
        args, "page-fault-syscall-semantic-model",
        config.page_fault_syscall_semantic_model);
    config.page_fault_roi_entry_page_state_model = boolean(
        args, "page-fault-initial-pte-state-model",
        config.page_fault_roi_entry_page_state_model);
    config.page_fault_roi_entry_page_state_model = boolean(
        args, "page-fault-roi-entry-page-state-model",
        config.page_fault_roi_entry_page_state_model);
    config.page_fault_syscall_semantic_fallback_write_probability_ppm = u32(
        args, "page-fault-syscall-semantic-fallback-write-probability-ppm",
        config
            .page_fault_syscall_semantic_fallback_write_probability_ppm);
    config.page_fault_probability_ppm = u32(
        args, "page-fault-probability-ppm",
        config.page_fault_probability_ppm);
    config.irq_event_model = boolean(
        args, "irq-event-model", config.irq_event_model);
    config.irq_period_cycles = u64(
        args, "irq-period-cycles", config.irq_period_cycles);
    config.domain_workers = u32(
        args, "domain-workers", config.domain_workers);
    config.domain_min_events = u32(
        args, "domain-min-events", config.domain_min_events);
    config.dtlb.page_walk_latency = u32(
        args, "dtlb-page-walk-latency",
        config.dtlb.page_walk_latency);
    config.dtlb.speculative_path_state = boolean(
        args, "dtlb-speculative-path-state",
        config.dtlb.speculative_path_state);
    config.dtlb.miss_model = text_option(
        args, "dtlb-miss-model", config.dtlb.miss_model);
    config.allow_mmio_escape = boolean(
        args, "allow-mmio-escape", config.allow_mmio_escape);
    config.require_instruction_page_map = boolean(
        args, "require-instruction-page-map",
        config.require_instruction_page_map);
    config.instruction_address_mode = text_option(
        args, "instruction-address-mode",
        config.instruction_address_mode);
    config.instruction_physical_address_bits = u32(
        args, "instruction-physical-address-bits",
        config.instruction_physical_address_bits);
    config.instruction_page_bits = u32(
        args, "instruction-page-bits",
        config.instruction_page_bits);
    config.instruction_mapping_seed = u64(
        args, "instruction-mapping-seed",
        config.instruction_mapping_seed);
    config.allow_cross_page_without_virtual_token = boolean(
        args, "allow-cross-page-without-virtual-token",
        config.allow_cross_page_without_virtual_token);
    config.dram.size_bytes = u64(
        args, "dram-size", config.dram.size_bytes);
    config.dram.scheduler = text_option(
        args, "dram-scheduler", config.dram.scheduler);
    config.dram.read_buffer_size = u32(
        args, "dram-read-buffer-size", config.dram.read_buffer_size);
    config.dram.separate_write_queue = boolean(
        args, "dram-separate-write-queue",
        config.dram.separate_write_queue);
    config.dram.write_buffer_size = u32(
        args, "dram-write-buffer-size", config.dram.write_buffer_size);
    config.dram.write_high_threshold_percent = u32(
        args, "dram-write-high-threshold-percent",
        config.dram.write_high_threshold_percent);
    config.dram.write_low_threshold_percent = u32(
        args, "dram-write-low-threshold-percent",
        config.dram.write_low_threshold_percent);
    config.dram.min_reads_per_switch = u32(
        args, "dram-min-reads-per-switch",
        config.dram.min_reads_per_switch);
    config.dram.min_writes_per_switch = u32(
        args, "dram-min-writes-per-switch",
        config.dram.min_writes_per_switch);
    config.dram.frfcfs_selection_window = u32(
        args, "dram-frfcfs-selection-window",
        config.dram.frfcfs_selection_window);
    config.dram.frfcfs_topology_scaled_window = boolean(
        args, "dram-frfcfs-topology-scaled-window",
        config.dram.frfcfs_topology_scaled_window);
    config.dram.frfcfs_full_queue_page_policy = boolean(
        args, "dram-frfcfs-full-queue-page-policy",
        config.dram.frfcfs_full_queue_page_policy);
    config.dram.frfcfs_row_cap_single_precharge = boolean(
        args, "dram-frfcfs-row-cap-single-precharge",
        config.dram.frfcfs_row_cap_single_precharge);
    config.dram.frfcfs_passes = u32(
        args, "dram-frfcfs-passes", config.dram.frfcfs_passes);
    config.dram.frfcfs_arrival_bucket_cycles = u32(
        args, "dram-frfcfs-arrival-bucket-cycles",
        config.dram.frfcfs_arrival_bucket_cycles);
    require_concrete_measurement_scope(config.measurement_scope);
    config.validate();
    const auto instructions = u64(args, "instructions-per-core", 100'000);
    const auto memory_percent = u32(args, "memory-percent", 30);
    const auto shared_percent = u32(args, "shared-percent", 5);
    const auto working_set =
        u64(args, "working-set-lines", 1ull << 18);
    const auto seed = u64(args, "seed", 1);
    auto traces = fastsim::make_synthetic_traces(
        config.cores, instructions, memory_percent, shared_percent,
        working_set, seed);
    fastsim::Simulator simulator(config, std::move(traces));
    emit_stats(simulator.run(), args, config);
    return 0;
}

int convert_gem5(const Args& args) {
    fastsim::convert_gem5_jsonl_to_binary(
        require(args, "input"), require(args, "output"),
        u32(args, "core", 0),
        text_option(args, "syscall-output", ""),
        fastsim::parse_syscall_abi(
            text_option(args, "syscall-abi", "linux-x86_64")));
    return 0;
}

int upgrade_fst(const Args& args) {
    fastsim::upgrade_binary_trace_to_v7(
        require(args, "input"), require(args, "output"),
        fastsim::parse_syscall_abi(
            text_option(args, "syscall-abi", "linux-x86_64")));
    return 0;
}

void usage(std::ostream& out) {
    out << "FastSim trace-driven multicore simulator\n\n"
        << "Usage:\n"
        << "  fastsim simulate --config FILE --manifest FILE "
           "--measurement-scope user|user-plus-kernel "
           "[--native-kernel-trace BOOL] "
           "[--cores N] [--chunk-instructions N] "
           "[--interval-reweave-passes N] "
           "[--interval-private-preview BOOL] "
           "[--interval-parallel-feedback BOOL] [--domain-workers N] "
           "[--interval-same-line-order-audit BOOL] "
           "[--response-activity-certificate BOOL] "
           "[--response-block-summary BOOL] "
           "[--domain-min-events N] "
           "[--dtlb-miss-model se_atomic|timing_walk] "
           "[--dtlb-page-walk-latency N] "
           "[--dtlb-speculative-path-state BOOL] "
           "[--branch-shadow-rob BOOL] [--branch-squash-width N] "
           "[--branch-population-audit BOOL] "
           "[--branch-population-history-cycles N] "
           "[--branch-ras-static-return-target BOOL] "
           "[--branch-speculative-history BOOL] "
           "[--fetch-buffer-refill-latency N] "
           "[--fetch-supply-model BOOL] "
           "[--fetch-supply-static-instruction-span BOOL] "
           "[--fetch-supply-speculative-shadow BOOL] "
           "[--l1i-enabled BOOL] "
           "[--fetch-supply-physical-request-ledger BOOL] "
           "[--fetch-supply-lower-hierarchy BOOL] "
           "[--instruction-address-mode modeled|trace] "
           "[--instruction-physical-address-bits N] "
           "[--instruction-page-bits N] "
           "[--instruction-mapping-seed N] "
           "[--require-instruction-page-map BOOL] "
           "[--l1i-miss-penalty N] "
           "[--l1i-speculative-entry-state BOOL] "
           "[--l1i-speculative-path-state BOOL] "
           "[--allow-mmio-escape BOOL] "
           "[--allow-cross-page-without-virtual-token BOOL] "
           "[--dram-size BYTES] "
           "[--dram-separate-write-queue BOOL] "
           "[--syscall-service-latency N] "
           "[--syscall-restart-latency N] "
           "[--syscall-cost-model BOOL] "
           "[--syscall-event-model BOOL] "
           "[--page-fault-event-model BOOL] "
           "[--page-fault-cache-state-model BOOL] "
           "[--page-fault-syscall-semantic-model BOOL] "
           "[--page-fault-roi-entry-page-state-model BOOL] "
           "[--page-fault-syscall-semantic-fallback-write-probability-ppm N] "
           "[--page-fault-probability-ppm N] "
           "[--irq-event-model BOOL] "
           "[--irq-period-cycles N] "
           "[--output FILE]\n"
        << "  fastsim benchmark --measurement-scope "
           "user|user-plus-kernel [--config FILE] [--cores N] "
           "[--native-kernel-trace BOOL] "
           "[--instructions-per-core N] [--chunk-instructions N] "
           "[--interval-reweave-passes N] "
           "[--interval-private-preview BOOL] "
           "[--interval-parallel-feedback BOOL] "
           "[--domain-workers N] "
           "[--interval-same-line-order-audit BOOL] "
           "[--response-activity-certificate BOOL] "
           "[--response-block-summary BOOL] "
           "[--domain-min-events N] "
           "[--dtlb-miss-model se_atomic|timing_walk] "
           "[--dtlb-page-walk-latency N] "
           "[--dtlb-speculative-path-state BOOL] "
           "[--branch-shadow-rob BOOL] [--branch-squash-width N] "
           "[--branch-population-audit BOOL] "
           "[--branch-population-history-cycles N] "
           "[--branch-ras-static-return-target BOOL] "
           "[--branch-speculative-history BOOL] "
           "[--fetch-buffer-refill-latency N] "
           "[--fetch-supply-model BOOL] "
           "[--fetch-supply-static-instruction-span BOOL] "
           "[--fetch-supply-speculative-shadow BOOL] "
           "[--l1i-enabled BOOL] "
           "[--fetch-supply-physical-request-ledger BOOL] "
           "[--fetch-supply-lower-hierarchy BOOL] "
           "[--instruction-address-mode modeled|trace] "
           "[--instruction-physical-address-bits N] "
           "[--instruction-page-bits N] "
           "[--instruction-mapping-seed N] "
           "[--require-instruction-page-map BOOL] "
           "[--l1i-miss-penalty N] "
           "[--l1i-speculative-entry-state BOOL] "
           "[--l1i-speculative-path-state BOOL] "
           "[--allow-mmio-escape BOOL] "
           "[--allow-cross-page-without-virtual-token BOOL] "
           "[--dram-size BYTES] "
           "[--dram-separate-write-queue BOOL] "
           "[--syscall-service-latency N] "
           "[--syscall-restart-latency N] "
           "[--syscall-cost-model BOOL] "
           "[--syscall-event-model BOOL] "
           "[--page-fault-event-model BOOL] "
           "[--page-fault-cache-state-model BOOL] "
           "[--page-fault-syscall-semantic-model BOOL] "
           "[--page-fault-roi-entry-page-state-model BOOL] "
           "[--page-fault-syscall-semantic-fallback-write-probability-ppm N] "
           "[--page-fault-probability-ppm N] "
           "[--irq-event-model BOOL] "
           "[--irq-period-cycles N] "
           "[--output FILE]\n"
        << "  fastsim convert-gem5 --input TRACE.jsonl "
           "--output TRACE.fst --core N "
           "[--syscall-output SYSCALLS.jsonl] "
           "[--syscall-abi linux-x86_64|linux-x86_32|"
           "linux-aarch64|linux-arm32|unknown]\n";
    out << "  fastsim upgrade-fst --input LEGACY.fst "
           "--output TRACE.v7.fst "
           "[--syscall-abi linux-x86_64|linux-x86_32|"
           "linux-aarch64|linux-arm32|unknown]\n";
}

}  // namespace

int main(int argc, char** argv) {
    try {
        if (argc < 2) {
            usage(std::cerr);
            return 2;
        }
        const std::string command(argv[1]);
        const auto args = parse_args(argc, argv, 2);
        if (command == "simulate") return simulate(args);
        if (command == "benchmark") return benchmark(args);
        if (command == "convert-gem5") return convert_gem5(args);
        if (command == "upgrade-fst") return upgrade_fst(args);
        if (command == "help" || command == "--help" ||
            command == "-h") {
            usage(std::cout);
            return 0;
        }
        throw std::invalid_argument("unknown command: " + command);
    } catch (const std::exception& error) {
        std::cerr << "fastsim: " << error.what() << '\n';
        return 1;
    }
}
