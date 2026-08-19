#include "fastsim/config.hpp"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <functional>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <unordered_set>
#include <vector>

namespace fastsim {
namespace {

std::string trim(std::string value) {
    const auto not_space = [](unsigned char c) { return !std::isspace(c); };
    value.erase(value.begin(),
                std::find_if(value.begin(), value.end(), not_space));
    value.erase(std::find_if(value.rbegin(), value.rend(), not_space).base(),
                value.end());
    return value;
}

std::string lower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(),
                   [](unsigned char c) {
                       return static_cast<char>(std::tolower(c));
                   });
    return value;
}

std::uint64_t parse_u64_value(std::string text) {
    text = trim(text);
    std::uint64_t multiplier = 1;
    const std::string normalized = lower(text);
    const struct {
        const char* suffix;
        std::uint64_t multiplier;
    } suffixes[] = {
        {"kib", 1ull << 10}, {"mib", 1ull << 20}, {"gib", 1ull << 30},
        {"kb", 1000ull}, {"mb", 1000ull * 1000ull},
        {"gb", 1000ull * 1000ull * 1000ull},
        {"k", 1ull << 10}, {"m", 1ull << 20}, {"g", 1ull << 30},
    };
    for (const auto& suffix : suffixes) {
        const std::string needle(suffix.suffix);
        if (normalized.size() >= needle.size() &&
            normalized.compare(normalized.size() - needle.size(),
                               needle.size(), needle) == 0) {
            multiplier = suffix.multiplier;
            text.resize(text.size() - needle.size());
            text = trim(text);
            break;
        }
    }
    std::size_t consumed = 0;
    const auto base = (text.size() > 2 && text[0] == '0' &&
                       (text[1] == 'x' || text[1] == 'X'))
                          ? 16
                          : 10;
    const auto value = std::stoull(text, &consumed, base);
    if (consumed != text.size()) {
        throw std::invalid_argument("invalid integer: " + text);
    }
    if (value > std::numeric_limits<std::uint64_t>::max() / multiplier) {
        throw std::overflow_error("integer with suffix overflows: " + text);
    }
    return value * multiplier;
}

std::vector<std::uint64_t> parse_colon_u64_fields(
    const std::string& text, const std::string& key) {
    std::vector<std::uint64_t> fields;
    std::size_t pos = 0;
    while (pos <= text.size()) {
        auto colon = text.find(':', pos);
        if (colon == std::string::npos) colon = text.size();
        const auto field = trim(text.substr(pos, colon - pos));
        if (field.empty()) {
            throw std::invalid_argument(
                "empty " + key + " field: " + text);
        }
        try {
            fields.push_back(parse_u64_value(field));
        } catch (const std::exception&) {
            throw std::invalid_argument(
                "invalid " + key + " entry: " + text);
        }
        if (colon == text.size()) break;
        pos = colon + 1;
    }
    return fields;
}

std::unordered_set<std::uint64_t> parse_comma_u64_set(
    const std::string& text, const std::string& key) {
    std::unordered_set<std::uint64_t> values;
    std::size_t pos = 0;
    while (pos < text.size()) {
        auto comma = text.find(',', pos);
        if (comma == std::string::npos) comma = text.size();
        const auto item = trim(text.substr(pos, comma - pos));
        if (item.empty()) {
            throw std::invalid_argument("empty " + key + " entry");
        }
        try {
            values.insert(parse_u64_value(item));
        } catch (const std::exception&) {
            throw std::invalid_argument(
                "invalid " + key + " entry: " + item);
        }
        pos = comma + 1;
    }
    return values;
}

KernelEventProfile parse_kernel_event_profile(
    const std::string& text, const std::string& key) {
    const auto fields = parse_colon_u64_fields(text, key);
    if (fields.size() != 14 && fields.size() != 16 &&
        fields.size() != 22) {
        throw std::invalid_argument(
            key + " requires 14 legacy, 16 transitional, or 22 P0 fields: " +
            text);
    }
    if (fields[0] > std::numeric_limits<std::uint32_t>::max()) {
        throw std::invalid_argument(
            key + " service exceeds uint32: " + text);
    }
    KernelEventProfile profile;
    profile.encoding_fields = static_cast<std::uint8_t>(fields.size());
    profile.service_cycles = static_cast<std::uint32_t>(fields[0]);
    profile.retired_instructions = fields[1];
    profile.retired_uops = fields[2];
    const bool has_memory_contract = fields.size() >= 16;
    const std::size_t offset = has_memory_contract ? 2 : 0;
    if (has_memory_contract) {
        profile.memory_uops = fields[3];
        profile.line_requests = fields[4];
    }
    profile.branches = fields[3 + offset];
    profile.branch_misses = fields[4 + offset];
    profile.l1d_accesses = fields[5 + offset];
    profile.l1d_misses = fields[6 + offset];
    profile.l2_accesses = fields[7 + offset];
    profile.l2_misses = fields[8 + offset];
    profile.llc_accesses = fields[9 + offset];
    profile.llc_misses = fields[10 + offset];
    std::size_t tail = 11 + offset;
    if (fields.size() == 22) {
        profile.permission_upgrades = fields[tail++];
        profile.remote_supplies = fields[tail++];
        profile.llc_merged_misses = fields[tail++];
        profile.llc_unique_fills = fields[tail++];
        profile.dram_reads = fields[tail++];
        profile.dram_writes = fields[tail++];
    }
    profile.dtlb_accesses = fields[tail++];
    profile.dtlb_misses = fields[tail++];
    profile.blocked_wall_cycles = fields[tail];
    if (fields.size() == 14) {
        // Diagnostic compatibility only: the old format conflated one
        // memory UOP, one line request, and one L1D lookup.
        profile.memory_uops = profile.l1d_accesses;
        profile.line_requests = profile.l1d_accesses;
    }
    return profile;
}

ReplacementPolicy parse_replacement(const std::string& value) {
    const auto normalized = lower(trim(value));
    if (normalized == "lru") return ReplacementPolicy::kLru;
    if (normalized == "tree_plru" || normalized == "treeplru" ||
        normalized == "plru") {
        return ReplacementPolicy::kTreePlru;
    }
    throw std::invalid_argument("unknown replacement policy: " + value);
}

void load_cache(const KeyValueConfig& source, const std::string& prefix,
                CacheConfig& cache) {
    cache.size_bytes = source.get_u64(prefix + ".size", cache.size_bytes);
    cache.associativity =
        source.get_u32(prefix + ".associativity", cache.associativity);
    cache.line_size =
        source.get_u32(prefix + ".line_size", cache.line_size);
    cache.hit_latency =
        source.get_u32(prefix + ".hit_latency", cache.hit_latency);
    cache.replacement = parse_replacement(
        source.get_string(prefix + ".replacement",
                          cache.replacement == ReplacementPolicy::kLru
                              ? "lru"
                              : "tree_plru"));
}

bool is_power_of_two(std::uint64_t value) {
    return value != 0 && (value & (value - 1)) == 0;
}

void validate_cache(const char* name, const CacheConfig& cache) {
    if (!is_power_of_two(cache.line_size)) {
        throw std::invalid_argument(std::string(name) +
                                    ".line_size must be a power of two");
    }
    if (cache.associativity == 0 || cache.size_bytes == 0 ||
        cache.size_bytes %
                (static_cast<std::uint64_t>(cache.associativity) *
                 cache.line_size) !=
            0) {
        throw std::invalid_argument(std::string(name) +
                                    " size/associativity geometry is invalid");
    }
    const auto maximum_associativity =
        cache.replacement == ReplacementPolicy::kTreePlru ? 64u : 256u;
    if (cache.associativity > maximum_associativity) {
        throw std::invalid_argument(
            std::string(name) + " associativity exceeds the " +
            (cache.replacement == ReplacementPolicy::kTreePlru
                 ? "64-way TreePLRU"
                 : "256-way LRU") +
            " implementation limit");
    }
    const auto sets =
        cache.size_bytes /
        (static_cast<std::uint64_t>(cache.associativity) * cache.line_size);
    if (!is_power_of_two(sets) ||
        sets > std::numeric_limits<std::uint32_t>::max()) {
        throw std::invalid_argument(std::string(name) +
                                    " set count must be a uint32 power of two");
    }
    if (cache.replacement == ReplacementPolicy::kTreePlru &&
        !is_power_of_two(cache.associativity)) {
        throw std::invalid_argument(std::string(name) +
                                    " TreePLRU requires power-of-two ways");
    }
}

}  // namespace

MeasurementScope parse_measurement_scope(const std::string& value) {
    const auto normalized = lower(trim(value));
    if (normalized.empty() || normalized == "unspecified") {
        return MeasurementScope::kUnspecified;
    }
    if (normalized == "user") return MeasurementScope::kUser;
    if (normalized == "user-plus-kernel" ||
        normalized == "user_plus_kernel") {
        return MeasurementScope::kUserPlusKernel;
    }
    throw std::invalid_argument(
        "measurement.scope must be user or user-plus-kernel");
}

const char* measurement_scope_name(MeasurementScope scope) {
    switch (scope) {
        case MeasurementScope::kUnspecified:
            return "unspecified";
        case MeasurementScope::kUser:
            return "user";
        case MeasurementScope::kUserPlusKernel:
            return "user-plus-kernel";
    }
    throw std::invalid_argument("invalid measurement scope");
}

KeyValueConfig KeyValueConfig::load(const std::string& path) {
    std::unordered_set<std::string> active;
    std::function<KeyValueConfig(const std::filesystem::path&)> load_one;
    load_one = [&](const std::filesystem::path& requested) {
        const auto resolved = std::filesystem::absolute(requested)
                                  .lexically_normal();
        const auto identity = resolved.string();
        if (!active.insert(identity).second) {
            throw std::invalid_argument(
                "cyclic config.include involving: " + identity);
        }

        std::ifstream input(resolved);
        if (!input) {
            active.erase(identity);
            throw std::runtime_error("cannot open config: " + identity);
        }
        std::ostringstream buffer;
        buffer << input.rdbuf();
        auto local = parse(buffer.str());

        KeyValueConfig result;
        const auto include = local.values_.find("config.include");
        if (include != local.values_.end()) {
            auto include_path = std::filesystem::path(include->second);
            if (include_path.is_relative()) {
                include_path = resolved.parent_path() / include_path;
            }
            result = load_one(include_path);
            local.values_.erase(include);
        }
        for (const auto& [key, value] : local.values_) {
            result.values_[key] = value;
        }
        active.erase(identity);
        return result;
    };
    return load_one(path);
}

KeyValueConfig KeyValueConfig::parse(const std::string& text) {
    KeyValueConfig result;
    std::istringstream input(text);
    std::string line;
    std::size_t line_number = 0;
    while (std::getline(input, line)) {
        ++line_number;
        const auto comment = line.find('#');
        if (comment != std::string::npos) line.resize(comment);
        line = trim(line);
        if (line.empty()) continue;
        const auto equals = line.find('=');
        if (equals == std::string::npos) {
            throw std::invalid_argument("config line " +
                                        std::to_string(line_number) +
                                        " has no '='");
        }
        auto key = trim(line.substr(0, equals));
        auto value = trim(line.substr(equals + 1));
        if (key.empty() || value.empty()) {
            throw std::invalid_argument("config line " +
                                        std::to_string(line_number) +
                                        " has empty key/value");
        }
        if (value.size() >= 2 &&
            ((value.front() == '"' && value.back() == '"') ||
             (value.front() == '\'' && value.back() == '\''))) {
            value = value.substr(1, value.size() - 2);
        }
        result.values_[key] = value;
    }
    return result;
}

bool KeyValueConfig::contains(const std::string& key) const {
    return values_.find(key) != values_.end();
}

std::string KeyValueConfig::get_string(const std::string& key,
                                       const std::string& fallback) const {
    const auto it = values_.find(key);
    return it == values_.end() ? fallback : it->second;
}

std::uint64_t KeyValueConfig::get_u64(const std::string& key,
                                      std::uint64_t fallback) const {
    const auto it = values_.find(key);
    return it == values_.end() ? fallback : parse_u64_value(it->second);
}

std::uint32_t KeyValueConfig::get_u32(const std::string& key,
                                      std::uint32_t fallback) const {
    const auto value = get_u64(key, fallback);
    if (value > std::numeric_limits<std::uint32_t>::max()) {
        throw std::overflow_error(key + " exceeds uint32");
    }
    return static_cast<std::uint32_t>(value);
}

double KeyValueConfig::get_double(const std::string& key,
                                  double fallback) const {
    const auto it = values_.find(key);
    if (it == values_.end()) return fallback;
    std::size_t consumed = 0;
    const auto value = std::stod(it->second, &consumed);
    if (consumed != it->second.size()) {
        throw std::invalid_argument("invalid floating point value for " + key);
    }
    return value;
}

bool KeyValueConfig::get_bool(const std::string& key, bool fallback) const {
    const auto it = values_.find(key);
    if (it == values_.end()) return fallback;
    const auto value = lower(trim(it->second));
    if (value == "true" || value == "yes" || value == "1" || value == "on") {
        return true;
    }
    if (value == "false" || value == "no" || value == "0" ||
        value == "off") {
        return false;
    }
    throw std::invalid_argument("invalid boolean value for " + key);
}

void SimulatorConfig::validate() const {
    const bool kernel_service_enabled =
        syscall_service_latency != 0 || syscall_cost_model ||
        syscall_kernel_event_model || page_fault_event_model ||
        irq_event_model;
    if (measurement_scope == MeasurementScope::kUser &&
        kernel_service_enabled) {
        throw std::invalid_argument(
            "measurement.scope=user requires syscall service, syscall "
            "cost/event, page-fault event, and IRQ event models to be "
            "disabled");
    }
    if (measurement_scope == MeasurementScope::kUserPlusKernel &&
        !kernel_service_enabled) {
        throw std::invalid_argument(
            "measurement.scope=user-plus-kernel requires at least one "
            "kernel service model");
    }
    if (cores == 0 || cores > 256) {
        throw std::invalid_argument("sim.cores must be in [1, 256]");
    }
    if (chunk_instructions == 0) {
        throw std::invalid_argument("sim.chunk_instructions must be nonzero");
    }
    if (lookahead_chunks == 0 || lookahead_chunks > 64) {
        throw std::invalid_argument(
            "sim.lookahead_chunks must be in [1, 64]");
    }
    if (core_model != "scalar" && core_model != "interval_bound" &&
        core_model != "interval_weave") {
        throw std::invalid_argument(
            "core.model must be scalar, interval_bound, or interval_weave");
    }
    if (interval_target_uops == 0 ||
        (core_model == "interval_weave" &&
         interval_target_uops > chunk_instructions)) {
        throw std::invalid_argument(
            "sim.interval_target_uops must be in [1, chunk_instructions]");
    }
    if (interval_max_cycles == 0) {
        throw std::invalid_argument(
            "sim.interval_max_cycles must be nonzero");
    }
    if (interval_scheduler != "frontier" &&
        interval_scheduler != "time_epoch") {
        throw std::invalid_argument(
            "sim.interval_scheduler must be frontier or time_epoch");
    }
    if (interval_reweave_passes == 0 || interval_reweave_passes > 8) {
        throw std::invalid_argument(
            "sim.interval_reweave_passes must be in [1, 8]");
    }
    if (interval_causal_passes < 2 || interval_causal_passes > 8) {
        throw std::invalid_argument(
            "sim.interval_causal_passes must be in [2, 8]");
    }
    if (interval_causal_max_closure_events == 0) {
        throw std::invalid_argument(
            "sim.interval_causal_max_closure_events must be nonzero");
    }
    if (interval_causal_timing && interval_reweave_passes != 1) {
        throw std::invalid_argument(
            "sim.interval_causal_timing cannot be combined with legacy "
            "whole-epoch reweave");
    }
    if (interval_response_retime && interval_reweave_passes != 1) {
        throw std::invalid_argument(
            "sim.interval_response_retime cannot be combined with legacy "
            "whole-epoch reweave");
    }
    if (interval_response_retime && interval_causal_timing) {
        throw std::invalid_argument(
            "sim.interval_response_retime cannot be combined with legacy "
            "causal timing repair");
    }
    if (interval_rob_head_suffix_replay &&
        interval_scheduler != "time_epoch") {
        throw std::invalid_argument(
            "sim.interval_rob_head_suffix_replay requires time_epoch");
    }
    if (interval_rob_head_suffix_replay &&
        interval_reweave_passes != 1) {
        throw std::invalid_argument(
            "sim.interval_rob_head_suffix_replay cannot be combined with "
            "legacy whole-epoch reweave");
    }
    if (interval_rob_head_suffix_replay &&
        (interval_response_retime || interval_causal_timing)) {
        throw std::invalid_argument(
            "sim.interval_rob_head_suffix_replay is mutually exclusive "
            "with whole-epoch response/causal retiming");
    }
    if (interval_corrected_suffix_carry &&
        interval_scheduler != "time_epoch") {
        throw std::invalid_argument(
            "sim.interval_corrected_suffix_carry requires time_epoch");
    }
    if (interval_corrected_suffix_carry &&
        interval_reweave_passes != 1) {
        throw std::invalid_argument(
            "sim.interval_corrected_suffix_carry cannot be combined with "
            "legacy whole-epoch reweave");
    }
    if (interval_corrected_suffix_carry && interval_causal_timing) {
        throw std::invalid_argument(
            "sim.interval_corrected_suffix_carry cannot be combined with "
            "legacy causal timing repair");
    }
    if (domain_workers > (1u << 16)) {
        throw std::invalid_argument(
            "sim.domain_workers must be in [0, 65536]");
    }
    if (domain_min_events == 0) {
        throw std::invalid_argument(
            "sim.domain_min_events must be nonzero");
    }
    if (response_rob_lsq_feedback && !response_queue_feedback) {
        throw std::invalid_argument(
            "core.response_rob_lsq_feedback requires "
            "core.response_queue_feedback");
    }
    if (response_sparse_scoreboard && !response_queue_feedback) {
        throw std::invalid_argument(
            "core.response_sparse_scoreboard requires "
            "core.response_queue_feedback");
    }
    if (response_sparse_scoreboard && response_rob_lsq_feedback) {
        throw std::invalid_argument(
            "core.response_sparse_scoreboard and "
            "core.response_rob_lsq_feedback are mutually exclusive");
    }
    if (response_block_summary && !response_sparse_scoreboard) {
        throw std::invalid_argument(
            "core.response_block_summary requires "
            "core.response_sparse_scoreboard");
    }
    if (response_sparse_resource_repair &&
        !response_sparse_scoreboard) {
        throw std::invalid_argument(
            "core.response_sparse_resource_repair requires "
            "core.response_sparse_scoreboard");
    }
    if (interval_rob_head_suffix_replay &&
        !response_sparse_scoreboard) {
        throw std::invalid_argument(
            "sim.interval_rob_head_suffix_replay requires "
            "core.response_sparse_scoreboard");
    }
    if (response_activity_certificate &&
        !response_sparse_scoreboard) {
        throw std::invalid_argument(
            "core.response_activity_certificate requires "
            "core.response_sparse_scoreboard");
    }
    if (!std::isfinite(response_retire_exposure) ||
        response_retire_exposure < 0.0 ||
        response_retire_exposure > 1.0) {
        throw std::invalid_argument(
            "core.response_retire_exposure must be in [0, 1]");
    }
    const auto check_core_count = [](const char* name,
                                     std::uint32_t value) {
        if (value == 0 || value > (1u << 16)) {
            throw std::invalid_argument(std::string(name) +
                                        " must be in [1, 65536]");
        }
    };
    check_core_count("core.fetch_width", fetch_width);
    if (fetch_buffer_bytes != 0 &&
        (!is_power_of_two(fetch_buffer_bytes) ||
         fetch_buffer_bytes > (1u << 20))) {
        throw std::invalid_argument(
            "core.fetch_buffer_bytes must be zero or a power of two in "
            "[1, 1048576]");
    }
    if (fetch_buffer_refill_latency > (1u << 20)) {
        throw std::invalid_argument(
            "core.fetch_buffer_refill_latency must be in [0, 1048576]");
    }
    if (l1i_enabled && fetch_buffer_bytes == 0) {
        throw std::invalid_argument(
            "cache.l1i.enabled requires core.fetch_buffer_bytes");
    }
    if (l1i_enabled && l1i.line_size != fetch_buffer_bytes) {
        throw std::invalid_argument(
            "cache.l1i.line_size must equal core.fetch_buffer_bytes");
    }
    if (l1i_speculative_entry_state && !l1i_enabled) {
        throw std::invalid_argument(
            "cache.l1i.speculative_entry_state requires cache.l1i.enabled");
    }
    if (l1i_speculative_path_state && !l1i_enabled) {
        throw std::invalid_argument(
            "cache.l1i.speculative_path_state requires cache.l1i.enabled");
    }
    if (l1i_speculative_entry_state && l1i_speculative_path_state) {
        throw std::invalid_argument(
            "cache.l1i speculative entry/path models are mutually exclusive");
    }
    if (dtlb.speculative_path_state &&
        (!dtlb.enabled || !l1i_speculative_path_state)) {
        throw std::invalid_argument(
            "dtlb.speculative_path_state requires dtlb.enabled and "
            "cache.l1i.speculative_path_state");
    }
    if (l1i_miss_penalty > (1u << 20)) {
        throw std::invalid_argument(
            "cache.l1i.miss_penalty must be in [0, 1048576]");
    }
    check_core_count("core.decode_width", decode_width);
    check_core_count("core.rename_width", rename_width);
    check_core_count("core.dispatch_width", dispatch_width);
    check_core_count("core.issue_width", issue_width);
    check_core_count("core.writeback_width", writeback_width);
    check_core_count("core.commit_width", commit_width);
    check_core_count("core.fetch_queue_entries", fetch_queue_entries);
    check_core_count("core.rob_entries", rob_entries);
    check_core_count("core.iq_entries", iq_entries);
    check_core_count("core.lq_entries", lq_entries);
    check_core_count("core.sq_entries", sq_entries);
    check_core_count("core.integer_alu_units", integer_alu_units);
    check_core_count("core.integer_multiply_units",
                     integer_multiply_units);
    check_core_count("core.float_simple_units", float_simple_units);
    check_core_count("core.float_complex_units", float_complex_units);
    check_core_count("core.simd_units", simd_units);
    check_core_count("core.predicate_units", predicate_units);
    check_core_count("core.memory_units", memory_units);
    check_core_count("core.system_units", system_units);
    check_core_count("core.cache_load_ports", cache_load_ports);
    check_core_count("core.cache_store_ports", cache_store_ports);
    for (const auto delay : {fetch_to_decode, decode_to_rename,
                             rename_to_dispatch, iew_to_rename,
                             commit_to_rename, dispatch_to_issue,
                             issue_to_execute, execute_to_commit}) {
        if (delay > (1u << 20)) {
            throw std::invalid_argument(
                "core pipeline delays must be in [0, 1048576]");
        }
    }
    const auto check_latency = [](const char* name, std::uint32_t value) {
        if (value == 0 || value > (1u << 20)) {
            throw std::invalid_argument(std::string(name) +
                                        " must be in [1, 1048576]");
        }
    };
    check_latency("core.integer_alu_latency", integer_alu_latency);
    check_latency("core.integer_multiply_latency",
                  integer_multiply_latency);
    check_latency("core.integer_divide_latency", integer_divide_latency);
    check_latency("core.float_simple_latency", float_simple_latency);
    check_latency("core.float_multiply_latency", float_multiply_latency);
    check_latency("core.float_multiply_accumulate_latency",
                  float_multiply_accumulate_latency);
    check_latency("core.float_misc_latency", float_misc_latency);
    check_latency("core.float_divide_latency", float_divide_latency);
    check_latency("core.float_sqrt_latency", float_sqrt_latency);
    check_latency("core.simd_latency", simd_latency);
    check_latency("core.predicate_latency", predicate_latency);
    check_latency("core.system_latency", system_latency);
    check_latency("core.minimum_load_latency", minimum_load_latency);
    if (syscall_service_latency > (1u << 20) ||
        syscall_restart_latency > (1u << 20)) {
        throw std::invalid_argument(
            "syscall service/restart latency must be in [0, 1048576]");
    }
    if (static_cast<std::uint64_t>(system_latency) +
            syscall_service_latency >
        (1ull << 21)) {
        throw std::invalid_argument(
            "combined system and syscall service latency is too large");
    }
    for (const auto& [sysnum, cycles] : syscall_cost_table) {
        (void)sysnum;
        if (cycles > (1u << 20) ||
            static_cast<std::uint64_t>(system_latency) + cycles >
                (1ull << 21)) {
            throw std::invalid_argument(
                "syscall cost table service latency is too large");
        }
    }
    const auto check_kernel_event_profile = [this](
            const char* name, const KernelEventProfile& profile,
            bool includes_system_latency) {
        const auto combined = static_cast<std::uint64_t>(
            profile.service_cycles) +
            (includes_system_latency ? system_latency : 0u);
        if (profile.service_cycles > (1u << 20) ||
            combined > (1ull << 21)) {
            throw std::invalid_argument(
                std::string(name) + " service latency is too large");
        }
        if (profile.retired_uops < profile.retired_instructions) {
            throw std::invalid_argument(
                std::string(name) + " uops must cover instructions");
        }
        const bool legacy_memory_contract =
            profile.memory_uops == 0 && profile.line_requests == 0 &&
            profile.l1d_accesses != 0;
        if (!legacy_memory_contract &&
            (profile.memory_uops > profile.retired_uops ||
             profile.line_requests < profile.memory_uops ||
             profile.l1d_accesses != profile.line_requests)) {
            throw std::invalid_argument(
                std::string(name) +
                " memory UOP/line-request accounting is inconsistent");
        }
        if (profile.branch_misses > profile.branches ||
            profile.l1d_misses > profile.l1d_accesses ||
            profile.l2_misses > profile.l2_accesses ||
            profile.llc_misses > profile.llc_accesses ||
            profile.dtlb_misses > profile.dtlb_accesses) {
            throw std::invalid_argument(
                std::string(name) + " PMU misses exceed accesses");
        }
        if (profile.permission_upgrades > profile.line_requests ||
            profile.remote_supplies > profile.line_requests ||
            profile.llc_merged_misses > profile.llc_misses ||
            profile.llc_unique_fills > profile.llc_misses ||
            profile.dram_reads > profile.llc_unique_fills) {
            throw std::invalid_argument(
                std::string(name) +
                " hierarchy events exceed their parent population");
        }
    };
    for (const auto& [sysnum, profile] : syscall_kernel_event_table) {
        (void)sysnum;
        check_kernel_event_profile(
            "syscall event profile", profile, true);
    }
    if (syscall_kernel_event_default_profile_enabled) {
        check_kernel_event_profile(
            "default syscall event profile",
            syscall_kernel_event_default_profile, true);
    }
    check_kernel_event_profile(
        "page-fault event profile", page_fault_event_profile, false);
    check_kernel_event_profile(
        "IRQ event profile", irq_event_profile, false);
    if (page_fault_probability_ppm > 1'000'000 ||
        page_fault_background_write_probability_ppm > 1'000'000 ||
        page_fault_allocation_probability_ppm > 1'000'000 ||
        page_fault_allocation_write_probability_ppm > 1'000'000 ||
        page_fault_syscall_semantic_fallback_write_probability_ppm >
            1'000'000) {
        throw std::invalid_argument(
            "page-fault probabilities must be in [0, 1000000]");
    }
    for (const auto& [sysnum, probability] :
         page_fault_allocation_probability_table) {
        (void)sysnum;
        if (probability.read_ppm > 1'000'000 ||
            probability.write_ppm > 1'000'000) {
            throw std::invalid_argument(
                "page-fault allocation table probabilities must be in "
                "[0, 1000000]");
        }
    }
    if ((page_fault_event_model || page_fault_cache_state_model ||
         page_fault_syscall_semantic_model ||
         page_fault_initial_pte_state_model) &&
        !require_virtual_page_token) {
        throw std::invalid_argument(
            "page-fault event/cache-state/semantic/initial-PTE models require "
            "trace.require_virtual_page_token=true");
    }
    if (!page_fault_syscall_semantic_model &&
        page_fault_syscall_semantic_fallback_write_probability_ppm != 0) {
        throw std::invalid_argument(
            "page_fault.syscall_semantic_fallback_write_probability_ppm "
            "requires page_fault.syscall_semantic_model=true");
    }
    if (irq_event_model && irq_period_cycles == 0) {
        throw std::invalid_argument(
            "irq.event_model requires irq.period_cycles > 0");
    }
    check_core_count("cache.l1d.mshrs", l1d_mshrs);
    check_core_count("cache.l2.mshrs", l2_mshrs);
    check_core_count("cache.llc.mshrs", llc_mshrs);
    if (ruby_sequencer_max_outstanding > (1u << 16)) {
        throw std::invalid_argument(
            "ruby.sequencer_max_outstanding must be 0 or in [1, 65536]");
    }
    if (ruby_sequencer_max_outstanding != 0 &&
        core_model != "interval_weave") {
        throw std::invalid_argument(
            "ruby.sequencer_max_outstanding currently requires "
            "core.model=interval_weave");
    }
    if (response_queue_feedback && core_model != "interval_weave") {
        throw std::invalid_argument(
            "core.response_queue_feedback requires "
            "core.model=interval_weave");
    }
    if (committed_pipeline_audit && core_model == "scalar") {
        throw std::invalid_argument(
            "core.committed_pipeline_audit requires an interval core model");
    }
    if (rename_free_list && core_model == "scalar") {
        throw std::invalid_argument(
            "core.rename_free_list requires an interval core model");
    }
    if (response_rename_feedback && core_model != "interval_weave") {
        throw std::invalid_argument(
            "core.response_rename_feedback requires "
            "core.model=interval_weave");
    }
    if (response_rename_feedback && rename_free_list) {
        throw std::invalid_argument(
            "core.response_rename_feedback and core.rename_free_list are "
            "mutually exclusive timing models");
    }
    if (response_rename_feedback &&
        !response_sparse_scoreboard) {
        throw std::invalid_argument(
            "core.response_rename_feedback requires "
            "core.response_sparse_scoreboard=true");
    }
    check_core_count(
        "core.rename_int_free_entries", rename_int_free_entries);
    check_core_count(
        "core.rename_float_free_entries", rename_float_free_entries);
    check_core_count(
        "core.rename_vec_free_entries", rename_vec_free_entries);
    check_core_count(
        "core.rename_cc_free_entries", rename_cc_free_entries);
    if (dtlb.enabled) {
        if (core_model == "scalar") {
            throw std::invalid_argument(
                "dtlb.enabled requires an interval core model");
        }
        check_core_count("dtlb.entries", dtlb.entries);
        if (dtlb.miss_model != "se_atomic" &&
            dtlb.miss_model != "timing_walk") {
            throw std::invalid_argument(
                "dtlb.miss_model must be se_atomic or timing_walk");
        }
        if (dtlb.miss_model == "timing_walk") {
            check_core_count("dtlb.page_walkers", dtlb.page_walkers);
            check_latency("dtlb.page_walk_latency",
                          dtlb.page_walk_latency);
        }
        if (dtlb.hit_latency > (1u << 20)) {
            throw std::invalid_argument(
                "dtlb.hit_latency must be in [0, 1048576]");
        }
    }
    if (cha_count == 0 || !is_power_of_two(cha_count)) {
        throw std::invalid_argument("uncore.cha_count must be a power of two");
    }
    if (directory_memory_latency > (1u << 20)) {
        throw std::invalid_argument(
            "uncore.directory_memory_latency must be in [0, 1048576]");
    }
    if (llc_fill_response_latency > (1u << 20)) {
        throw std::invalid_argument(
            "uncore.llc_fill_response_latency must be in [0, 1048576]");
    }
    if (!std::isfinite(memory_exposure) ||
        memory_exposure < 0.0 || memory_exposure > 1.0) {
        throw std::invalid_argument("core.memory_exposure must be in [0,1]");
    }
    validate_cache("cache.l1i", l1i);
    validate_cache("cache.l1d", l1d);
    validate_cache("cache.l2", l2);
    validate_cache("cache.llc", llc);
    if (l1d.line_size != l2.line_size || l1d.line_size != llc.line_size) {
        throw std::invalid_argument("all cache levels must use one line size");
    }
    if (page_fault_cache_state_model &&
        (l1d.line_size > 4096 || 4096 % l1d.line_size != 0)) {
        throw std::invalid_argument(
            "page_fault.cache_state_model requires a cache line size "
            "that divides 4096 bytes");
    }
    if (dram.channels == 0 || !is_power_of_two(dram.channels) ||
        dram.banks_per_channel == 0 ||
        !is_power_of_two(dram.banks_per_channel) ||
        dram.ranks_per_channel == 0 ||
        !is_power_of_two(dram.ranks_per_channel) ||
        dram.bank_groups_per_rank == 0 ||
        !is_power_of_two(dram.bank_groups_per_rank) ||
        dram.bank_groups_per_rank > dram.banks_per_channel ||
        dram.banks_per_channel % dram.bank_groups_per_rank != 0 ||
        dram.row_bytes == 0 || !is_power_of_two(dram.row_bytes) ||
        dram.row_bytes < l1d.line_size ||
        dram.row_bytes % l1d.line_size != 0 ||
        dram.size_bytes < l1d.line_size ||
        dram.size_bytes % l1d.line_size != 0 ||
        dram.frontend_latency > (1u << 20) ||
        dram.backend_latency > (1u << 20) ||
        dram.t_ras > (1u << 20) ||
        dram.t_rtp > (1u << 20) ||
        dram.t_rrd > (1u << 20) ||
        dram.t_rrd_l > (1u << 20) ||
        dram.t_xaw > (1u << 20) ||
        dram.activation_limit > dram.banks_per_channel ||
        (dram.t_rrd_l != 0 && dram.t_rrd_l < dram.t_rrd) ||
        (dram.activation_limit != 0 && dram.t_xaw == 0) ||
        dram.t_ccd_l > (1u << 20) ||
        dram.t_cs > (1u << 20)) {
        throw std::invalid_argument(
            "DRAM channels, banks, ranks, bank groups, and row bytes "
            "must be powers of two, bank groups must divide banks/rank, "
            "row bytes must contain whole cache lines, and DRAM size must "
            "be a nonzero whole number of cache lines");
    }
    if (dram.scheduler != "fcfs" && dram.scheduler != "frfcfs") {
        throw std::invalid_argument(
            "dram.scheduler must be fcfs or frfcfs");
    }
    if (dram.read_buffer_size == 0 ||
        dram.read_buffer_size > (1u << 20) ||
        dram.write_buffer_size == 0 ||
        dram.write_buffer_size > (1u << 20) ||
        dram.write_high_threshold_percent == 0 ||
        dram.write_high_threshold_percent > 100 ||
        dram.write_low_threshold_percent >=
            dram.write_high_threshold_percent ||
        dram.min_reads_per_switch == 0 ||
        dram.min_reads_per_switch > dram.read_buffer_size ||
        dram.min_writes_per_switch == 0 ||
        dram.min_writes_per_switch > dram.write_buffer_size ||
        dram.frfcfs_selection_window > dram.read_buffer_size ||
        dram.frfcfs_passes == 0 || dram.frfcfs_passes > 32 ||
        (dram.frfcfs_arrival_bucket_cycles != 0 &&
         !is_power_of_two(dram.frfcfs_arrival_bucket_cycles)) ||
        dram.max_accesses_per_row > (1u << 20)) {
        throw std::invalid_argument(
            "DRAM read/write buffers must be in [1,1048576], write "
            "thresholds must satisfy 0 <= low < high <= 100, minimum "
            "read/write bursts must fit their physical buffers, and "
            "dram.frfcfs_selection_window must be zero or no larger "
            "than dram.read_buffer_size; "
            "dram.frfcfs_passes must be in [1,32]");
    }
    const auto check_entries = [](const char* name, std::uint32_t value) {
        if (!is_power_of_two(value)) {
            throw std::invalid_argument(std::string(name) +
                                        " must be a power of two");
        }
    };
    check_entries("branch.local_history_entries",
                  branch.local_history_entries);
    check_entries("branch.local_entries", branch.local_entries);
    check_entries("branch.global_entries", branch.global_entries);
    check_entries("branch.choice_entries", branch.choice_entries);
    check_entries("branch.btb_entries", branch.btb_entries);
    check_entries("branch.indirect_sets", branch.indirect_sets);
    if (branch.type != "tournament" && branch.type != "gshare") {
        throw std::invalid_argument(
            "branch.type must be tournament or gshare");
    }
    if (branch.btb_associativity == 0 ||
        branch.btb_entries % branch.btb_associativity != 0) {
        throw std::invalid_argument("invalid BTB geometry");
    }
    if (branch.indirect_ways == 0) {
        throw std::invalid_argument("branch.indirect_ways must be nonzero");
    }
    const auto btb_sets =
        branch.btb_entries / branch.btb_associativity;
    std::uint32_t btb_set_bits = 0;
    for (auto remaining = btb_sets; remaining > 1; remaining >>= 1) {
        ++btb_set_bits;
    }
    if (branch.inst_shift >= 64 || branch.btb_set_shift >= 64 ||
        branch.btb_set_shift + btb_set_bits >= 64 ||
        static_cast<std::uint64_t>(
            branch.btb_set_shift + btb_set_bits) +
                branch.btb_tag_bits >
            64 ||
        static_cast<std::uint64_t>(branch.inst_shift) +
                branch.indirect_tag_bits >
            64) {
        throw std::invalid_argument("invalid branch PC/BTB shift geometry");
    }
    for (const auto bits : {branch.local_counter_bits,
                            branch.global_counter_bits,
                            branch.choice_counter_bits}) {
        if (bits == 0 || bits > 8) {
            throw std::invalid_argument(
                "branch counter bits must be in [1,8]");
        }
    }
    if (branch.ras_entries == 0 ||
        branch.btb_tag_bits == 0 || branch.btb_tag_bits > 64 ||
        branch.indirect_tag_bits == 0 || branch.indirect_tag_bits > 31 ||
        branch.indirect_ghr_bits == 0 || branch.indirect_ghr_bits > 31 ||
        branch.indirect_path_length == 0) {
        throw std::invalid_argument("invalid branch tag/history geometry");
    }
    if (branch.squash_width > (1u << 20)) {
        throw std::invalid_argument(
            "branch.squash_width must be zero (unlimited) or in "
            "[1, 1048576]");
    }
}

SimulatorConfig load_simulator_config(const std::string& path) {
    const auto source = KeyValueConfig::load(path);
    SimulatorConfig config;
    config.measurement_scope = parse_measurement_scope(
        source.get_string(
            "measurement.scope",
            measurement_scope_name(config.measurement_scope)));
    config.cores = source.get_u32("sim.cores", config.cores);
    config.chunk_instructions = source.get_u32(
        "sim.chunk_instructions", config.chunk_instructions);
    config.lookahead_chunks = source.get_u32(
        "sim.lookahead_chunks", config.lookahead_chunks);
    config.interval_target_uops = source.get_u32(
        "sim.interval_target_uops", config.interval_target_uops);
    config.interval_max_cycles = source.get_u32(
        "sim.interval_max_cycles", config.interval_max_cycles);
    config.interval_scheduler = source.get_string(
        "sim.interval_scheduler", config.interval_scheduler);
    config.interval_full_order_audit = source.get_bool(
        "sim.interval_full_order_audit",
        config.interval_full_order_audit);
    config.interval_same_line_order_audit = source.get_bool(
        "sim.interval_same_line_order_audit",
        config.interval_same_line_order_audit);
    config.cpi_attribution = source.get_bool(
        "sim.cpi_attribution", config.cpi_attribution);
    config.interval_private_preview = source.get_bool(
        "sim.interval_private_preview",
        config.interval_private_preview);
    config.interval_reweave_passes = source.get_u32(
        "sim.interval_reweave_passes",
        config.interval_reweave_passes);
    config.interval_causal_timing = source.get_bool(
        "sim.interval_causal_timing",
        config.interval_causal_timing);
    config.interval_response_retime = source.get_bool(
        "sim.interval_response_retime",
        config.interval_response_retime);
    config.interval_rob_head_suffix_replay = source.get_bool(
        "sim.interval_rob_head_suffix_replay",
        config.interval_rob_head_suffix_replay);
    config.interval_causal_passes = source.get_u32(
        "sim.interval_causal_passes",
        config.interval_causal_passes);
    config.interval_causal_max_closure_events = source.get_u32(
        "sim.interval_causal_max_closure_events",
        config.interval_causal_max_closure_events);
    config.interval_corrected_suffix_carry = source.get_bool(
        "sim.interval_corrected_suffix_carry",
        config.interval_corrected_suffix_carry);
    config.interval_parallel_feedback = source.get_bool(
        "sim.interval_parallel_feedback",
        config.interval_parallel_feedback);
    config.domain_workers = source.get_u32(
        "sim.domain_workers", config.domain_workers);
    config.domain_min_events = source.get_u32(
        "sim.domain_min_events", config.domain_min_events);
    config.core_model =
        source.get_string("core.model", config.core_model);
    config.fetch_width =
        source.get_u32("core.fetch_width", config.fetch_width);
    config.fetch_buffer_bytes = source.get_u32(
        "core.fetch_buffer_bytes", config.fetch_buffer_bytes);
    config.fetch_buffer_refill_latency = source.get_u32(
        "core.fetch_buffer_refill_latency",
        config.fetch_buffer_refill_latency);
    config.l1i_enabled = source.get_bool(
        "cache.l1i.enabled", config.l1i_enabled);
    config.l1i_miss_penalty = source.get_u32(
        "cache.l1i.miss_penalty", config.l1i_miss_penalty);
    config.l1i_speculative_entry_state = source.get_bool(
        "cache.l1i.speculative_entry_state",
        config.l1i_speculative_entry_state);
    config.l1i_speculative_path_state = source.get_bool(
        "cache.l1i.speculative_path_state",
        config.l1i_speculative_path_state);
    config.decode_width =
        source.get_u32("core.decode_width", config.decode_width);
    config.rename_width =
        source.get_u32("core.rename_width", config.rename_width);
    config.issue_width =
        source.get_u32("core.issue_width", config.issue_width);
    config.dispatch_width =
        source.get_u32("core.dispatch_width", config.dispatch_width);
    config.writeback_width =
        source.get_u32("core.writeback_width", config.writeback_width);
    config.commit_width =
        source.get_u32("core.commit_width", config.commit_width);
    config.fetch_queue_entries = source.get_u32(
        "core.fetch_queue_entries", config.fetch_queue_entries);
    config.rob_entries =
        source.get_u32("core.rob_entries", config.rob_entries);
    config.iq_entries =
        source.get_u32("core.iq_entries", config.iq_entries);
    config.lq_entries =
        source.get_u32("core.lq_entries", config.lq_entries);
    config.sq_entries =
        source.get_u32("core.sq_entries", config.sq_entries);
    config.committed_pipeline_audit = source.get_bool(
        "core.committed_pipeline_audit",
        config.committed_pipeline_audit);
    config.rename_free_list = source.get_bool(
        "core.rename_free_list", config.rename_free_list);
    config.response_rename_feedback = source.get_bool(
        "core.response_rename_feedback",
        config.response_rename_feedback);
    config.rename_int_free_entries = source.get_u32(
        "core.rename_int_free_entries",
        config.rename_int_free_entries);
    config.rename_float_free_entries = source.get_u32(
        "core.rename_float_free_entries",
        config.rename_float_free_entries);
    config.rename_vec_free_entries = source.get_u32(
        "core.rename_vec_free_entries",
        config.rename_vec_free_entries);
    config.rename_cc_free_entries = source.get_u32(
        "core.rename_cc_free_entries",
        config.rename_cc_free_entries);
    config.dispatch_to_issue = source.get_u32(
        "core.dispatch_to_issue", config.dispatch_to_issue);
    config.fetch_to_decode = source.get_u32(
        "core.fetch_to_decode", config.fetch_to_decode);
    config.decode_to_rename = source.get_u32(
        "core.decode_to_rename", config.decode_to_rename);
    config.rename_to_dispatch = source.get_u32(
        "core.rename_to_dispatch", config.rename_to_dispatch);
    config.iew_to_rename = source.get_u32(
        "core.iew_to_rename", config.iew_to_rename);
    config.commit_to_rename = source.get_u32(
        "core.commit_to_rename", config.commit_to_rename);
    config.issue_to_execute = source.get_u32(
        "core.issue_to_execute", config.issue_to_execute);
    config.execute_to_commit = source.get_u32(
        "core.execute_to_commit", config.execute_to_commit);
    config.minimum_load_latency = source.get_u32(
        "core.minimum_load_latency", config.minimum_load_latency);
    config.response_queue_feedback = source.get_bool(
        "core.response_queue_feedback",
        config.response_queue_feedback);
    config.response_rob_lsq_feedback = source.get_bool(
        "core.response_rob_lsq_feedback",
        config.response_rob_lsq_feedback);
    config.response_sparse_scoreboard = source.get_bool(
        "core.response_sparse_scoreboard",
        config.response_sparse_scoreboard);
    config.response_block_summary = source.get_bool(
        "core.response_block_summary",
        config.response_block_summary);
    config.response_memory_descriptor = source.get_bool(
        "core.response_memory_descriptor",
        config.response_memory_descriptor);
    config.response_batch_timing_encode = source.get_bool(
        "core.response_batch_timing_encode",
        config.response_batch_timing_encode);
    config.response_sparse_resource_repair = source.get_bool(
        "core.response_sparse_resource_repair",
        config.response_sparse_resource_repair);
    config.response_activity_certificate = source.get_bool(
        "core.response_activity_certificate",
        config.response_activity_certificate);
    config.response_retire_exposure = source.get_double(
        "core.response_retire_exposure",
        config.response_retire_exposure);
    config.needs_tso = source.get_bool(
        "core.needs_tso", config.needs_tso);
    config.integer_alu_units = source.get_u32(
        "core.integer_alu_units", config.integer_alu_units);
    config.integer_multiply_units = source.get_u32(
        "core.integer_multiply_units", config.integer_multiply_units);
    config.float_simple_units = source.get_u32(
        "core.float_simple_units", config.float_simple_units);
    config.float_complex_units = source.get_u32(
        "core.float_complex_units", config.float_complex_units);
    config.simd_units =
        source.get_u32("core.simd_units", config.simd_units);
    config.predicate_units = source.get_u32(
        "core.predicate_units", config.predicate_units);
    config.memory_units =
        source.get_u32("core.memory_units", config.memory_units);
    config.system_units =
        source.get_u32("core.system_units", config.system_units);
    config.cache_load_ports = source.get_u32(
        "core.cache_load_ports", config.cache_load_ports);
    config.cache_store_ports = source.get_u32(
        "core.cache_store_ports", config.cache_store_ports);
    config.integer_alu_latency = source.get_u32(
        "core.integer_alu_latency", config.integer_alu_latency);
    config.integer_multiply_latency = source.get_u32(
        "core.integer_multiply_latency",
        config.integer_multiply_latency);
    config.integer_divide_latency = source.get_u32(
        "core.integer_divide_latency", config.integer_divide_latency);
    config.integer_alu_pipelined = source.get_bool(
        "core.integer_alu_pipelined", config.integer_alu_pipelined);
    config.integer_multiply_pipelined = source.get_bool(
        "core.integer_multiply_pipelined",
        config.integer_multiply_pipelined);
    config.integer_divide_pipelined = source.get_bool(
        "core.integer_divide_pipelined",
        config.integer_divide_pipelined);
    config.float_simple_latency = source.get_u32(
        "core.float_simple_latency", config.float_simple_latency);
    config.float_multiply_latency = source.get_u32(
        "core.float_multiply_latency", config.float_multiply_latency);
    config.float_multiply_accumulate_latency = source.get_u32(
        "core.float_multiply_accumulate_latency",
        config.float_multiply_accumulate_latency);
    config.float_misc_latency = source.get_u32(
        "core.float_misc_latency", config.float_misc_latency);
    config.float_divide_latency = source.get_u32(
        "core.float_divide_latency", config.float_divide_latency);
    config.float_sqrt_latency = source.get_u32(
        "core.float_sqrt_latency", config.float_sqrt_latency);
    config.float_simple_pipelined = source.get_bool(
        "core.float_simple_pipelined", config.float_simple_pipelined);
    config.float_complex_pipelined = source.get_bool(
        "core.float_complex_pipelined", config.float_complex_pipelined);
    config.float_divide_pipelined = source.get_bool(
        "core.float_divide_pipelined", config.float_divide_pipelined);
    config.float_sqrt_pipelined = source.get_bool(
        "core.float_sqrt_pipelined", config.float_sqrt_pipelined);
    config.simd_latency =
        source.get_u32("core.simd_latency", config.simd_latency);
    config.predicate_latency = source.get_u32(
        "core.predicate_latency", config.predicate_latency);
    config.system_latency =
        source.get_u32("core.system_latency", config.system_latency);
    config.syscall_service_latency = source.get_u32(
        "syscall.service_latency", config.syscall_service_latency);
    config.syscall_restart_latency = source.get_u32(
        "syscall.restart_latency", config.syscall_restart_latency);
    config.syscall_cost_model = source.get_bool(
        "syscall.cost_model", config.syscall_cost_model);
    {
        // Compact table form: "sysnum:cycles,sysnum:cycles,...".
        const auto table = source.get_string("syscall.cost_table", "");
        std::size_t pos = 0;
        while (pos < table.size()) {
            auto comma = table.find(',', pos);
            if (comma == std::string::npos) comma = table.size();
            const auto item = table.substr(pos, comma - pos);
            const auto colon = item.find(':');
            if (colon != std::string::npos) {
                try {
                    const auto num = static_cast<std::uint64_t>(
                        std::stoull(item.substr(0, colon)));
                    const auto cyc = static_cast<std::uint32_t>(
                        std::stoul(item.substr(colon + 1)));
                    config.syscall_cost_table[num] = cyc;
                } catch (const std::exception&) {
                    throw std::invalid_argument(
                        "invalid syscall.cost_table entry: " + item);
                }
            }
            pos = comma + 1;
        }
    }
    config.syscall_kernel_event_model = source.get_bool(
        "syscall.event_model", config.syscall_kernel_event_model);
    {
        // Compact frozen-profile form. Entries are comma separated; fields
        // are colon separated in this order:
        // sysnum:service:instructions:uops:memory_uops:line_requests:
        // branches:branch_misses:
        // l1d_accesses:l1d_misses:l2_accesses:l2_misses:
        // llc_accesses:llc_misses:permission_upgrades:remote_supplies:
        // llc_merged_misses:llc_unique_fills:dram_reads:dram_writes:
        // dtlb_accesses:dtlb_misses:blocked_wall_cycles. Legacy 14-field and
        // transitional 16-field profiles remain readable but are non-formal.
        const auto table = source.get_string("syscall.event_table", "");
        std::size_t pos = 0;
        while (pos < table.size()) {
            auto comma = table.find(',', pos);
            if (comma == std::string::npos) comma = table.size();
            const auto item = trim(table.substr(pos, comma - pos));
            const auto colon = item.find(':');
            if (colon == std::string::npos) {
                throw std::invalid_argument(
                    "syscall.event_table entry requires 23 P0 fields "
                    "(15 legacy or 17 transitional): " +
                    item);
            }
            std::uint64_t sysnum = 0;
            try {
                sysnum = parse_u64_value(item.substr(0, colon));
            } catch (const std::exception&) {
                throw std::invalid_argument(
                    "invalid syscall.event_table entry: " + item);
            }
            const auto profile = parse_kernel_event_profile(
                item.substr(colon + 1), "syscall.event_table profile");
            config.syscall_kernel_event_table[sysnum] = profile;
            pos = comma + 1;
        }
    }
    {
        const auto profile = source.get_string(
            "syscall.event_default_profile", "");
        if (!profile.empty()) {
            config.syscall_kernel_event_default_profile =
                parse_kernel_event_profile(
                    profile, "syscall.event_default_profile");
            config.syscall_kernel_event_default_profile_enabled = true;
        }
    }
    config.page_fault_event_model = source.get_bool(
        "page_fault.event_model", config.page_fault_event_model);
    config.page_fault_cache_state_model = source.get_bool(
        "page_fault.cache_state_model",
        config.page_fault_cache_state_model);
    config.page_fault_syscall_semantic_model = source.get_bool(
        "page_fault.syscall_semantic_model",
        config.page_fault_syscall_semantic_model);
    config.page_fault_initial_pte_state_model = source.get_bool(
        "page_fault.initial_pte_state_model",
        config.page_fault_initial_pte_state_model);
    config.page_fault_syscall_semantic_fallback_write_probability_ppm =
        source.get_u32(
            "page_fault.syscall_semantic_fallback_write_probability_ppm",
            config
                .page_fault_syscall_semantic_fallback_write_probability_ppm);
    {
        const auto syscalls = source.get_string(
            "page_fault.allocation_syscalls", "");
        if (!syscalls.empty()) {
            config.page_fault_allocation_syscalls = parse_comma_u64_set(
                syscalls, "page_fault.allocation_syscalls");
        }
    }
    config.page_fault_probability_ppm = source.get_u32(
        "page_fault.probability_ppm",
        config.page_fault_probability_ppm);
    config.page_fault_background_write_probability_ppm =
        source.contains("page_fault.background_write_probability_ppm")
            ? source.get_u32(
                  "page_fault.background_write_probability_ppm",
                  config.page_fault_background_write_probability_ppm)
            : config.page_fault_probability_ppm;
    config.page_fault_allocation_window_records = source.get_u64(
        "page_fault.allocation_window_records",
        config.page_fault_allocation_window_records);
    config.page_fault_allocation_probability_ppm = source.get_u32(
        "page_fault.allocation_probability_ppm",
        config.page_fault_allocation_probability_ppm);
    config.page_fault_allocation_write_probability_ppm =
        source.contains("page_fault.allocation_write_probability_ppm")
            ? source.get_u32(
                  "page_fault.allocation_write_probability_ppm",
                  config.page_fault_allocation_write_probability_ppm)
            : config.page_fault_allocation_probability_ppm;
    {
        // Compact frozen hierarchical table:
        // sysnum:read_probability_ppm:write_probability_ppm,...
        const auto table = source.get_string(
            "page_fault.allocation_probability_table", "");
        std::size_t pos = 0;
        while (pos < table.size()) {
            auto comma = table.find(',', pos);
            if (comma == std::string::npos) comma = table.size();
            const auto item = trim(table.substr(pos, comma - pos));
            const auto fields = parse_colon_u64_fields(
                item, "page_fault.allocation_probability_table");
            if (fields.size() != 3 ||
                fields[1] > std::numeric_limits<std::uint32_t>::max() ||
                fields[2] > std::numeric_limits<std::uint32_t>::max()) {
                throw std::invalid_argument(
                    "page_fault.allocation_probability_table entry "
                    "requires sysnum:read_ppm:write_ppm: " + item);
            }
            config.page_fault_allocation_probability_table[fields[0]] = {
                static_cast<std::uint32_t>(fields[1]),
                static_cast<std::uint32_t>(fields[2])};
            pos = comma + 1;
        }
    }
    {
        const auto profile = source.get_string(
            "page_fault.event_profile", "");
        if (!profile.empty()) {
            config.page_fault_event_profile = parse_kernel_event_profile(
                profile, "page_fault.event_profile");
        }
    }
    config.irq_event_model = source.get_bool(
        "irq.event_model", config.irq_event_model);
    config.irq_period_cycles = source.get_u64(
        "irq.period_cycles", config.irq_period_cycles);
    {
        const auto profile = source.get_string(
            "irq.event_profile", "");
        if (!profile.empty()) {
            config.irq_event_profile = parse_kernel_event_profile(
                profile, "irq.event_profile");
        }
    }
    config.l1d_mshrs =
        source.get_u32("cache.l1d.mshrs", config.l1d_mshrs);
    config.l2_mshrs =
        source.get_u32("cache.l2.mshrs", config.l2_mshrs);
    config.llc_mshrs =
        source.get_u32("cache.llc.mshrs", config.llc_mshrs);
    config.ruby_sequencer_max_outstanding = source.get_u32(
        "ruby.sequencer_max_outstanding",
        config.ruby_sequencer_max_outstanding);
    config.memory_exposure =
        source.get_double("core.memory_exposure", config.memory_exposure);
    config.cha_count =
        source.get_u32("uncore.cha_count", config.cha_count);
    config.noc_one_way_latency = source.get_u32(
        "uncore.noc_one_way_latency", config.noc_one_way_latency);
    config.llc_service_cycles = source.get_u32(
        "uncore.llc_service_cycles", config.llc_service_cycles);
    config.directory_memory_latency = source.get_u32(
        "uncore.directory_memory_latency",
        config.directory_memory_latency);
    config.llc_fill_response_latency = source.get_u32(
        "uncore.llc_fill_response_latency",
        config.llc_fill_response_latency);
    config.cha_xor_hash = source.get_bool(
        "uncore.cha_xor_hash", config.cha_xor_hash);
    config.coherence =
        source.get_bool("uncore.coherence", config.coherence);
    config.inclusive_llc =
        source.get_bool("cache.llc.inclusive", config.inclusive_llc);
    config.strict_physical_address = source.get_bool(
        "trace.strict_physical_address", config.strict_physical_address);
    config.require_virtual_page_token = source.get_bool(
        "trace.require_virtual_page_token",
        config.require_virtual_page_token);
    config.allow_cross_page_without_virtual_token = source.get_bool(
        "trace.allow_cross_page_without_virtual_token",
        config.allow_cross_page_without_virtual_token);
    config.allow_mmio_escape = source.get_bool(
        "trace.allow_mmio_escape", config.allow_mmio_escape);

    auto& dtlb = config.dtlb;
    dtlb.enabled = source.get_bool("dtlb.enabled", dtlb.enabled);
    dtlb.speculative_path_state = source.get_bool(
        "dtlb.speculative_path_state", dtlb.speculative_path_state);
    dtlb.entries = source.get_u32("dtlb.entries", dtlb.entries);
    dtlb.hit_latency = source.get_u32(
        "dtlb.hit_latency", dtlb.hit_latency);
    dtlb.page_walk_latency = source.get_u32(
        "dtlb.page_walk_latency", dtlb.page_walk_latency);
    dtlb.miss_model = source.get_string(
        "dtlb.miss_model", dtlb.miss_model);
    dtlb.page_walkers = source.get_u32(
        "dtlb.page_walkers", dtlb.page_walkers);
    dtlb.coalesce_misses = source.get_bool(
        "dtlb.coalesce_misses", dtlb.coalesce_misses);

    load_cache(source, "cache.l1i", config.l1i);
    load_cache(source, "cache.l1d", config.l1d);
    load_cache(source, "cache.l2", config.l2);
    load_cache(source, "cache.llc", config.llc);

    auto& branch = config.branch;
    branch.type = source.get_string("branch.type", branch.type);
    const auto common_counter_bits =
        source.get_u32("branch.counter_bits", branch.local_counter_bits);
    branch.local_counter_bits = source.get_u32(
        "branch.local_counter_bits", common_counter_bits);
    branch.global_counter_bits = source.get_u32(
        "branch.global_counter_bits", common_counter_bits);
    branch.choice_counter_bits = source.get_u32(
        "branch.choice_counter_bits", common_counter_bits);
    branch.local_history_entries = source.get_u32(
        "branch.local_history_entries", branch.local_history_entries);
    branch.local_entries =
        source.get_u32("branch.local_entries", branch.local_entries);
    branch.global_entries =
        source.get_u32("branch.global_entries", branch.global_entries);
    branch.choice_entries =
        source.get_u32("branch.choice_entries", branch.choice_entries);
    branch.inst_shift =
        source.get_u32("branch.inst_shift", branch.inst_shift);
    branch.btb_entries =
        source.get_u32("branch.btb_entries", branch.btb_entries);
    branch.btb_associativity = source.get_u32(
        "branch.btb_associativity", branch.btb_associativity);
    branch.btb_tag_bits =
        source.get_u32("branch.btb_tag_bits", branch.btb_tag_bits);
    branch.btb_set_shift =
        source.get_u32("branch.btb_set_shift", branch.btb_set_shift);
    branch.ras_entries =
        source.get_u32("branch.ras_entries", branch.ras_entries);
    branch.indirect_sets =
        source.get_u32("branch.indirect_sets", branch.indirect_sets);
    branch.indirect_ways =
        source.get_u32("branch.indirect_ways", branch.indirect_ways);
    branch.indirect_tag_bits = source.get_u32(
        "branch.indirect_tag_bits", branch.indirect_tag_bits);
    branch.indirect_path_length = source.get_u32(
        "branch.indirect_path_length", branch.indirect_path_length);
    branch.indirect_speculative_path_length = source.get_u32(
        "branch.indirect_speculative_path_length",
        branch.indirect_speculative_path_length);
    branch.indirect_ghr_bits = source.get_u32(
        "branch.indirect_ghr_bits", branch.indirect_ghr_bits);
    branch.indirect_hash_ghr = source.get_bool(
        "branch.indirect_hash_ghr", branch.indirect_hash_ghr);
    branch.indirect_hash_targets = source.get_bool(
        "branch.indirect_hash_targets", branch.indirect_hash_targets);
    branch.requires_btb_hit = source.get_bool(
        "branch.requires_btb_hit", branch.requires_btb_hit);
    branch.update_btb_at_squash = source.get_bool(
        "branch.update_btb_at_squash", branch.update_btb_at_squash);
    branch.mispredict_penalty = source.get_u32(
        "branch.mispredict_penalty", branch.mispredict_penalty);
    branch.squash_width = source.get_u32(
        "branch.squash_width", branch.squash_width);
    branch.shadow_rob = source.get_bool(
        "branch.shadow_rob", branch.shadow_rob);

    auto& dram = config.dram;
    dram.size_bytes =
        source.get_u64("dram.size", dram.size_bytes);
    dram.channels =
        source.get_u32("dram.channels", dram.channels);
    dram.banks_per_channel = source.get_u32(
        "dram.banks_per_channel", dram.banks_per_channel);
    dram.ranks_per_channel = source.get_u32(
        "dram.ranks_per_channel", dram.ranks_per_channel);
    dram.bank_groups_per_rank = source.get_u32(
        "dram.bank_groups_per_rank", dram.bank_groups_per_rank);
    dram.row_bytes =
        source.get_u32("dram.row_bytes", dram.row_bytes);
    dram.t_cl = source.get_u32("dram.t_cl", dram.t_cl);
    dram.t_rcd = source.get_u32("dram.t_rcd", dram.t_rcd);
    dram.t_rp = source.get_u32("dram.t_rp", dram.t_rp);
    dram.t_ras = source.get_u32("dram.t_ras", dram.t_ras);
    dram.t_rtp = source.get_u32("dram.t_rtp", dram.t_rtp);
    dram.t_rrd = source.get_u32("dram.t_rrd", dram.t_rrd);
    dram.t_rrd_l = source.get_u32("dram.t_rrd_l", dram.t_rrd_l);
    dram.t_xaw = source.get_u32("dram.t_xaw", dram.t_xaw);
    dram.activation_limit = source.get_u32(
        "dram.activation_limit", dram.activation_limit);
    dram.burst_cycles =
        source.get_u32("dram.burst_cycles", dram.burst_cycles);
    dram.t_ccd_l = source.get_u32("dram.t_ccd_l", dram.t_ccd_l);
    dram.t_cs = source.get_u32("dram.t_cs", dram.t_cs);
    dram.frontend_latency = source.get_u32(
        "dram.frontend_latency", dram.frontend_latency);
    dram.backend_latency = source.get_u32(
        "dram.backend_latency", dram.backend_latency);
    dram.scheduler = source.get_string(
        "dram.scheduler", dram.scheduler);
    dram.read_buffer_size = source.get_u32(
        "dram.read_buffer_size", dram.read_buffer_size);
    dram.separate_write_queue = source.get_bool(
        "dram.separate_write_queue", dram.separate_write_queue);
    dram.write_buffer_size = source.get_u32(
        "dram.write_buffer_size", dram.write_buffer_size);
    dram.write_high_threshold_percent = source.get_u32(
        "dram.write_high_threshold_percent",
        dram.write_high_threshold_percent);
    dram.write_low_threshold_percent = source.get_u32(
        "dram.write_low_threshold_percent",
        dram.write_low_threshold_percent);
    dram.min_reads_per_switch = source.get_u32(
        "dram.min_reads_per_switch", dram.min_reads_per_switch);
    dram.min_writes_per_switch = source.get_u32(
        "dram.min_writes_per_switch", dram.min_writes_per_switch);
    dram.frfcfs_selection_window = source.get_u32(
        "dram.frfcfs_selection_window",
        dram.frfcfs_selection_window);
    dram.frfcfs_topology_scaled_window = source.get_bool(
        "dram.frfcfs_topology_scaled_window",
        dram.frfcfs_topology_scaled_window);
    dram.frfcfs_full_queue_page_policy = source.get_bool(
        "dram.frfcfs_full_queue_page_policy",
        dram.frfcfs_full_queue_page_policy);
    dram.frfcfs_row_cap_single_precharge = source.get_bool(
        "dram.frfcfs_row_cap_single_precharge",
        dram.frfcfs_row_cap_single_precharge);
    dram.frfcfs_passes = source.get_u32(
        "dram.frfcfs_passes", dram.frfcfs_passes);
    dram.frfcfs_arrival_bucket_cycles = source.get_u32(
        "dram.frfcfs_arrival_bucket_cycles",
        dram.frfcfs_arrival_bucket_cycles);
    dram.max_accesses_per_row = source.get_u32(
        "dram.max_accesses_per_row", dram.max_accesses_per_row);

    config.validate();
    return config;
}

}  // namespace fastsim
