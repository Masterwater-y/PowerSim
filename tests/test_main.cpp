#include <array>
#include <cstdio>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <numeric>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <unistd.h>

#include "fastsim/cache.hpp"
#include "fastsim/config.hpp"
#include "fastsim/interval_core.hpp"
#include "fastsim/predictor.hpp"
#include "fastsim/pending_fill.hpp"
#include "fastsim/simulator.hpp"
#include "fastsim/trace.hpp"

namespace {

class VectorTraceSource final : public fastsim::TraceSource {
  public:
    explicit VectorTraceSource(
        std::vector<fastsim::TraceRecord> records)
        : records_(std::move(records)) {}

    bool next(fastsim::TraceRecord& record) override {
        if (cursor_ == records_.size()) return false;
        record = records_[cursor_++];
        return true;
    }

    std::string description() const override { return "test-vector"; }

  private:
    std::vector<fastsim::TraceRecord> records_;
    std::size_t cursor_ = 0;
};

class StaticMapTraceSource final : public fastsim::TraceSource {
  public:
    explicit StaticMapTraceSource(
        std::vector<fastsim::StaticInstructionInfo> instructions)
        : instructions_(std::move(instructions)) {}

    bool next(fastsim::TraceRecord&) override { return false; }
    std::string description() const override { return "test-static-map"; }
    const fastsim::StaticInstructionInfo* static_instruction(
        std::uint64_t pc) const override {
        for (const auto& instruction : instructions_) {
            if (instruction.pc == pc) return &instruction;
        }
        return nullptr;
    }
    bool static_instruction_map_complete() const override { return true; }
    bool static_instruction_operands_complete() const override {
        if (instructions_.empty()) return false;
        for (const auto& instruction : instructions_) {
            if (!instruction.operand_semantics_valid) return false;
        }
        return true;
    }

  private:
    std::vector<fastsim::StaticInstructionInfo> instructions_;
};

class AddressSpaceTraceSource final : public fastsim::TraceSource {
  public:
    bool next(fastsim::TraceRecord&) override { return false; }
    std::string description() const override {
        return "test-address-space";
    }
    std::uint64_t current_address_space_id() const override {
        return address_space_id_;
    }
    void set_address_space_id(std::uint64_t value) {
        address_space_id_ = value;
    }

  private:
    std::uint64_t address_space_id_ = 0;
};

class VirtualPageTraceSource final : public fastsim::TraceSource {
  public:
    VirtualPageTraceSource(
        std::vector<fastsim::TraceRecord> records,
        fastsim::VirtualPageMapping mapping)
        : records_(std::move(records)), mapping_(std::move(mapping)) {}

    bool next(fastsim::TraceRecord& record) override {
        if (cursor_ == records_.size()) return false;
        record = records_[cursor_++];
        return true;
    }
    std::string description() const override {
        return "test-virtual-page-map";
    }
    const fastsim::VirtualPageMapping* virtual_page_mapping(
        std::uint32_t token) const override {
        return token == mapping_.token ? &mapping_ : nullptr;
    }

  private:
    std::vector<fastsim::TraceRecord> records_;
    fastsim::VirtualPageMapping mapping_;
    std::size_t cursor_ = 0;
};

class InstructionPageTraceSource final : public fastsim::TraceSource {
  public:
    InstructionPageTraceSource(std::uint64_t address_space_id,
                               std::uint64_t virtual_page,
                               std::uint64_t physical_page)
        : mapping_{0, address_space_id, virtual_page, physical_page} {}

    bool next(fastsim::TraceRecord&) override { return false; }
    std::string description() const override {
        return "test-instruction-page-map";
    }
    const fastsim::InstructionPageMapping* instruction_page_mapping(
        std::uint64_t virtual_address) const override {
        return virtual_address >> 12 == mapping_.virtual_page
            ? &mapping_
            : nullptr;
    }

  private:
    fastsim::InstructionPageMapping mapping_;
};

void check(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}

std::string test_tmp_path(const std::string& name) {
    const auto directory =
        std::filesystem::path(FASTSIM_PROJECT_ROOT) /
        "tmp" / "fastsim-tests" /
        std::to_string(static_cast<unsigned long long>(::getpid()));
    std::filesystem::create_directories(directory);
    return (directory / name).string();
}

void test_config() {
    check(fastsim::DramConfig{}.separate_write_queue,
          "per-channel DRAM write queue must be enabled by default");
    check(
        fastsim::parse_measurement_scope("user") ==
                fastsim::MeasurementScope::kUser &&
            fastsim::parse_measurement_scope("user_plus_kernel") ==
                fastsim::MeasurementScope::kUserPlusKernel &&
            std::string(fastsim::measurement_scope_name(
                fastsim::MeasurementScope::kUserPlusKernel)) ==
                "user-plus-kernel",
        "measurement scopes must parse and serialize canonically");

    const auto source = fastsim::KeyValueConfig::parse(
        "sim.cores = 8\n"
        "cache.l2.size = 2MiB\n"
        "uncore.coherence = false\n");
    check(source.get_u32("sim.cores", 0) == 8, "config integer");
    check(source.get_u64("cache.l2.size", 0) == (2ull << 20),
          "config size suffix");
    check(!source.get_bool("uncore.coherence", true), "config bool");

    const auto native_default = fastsim::load_simulator_config(
        (std::filesystem::path(FASTSIM_PROJECT_ROOT) /
         "configs/gem5-fs-native-kernel.cfg").string());
    check(native_default.native_kernel_trace &&
              native_default.measurement_scope ==
                  fastsim::MeasurementScope::kUserPlusKernel &&
              native_default.fetch_supply_model &&
              native_default.l1i_enabled &&
              native_default.fetch_supply_physical_request_ledger &&
              native_default.fetch_supply_lower_hierarchy &&
              native_default.instruction_address_mode == "modeled" &&
              native_default.instruction_mapping_seed == 1 &&
              native_default.response_monotone_iq_calendar &&
              !native_default.response_causal_block_transfer &&
              native_default.response_materialized_uop_fast_kernel &&
              !native_default.response_paired_frontier &&
              !native_default.dtlb.hierarchy_walk &&
              native_default.dtlb.page_walk_levels == 4 &&
              native_default.dtlb.page_walk_address_mode ==
                  "physical_sidecar" &&
              native_default.dtlb.page_walk_restart_latency == 2 &&
              native_default.functional_warmup_interval_max_cycles == 0 &&
              !native_default.ruby_sequencer_load_admission &&
              !native_default.response_pending_fill &&
              !native_default.response_pending_fill_load_admission &&
              !native_default.response_pending_fill_store_commit &&
              !native_default.fu_gap_aware_schedule &&
              !native_default.response_event_only_approximation,
          "maintained native-FS profile must enable the promoted modeled "
          "I-fetch hierarchy, monotone-IQ path, and exact materialized-UOP "
          "kernel without promoting either experimental frontier path");

    const auto load_admission_overlay_path =
        test_tmp_path("config-load-admission-overlay.cfg");
    {
        std::ofstream output(load_admission_overlay_path);
        output << "config.include = "
               << (std::filesystem::path(FASTSIM_PROJECT_ROOT) /
                   "configs/gem5-fs-native-kernel.cfg").string()
               << "\n"
               << "ruby.sequencer_load_admission = true\n";
    }
    const auto load_admission_overlay =
        fastsim::load_simulator_config(load_admission_overlay_path);
    check(load_admission_overlay.ruby_sequencer_load_admission &&
              load_admission_overlay.interval_max_cycles == 1024,
          "source load admission must parse without changing fixed Q=1024");

    const auto fu_gap_overlay_path =
        test_tmp_path("config-fu-gap-overlay.cfg");
    {
        std::ofstream output(fu_gap_overlay_path);
        output << "config.include = "
               << (std::filesystem::path(FASTSIM_PROJECT_ROOT) /
                   "configs/gem5-fs-native-kernel.cfg").string()
               << "\n"
               << "core.fu_gap_aware_schedule = true\n";
    }
    const auto fu_gap_overlay =
        fastsim::load_simulator_config(fu_gap_overlay_path);
    check(fu_gap_overlay.fu_gap_aware_schedule &&
              fu_gap_overlay.interval_max_cycles == 1024,
          "gap-aware FU scheduling must parse as an explicit fixed-Q "
          "experiment");

    const auto event_only_p0 = fastsim::load_simulator_config(
        (std::filesystem::path(FASTSIM_PROJECT_ROOT) /
         "configs/gem5-v28_8-fs-event-feedback-p0.cfg").string());
    check(event_only_p0.response_materialized_uop_fast_kernel &&
              event_only_p0.response_event_only_approximation &&
              event_only_p0.response_event_only_calibration_checkpoints ==
                  256 &&
              event_only_p0.response_event_only_teacher_stride == 2 &&
              event_only_p0.response_event_only_teacher_offset == 1 &&
              event_only_p0.response_event_only_teacher_window_epochs ==
                  16,
          "event-only P0 must be an explicit overlay on the maintained exact "
          "materialized-UOP profile");

    const auto include_base_path = test_tmp_path("config-include-base.cfg");
    const auto include_overlay_path =
        test_tmp_path("config-include-overlay.cfg");
    {
        std::ofstream output(include_base_path);
        output << "sim.cores = 4\n"
               << "cache.l2.replacement = lru\n";
    }
    {
        std::ofstream output(include_overlay_path);
        output << "config.include = config-include-base.cfg\n"
               << "sim.cores = 8\n";
    }
    const auto included = fastsim::KeyValueConfig::load(
        include_overlay_path);
    check(included.get_u32("sim.cores", 0) == 8,
          "config overlay must override its included base");
    check(included.get_string("cache.l2.replacement", "") == "lru",
          "config overlay must inherit unspecified base keys");

    const auto config_path = test_tmp_path(
        "fastsim_test_dram_topology.cfg");
    {
        std::ofstream output(config_path);
        output
            << "measurement.scope = user-plus-kernel\n"
            << "sim.cores = 2\n"
            << "sim.reference_frequency_hz = 3000000000\n"
            << "core.frequency_hz = 2400000000\n"
            << "core.frequencies_hz = 1800000000,2200000000\n"
            << "dram.channels = 8\n"
            << "dram.ranks_per_channel = 2\n"
            << "dram.bank_groups_per_rank = 4\n"
            << "dram.t_ras = 96\n"
            << "dram.t_rtp = 23\n"
            << "dram.t_rrd = 11\n"
            << "dram.t_rrd_l = 15\n"
            << "dram.t_xaw = 64\n"
            << "dram.activation_limit = 4\n"
            << "dram.t_ccd_l = 16\n"
            << "dram.t_cs = 5\n"
            << "dram.read_buffer_size = 64\n"
            << "dram.separate_write_queue = true\n"
            << "dram.write_buffer_size = 128\n"
            << "dram.write_high_threshold_percent = 85\n"
            << "dram.write_low_threshold_percent = 50\n"
            << "dram.min_reads_per_switch = 16\n"
            << "dram.min_writes_per_switch = 16\n"
            << "dram.frfcfs_selection_window = 8\n"
            << "dram.frfcfs_topology_scaled_window = true\n"
            << "dram.frfcfs_full_queue_page_policy = true\n"
            << "dram.frfcfs_row_cap_single_precharge = true\n"
            << "dram.frfcfs_causal_selection = true\n"
            << "sim.interval_same_line_order_audit = false\n"
            << "core.minimum_load_latency = 4\n"
            << "uncore.directory_memory_latency = 18\n"
            << "syscall.service_latency = 9\n"
            << "syscall.restart_latency = 3\n"
            << "syscall.event_model = true\n"
            << "syscall.event_table = "
               "202:40:120:180:60:80:30:2:80:8:8:3:8:1:"
               "5:2:0:1:1:3:50:4:900\n"
            << "syscall.event_default_profile = "
               "11:12:14:2:1:4:1:1:0:0:0:3:1:0\n"
            << "trace.require_virtual_page_token = true\n"
            << "page_fault.event_model = true\n"
            << "page_fault.cache_state_model = true\n"
            << "page_fault.syscall_semantic_model = true\n"
            << "page_fault.initial_pte_state_model = false\n"
            << "page_fault.roi_entry_page_state_model = true\n"
            << "page_fault.syscall_semantic_fallback_write_probability_ppm = "
               "875000\n"
            << "page_fault.allocation_syscalls = 9,12\n"
            << "page_fault.allocation_window_records = 4096\n"
            << "page_fault.probability_ppm = 250000\n"
            << "page_fault.background_write_probability_ppm = 500000\n"
            << "page_fault.allocation_probability_ppm = 750000\n"
            << "page_fault.allocation_write_probability_ppm = 625000\n"
            << "page_fault.allocation_probability_table = "
               "9:800000:700000,12:600000:500000\n"
            << "page_fault.event_profile = "
               "20:30:40:5:1:10:2:2:1:1:1:8:2:0\n"
            << "irq.event_model = true\n"
            << "irq.period_cycles = 1000\n"
            << "irq.event_profile = "
               "7:9:12:2:1:4:1:1:0:0:0:3:1:0\n";
    }
    const auto loaded = fastsim::load_simulator_config(config_path);
    check(loaded.reference_frequency_hz == 3'000'000'000ull &&
              loaded.core_frequency_hz == 2'400'000'000ull &&
              loaded.core_frequencies_hz ==
                  std::vector<std::uint64_t>{
                      1'800'000'000ull, 2'200'000'000ull} &&
              loaded.dram.ranks_per_channel == 2 &&
              loaded.measurement_scope ==
                  fastsim::MeasurementScope::kUserPlusKernel &&
              loaded.dram.bank_groups_per_rank == 4 &&
              loaded.dram.t_ras == 96 &&
              loaded.dram.t_rtp == 23 &&
              loaded.dram.t_rrd == 11 &&
              loaded.dram.t_rrd_l == 15 &&
              loaded.dram.t_xaw == 64 &&
              loaded.dram.activation_limit == 4 &&
              loaded.dram.t_ccd_l == 16 &&
              loaded.dram.t_cs == 5 &&
              loaded.dram.read_buffer_size == 64 &&
              loaded.dram.separate_write_queue &&
              loaded.dram.write_buffer_size == 128 &&
              loaded.dram.write_high_threshold_percent == 85 &&
              loaded.dram.write_low_threshold_percent == 50 &&
              loaded.dram.min_reads_per_switch == 16 &&
              loaded.dram.min_writes_per_switch == 16 &&
              loaded.dram.frfcfs_selection_window == 8 &&
              loaded.dram.frfcfs_topology_scaled_window &&
              loaded.dram.frfcfs_full_queue_page_policy &&
              loaded.dram.frfcfs_row_cap_single_precharge &&
              loaded.dram.frfcfs_causal_selection &&
              !loaded.interval_same_line_order_audit &&
              loaded.minimum_load_latency == 4 &&
              loaded.directory_memory_latency == 18 &&
              loaded.syscall_service_latency == 9 &&
              loaded.syscall_restart_latency == 3 &&
              loaded.syscall_kernel_event_model &&
              loaded.syscall_kernel_event_table.size() == 1 &&
              loaded.syscall_kernel_event_table.at(202)
                      .encoding_fields == 22 &&
              loaded.syscall_kernel_event_table.at(202).service_cycles == 40 &&
              loaded.syscall_kernel_event_table.at(202)
                      .retired_instructions == 120 &&
              loaded.syscall_kernel_event_table.at(202).memory_uops == 60 &&
              loaded.syscall_kernel_event_table.at(202).line_requests == 80 &&
              loaded.syscall_kernel_event_table.at(202).l1d_misses == 8 &&
              loaded.syscall_kernel_event_table.at(202)
                      .permission_upgrades == 5 &&
              loaded.syscall_kernel_event_table.at(202).remote_supplies == 2 &&
              loaded.syscall_kernel_event_table.at(202).llc_unique_fills == 1 &&
              loaded.syscall_kernel_event_table.at(202).dram_reads == 1 &&
              loaded.syscall_kernel_event_table.at(202).dram_writes == 3 &&
              loaded.syscall_kernel_event_table.at(202)
                      .blocked_wall_cycles == 900 &&
              loaded.syscall_kernel_event_default_profile_enabled &&
              loaded.syscall_kernel_event_default_profile.encoding_fields ==
                  14 &&
              loaded.syscall_kernel_event_default_profile.service_cycles ==
                  11 &&
              loaded.syscall_kernel_event_default_profile.dtlb_misses == 1 &&
              loaded.page_fault_event_model &&
              loaded.page_fault_cache_state_model &&
              loaded.page_fault_syscall_semantic_model &&
              loaded.page_fault_roi_entry_page_state_model &&
              loaded
                      .page_fault_syscall_semantic_fallback_write_probability_ppm ==
                  875000 &&
              loaded.page_fault_allocation_syscalls.size() == 2 &&
              loaded.page_fault_allocation_syscalls.count(9) == 1 &&
              loaded.page_fault_allocation_window_records == 4096 &&
              loaded.page_fault_probability_ppm == 250000 &&
              loaded.page_fault_background_write_probability_ppm == 500000 &&
              loaded.page_fault_allocation_probability_ppm == 750000 &&
              loaded.page_fault_allocation_write_probability_ppm == 625000 &&
              loaded.page_fault_allocation_probability_table.size() == 2 &&
              loaded.page_fault_allocation_probability_for(9, false) ==
                  800000 &&
              loaded.page_fault_allocation_probability_for(9, true) ==
                  700000 &&
              loaded.page_fault_allocation_probability_for(99, true) ==
                  625000 &&
              loaded.page_fault_event_profile.service_cycles == 20 &&
              loaded.page_fault_event_profile.dtlb_misses == 2 &&
              loaded.irq_event_model &&
              loaded.irq_period_cycles == 1000 &&
              loaded.irq_event_profile.service_cycles == 7 &&
              loaded.irq_event_profile.retired_uops == 12,
          "DRAM topology-scaled FR-FCFS config must round-trip");

    const auto legacy_page_fault_path =
        test_tmp_path("fastsim_test_legacy_page_fault.cfg");
    {
        std::ofstream legacy_page_fault(legacy_page_fault_path);
        legacy_page_fault
            << "page_fault.probability_ppm = 123456\n"
            << "page_fault.allocation_probability_ppm = 654321\n";
    }
    const auto legacy_page_fault =
        fastsim::load_simulator_config(legacy_page_fault_path);
    check(legacy_page_fault.page_fault_probability_ppm == 123456 &&
              legacy_page_fault
                      .page_fault_background_write_probability_ppm ==
                  123456 &&
              legacy_page_fault
                      .page_fault_allocation_write_probability_ppm ==
                  654321,
          "legacy page-fault probabilities must apply to reads and writes");

    auto invalid_page_fault_probability = loaded;
    invalid_page_fault_probability
        .page_fault_allocation_probability_table[9].write_ppm = 1'000'001;
    bool invalid_probability_rejected = false;
    try {
        invalid_page_fault_probability.validate();
    } catch (const std::invalid_argument&) {
        invalid_probability_rejected = true;
    }
    check(invalid_probability_rejected,
          "page-fault syscall probability table must reject values over one");

    auto invalid = loaded;
    invalid.dram.frfcfs_selection_window = 65;
    bool rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "FR-FCFS selection window must not exceed the physical queue");

    invalid = loaded;
    invalid.measurement_scope = fastsim::MeasurementScope::kUser;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "user scope must reject enabled kernel timing/event models");

    fastsim::SimulatorConfig valid_user_scope;
    valid_user_scope.measurement_scope =
        fastsim::MeasurementScope::kUser;
    valid_user_scope.validate();

    fastsim::SimulatorConfig invalid_combined_scope;
    invalid_combined_scope.measurement_scope =
        fastsim::MeasurementScope::kUserPlusKernel;
    invalid_combined_scope.syscall_restart_latency = 0;
    rejected = false;
    try {
        invalid_combined_scope.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "user-plus-kernel scope must require a kernel service model");

    invalid = loaded;
    invalid.dram.bank_groups_per_rank = 8;
    invalid.dram.banks_per_channel = 4;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "DRAM bank groups must divide the banks in each rank");

    invalid = loaded;
    invalid.dram.write_low_threshold_percent = 85;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "DRAM write low watermark must be below the high watermark");

    invalid = loaded;
    invalid.response_activity_certificate = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "response activity certificate must require the sparse "
          "scoreboard state it certifies");

    invalid = loaded;
    invalid.response_monotone_iq_calendar = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "monotone IQ calendar must require response queue feedback");

    invalid = loaded;
    invalid.response_block_summary = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "response block summary must require the sparse scoreboard");

    invalid = loaded;
    invalid.response_frontier_audit_stride_uops = 4;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "response frontier auditing must require the exact attributed "
          "time-epoch sparse path");

    invalid = loaded;
    invalid.response_branch_recovery_audit = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "branch recovery auditing must require frontier audit sampling");

    invalid = loaded;
    invalid.response_branch_recovery = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "response-causal branch recovery must require the supported "
          "single-pass sparse timing path");

    invalid = loaded;
    invalid.response_paired_frontier = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "paired response frontier must require the exact time-epoch "
          "sparse scoreboard path");

    invalid = loaded;
    invalid.response_causal_block_transfer = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "causal block transfer must require its sparse ROB, block-summary, "
          "memory-descriptor, and monotone-IQ state contracts");

    invalid = loaded;
    invalid.response_materialized_uop_fast_kernel = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "materialized-UOP fast kernel must require its exact maintained "
          "sparse response feature contract");

    invalid = loaded;
    invalid.response_event_only_approximation = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "event-only response approximation must require the maintained "
          "materialized time-epoch profile");

    invalid = event_only_p0;
    invalid.response_event_only_calibration_checkpoints = 0;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "event-only response approximation must reject an empty exact "
          "calibration window");

    invalid = event_only_p0;
    invalid.response_event_only_teacher_offset =
        invalid.response_event_only_teacher_stride;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "event-only response approximation must reject a teacher offset "
          "outside its stride");

    invalid = loaded;
    invalid.interval_scheduler = "time_epoch";
    invalid.interval_rob_head_suffix_replay = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "ROB-head suffix replay must require the sparse scoreboard");

    invalid = loaded;
    invalid.functional_warmup_interval_max_cycles = 1024;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "a canonical functional-warmup quantum must require the "
          "time-epoch scheduler");

    invalid = loaded;
    invalid.committed_pipeline_audit = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "committed pipeline audit must require an interval core");

    invalid = loaded;
    invalid.fu_gap_aware_schedule = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "gap-aware FU scheduling must require an interval core");

    invalid = loaded;
    invalid.committed_static_dependency_feedback = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "committed static dependency feedback must require an interval "
          "core");

    invalid = loaded;
    invalid.committed_static_memory_ordering = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "committed static memory ordering must require an interval core");

    invalid = loaded;
    invalid.store_set_same_pc_feedback = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "same-PC StoreSet feedback must require an interval core");

    invalid = loaded;
    invalid.rename_free_list = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "rename free list must require an interval core");

    auto invalid_rename_feedback = loaded;
    invalid_rename_feedback.core_model = "interval_weave";
    invalid_rename_feedback.response_queue_feedback = true;
    invalid_rename_feedback.response_sparse_scoreboard = true;
    invalid_rename_feedback.rename_free_list = true;
    invalid_rename_feedback.response_rename_feedback = true;
    rejected = false;
    try {
        invalid_rename_feedback.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "lower-bound and response-aware free lists must be mutually "
          "exclusive");

    auto valid_suffix = loaded;
    valid_suffix.core_model = "interval_weave";
    valid_suffix.interval_scheduler = "time_epoch";
    valid_suffix.response_queue_feedback = true;
    valid_suffix.response_sparse_scoreboard = true;
    valid_suffix.interval_rob_head_suffix_replay = true;
    valid_suffix.validate();

    invalid = loaded;
    invalid.store_post_commit_request = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "post-commit store requests must require the time-epoch sparse "
          "scoreboard");

    fastsim::SimulatorConfig native_kernel;
    native_kernel.measurement_scope =
        fastsim::MeasurementScope::kUserPlusKernel;
    native_kernel.native_kernel_trace = true;
    native_kernel.syscall_restart_latency = 0;
    native_kernel.validate();

    auto native_with_synthetic = native_kernel;
    native_with_synthetic.page_fault_event_model = true;
    rejected = false;
    try {
        native_with_synthetic.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "native kernel trace must reject synthetic kernel models");

    auto native_user_scope = native_kernel;
    native_user_scope.measurement_scope =
        fastsim::MeasurementScope::kUser;
    rejected = false;
    try {
        native_user_scope.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "native kernel trace must require user-plus-kernel scope");
}

void test_cache_transaction() {
    fastsim::CacheConfig config;
    config.size_bytes = 4 * 64;
    config.associativity = 2;
    config.line_size = 64;
    config.replacement = fastsim::ReplacementPolicy::kLru;
    fastsim::SetAssociativeCache cache(config);
    fastsim::CacheCounters counters;
    auto transaction = cache.begin_transaction();
    check(!cache.access(0, false, counters, &transaction).hit,
          "first cache access must miss");
    check(cache.access(0, false, counters, &transaction).hit,
          "second cache access must hit");
    cache.restore(transaction);
    fastsim::CacheCounters after_restore;
    check(!cache.access(0, false, after_restore).hit,
          "transaction restore must roll back fill");

    fastsim::CacheConfig tree_config;
    tree_config.size_bytes = 4 * 64;
    tree_config.associativity = 4;
    tree_config.line_size = 64;
    tree_config.replacement = fastsim::ReplacementPolicy::kTreePlru;
    fastsim::SetAssociativeCache tree_cache(tree_config);
    fastsim::CacheCounters tree_counters;
    for (std::uint64_t line = 0; line < 4; ++line) {
        check(!tree_cache.access(line, false, tree_counters).hit,
              "TreePLRU cold fill must miss");
    }
    check(tree_cache.access(0, false, tree_counters).hit,
          "TreePLRU touch must hit the resident line");
    const auto tree_eviction =
        tree_cache.access(4, false, tree_counters);
    check(tree_eviction.evicted && tree_eviction.evicted_line == 2 &&
              !tree_cache.contains(2) && tree_cache.contains(0),
          "TreePLRU must follow gem5 parent bits away from the MRU leaf");

    fastsim::SetAssociativeCache vipt_cache(config);
    fastsim::CacheCounters vipt_counters;
    check(!vipt_cache.access_indexed(
              0, 0x100, false, vipt_counters).hit &&
              vipt_cache.access_indexed(
                  0, 0x100, false, vipt_counters).hit &&
              !vipt_cache.access_indexed(
                  1, 0x100, false, vipt_counters).hit,
          "VIPT access must take its set from the virtual line and tag from "
          "the physical line");
}

void test_private_dirty_victim_merge() {
    fastsim::CacheConfig l1;
    l1.size_bytes = 64;
    l1.associativity = 1;
    l1.line_size = 64;
    fastsim::CacheConfig l2 = l1;
    fastsim::PrivateHierarchy hierarchy(l1, l2);
    fastsim::CoreCounters counters;
    (void)hierarchy.access(0, true, counters);
    const auto eviction = hierarchy.access(1, false, counters);
    check(eviction.l2_evicted && eviction.l2_evicted_line == 0,
          "matching L1/L2 victim must leave the private hierarchy");
    check(eviction.l2_evicted_dirty,
          "dirty L1 data must be carried by the matching L2 eviction");
    check(!hierarchy.contains(0) && hierarchy.contains(1),
          "matching dirty victim must not be spuriously refilled");

    fastsim::PrivateHierarchy instruction_hierarchy(l1, l2);
    fastsim::CoreCounters instruction_counters;
    const auto instruction_cold = instruction_hierarchy.access_l2(
        7, instruction_counters.instruction_l2);
    const auto instruction_hot = instruction_hierarchy.access_l2(
        7, instruction_counters.instruction_l2);
    check(instruction_cold.level == fastsim::HitLevel::kLlc &&
              instruction_hot.level == fastsim::HitLevel::kL2 &&
              instruction_counters.l1d.accesses == 0 &&
              instruction_counters.l2.accesses == 0 &&
              instruction_counters.instruction_l2.accesses == 2 &&
              instruction_counters.instruction_l2.misses == 1,
          "instruction requests must bypass L1D and share the private L2");
}

void test_predictor() {
    fastsim::BranchConfig config;
    config.type = "gshare";
    config.global_entries = 16;
    config.local_entries = 16;
    config.local_history_entries = 16;
    config.choice_entries = 16;
    config.btb_entries = 16;
    config.btb_associativity = 2;
    config.indirect_sets = 8;
    config.indirect_ways = 1;
    fastsim::BranchPredictor predictor(config);
    fastsim::BranchCounters counters;
    fastsim::TraceRecord branch;
    branch.pc = 0x1000;
    branch.target = 0x1100;
    branch.next_pc = 0x1100;
    branch.flags =
        fastsim::kRetires | fastsim::kBranch |
        fastsim::kConditional | fastsim::kTaken |
        fastsim::kBranchOutcomeValid;
    for (int i = 0; i < 32; ++i) predictor.process(branch, counters);
    check(counters.branches == 32, "branch count");
    check(counters.misses < counters.branches, "predictor must learn");
    check(counters.btb_hits > 0, "BTB must learn target");

    fastsim::StaticInstructionInfo outer;
    outer.pc = 0x1800;
    outer.size = 2;
    outer.fallthrough_pc = 0x2000;
    outer.direct_target = 0x3000;
    outer.flags = fastsim::kStaticBranch |
                  fastsim::kStaticConditional |
                  fastsim::kStaticDirectTargetValid;
    fastsim::StaticInstructionInfo nested = outer;
    nested.pc = 0x2000;
    nested.fallthrough_pc = 0x2002;
    nested.direct_target = 0x4000;
    fastsim::StaticInstructionInfo linear;
    linear.pc = 0x2002;
    linear.size = 5;
    linear.fallthrough_pc = 0x2007;
    fastsim::StaticInstructionInfo indirect;
    indirect.pc = 0x2007;
    indirect.size = 2;
    indirect.fallthrough_pc = 0x2009;
    indirect.flags = fastsim::kStaticBranch | fastsim::kStaticIndirect;
    StaticMapTraceSource static_map({outer, nested, linear, indirect});

    fastsim::BranchPredictor cold_predictor(config);
    fastsim::BranchCounters cold_counters;
    auto resolving = branch;
    resolving.pc = outer.pc;
    resolving.next_pc = outer.direct_target;
    const auto cold = cold_predictor.process(
        resolving, cold_counters, &static_map, 8);
    check(cold.miss &&
              cold.speculative_path ==
                  std::vector<std::uint64_t>{0x2000, 0x2002, 0x2007},
          "pre-repair predictor snapshot must cross nested conditionals "
          "without leaking outcomes and stop at an indirect target");

    fastsim::BranchPredictor learned_predictor(config);
    fastsim::BranchCounters learned_counters;
    auto unconditional = resolving;
    unconditional.flags = fastsim::kRetires | fastsim::kBranch |
                          fastsim::kTaken |
                          fastsim::kBranchOutcomeValid;
    (void)learned_predictor.process(
        unconditional, learned_counters, &static_map, 8);
    const auto correct = learned_predictor.process(
        unconditional, learned_counters, &static_map, 8);
    check(!correct.miss && correct.speculative_path.empty(),
          "correctly predicted branches must not build a discarded "
          "wrong-path audit");
}

void test_interval_core_dependency_and_width() {
    fastsim::SimulatorConfig config;
    check(config.fetch_supply_static_instruction_span,
          "portable static instruction-span Fetch supply must be enabled by "
          "default");
    config.core_model = "interval_bound";
    config.dispatch_width = 8;
    config.issue_width = 8;
    config.commit_width = 8;
    config.validate();

    fastsim::IntervalCoreModel independent(config);
    fastsim::IntervalCoreModel chain(config);
    fastsim::TraceRecord free_uop;
    free_uop.op_class = 1;
    fastsim::TraceRecord dependent = free_uop;
    dependent.producer_dists[0] = 1;
    for (int index = 0; index < 256; ++index) {
        (void)independent.schedule(free_uop, false);
        (void)chain.schedule(dependent, false);
    }
    check(independent.last_retire_cycle() < 60,
          "8-wide interval core must overlap independent UOPs");
    check(chain.last_retire_cycle() >
              independent.last_retire_cycle() * 3,
          "producer-distance chain must constrain interval progress");

    auto fu_audit_config = config;
    fu_audit_config.fetch_width = 8;
    fu_audit_config.decode_width = 8;
    fu_audit_config.rename_width = 8;
    fu_audit_config.writeback_width = 8;
    fu_audit_config.committed_pipeline_audit = true;
    fu_audit_config.validate();
    auto fu_candidate_config = fu_audit_config;
    fu_candidate_config.fu_gap_aware_schedule = true;
    fu_candidate_config.validate();

    const auto alu = [](std::uint8_t op_class,
                        std::uint32_t producer_distance = 0) {
        fastsim::TraceRecord record;
        record.flags = fastsim::kRetires;
        record.op_class = op_class;
        if (producer_distance != 0) {
            record.n_src = 1;
            record.producer_dists[0] = producer_distance;
        }
        return record;
    };
    std::vector<fastsim::TraceRecord> future_reservations{alu(11)};
    for (std::uint32_t distance = 1; distance <= 6; ++distance) {
        future_reservations.push_back(alu(1, distance));
    }
    future_reservations.push_back(alu(1));
    future_reservations.push_back(alu(11, 1));

    fastsim::IntervalCoreModel fu_legacy(fu_audit_config);
    fastsim::IntervalCoreModel fu_candidate(fu_candidate_config);
    std::vector<fastsim::IntervalTiming> legacy_timing;
    std::vector<fastsim::IntervalTiming> candidate_timing;
    for (const auto& record : future_reservations) {
        legacy_timing.push_back(fu_legacy.schedule(record, false));
        candidate_timing.push_back(fu_candidate.schedule(record, false));
    }
    const auto& fu_audit = fu_legacy.committed_pipeline_audit();
    const auto& fu_candidate_audit =
        fu_candidate.committed_pipeline_audit();
    check(legacy_timing[7].issue_cycle == 30 &&
              candidate_timing[7].issue_cycle == 5 &&
              legacy_timing.back().retire_cycle == 55 &&
              candidate_timing.back().retire_cycle == 30,
          "gap-aware FU scheduling must fill a legal hole before an older "
          "future reservation");
    check(fu_audit.fu_calendar_queries == future_reservations.size() &&
              fu_audit.fu_future_reservation_uops != 0 &&
              fu_audit.fu_future_reservation_cycles >= 25 &&
              fu_audit.fu_future_reservation_max_cycles >= 25 &&
              fu_audit.fu_gap_aware_scheduled_uops == 0 &&
              fu_candidate_audit.fu_gap_aware_scheduled_uops ==
                  future_reservations.size(),
          "FU audit counters must distinguish timing-neutral opportunities "
          "from the opt-in candidate schedule");

    auto no_future_reservations = future_reservations;
    for (std::size_t index = 1; index <= 6; ++index) {
        no_future_reservations[index].n_src = 0;
        no_future_reservations[index].producer_dists[0] = 0;
    }
    fastsim::IntervalCoreModel fu_control_legacy(fu_audit_config);
    fastsim::IntervalCoreModel fu_control_candidate(fu_candidate_config);
    for (const auto& record : no_future_reservations) {
        (void)fu_control_legacy.schedule(record, false);
        (void)fu_control_candidate.schedule(record, false);
    }
    check(fu_control_legacy.last_retire_cycle() ==
                  fu_control_candidate.last_retire_cycle() &&
              fu_control_legacy.committed_pipeline_audit()
                      .fu_future_reservation_uops == 0,
          "gap-aware FU scheduling must preserve a control graph without "
          "dependency-delayed future reservations");

    fastsim::IntervalCoreModel branch_stalled(config);
    for (int index = 0; index < 256; ++index) {
        (void)branch_stalled.schedule(free_uop, index == 31);
    }
    check(branch_stalled.last_retire_cycle() >=
              independent.last_retire_cycle() +
                  config.branch.mispredict_penalty,
          "branch feedback must stall the interval front end");

    auto narrow_config = config;
    narrow_config.fetch_width = 1;
    narrow_config.decode_width = 1;
    narrow_config.rename_width = 1;
    narrow_config.writeback_width = 1;
    narrow_config.validate();
    fastsim::IntervalCoreModel narrow(narrow_config);
    for (int index = 0; index < 256; ++index) {
        (void)narrow.schedule(free_uop, false);
    }
    check(narrow.last_retire_cycle() > independent.last_retire_cycle() * 3,
          "gem5-style front-end/writeback widths must constrain progress");

    auto iq_config = config;
    iq_config.iq_entries = 1;
    iq_config.validate();
    fastsim::TraceRecord load;
    load.address = 0x8000;
    load.size = 8;
    load.flags = fastsim::kRetires | fastsim::kLoad |
                 fastsim::kPhysicalAddress;
    fastsim::IntervalCoreModel alu_iq(iq_config);
    const auto first_alu = alu_iq.schedule(free_uop, false);
    const auto second_alu = alu_iq.schedule(free_uop, false);
    fastsim::IntervalCoreModel memory_iq(iq_config);
    const auto first_load = memory_iq.schedule(load, false);
    const auto second_load = memory_iq.schedule(load, false);
    check(first_alu.issue_cycle < first_alu.completion_cycle &&
              second_alu.dispatch_cycle < second_load.dispatch_cycle &&
              first_load.completion_cycle <= second_load.dispatch_cycle,
          "memory UOP must retain its IQ entry through core completion");

    auto split_latency_config = config;
    split_latency_config.minimum_load_latency = 3;
    split_latency_config.l1d.hit_latency = 11;
    split_latency_config.validate();
    fastsim::IntervalCoreModel split_latency(split_latency_config);
    const auto split_load = split_latency.schedule(load, false);
    check(split_load.completion_cycle - split_load.execute_cycle == 3,
          "core minimum load latency must be independent of the private "
          "cache response latency");

    auto fetch_buffer_config = config;
    fetch_buffer_config.fetch_buffer_bytes = 64;
    fetch_buffer_config.fetch_buffer_refill_latency = 1;
    fetch_buffer_config.validate();
    fastsim::IntervalCoreModel fetch_buffer(fetch_buffer_config);
    auto first_block = free_uop;
    first_block.pc = 0x1000;
    auto second_block = free_uop;
    second_block.pc = 0x1040;
    const auto before_switch = fetch_buffer.schedule(first_block, false);
    const auto after_switch = fetch_buffer.schedule(second_block, false);
    check(!before_switch.fetch_buffer_transition &&
              after_switch.fetch_buffer_transition &&
              after_switch.fetch_buffer_refill_delay_cycles == 1 &&
              after_switch.fetch_block_response_wait_cycles == 1 &&
              after_switch.fetch_block_response_hidden_cycles == 0 &&
              after_switch.fetch_block_response_exposed_cycles == 1 &&
              after_switch.fetch_block_response_wait_cycles ==
                  after_switch.fetch_block_response_hidden_cycles +
                      after_switch.fetch_block_response_exposed_cycles &&
              after_switch.fetch_response_ledger_committed_requests == 0 &&
              after_switch.fetch_response_ledger_shadow_requests == 0 &&
              after_switch.fetch_response_ledger_responses == 0 &&
              after_switch.fetch_response_ledger_server_wait_cycles == 0,
          "fetch-buffer transitions must expose their raw refill delay");
    check(after_switch.fetch_cycle >= before_switch.fetch_cycle + 2,
          "a gem5 fetch-buffer block switch must expose its refill bubble");

    auto l1i_config = fetch_buffer_config;
    l1i_config.l1i_enabled = true;
    l1i_config.l1i.size_bytes = 64;
    l1i_config.l1i.associativity = 1;
    l1i_config.l1i.line_size = 64;
    l1i_config.l1i.hit_latency = 1;
    l1i_config.l1i_miss_penalty = 7;
    l1i_config.validate();
    fastsim::IntervalCoreModel l1i(l1i_config);
    const auto l1i_cold = l1i.schedule(first_block, false);
    const auto l1i_other = l1i.schedule(second_block, false);
    const auto l1i_revisit = l1i.schedule(first_block, false);
    check(l1i_cold.l1i_access && l1i_cold.l1i_miss &&
              l1i_other.l1i_access && l1i_other.l1i_miss &&
              l1i_revisit.l1i_access && l1i_revisit.l1i_miss &&
              l1i_revisit.l1i_miss_stall_cycles == 7,
          "committed-PC L1I must apply target geometry and replacement");
    check(l1i_revisit.fetch_cycle >= l1i_other.fetch_cycle + 9,
          "an L1I miss must add only its configured extra refill stall");

    auto physical_fetch_config = l1i_config;
    physical_fetch_config.fetch_supply_physical_request_ledger = true;
    physical_fetch_config.instruction_address_mode = "trace";
    physical_fetch_config.validate();
    InstructionPageTraceSource instruction_pages(7, 1, 9);
    fastsim::IntervalCoreModel physical_fetch(physical_fetch_config);
    const auto physical_cold = physical_fetch.schedule(
        first_block, false, false, 0, false, &instruction_pages);
    check(physical_cold.instruction_page_map_lookups == 1 &&
              physical_cold.instruction_page_map_hits == 1 &&
              physical_cold.instruction_page_map_misses == 0 &&
              physical_cold.physical_instruction_fetch_request_count == 1 &&
              physical_cold.physical_instruction_fetch_requests[0]
                      .virtual_block == 0x1000 / 64 &&
              physical_cold.physical_instruction_fetch_requests[0]
                      .physical_line == 0x9000 / 64 &&
              physical_cold.physical_instruction_fetch_requests[0]
                      .baseline_response_cycle >=
                  physical_cold.physical_instruction_fetch_requests[0]
                      .request_cycle,
          "an L1I miss with ifmap coverage must create one physical request "
          "descriptor without changing timing");
    const auto physical_hit = physical_fetch.schedule(
        first_block, false, false, 0, false, &instruction_pages);
    check(physical_hit.instruction_page_map_lookups == 0 &&
              physical_hit.physical_instruction_fetch_request_count == 0,
          "an L1I hit must not create a lower-hierarchy request");
    auto strict_physical_fetch_config = physical_fetch_config;
    strict_physical_fetch_config.require_instruction_page_map = true;
    strict_physical_fetch_config.validate();
    fastsim::IntervalCoreModel strict_physical_fetch(
        strict_physical_fetch_config);
    bool missing_instruction_page_rejected = false;
    try {
        strict_physical_fetch.schedule(first_block, false);
    } catch (const std::runtime_error&) {
        missing_instruction_page_rejected = true;
    }
    check(missing_instruction_page_rejected,
          "strict physical I-fetch ledger must fail closed without ifmap");
    InstructionPageTraceSource oversized_instruction_page(
        7, 1, std::uint64_t{1} << 36);
    fastsim::IntervalCoreModel bounded_physical_fetch(
        physical_fetch_config);
    bool oversized_instruction_page_rejected = false;
    try {
        bounded_physical_fetch.schedule(
            first_block, false, false, 0, false,
            &oversized_instruction_page);
    } catch (const std::runtime_error&) {
        oversized_instruction_page_rejected = true;
    }
    check(oversized_instruction_page_rejected,
          "trace I-fetch pages must fit the configured physical width");

    auto modeled_fetch_config = l1i_config;
    modeled_fetch_config.fetch_supply_physical_request_ledger = true;
    modeled_fetch_config.instruction_address_mode = "modeled";
    modeled_fetch_config.instruction_physical_address_bits = 38;
    modeled_fetch_config.instruction_page_bits = 12;
    modeled_fetch_config.instruction_mapping_seed = 17;
    modeled_fetch_config.validate();
    fastsim::IntervalCoreModel modeled_fetch(modeled_fetch_config);
    const auto modeled_cold = modeled_fetch.schedule(
        first_block, false, false, 0, false, nullptr, nullptr, 41);
    const auto modeled_address =
        fastsim::modeled_instruction_physical_address(
            modeled_fetch_config, 41, first_block.pc);
    check(modeled_cold.modeled_instruction_page_lookups == 1 &&
              modeled_cold.instruction_page_map_lookups == 0 &&
              modeled_cold.physical_instruction_fetch_request_count == 1 &&
              modeled_cold.physical_instruction_fetch_requests[0]
                  .modeled_address &&
              modeled_cold.physical_instruction_fetch_requests[0]
                      .physical_line == modeled_address / 64 &&
              (modeled_address & 0xfffull) ==
                  (first_block.pc & 0xfffull) &&
              modeled_address >= (std::uint64_t{1} << 37) &&
              modeled_address < (std::uint64_t{1} << 38),
          "modeled I-fetch mapping must preserve page offsets, avoid ifmap, "
          "and remain in the configured disjoint physical namespace");
    auto alternate_seed_config = modeled_fetch_config;
    alternate_seed_config.instruction_mapping_seed = 18;
    check(fastsim::modeled_instruction_physical_address(
              modeled_fetch_config, 41, first_block.pc) == modeled_address &&
              fastsim::modeled_instruction_physical_address(
                  modeled_fetch_config, 42, first_block.pc) !=
                  modeled_address &&
              fastsim::modeled_instruction_physical_address(
                  alternate_seed_config, 41, first_block.pc) !=
                  modeled_address,
          "modeled I-fetch mapping must be reproducible and sensitive to "
          "ASID and seed");
    auto invalid_vipt_config = modeled_fetch_config;
    invalid_vipt_config.l1i.size_bytes = 64ull << 10;
    invalid_vipt_config.l1i.associativity = 8;
    bool invalid_vipt_rejected = false;
    try {
        invalid_vipt_config.validate();
    } catch (const std::invalid_argument&) {
        invalid_vipt_rejected = true;
    }
    check(invalid_vipt_rejected,
          "physical L1I tags must reject set-index bits above the page "
          "offset");

    auto speculative_l1i_config = l1i_config;
    speculative_l1i_config.l1i_speculative_entry_state = true;
    speculative_l1i_config.validate();
    fastsim::IntervalCoreModel speculative_l1i(speculative_l1i_config);
    auto missed_branch = first_block;
    missed_branch.flags = fastsim::kRetires | fastsim::kBranch |
                          fastsim::kConditional |
                          fastsim::kBranchOutcomeValid;
    const auto missed_branch_timing = speculative_l1i.schedule(
        missed_branch, true, true, second_block.pc, true);
    const auto recovered_target =
        speculative_l1i.schedule(second_block, false);
    check(missed_branch_timing.l1i_speculative_entry_access &&
              missed_branch_timing.l1i_speculative_entry_miss &&
              !missed_branch_timing.l1i_speculative_entry_untracked &&
              recovered_target.l1i_access && recovered_target.l1i_hit,
          "a replayed BTB target may warm L1I state without fabricating a "
          "retired instruction access");
    fastsim::IntervalCoreModel unknown_fallthrough(
        speculative_l1i_config);
    const auto unknown_timing = unknown_fallthrough.schedule(
        missed_branch, true, false, 0, false);
    check(unknown_timing.l1i_speculative_entry_untracked &&
              !unknown_timing.l1i_speculative_entry_access,
          "a missing x86 fallthrough must fail closed instead of assuming "
          "a fixed instruction length");

    auto path_l1i_config = l1i_config;
    path_l1i_config.l1i_speculative_path_state = true;
    path_l1i_config.validate();
    fastsim::IntervalCoreModel path_l1i(path_l1i_config);
    auto third_block = second_block;
    third_block.pc = 0x1080;
    const std::vector<std::uint64_t> supplied_cold_path{
        second_block.pc, third_block.pc};
    fastsim::IntervalCoreModel supplied_path_l1i(path_l1i_config);
    const auto supplied_path_timing = supplied_path_l1i.schedule(
        missed_branch, true, false, 0, false, nullptr,
        &supplied_cold_path);
    check(supplied_path_timing.l1i_speculative_path_records == 2 &&
              !supplied_path_timing.l1i_speculative_entry_untracked,
          "a predictor-supplied static fallthrough path must not require "
          "duplicate committed fallthrough history at the interval boundary");

    auto physical_path_config = modeled_fetch_config;
    physical_path_config.l1i_speculative_path_state = true;
    physical_path_config.validate();
    fastsim::IntervalCoreModel physical_path_l1i(physical_path_config);
    const auto physical_path_timing = physical_path_l1i.schedule(
        missed_branch, true, false, 0, false, nullptr,
        &supplied_cold_path, 41);
    check(physical_path_timing.l1i_speculative_path_accesses == 2 &&
              physical_path_timing.l1i_speculative_path_misses == 2 &&
              physical_path_timing
                      .speculative_physical_instruction_fetch_requests
                      .size() == 2 &&
              physical_path_timing
                  .speculative_physical_instruction_fetch_requests[0]
                  .modeled_address &&
              physical_path_timing
                      .speculative_physical_instruction_fetch_requests[0]
                      .physical_line ==
                  fastsim::modeled_instruction_physical_address(
                      physical_path_config, 41, second_block.pc) / 64 &&
              physical_path_timing
                      .speculative_physical_instruction_fetch_requests[1]
                      .request_cycle >
                  physical_path_timing
                      .speculative_physical_instruction_fetch_requests[0]
                      .baseline_response_cycle &&
              physical_path_timing
                      .l1i_speculative_path_physical_untracked == 0,
          "predictor-visible wrong-path misses must produce ordered physical "
          "I-fetch descriptors without borrowing a committed response");
    (void)path_l1i.schedule(second_block, false);
    (void)path_l1i.schedule(third_block, false);
    const auto path_timing = path_l1i.schedule(
        missed_branch, true, true, second_block.pc, true);
    check(path_timing.l1i_speculative_path_records == 3 &&
              path_timing.l1i_speculative_path_accesses == 3 &&
              path_timing.l1i_speculative_path_unknown_edge,
          "speculative L1I replay must follow only causally observed PC "
          "successors and stop at the first unknown edge");

    fastsim::StaticInstructionInfo cold_jump;
    cold_jump.pc = second_block.pc;
    cold_jump.size = 2;
    cold_jump.fallthrough_pc = second_block.pc + 2;
    cold_jump.direct_target = third_block.pc;
    cold_jump.flags = fastsim::kStaticBranch |
                      fastsim::kStaticDirectTargetValid;
    cold_jump.operand_semantics_valid = true;
    cold_jump.write_register_mask[0] = 1ull << 0;
    fastsim::StaticInstructionInfo cold_target;
    cold_target.pc = third_block.pc;
    cold_target.size = 5;
    cold_target.fallthrough_pc = third_block.pc + 5;
    cold_target.operand_semantics_valid = true;
    cold_target.read_register_mask[0] = 1ull << 0;
    cold_target.write_register_mask[0] = 1ull << 1;
    StaticMapTraceSource static_map({cold_jump, cold_target});
    fastsim::IntervalCoreModel static_path_l1i(path_l1i_config);
    const auto static_path_timing = static_path_l1i.schedule(
        missed_branch, true, true, second_block.pc, true, &static_map);
    check(static_path_timing.l1i_speculative_path_records == 3 &&
              static_path_timing.l1i_speculative_path_accesses == 2 &&
              static_path_timing.l1i_speculative_path_misses == 2 &&
              static_path_timing
                      .l1i_speculative_path_static_instructions == 2 &&
              static_path_timing
                      .l1i_speculative_path_operand_instructions == 2 &&
              static_path_timing
                      .l1i_speculative_path_operand_segments == 1 &&
              static_path_timing.l1i_speculative_path_raw_edges == 1 &&
              static_path_timing
                      .l1i_speculative_path_dependent_instructions == 1 &&
              static_path_timing
                      .l1i_speculative_path_chain_depth_sum == 3 &&
              static_path_timing
                      .l1i_speculative_path_chain_depth_max == 2 &&
              static_path_timing
                      .l1i_speculative_path_operand_rob_prefix_uops_q16 ==
                  2 * 65'536 &&
              static_path_timing
                      .l1i_speculative_path_operand_rob_capped_instructions ==
                  2 &&
              static_path_timing
                      .l1i_speculative_path_operand_rob_capped_write_registers ==
                  2 &&
              static_path_timing
                      .l1i_speculative_path_operand_rob_capped_raw_edges == 1 &&
              static_path_timing
                      .l1i_speculative_path_operand_rob_capped_chain_depth_max ==
                  2 &&
              static_path_timing
                      .l1i_speculative_path_conditional_stops == 0 &&
              static_path_timing
                      .l1i_speculative_path_indirect_stops == 0 &&
              static_path_timing
                      .l1i_speculative_path_static_map_misses == 1 &&
              static_path_timing.l1i_speculative_path_unknown_edge,
          "static instruction facts must extend a cold predicted path across "
          "an exact direct edge and stop before inventing an unknown edge");

    auto operand_rob_config = path_l1i_config;
    operand_rob_config.rob_entries = 2;
    operand_rob_config.validate();
    fastsim::IntervalCoreModel operand_rob_limited(operand_rob_config);
    const auto operand_rob_timing = operand_rob_limited.schedule(
        missed_branch, true, true, second_block.pc, true, &static_map);
    check(operand_rob_timing.l1i_speculative_path_raw_edges == 1 &&
              operand_rob_timing
                      .l1i_speculative_path_operand_rob_prefix_uops_q16 ==
                  65'536 &&
              operand_rob_timing
                      .l1i_speculative_path_operand_rob_capped_instructions ==
                  1 &&
              operand_rob_timing
                      .l1i_speculative_path_operand_rob_capped_write_registers ==
                  1 &&
              operand_rob_timing
                      .l1i_speculative_path_operand_rob_capped_raw_edges == 0 &&
              operand_rob_timing
                      .l1i_speculative_path_operand_rob_capped_chain_depth_max ==
                  1,
          "operand audit must retain only complete macro instructions in "
          "the causally available ROB prefix");

    auto memory_target = cold_target;
    memory_target.flags = fastsim::kStaticMemory;
    StaticMapTraceSource memory_map({memory_target});
    fastsim::IntervalCoreModel unstable_memory_path(path_l1i_config);
    auto dynamic_memory = load;
    dynamic_memory.pc = memory_target.pc;
    dynamic_memory.flags |= fastsim::kVirtualPageToken;
    dynamic_memory.reserved = 1;
    (void)unstable_memory_path.schedule(dynamic_memory, false);
    dynamic_memory.reserved = 2;
    (void)unstable_memory_path.schedule(dynamic_memory, false);
    const auto unstable_path_timing = unstable_memory_path.schedule(
        missed_branch, true, true, memory_target.pc, true, &memory_map);
    check(unstable_path_timing
                  .l1i_speculative_path_memory_instructions >= 1 &&
              unstable_path_timing
                  .l1i_speculative_path_memory_page_known >= 1 &&
              unstable_path_timing
                  .l1i_speculative_path_memory_page_unstable >= 1 &&
              unstable_path_timing
                  .l1i_speculative_path_memory_page_transition_samples >= 1 &&
              unstable_path_timing
                  .l1i_speculative_path_memory_page_transition_score_ppm >=
                  1'000'000 &&
              unstable_path_timing
                  .l1i_speculative_path_profiled_instructions >= 1 &&
              unstable_path_timing
                  .l1i_speculative_path_profile_uops_q16[
                      static_cast<std::size_t>(
                          fastsim::IntervalFuPool::kMemory)] >= 65'536 &&
              unstable_path_timing
                  .l1i_speculative_path_profile_rob_capped_uops_q16 >=
                  65'536 &&
              unstable_path_timing
                      .l1i_speculative_path_operand_rob_capped_memory_instructions >=
                  1,
          "speculative memory diagnostics must causally distinguish a known "
          "PC from one already observed on multiple virtual pages");

    auto speculative_dtlb_config = modeled_fetch_config;
    speculative_dtlb_config.dtlb.enabled = true;
    speculative_dtlb_config.dtlb.speculative_path_state = true;
    speculative_dtlb_config.dtlb.entries = 2;
    speculative_dtlb_config.dtlb.page_walk_latency = 4;
    speculative_dtlb_config.validate();
    fastsim::IntervalCoreModel speculative_dtlb(speculative_dtlb_config);
    auto mapped_memory = load;
    mapped_memory.pc = memory_target.pc;
    mapped_memory.flags |= fastsim::kVirtualPageToken;
    mapped_memory.reserved = 7;
    (void)speculative_dtlb.schedule(mapped_memory, false);
    const std::vector<std::uint64_t> supplied_dtlb_path{memory_target.pc};
    const auto speculative_dtlb_timing = speculative_dtlb.schedule(
        missed_branch, true, true, memory_target.pc, true, &memory_map,
        &supplied_dtlb_path);
    check(speculative_dtlb_timing.l1i_speculative_path_records == 1,
          "DTLB-only predicted-path replay must consume the supplied path");
    check(speculative_dtlb_timing.l1i_speculative_path_accesses == 0,
          "DTLB-only predicted-path replay must not mutate L1I state");
    check(speculative_dtlb_timing.speculative_dtlb_accesses == 1 &&
              speculative_dtlb_timing.speculative_dtlb_hits +
                      speculative_dtlb_timing.speculative_dtlb_misses ==
                  1 &&
              speculative_dtlb_timing.speculative_dtlb_untracked == 0,
          "predicted-path DTLB state must reuse a causally observed PC/page "
          "mapping with physical committed I-fetch replay enabled");

    fastsim::StaticInstructionInfo profiled_target;
    profiled_target.pc = 0x3000;
    profiled_target.size = 4;
    profiled_target.fallthrough_pc = 0x3004;
    profiled_target.flags = fastsim::kStaticMemory;
    StaticMapTraceSource profiled_map({profiled_target});
    fastsim::IntervalCoreModel multi_uop_profile(path_l1i_config);
    auto integer_micro_op = free_uop;
    integer_micro_op.pc = profiled_target.pc;
    integer_micro_op.flags = fastsim::kRetires | fastsim::kMicroOp;
    auto memory_micro_op = load;
    memory_micro_op.pc = profiled_target.pc;
    memory_micro_op.flags = fastsim::kRetires | fastsim::kLoad |
                            fastsim::kMicroOp | fastsim::kLastMicroOp;
    (void)multi_uop_profile.schedule(integer_micro_op, false);
    (void)multi_uop_profile.schedule(memory_micro_op, false);
    const auto multi_uop_path = multi_uop_profile.schedule(
        missed_branch, true, true, profiled_target.pc, true,
        &profiled_map);
    check(multi_uop_path.l1i_speculative_path_profiled_instructions >= 1 &&
              multi_uop_path.l1i_speculative_path_profile_uops_q16[
                  static_cast<std::size_t>(
                      fastsim::IntervalFuPool::kInteger)] >= 65'536 &&
              multi_uop_path.l1i_speculative_path_profile_uops_q16[
                  static_cast<std::size_t>(
                      fastsim::IntervalFuPool::kMemory)] >= 65'536 &&
              multi_uop_path
                  .l1i_speculative_path_profile_rob_capped_uops_q16 >=
                  2 * 65'536,
          "causal PC profiles must retain a complete multi-UOP macro's FU "
          "mix and apply the ROB cap only after UOP expansion");

    fastsim::IntervalCoreModel taken_frontend(fetch_buffer_config);
    auto taken = free_uop;
    taken.pc = 0x2000;
    taken.flags = fastsim::kRetires | fastsim::kBranch |
                  fastsim::kTaken | fastsim::kBranchOutcomeValid;
    auto same_block = free_uop;
    same_block.pc = 0x2004;
    const auto branch_timing = taken_frontend.schedule(taken, false);
    const auto target_timing = taken_frontend.schedule(same_block, false);
    check(target_timing.fetch_cycle >= branch_timing.fetch_cycle + 1,
          "a predicted-taken branch must terminate the current fetch group");

    auto admitted_supply_config = fetch_buffer_config;
    admitted_supply_config.fetch_queue_entries = 1;
    admitted_supply_config.fetch_supply_model = true;
    admitted_supply_config.validate();
    auto legacy_supply_config = admitted_supply_config;
    legacy_supply_config.fetch_supply_model = false;
    legacy_supply_config.validate();
    fastsim::IntervalCoreModel legacy_supply(legacy_supply_config);
    fastsim::IntervalCoreModel admitted_supply(admitted_supply_config);
    (void)legacy_supply.schedule(first_block, false);
    (void)admitted_supply.schedule(first_block, false);
    const auto legacy_queue_refill =
        legacy_supply.schedule(second_block, false);
    const auto admitted_queue_refill =
        admitted_supply.schedule(second_block, false);
    check(admitted_queue_refill.fetch_block_request_admission_delay_cycles >
                  0 &&
              admitted_queue_refill.fetch_cycle >
                  legacy_queue_refill.fetch_cycle &&
              admitted_queue_refill
                      .fetch_response_ledger_committed_requests == 1 &&
              admitted_queue_refill.fetch_response_ledger_responses == 1 &&
              admitted_queue_refill.fetch_response_ledger_shadow_requests ==
                  0,
          "source-aligned Fetch supply must not complete a new block request "
          "while the one-entry fetch queue prevents request admission, and "
          "each committed request must schedule exactly one response");

    auto ledger_third_block = free_uop;
    ledger_third_block.pc = 0x1080;
    const auto admitted_next_refill =
        admitted_supply.schedule(ledger_third_block, false);
    check(admitted_next_refill.fetch_block_request_cycle >
                  admitted_queue_refill.fetch_block_response_cycle &&
              admitted_next_refill
                      .fetch_response_ledger_committed_requests == 1 &&
              admitted_next_refill.fetch_response_ledger_responses == 1,
          "the Fetch response slot must serialize committed requests across "
          "trace-record boundaries");

    auto speculative_supply_config = admitted_supply_config;
    speculative_supply_config.fetch_queue_entries = 32;
    speculative_supply_config.branch.population_audit = true;
    speculative_supply_config.fetch_supply_speculative_shadow = true;
    speculative_supply_config.validate();
    fastsim::IntervalCoreModel speculative_supply(
        speculative_supply_config);
    (void)speculative_supply.schedule(first_block, false);
    (void)speculative_supply.schedule(second_block, false);
    auto supply_branch = second_block;
    supply_branch.flags = fastsim::kRetires | fastsim::kBranch |
                          fastsim::kConditional |
                          fastsim::kBranchOutcomeValid;
    const auto speculative_supply_branch =
        speculative_supply.schedule(supply_branch, true);
    check(speculative_supply_branch.speculative_fetch_shadow_uops > 0 &&
              speculative_supply_branch.speculative_fetch_shadow_uops ==
                  speculative_supply_branch.branch_population
                      .estimated_squashed_uops &&
              speculative_supply_branch.branch_population
                      .supply_history_fetch_requests == 1 &&
              speculative_supply_branch.branch_population
                      .supply_history_fetch_response_cycles == 1 &&
              speculative_supply_branch
                      .speculative_fetch_shadow_requests_estimated > 0 &&
              speculative_supply_branch
                      .speculative_fetch_shadow_requests_issued > 0 &&
              speculative_supply_branch
                      .speculative_fetch_shadow_response_wait_cycles ==
                  speculative_supply_branch
                          .speculative_fetch_shadow_recovery_hidden_cycles +
                      speculative_supply_branch
                          .speculative_fetch_shadow_recovery_exposed_cycles,
          "address-free speculative Fetch pressure must use only the local "
          "history-derived population/density and conserve response wait "
          "without a PC");

    auto persistent_shadow_config = speculative_supply_config;
    persistent_shadow_config.fetch_buffer_refill_latency = 32;
    persistent_shadow_config.branch.mispredict_penalty = 1;
    persistent_shadow_config.validate();
    fastsim::IntervalCoreModel persistent_shadow(
        persistent_shadow_config);
    (void)persistent_shadow.schedule(first_block, false);
    (void)persistent_shadow.schedule(second_block, false);
    const auto persistent_shadow_branch =
        persistent_shadow.schedule(supply_branch, true);
    const auto after_shadow_response =
        persistent_shadow.schedule(ledger_third_block, false);
    check(persistent_shadow_branch
                  .fetch_response_ledger_shadow_requests ==
              persistent_shadow_branch
                  .speculative_fetch_shadow_requests_issued &&
              persistent_shadow_branch
                      .fetch_response_ledger_shadow_requests > 0 &&
              persistent_shadow_branch.fetch_response_ledger_responses ==
                  persistent_shadow_branch
                      .fetch_response_ledger_shadow_requests &&
              after_shadow_response
                      .fetch_response_ledger_server_wait_cycles > 0,
          "an anonymous response still in flight at branch recovery must "
          "survive the squash, occupy the shared Fetch response slot, and "
          "delay the next committed request without creating wrong-path "
          "instructions");

    auto zero_density_config = speculative_supply_config;
    zero_density_config.fetch_width = 1;
    zero_density_config.decode_width = 1;
    zero_density_config.rename_width = 1;
    zero_density_config.branch.population_history_cycles = 4;
    zero_density_config.validate();
    fastsim::IntervalCoreModel zero_density_shadow(zero_density_config);
    (void)zero_density_shadow.schedule(first_block, false);
    (void)zero_density_shadow.schedule(second_block, false);
    auto resident = second_block;
    resident.pc = 0x1044;
    for (int index = 0; index < 8; ++index) {
        (void)zero_density_shadow.schedule(resident, false);
    }
    auto resident_branch = resident;
    resident_branch.op_class = 3;
    resident_branch.flags = fastsim::kRetires | fastsim::kBranch |
                            fastsim::kConditional |
                            fastsim::kBranchOutcomeValid;
    const auto zero_density_branch =
        zero_density_shadow.schedule(resident_branch, true);
    check(zero_density_branch.speculative_fetch_shadow_uops > 0 &&
              zero_density_branch.branch_population
                      .supply_history_fetch_requests == 0 &&
              zero_density_branch
                      .speculative_fetch_shadow_requests_estimated == 0 &&
              !zero_density_branch
                       .speculative_fetch_shadow_density_unavailable,
          "a local history window with known zero Fetch-request density "
          "must not borrow stale requests or report missing history");

    fastsim::StaticInstructionInfo crossing_instruction;
    crossing_instruction.pc = 0x103f;
    crossing_instruction.size = 2;
    crossing_instruction.fallthrough_pc = 0x1041;
    StaticMapTraceSource crossing_map({crossing_instruction});
    auto spanning_supply_config = fetch_buffer_config;
    spanning_supply_config.fetch_supply_static_instruction_span = true;
    spanning_supply_config.validate();
    fastsim::IntervalCoreModel spanning_supply(spanning_supply_config);
    (void)spanning_supply.schedule(first_block, false);
    auto crossing_first_uop = free_uop;
    crossing_first_uop.pc = crossing_instruction.pc;
    crossing_first_uop.flags = fastsim::kRetires | fastsim::kMicroOp;
    auto crossing_last_uop = crossing_first_uop;
    crossing_last_uop.flags |= fastsim::kLastMicroOp;
    const auto crossing_first = spanning_supply.schedule(
        crossing_first_uop, false, false, 0, false, &crossing_map);
    const auto crossing_last = spanning_supply.schedule(
        crossing_last_uop, false, false, 0, false, &crossing_map);
    check(crossing_first.fetch_supply_static_span_lookup &&
              crossing_first.fetch_supply_cross_block_instruction &&
              crossing_first.fetch_supply_cross_block_extra_requests == 1 &&
              crossing_first.fetch_buffer_transition_count == 1 &&
              !crossing_last.fetch_buffer_transition,
          "a macro instruction spanning two 64-byte Fetch blocks must request "
          "the second block once while its remaining micro-ops reuse the "
          "decoded macro instruction");

    fastsim::IntervalCoreModel filtered_span_supply(spanning_supply_config);
    auto safe_without_map = free_uop;
    safe_without_map.pc = 0x1080;
    const auto filtered_safe =
        filtered_span_supply.schedule(safe_without_map, false);
    auto risky_without_map = free_uop;
    risky_without_map.pc = 0x10bf;
    const auto filtered_risky =
        filtered_span_supply.schedule(risky_without_map, false);
    check(!filtered_safe.fetch_supply_static_span_lookup &&
              !filtered_safe.fetch_supply_static_span_unavailable &&
              filtered_risky.fetch_supply_static_span_unavailable,
          "the x86 15-byte prefilter must skip impossible crossings and "
          "fail closed only when a block-tail instruction needs a map");
}

void test_branch_shadow_rob() {
    fastsim::SimulatorConfig base;
    base.core_model = "interval_bound";
    base.fetch_width = 8;
    base.decode_width = 8;
    base.rename_width = 8;
    base.commit_width = 8;
    base.rob_entries = 192;
    base.branch.mispredict_penalty = 2;
    base.validate();
    auto shadow_config = base;
    shadow_config.branch.shadow_rob = true;
    shadow_config.branch.squash_width = 8;
    shadow_config.validate();
    auto instant_squash_config = shadow_config;
    instant_squash_config.branch.squash_width = 0;
    instant_squash_config.validate();

    fastsim::TraceRecord branch;
    branch.pc = 0x1000;
    branch.target = 0x2000;
    branch.next_pc = 0x2000;
    branch.flags = fastsim::kRetires | fastsim::kBranch |
                   fastsim::kConditional | fastsim::kTaken |
                   fastsim::kBranchOutcomeValid;
    fastsim::TraceRecord target;
    target.pc = 0x2000;

    fastsim::IntervalCoreModel reference(base);
    fastsim::IntervalCoreModel shadow(shadow_config);
    fastsim::IntervalCoreModel instant_squash(instant_squash_config);
    const auto reference_branch = reference.schedule(branch, true);
    const auto shadow_branch = shadow.schedule(branch, true);
    const auto instant_branch = instant_squash.schedule(branch, true);
    const auto reference_target = reference.schedule(target, false);
    const auto shadow_target = shadow.schedule(target, false);
    const auto instant_target = instant_squash.schedule(target, false);

    check(shadow_branch.branch_shadow_uops > 0 &&
              shadow_branch.branch_shadow_uops <=
                  shadow_config.rob_entries &&
              shadow_branch.branch_shadow_cycles ==
                  (shadow_branch.branch_shadow_uops +
                   shadow_config.branch.squash_width - 1) /
                      shadow_config.branch.squash_width,
          "branch shadow occupancy must be derived from the target pipeline "
          "and bounded by the ROB");
    check(shadow_target.rename_cycle > reference_target.rename_cycle &&
              shadow_branch.fetch_cycle == reference_branch.fetch_cycle &&
              shadow_branch.completion_cycle ==
                  reference_branch.completion_cycle,
          "anonymous wrong-path occupancy must delay only correct-path "
          "rename, not the resolving branch or functional execution");
    check(instant_branch.branch_shadow_uops == 0 &&
              instant_branch.branch_shadow_cycles == 0 &&
              instant_target.rename_cycle == reference_target.rename_cycle,
          "gem5 NullOpt squashWidth must make shadow occupancy a zero-cost "
          "no-op");
}

void test_branch_population_audit() {
    fastsim::SimulatorConfig reference_config;
    reference_config.core_model = "interval_bound";
    reference_config.fetch_width = 8;
    reference_config.decode_width = 8;
    reference_config.rename_width = 8;
    reference_config.commit_width = 8;
    reference_config.rob_entries = 192;
    reference_config.branch.mispredict_penalty = 2;
    reference_config.validate();

    auto audit_config = reference_config;
    audit_config.branch.population_audit = true;
    audit_config.branch.population_history_cycles = 64;
    audit_config.validate();

    fastsim::IntervalCoreModel reference(reference_config);
    fastsim::IntervalCoreModel audited(audit_config);
    fastsim::TraceRecord warmup;
    warmup.flags = fastsim::kRetires;
    for (std::uint64_t index = 0; index < 64; ++index) {
        warmup.pc = 0x1000 + index * 4;
        (void)reference.schedule(warmup, false);
        (void)audited.schedule(warmup, false);
    }

    fastsim::TraceRecord branch;
    branch.pc = 0x2000;
    branch.target = 0x4000;
    branch.next_pc = 0x4000;
    branch.op_class = 3;  // Long enough to expose a populated wrong path.
    branch.flags = fastsim::kRetires | fastsim::kBranch |
                   fastsim::kConditional | fastsim::kTaken |
                   fastsim::kBranchOutcomeValid;
    std::vector<std::uint64_t> predicted_path;
    for (std::uint64_t index = 0; index < 64; ++index) {
        predicted_path.push_back(0x3000 + index * 4);
    }
    const auto reference_branch = reference.schedule(
        branch, true, false, 0, false, nullptr, &predicted_path);
    const auto audited_branch = audited.schedule(
        branch, true, false, 0, false, nullptr, &predicted_path);

    fastsim::TraceRecord target;
    target.pc = branch.next_pc;
    target.flags = fastsim::kRetires;
    const auto reference_target = reference.schedule(target, false);
    const auto audited_target = audited.schedule(target, false);
    const auto& population = audited_branch.branch_population;
    check(population.conserved() && population.miss_events == 1 &&
              population.history_ready_events == 1 &&
              population.predicted_path_covered_events == 1 &&
              population.predicted_path_records == predicted_path.size() &&
              population.resolution_cycles > 0 &&
              population.estimated_squashed_uops > 0 &&
              population.estimated_squashed_uops <=
                  population.rob_free_uops &&
              population.estimated_squashed_uops <=
                  population.supply_budget_uops &&
              population.predicted_path_covered_uops <=
                  population.estimated_squashed_uops,
          "branch population audit must conserve its history, path, ROB, "
          "and frontend-supply bounds");
    check(audited_branch.fetch_cycle == reference_branch.fetch_cycle &&
              audited_branch.completion_cycle ==
                  reference_branch.completion_cycle &&
              audited_branch.retire_cycle == reference_branch.retire_cycle &&
              audited_target.fetch_cycle == reference_target.fetch_cycle &&
              audited_target.rename_cycle == reference_target.rename_cycle &&
              audited_target.retire_cycle == reference_target.retire_cycle,
          "branch population audit must not change the committed CPI "
          "timeline");

    fastsim::IntervalCoreModel cold(audit_config);
    const auto cold_branch = cold.schedule(
        branch, true, false, 0, false, nullptr, &predicted_path);
    check(cold_branch.branch_population.conserved() &&
              cold_branch.branch_population.history_unavailable_events == 1 &&
              cold_branch.branch_population.estimated_squashed_uops == 0,
          "a branch miss without causal supply history must fail closed");

    auto invalid = audit_config;
    invalid.branch.population_history_cycles = 0;
    bool rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "branch population history must reject an empty rolling window");

    invalid = reference_config;
    invalid.fetch_buffer_bytes = 64;
    invalid.fetch_buffer_refill_latency = 1;
    invalid.fetch_supply_model = true;
    invalid.fetch_supply_speculative_shadow = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "speculative Fetch shadow must require the causal population "
          "history instead of falling back to frontend width");
}

void test_committed_pipeline_audit() {
    fastsim::SimulatorConfig base;
    base.core_model = "interval_bound";
    base.fetch_width = 8;
    base.decode_width = 8;
    base.rename_width = 8;
    base.dispatch_width = 8;
    base.issue_width = 8;
    base.writeback_width = 8;
    base.commit_width = 8;
    base.rob_entries = 256;
    base.iq_entries = 1;
    base.lq_entries = 256;
    base.sq_entries = 256;
    base.minimum_load_latency = 12;
    base.validate();

    auto audited_config = base;
    audited_config.committed_pipeline_audit = true;
    audited_config.validate();
    fastsim::IntervalCoreModel reference(base);
    fastsim::IntervalCoreModel audited(audited_config);
    fastsim::TraceRecord load;
    load.address = 0x8000;
    load.size = 8;
    load.flags = fastsim::kRetires | fastsim::kLoad |
                 fastsim::kPhysicalAddress;
    load.n_dst = 8;
    for (int index = 0; index < 80; ++index) {
        load.address += 64;
        const auto expected = reference.schedule(load, false);
        const auto observed = audited.schedule(load, false);
        check(expected.fetch_cycle == observed.fetch_cycle &&
                  expected.decode_cycle == observed.decode_cycle &&
                  expected.rename_cycle == observed.rename_cycle &&
                  expected.dispatch_cycle == observed.dispatch_cycle &&
                  expected.issue_cycle == observed.issue_cycle &&
                  expected.execute_cycle == observed.execute_cycle &&
                  expected.completion_cycle == observed.completion_cycle &&
                  expected.retire_cycle == observed.retire_cycle,
              "committed pipeline audit must be timing-neutral");
    }
    const auto& counters = audited.committed_pipeline_audit();
    check(reference.last_retire_cycle() == audited.last_retire_cycle(),
          "audit must preserve final interval timing");
    check(counters.uops == 80 && counters.destination_tokens == 640 &&
              counters.destination_conserved(),
          "destination-token lifecycle must conserve committed outputs");
    check(counters.max_live_destination_tokens > 64 &&
              counters.destination_threshold_events[0] != 0,
          "destination audit must expose threshold pressure");
    check(counters.iq_capacity_events != 0 &&
              counters.memory_iq_post_issue_uops == 80 &&
              counters.memory_iq_post_issue_cycles != 0,
          "one-entry IQ must expose the modeled memory residency");
    check(counters.dispatch_delayed_uops != 0 &&
              counters.dispatch_conserved(),
          "dispatch delay must have a mutually exclusive attribution");
    check(counters.stage_conserved() &&
              counters.stage_fetch_to_retire_cycles > 0 &&
              counters.stage_memory_uops == 80 &&
              counters.stage_memory_issue_to_completion_cycles > 0,
          "committed lower-bound stage edges must conserve fetch-to-retire "
          "residence");

    auto rob_config = audited_config;
    rob_config.rob_entries = 2;
    rob_config.iq_entries = 256;
    rob_config.validate();
    fastsim::IntervalCoreModel rob_limited(rob_config);
    fastsim::TraceRecord chain;
    chain.op_class = 3;
    chain.producer_dists[0] = 1;
    for (int index = 0; index < 32; ++index) {
        (void)rob_limited.schedule(chain, false);
    }
    const auto& rob = rob_limited.committed_pipeline_audit();
    check(rob.rob_capacity_events != 0 && rob.dispatch_conserved(),
          "small ROB must be visible in the committed dispatch ledger");
    check(rob.dependency_edges == 31 && rob.dependent_uops == 31 &&
              rob.dependency_producer_uops == 31 &&
              rob.dependency_edges_by_pool[1] == 31 &&
              rob.pool_uops[1] == 32 &&
              rob.dependency_distance_sum == 31 &&
              rob.dependency_distance_max == 1 &&
              rob.dependency_gated_uops > 0 &&
              rob.dependency_gate_cycles > 0 &&
              rob.dependency_gated_uops_by_pool[1] > 0 &&
              rob.dependency_gate_cycles_by_pool[1] > 0,
          "committed dependency audit must count exact dynamic RAW edges");

    fastsim::TraceRecord wide_sources = chain;
    wide_sources.n_src = 7;
    (void)rob_limited.schedule(wide_sources, false);
    check(rob_limited.committed_pipeline_audit()
                  .source_uops_over_dependency_slots == 1 &&
              rob_limited.committed_pipeline_audit()
                  .source_operands_over_dependency_slots == 3,
          "dependency audit must expose FST producer-slot coverage limits");

    fastsim::StaticInstructionInfo static_writer;
    static_writer.pc = 0x1000;
    static_writer.operand_semantics_valid = true;
    static_writer.write_register_mask[0] = 1ull;
    fastsim::StaticInstructionInfo static_reader;
    static_reader.pc = 0x1004;
    static_reader.operand_semantics_valid = true;
    static_reader.read_register_mask[0] = 1ull;
    StaticMapTraceSource static_dependencies(
        {static_writer, static_reader});
    fastsim::TraceRecord writer;
    writer.pc = static_writer.pc;
    writer.op_class = 3;
    fastsim::TraceRecord reader;
    reader.pc = static_reader.pc;
    reader.n_src = 5;
    auto static_control_config = audited_config;
    static_control_config.iq_entries = 256;
    auto static_feedback_config = static_control_config;
    static_feedback_config.committed_static_dependency_feedback = true;
    static_feedback_config.validate();
    fastsim::IntervalCoreModel static_control(static_control_config);
    fastsim::IntervalCoreModel static_feedback(static_feedback_config);
    const auto control_writer = static_control.schedule(
        writer, false, false, 0, false, &static_dependencies);
    const auto control_reader = static_control.schedule(
        reader, false, false, 0, false, &static_dependencies);
    const auto feedback_writer = static_feedback.schedule(
        writer, false, false, 0, false, &static_dependencies);
    const auto feedback_reader = static_feedback.schedule(
        reader, false, false, 0, false, &static_dependencies);
    const auto& static_audit =
        static_control.committed_pipeline_audit();
    check(static_audit.static_dependency_edges == 1 &&
              static_audit.static_dependency_duplicate_edges == 0 &&
              static_audit.static_dependency_supplemental_edges == 1 &&
              static_audit.static_dependency_ready_extension_uops == 1,
          "static operand audit must identify a missing committed RAW edge");
    check(control_reader.issue_cycle < control_writer.completion_cycle &&
              feedback_reader.issue_cycle >=
                  feedback_writer.completion_cycle &&
              feedback_reader.issue_cycle > control_reader.issue_cycle,
          "static dependency feedback must gate a consumer on the prior "
          "architectural writer only when enabled");

    auto store_set_control_config = audited_config;
    store_set_control_config.iq_entries = 256;
    store_set_control_config.integer_divide_latency = 32;
    auto store_set_feedback_config = store_set_control_config;
    store_set_feedback_config.store_set_same_pc_feedback = true;
    store_set_feedback_config.validate();
    fastsim::IntervalCoreModel store_set_control(store_set_control_config);
    fastsim::IntervalCoreModel store_set_feedback(store_set_feedback_config);
    fastsim::TraceRecord rmw_load;
    rmw_load.pc = 0x3000;
    rmw_load.address = 0x7000;
    rmw_load.size = 8;
    rmw_load.flags = fastsim::kRetires | fastsim::kLoad |
                     fastsim::kPhysicalAddress | fastsim::kMicroOp;
    fastsim::TraceRecord rmw_store;
    rmw_store.pc = rmw_load.pc;
    rmw_store.address = rmw_load.address;
    rmw_store.size = rmw_load.size;
    rmw_store.flags = fastsim::kRetires | fastsim::kStore |
                      fastsim::kPhysicalAddress | fastsim::kMicroOp |
                      fastsim::kLastMicroOp;
    fastsim::TraceRecord long_producer;
    long_producer.pc = rmw_load.pc;
    long_producer.op_class = 3;
    long_producer.flags = fastsim::kRetires | fastsim::kMicroOp;
    fastsim::TraceRecord delayed_store;
    delayed_store.pc = 0x3000;
    delayed_store.address = 0x8000;
    delayed_store.size = 8;
    delayed_store.flags = fastsim::kRetires | fastsim::kStore |
                          fastsim::kPhysicalAddress;
    rmw_store.producer_dists[0] = 1;
    delayed_store.producer_dists[0] = 2;
    fastsim::TraceRecord same_pc_load;
    same_pc_load.pc = delayed_store.pc;
    same_pc_load.address = 0x9000;
    same_pc_load.size = 8;
    same_pc_load.flags = fastsim::kRetires | fastsim::kLoad |
                         fastsim::kPhysicalAddress;
    (void)store_set_control.schedule(rmw_load, false);
    (void)store_set_control.schedule(long_producer, false);
    (void)store_set_control.schedule(rmw_store, false);
    const auto control_store =
        store_set_control.schedule(delayed_store, false);
    const auto control_load =
        store_set_control.schedule(same_pc_load, false);
    (void)store_set_feedback.schedule(rmw_load, false);
    (void)store_set_feedback.schedule(long_producer, false);
    (void)store_set_feedback.schedule(rmw_store, false);
    const auto feedback_store =
        store_set_feedback.schedule(delayed_store, false);
    const auto feedback_load =
        store_set_feedback.schedule(same_pc_load, false);
    const auto& store_set_audit =
        store_set_control.committed_pipeline_audit();
    check(store_set_audit.store_set_rmw_observations == 1 &&
              store_set_audit.store_set_rmw_pc_trainings == 1 &&
              store_set_audit.store_set_same_pc_load_candidates == 2 &&
              store_set_audit.store_set_same_pc_store_candidates == 2 &&
              store_set_audit.store_set_same_pc_edges == 2 &&
              store_set_audit.store_set_same_pc_load_edges == 1 &&
              store_set_audit.store_set_same_pc_store_edges == 1 &&
              store_set_audit.store_set_same_pc_nonoverlap_edges == 2 &&
              store_set_audit.store_set_same_pc_ready_extension_uops >= 1,
          "same-PC StoreSet audit must expose a false non-aliasing memory "
          "dependency without changing the control timeline: observations=" +
              std::to_string(store_set_audit.store_set_rmw_observations) +
              " trainings=" +
              std::to_string(store_set_audit.store_set_rmw_pc_trainings) +
              " load_candidates=" +
              std::to_string(
                  store_set_audit.store_set_same_pc_load_candidates) +
              " store_candidates=" +
              std::to_string(
                  store_set_audit.store_set_same_pc_store_candidates) +
              " edges=" +
              std::to_string(store_set_audit.store_set_same_pc_edges) +
              " load_edges=" +
              std::to_string(store_set_audit.store_set_same_pc_load_edges) +
              " store_edges=" +
              std::to_string(store_set_audit.store_set_same_pc_store_edges));
    check(control_load.issue_cycle < control_store.completion_cycle &&
              feedback_load.store_set_dependency_distance == 1 &&
              feedback_load.issue_cycle >= feedback_store.completion_cycle &&
              feedback_load.issue_cycle > control_load.issue_cycle,
          "same-PC StoreSet feedback must wake a load from the prior live "
          "store only when the candidate is enabled");

    auto free_list_config = audited_config;
    free_list_config.iq_entries = 256;
    free_list_config.rename_free_list = true;
    free_list_config.rename_int_free_entries = 2;
    free_list_config.validate();
    auto no_free_list_config = free_list_config;
    no_free_list_config.rename_free_list = false;
    fastsim::IntervalCoreModel free_list(free_list_config);
    fastsim::IntervalCoreModel no_free_list(no_free_list_config);
    fastsim::TraceRecord integer_output;
    integer_output.op_class = 3;
    integer_output.n_dst = 1;
    integer_output.set_register_class_metadata(
        {255, 255, 255, 255}, {1, 0, 0, 0});
    for (int index = 0; index < 32; ++index) {
        (void)free_list.schedule(integer_output, false);
        (void)no_free_list.schedule(integer_output, false);
    }
    const auto& registers = free_list.committed_pipeline_audit();
    check(registers.rename_free_list_stall_uops != 0 &&
              registers.destination_class_max_live_tokens[0] == 2 &&
              registers.destination_classes_conserved(),
          "per-class free list must stall and conserve committed mappings");
    check(free_list.last_retire_cycle() > no_free_list.last_retire_cycle(),
          "committed free-list pressure must affect timing only when enabled");

    bool missing_classes_rejected = false;
    try {
        fastsim::IntervalCoreModel missing(free_list_config);
        fastsim::TraceRecord legacy;
        legacy.n_dst = 1;
        (void)missing.schedule(legacy, false);
    } catch (const std::runtime_error&) {
        missing_classes_rejected = true;
    }
    check(missing_classes_rejected,
          "free-list timing must not guess classes for an FST v5 record");
}

void test_static_memory_ordering() {
    fastsim::SimulatorConfig config;
    config.core_model = "interval_bound";
    config.fetch_width = 8;
    config.decode_width = 8;
    config.rename_width = 8;
    config.dispatch_width = 8;
    config.issue_width = 8;
    config.writeback_width = 8;
    config.commit_width = 8;
    config.rob_entries = 256;
    config.iq_entries = 256;
    config.lq_entries = 256;
    config.sq_entries = 256;
    config.integer_divide_latency = 32;
    config.committed_static_memory_ordering = true;
    config.validate();

    fastsim::StaticInstructionInfo locked;
    locked.pc = 0x2000;
    locked.size = 4;
    locked.fallthrough_pc = 0x2004;
    locked.flags = fastsim::kStaticMemory |
                   fastsim::kStaticReadBarrier |
                   fastsim::kStaticWriteBarrier |
                   fastsim::kStaticLockedRmw;
    StaticMapTraceSource static_map({locked});

    fastsim::TraceRecord old_divide;
    old_divide.pc = 0x1000;
    old_divide.op_class = 3;
    fastsim::TraceRecord immediate;
    immediate.pc = locked.pc;
    immediate.flags = fastsim::kRetires | fastsim::kMicroOp;
    fastsim::TraceRecord locked_load;
    locked_load.pc = locked.pc;
    locked_load.address = 0x8000;
    locked_load.size = 4;
    locked_load.flags = fastsim::kRetires | fastsim::kLoad |
                        fastsim::kPhysicalAddress | fastsim::kMicroOp;
    fastsim::TraceRecord arithmetic = immediate;
    fastsim::TraceRecord locked_store;
    locked_store.pc = locked.pc;
    locked_store.address = locked_load.address;
    locked_store.size = locked_load.size;
    locked_store.flags = fastsim::kRetires | fastsim::kStore |
                         fastsim::kPhysicalAddress | fastsim::kMicroOp;
    fastsim::TraceRecord trailing_fence = immediate;
    trailing_fence.flags |= fastsim::kLastMicroOp;
    fastsim::TraceRecord younger_alu;
    younger_alu.pc = 0x3000;
    fastsim::TraceRecord younger_load = locked_load;
    younger_load.pc = 0x3004;
    younger_load.address += 64;
    younger_load.flags = fastsim::kRetires | fastsim::kLoad |
                         fastsim::kPhysicalAddress;

    fastsim::IntervalCoreModel ordered(config);
    const auto old = ordered.schedule(old_divide, false, false, 0, false,
                                      &static_map);
    const auto prep = ordered.schedule(immediate, false, false, 0, false,
                                       &static_map);
    const auto load = ordered.schedule(locked_load, false, false, 0, false,
                                       &static_map);
    (void)ordered.schedule(arithmetic, false, false, 0, false, &static_map);
    const auto store = ordered.schedule(locked_store, false, false, 0, false,
                                        &static_map);
    const auto post = ordered.schedule(trailing_fence, false, false, 0, false,
                                       &static_map);
    const auto alu = ordered.schedule(younger_alu, false, false, 0, false,
                                      &static_map);
    const auto after = ordered.schedule(younger_load, false, false, 0, false,
                                        &static_map);

    check(prep.issue_cycle < old.completion_cycle,
          "LOCK helper UOPs must remain able to overlap an older long ALU");
    check(load.static_memory_barrier_before && load.static_locked_rmw &&
              load.issue_cycle >= prep.retire_cycle + 1,
          "the first LOCK memory UOP must wait for the leading ROB-head "
          "fence");
    check(post.static_memory_barrier_before &&
              post.static_memory_barrier_after &&
              post.issue_cycle >= store.retire_cycle + 1,
          "the final LOCK UOP must represent the trailing ROB-head fence");
    check(alu.issue_cycle < post.completion_cycle,
          "a memory fence must not globally serialize independent ALU issue");
    check(after.issue_cycle >= post.completion_cycle,
          "younger memory issue must not cross the completed trailing fence");
}

void test_interval_dtlb() {
    fastsim::SimulatorConfig config;
    config.core_model = "interval_bound";
    config.dtlb.enabled = true;
    config.dtlb.entries = 2;
    config.dtlb.hit_latency = 0;
    config.dtlb.page_walk_latency = 10;
    config.dtlb.page_walkers = 1;
    config.dtlb.coalesce_misses = false;
    config.validate();

    fastsim::TraceRecord load;
    load.address = 0x8000;
    load.size = 8;
    load.flags = fastsim::kRetires | fastsim::kLoad |
                 fastsim::kPhysicalAddress |
                 fastsim::kVirtualPageToken;
    load.reserved = 1;
    fastsim::IntervalCoreModel model(config);
    const auto cold = model.schedule(load, false);
    const auto queued = model.schedule(load, false);
    fastsim::TraceRecord barrier;
    barrier.flags = fastsim::kRetires | fastsim::kSerialize;
    (void)model.schedule(barrier, false);
    const auto warm = model.schedule(load, false);
    check(cold.dtlb_miss && !cold.dtlb_hit &&
              cold.dtlb_timing_miss &&
              !cold.dtlb_timing_merged_miss &&
              cold.dtlb_fill_generation != 0 &&
              cold.translation_delay_cycles >= 10,
          "cold virtual page must allocate a page walk");
    check(queued.dtlb_hit && !queued.dtlb_miss &&
              queued.dtlb_timing_miss &&
              !queued.dtlb_timing_merged_miss &&
              queued.dtlb_fill_generation != 0 &&
              queued.dtlb_fill_generation !=
                  cold.dtlb_fill_generation &&
              queued.translation_delay_cycles >
                  cold.translation_delay_cycles,
          "gem5 x86 follower must queue without corrupting retired PMU");
    check(warm.dtlb_hit && !warm.dtlb_miss &&
              warm.dtlb_timing_hit && !warm.dtlb_timing_miss &&
              warm.dtlb_fill_generation ==
                  cold.dtlb_fill_generation,
          "completed page walk must fill the DTLB with the provenance of "
          "the first visible same-page walk");

    auto coalescing_config = config;
    coalescing_config.dtlb.coalesce_misses = true;
    fastsim::IntervalCoreModel coalescing(coalescing_config);
    (void)coalescing.schedule(load, false);
    const auto merged = coalescing.schedule(load, false);
    check(merged.dtlb_hit && !merged.dtlb_miss &&
              merged.dtlb_timing_miss &&
              merged.dtlb_timing_merged_miss &&
              merged.dtlb_fill_generation != 0,
          "optional coalescing must remain timing-only and auditable");

    fastsim::IntervalCoreModel address_spaces(config);
    const auto schedule_in = [&](std::uint64_t address_space_id) {
        return address_spaces.schedule(
            load, false, false, 0, false, nullptr, nullptr,
            address_space_id);
    };
    const auto as7_cold = schedule_in(7);
    const auto as7_warm = schedule_in(7);
    const auto as11_cold = schedule_in(11);
    const auto as11_warm = schedule_in(11);
    const auto as7_after_switch = schedule_in(7);
    check(as7_cold.dtlb_miss && as7_warm.dtlb_hit &&
              as11_cold.dtlb_miss && as11_warm.dtlb_hit &&
              as7_after_switch.dtlb_miss,
          "CR3/address-space transitions must flush modeled non-global "
          "DTLB state instead of borrowing a same-token translation");
}

fastsim::SimulationStats run_dtlb_hierarchy_walk_case(
    bool enabled, bool include_late_same_page_access = false,
    bool synthetic_addresses = false,
    bool include_physical_path = true,
    std::uint8_t physical_path_levels = 4) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.interval_max_cycles = 1024;
    config.interval_target_uops = 8;
    config.chunk_instructions = include_late_same_page_access ? 128 : 8;
    config.lookahead_chunks = 2;
    config.interval_private_preview = false;
    config.interval_reweave_passes = 1;
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.cpi_attribution = true;
    config.ruby_sequencer_max_outstanding = 16;
    config.dtlb.enabled = true;
    config.dtlb.entries = 64;
    config.dtlb.hit_latency = 0;
    config.dtlb.miss_model = "timing_walk";
    config.dtlb.page_walkers = 1;
    config.dtlb.coalesce_misses = false;
    config.dtlb.page_walk_levels = 4;
    config.dtlb.page_walk_address_mode = synthetic_addresses
        ? "synthetic" : "physical_sidecar";
    config.l1d.hit_latency = 2;
    config.l2.hit_latency = 6;
    config.dtlb.page_walk_latency =
        config.dtlb.page_walk_levels * config.l1d.hit_latency +
        config.dtlb.page_walk_restart_latency;
    config.dtlb.hierarchy_walk = enabled;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.size_bytes = 64ull << 20;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    fastsim::TraceRecord load;
    load.pc = 0x400000;
    load.address = 0x200000;
    load.size = 8;
    load.flags = fastsim::kRetires | fastsim::kLoad |
                 fastsim::kPhysicalAddress |
                 fastsim::kVirtualPageToken;
    load.reserved = 7;
    std::vector<fastsim::TraceRecord> records{load};
    if (include_late_same_page_access) {
        // Move the second lookup past the fixed four-level all-L1 lower
        // bound without making it data-dependent on the first load.  A cold
        // hierarchy response remains outstanding, so consuming the fixed
        // DTLB fill here would be future information.
        for (std::uint64_t index = 0; index < 64; ++index) {
            fastsim::TraceRecord alu;
            alu.pc = 0x401000 + index * 4;
            alu.op_class = 1;
            records.push_back(alu);
        }
        // Two adjacent apparent hits deliberately share the same lower-bound
        // fill generation. Response repair must retain that provenance for
        // both; consuming/removing page-only state after the first hit would
        // let the second one see the same future fill without waiting.
        for (std::uint64_t index = 0; index < 2; ++index) {
            auto later_load = load;
            later_load.pc += 4 + index * 4;
            records.push_back(later_load);
        }
    }
    fastsim::VirtualPageMapping mapping{7, 0, 0x12345, 0x200, true};
    if (include_physical_path) {
        mapping.initial_page_table_path.valid = true;
        mapping.initial_page_table_path.levels = physical_path_levels;
        mapping.initial_page_table_path.page_size_bits =
            physical_path_levels == 2 ? 30 :
            physical_path_levels == 3 ? 21 : 12;
        const std::array<std::uint64_t, 4> addresses{
            0x1008, 0x2040, 0x3180, 0x4a28};
        for (std::uint8_t level = 0; level < physical_path_levels;
             ++level) {
            mapping.initial_page_table_path.pte_physical_addresses[level] =
                addresses[level];
        }
        mapping.roi_entry_page_table_path =
            mapping.initial_page_table_path;
    }
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VirtualPageTraceSource>(
        std::move(records), std::move(mapping)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_dtlb_hierarchy_walk() {
    const auto fixed = run_dtlb_hierarchy_walk_case(false);
    const auto hierarchy = run_dtlb_hierarchy_walk_case(true);
    const auto fixed_core = fixed.total_core();
    const auto hierarchy_core = hierarchy.total_core();
    check(hierarchy_core.retired_uops == fixed_core.retired_uops &&
              hierarchy_core.memory_accesses == fixed_core.memory_accesses &&
              hierarchy_core.dtlb.misses == fixed_core.dtlb.misses,
          "cache-visible page walks must not fabricate architectural work");
    check(hierarchy_core.dtlb_hierarchy_walks == 1 &&
              hierarchy_core.dtlb_hierarchy_requests == 4 &&
              hierarchy_core.dtlb_hierarchy_physical_requests == 4 &&
              hierarchy_core.dtlb_hierarchy_synthetic_requests == 0 &&
              hierarchy_core.dtlb_hierarchy_initial_path_walks == 1 &&
              hierarchy_core.dtlb_hierarchy_roi_entry_path_walks == 0 &&
              hierarchy_core.dtlb_hierarchy_short_path_walks == 0 &&
              hierarchy_core
                      .dtlb_hierarchy_fixed_latency_fallback_walks == 0 &&
              hierarchy_core.l1d.accesses == fixed_core.l1d.accesses + 4 &&
              hierarchy.total_sequencer().requests ==
                  fixed.total_sequencer().requests + 4,
          "one four-level timing miss must emit exactly four ordinary "
          "data-hierarchy and Sequencer requests");
    check(hierarchy_core.dtlb_hierarchy_l1_hits +
                  hierarchy_core.dtlb_hierarchy_l2_hits +
                  hierarchy_core.dtlb_hierarchy_shared_requests ==
              hierarchy_core.dtlb_hierarchy_requests,
          "every page-walk request must have one conserved hierarchy path");
    std::uint64_t level_l1_hits = 0;
    std::uint64_t level_l2_hits = 0;
    std::uint64_t level_shared_requests = 0;
    for (std::size_t level = 0;
         level < hierarchy_core.dtlb_hierarchy_level_l1_hits.size();
         ++level) {
        level_l1_hits +=
            hierarchy_core.dtlb_hierarchy_level_l1_hits[level];
        level_l2_hits +=
            hierarchy_core.dtlb_hierarchy_level_l2_hits[level];
        level_shared_requests +=
            hierarchy_core.dtlb_hierarchy_level_shared_requests[level];
    }
    check(level_l1_hits == hierarchy_core.dtlb_hierarchy_l1_hits &&
              level_l2_hits == hierarchy_core.dtlb_hierarchy_l2_hits &&
              level_shared_requests ==
                  hierarchy_core.dtlb_hierarchy_shared_requests,
          "level-resolved DTLB hierarchy paths must conserve totals");
    check(hierarchy_core.dtlb_hierarchy_walk_extra_cycles > 0 &&
              hierarchy_core.cycles > fixed_core.cycles,
          "cold PTE responses must delay the dependent data request beyond "
          "the fixed all-L1 walker lower bound");

    const auto premature_stats =
        run_dtlb_hierarchy_walk_case(true, true);
    const auto premature = premature_stats.total_core();
    check(premature.dtlb_hierarchy_premature_hits >= 2 &&
              premature.dtlb_hierarchy_premature_hit_cycles != 0 &&
              premature.dtlb_hierarchy_premature_hit_max_cycles != 0 &&
              premature_stats.total_response_residuals()
                      .memory_issue_moved_cycles >=
                  premature.dtlb_hierarchy_premature_hit_max_cycles,
          "hierarchy replay must detect and delay a lower-bound DTLB hit "
          "until the actual PTE response instead of consuming a future "
          "translation fill");

    const auto synthetic =
        run_dtlb_hierarchy_walk_case(true, false, true).total_core();
    check(synthetic.dtlb_hierarchy_requests == 4 &&
              synthetic.dtlb_hierarchy_physical_requests == 0 &&
              synthetic.dtlb_hierarchy_synthetic_requests == 4 &&
              synthetic.dtlb_hierarchy_initial_path_walks == 0 &&
              synthetic.dtlb_hierarchy_roi_entry_path_walks == 0 &&
              synthetic.dtlb_hierarchy_short_path_walks == 0 &&
              synthetic.dtlb_hierarchy_fixed_latency_fallback_walks == 0,
          "synthetic page-table addresses must remain an explicit, "
          "separately counted diagnostic mode");

    const auto short_path =
        run_dtlb_hierarchy_walk_case(true, false, false, true, 3)
            .total_core();
    check(short_path.dtlb_hierarchy_walks == 1 &&
              short_path.dtlb_hierarchy_requests == 3 &&
              short_path.dtlb_hierarchy_physical_requests == 3 &&
              short_path.dtlb_hierarchy_short_path_walks == 1 &&
              short_path.dtlb_hierarchy_initial_path_walks == 1 &&
              short_path.dtlb_hierarchy_fixed_latency_fallback_walks == 0 &&
              short_path.dtlb_timing.walk_delay_cycles +
                      2 ==
                  hierarchy_core.dtlb_timing.walk_delay_cycles,
          "a captured huge-page path must emit and charge only its actual "
          "number of PTE levels");

    const auto missing_path =
        run_dtlb_hierarchy_walk_case(true, false, false, false)
            .total_core();
    check(missing_path.dtlb_hierarchy_walks == 0 &&
              missing_path.dtlb_hierarchy_requests == 0 &&
              missing_path.dtlb_hierarchy_physical_requests == 0 &&
              missing_path.dtlb_hierarchy_synthetic_requests == 0 &&
              missing_path.dtlb_hierarchy_short_path_walks == 0 &&
              missing_path.dtlb_hierarchy_fixed_latency_fallback_walks == 1 &&
              missing_path.dtlb_timing.walk_delay_cycles ==
                  fixed_core.dtlb_timing.walk_delay_cycles,
          "a path absent from both causal snapshots must retain the fixed "
          "walk without fabricating a physical or synthetic PTE address");

    auto invalid = fastsim::SimulatorConfig{};
    invalid.cores = 1;
    invalid.core_model = "interval_weave";
    invalid.interval_scheduler = "time_epoch";
    invalid.interval_private_preview = false;
    invalid.interval_reweave_passes = 1;
    invalid.response_queue_feedback = true;
    invalid.response_sparse_scoreboard = true;
    invalid.ruby_sequencer_max_outstanding = 16;
    invalid.dtlb.enabled = true;
    invalid.dtlb.miss_model = "timing_walk";
    invalid.dtlb.hierarchy_walk = true;
    invalid.dtlb.page_walk_levels = 4;
    invalid.dtlb.page_walk_latency = 11;
    invalid.l1d.hit_latency = 2;
    bool rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "hierarchy walk must reject a fixed baseline inconsistent with "
          "four L1 responses and the source-defined O3 restart edge");

    invalid.dtlb.page_walk_latency =
        invalid.dtlb.page_walk_levels * invalid.l1d.hit_latency +
        invalid.dtlb.page_walk_restart_latency;
    invalid.dtlb.speculative_path_state = true;
    rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "cache-visible page walks must reject address-free speculative "
          "DTLB state instead of inventing wrong-path PTE identities");
}

void test_interval_syscall_serialization() {
    fastsim::SimulatorConfig config;
    config.core_model = "interval_bound";
    config.system_latency = 3;
    config.syscall_service_latency = 7;
    config.syscall_restart_latency = 4;
    config.validate();

    fastsim::IntervalCoreModel model(config);
    fastsim::TraceRecord older_load;
    older_load.address = 0x8000;
    older_load.size = 8;
    older_load.flags = fastsim::kRetires | fastsim::kLoad |
                       fastsim::kPhysicalAddress;
    const auto older = model.schedule(older_load, false);

    fastsim::TraceRecord syscall;
    syscall.pc = 0x1004;
    syscall.op_class = fastsim::kSyscallOpClass;
    syscall.flags = fastsim::kRetires;
    const auto system = model.schedule(syscall, false);

    fastsim::TraceRecord follower;
    follower.pc = 0x1008;
    const auto resumed = model.schedule(follower, false);
    check(system.rename_cycle >= older.retire_cycle + 1,
          "syscall must wait for every older UOP to retire before rename");
    check(system.completion_cycle >=
              system.execute_cycle + config.system_latency +
                  config.syscall_service_latency,
          "syscall must consume system-FU plus explicit SE service latency");
    check(system.syscall_service_cycles ==
              config.syscall_service_latency &&
              system.syscall_restart_cycles ==
                  config.syscall_restart_latency,
          "syscall timing components must remain auditable");
    check(resumed.fetch_cycle >=
              system.retire_cycle + config.syscall_restart_latency,
          "post-syscall fetch must honor the configured restart latency");
}

std::uint64_t syscall_service_span(bool cost_model, std::uint64_t sysnum,
                                   std::uint32_t table_cycles,
                                   std::uint32_t scalar_cycles) {
    fastsim::SimulatorConfig config;
    config.core_model = "interval_bound";
    config.system_latency = 3;
    config.syscall_service_latency = scalar_cycles;
    config.syscall_restart_latency = 4;
    config.syscall_cost_model = cost_model;
    // The table only ever maps sysnum 202; other numbers exercise the fallback.
    config.syscall_cost_table[202] = table_cycles;
    config.validate();

    fastsim::IntervalCoreModel model(config);
    fastsim::TraceRecord syscall;
    syscall.pc = 0x1004;
    syscall.op_class = fastsim::kSyscallOpClass;
    syscall.flags = fastsim::kRetires;
    syscall.set_syscall_number(sysnum);
    const auto system = model.schedule(syscall, false);
    return system.syscall_service_cycles;
}

void test_syscall_cost_model() {
    // Model on: the per-sysnum table value is charged, not the scalar.
    check(syscall_service_span(true, 202, 40, 7) == 40,
          "cost model must charge the per-sysnum table value");
    // Model on but sysnum absent from table: scalar fallback.
    check(syscall_service_span(true, /*sysnum*/ 999, /*table for 202*/ 40, 7) ==
              7,
          "unknown sysnum must fall back to the scalar service latency");
    // Model off: scalar regardless of a populated table (reference behavior).
    check(syscall_service_span(false, 202, 40, 7) == 7,
          "cost model off must reproduce the scalar service latency");

    fastsim::SimulatorConfig scalar;
    scalar.syscall_service_latency = 7;
    scalar.syscall_cost_model = true;
    scalar.syscall_cost_table[202] = 40;
    fastsim::TraceRecord syscall;
    syscall.op_class = fastsim::kSyscallOpClass;
    syscall.set_syscall_number(202);
    check(scalar.syscall_service_cycles(syscall.syscall_number()) == 40,
          "shared syscall lookup must serve the scalar compatibility path");
    syscall.set_syscall_number(999);
    check(scalar.syscall_service_cycles(syscall.syscall_number()) == 7,
          "shared syscall lookup must preserve scalar fallback");
    scalar.syscall_cost_model = false;
    syscall.set_syscall_number(202);
    check(scalar.syscall_service_cycles(syscall.syscall_number()) == 7,
          "shared syscall lookup must preserve model-off behavior");
}

void test_syscall_kernel_event_model() {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_bound";
    config.chunk_instructions = 4;
    config.syscall_service_latency = 7;
    config.syscall_restart_latency = 1;
    config.syscall_cost_model = true;
    config.syscall_cost_table[202] = 99;
    config.syscall_kernel_event_model = true;
    fastsim::SyscallKernelEventProfile profile;
    profile.service_cycles = 40;
    profile.blocked_wall_cycles = 900;
    profile.retired_instructions = 120;
    profile.retired_uops = 180;
    profile.memory_uops = 60;
    profile.line_requests = 80;
    profile.branches = 30;
    profile.branch_misses = 2;
    profile.l1d_accesses = 80;
    profile.l1d_misses = 8;
    profile.l2_accesses = 8;
    profile.l2_misses = 3;
    profile.llc_accesses = 8;
    profile.llc_misses = 1;
    profile.permission_upgrades = 5;
    profile.remote_supplies = 2;
    profile.llc_unique_fills = 1;
    profile.dram_reads = 1;
    profile.dram_writes = 3;
    profile.dtlb_accesses = 50;
    profile.dtlb_misses = 4;
    config.syscall_kernel_event_table[202] = profile;
    config.validate();

    fastsim::TraceRecord user;
    user.pc = 0x1000;
    fastsim::TraceRecord modeled = user;
    modeled.pc = 0x1004;
    modeled.op_class = fastsim::kSyscallOpClass;
    modeled.set_syscall_number(202);
    fastsim::TraceRecord fallback = modeled;
    fallback.pc = 0x1008;
    fallback.set_syscall_number(999);
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{user, modeled, fallback, user}));
    fastsim::Simulator simulator(config, std::move(traces));
    const auto stats = simulator.run();
    const auto total = stats.total_core();
    const auto& kernel = total.syscall_kernel;
    check(total.retired_instructions == 4 && total.syscall_uops == 2,
          "synthetic kernel PMU must not alter functional user counts");
    check(total.syscall_service_cycles == 47,
          "event-profile service must precede cost-table and scalar fallback");
    check(kernel.events == 1 && kernel.active_cycles == 40 &&
              kernel.blocked_wall_cycles == 900 &&
              kernel.retired_instructions == 120 &&
              kernel.retired_uops == 180 && kernel.memory_uops == 60 &&
              kernel.line_requests == 80,
          "known sysnum must emit exactly one synthetic kernel event");
    check(kernel.branch.branches == 30 && kernel.branch.misses == 2 &&
              kernel.l1d.accesses == 80 && kernel.l1d.hits == 72 &&
              kernel.l1d.misses == 8 && kernel.l2.accesses == 8 &&
              kernel.l2.hits == 5 && kernel.llc.accesses == 8 &&
              kernel.llc.hits == 0 && kernel.llc.misses == 1 &&
              kernel.permission_upgrades == 5 &&
              kernel.remote_supplies == 2 &&
              kernel.llc_unique_fills == 1 && kernel.dram_reads == 1 &&
              kernel.dram_writes == 3 && kernel.dtlb.accesses == 50 &&
              kernel.dtlb.hits == 46,
          "synthetic kernel PMU accesses and misses must conserve hits");
    check(total.page_fault_kernel.events == 0 &&
              total.irq_kernel.events == 0,
          "future fault/IRQ domains must remain empty until modeled");

    auto defaulted = config;
    defaulted.syscall_kernel_event_default_profile_enabled = true;
    defaulted.syscall_kernel_event_default_profile.service_cycles = 7;
    defaulted.validate();
    std::vector<std::unique_ptr<fastsim::TraceSource>> default_traces;
    default_traces.push_back(std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{user, modeled, fallback, user}));
    fastsim::Simulator default_simulator(
        defaulted, std::move(default_traces));
    const auto default_total = default_simulator.run().total_core();
    check(default_total.syscall_kernel.events == 2 &&
              default_total.syscall_kernel.active_cycles == 47 &&
              default_total.syscall_service_cycles == 47,
          "unknown sysnum must use a configured default event profile");

    auto disabled = config;
    disabled.syscall_kernel_event_model = false;
    check(disabled.syscall_service_cycles(202) == 99 &&
              disabled.syscall_kernel_event_profile(202) == nullptr,
          "event-model off must restore legacy timing and zero PMU injection");

    auto invalid = config;
    invalid.syscall_kernel_event_table[202].branch_misses = 31;
    bool rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected, "inconsistent synthetic kernel PMU must be rejected");
}

void test_page_fault_kernel_event_model() {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_bound";
    config.chunk_instructions = 8;
    config.require_virtual_page_token = true;
    config.page_fault_event_model = true;
    config.page_fault_probability_ppm = 500'000;
    auto& profile = config.page_fault_event_profile;
    profile.service_cycles = 20;
    profile.retired_instructions = 30;
    profile.retired_uops = 40;
    profile.memory_uops = 7;
    profile.line_requests = 10;
    profile.branches = 5;
    profile.branch_misses = 1;
    profile.l1d_accesses = 10;
    profile.l1d_misses = 2;
    profile.l2_accesses = 2;
    profile.l2_misses = 1;
    profile.llc_accesses = 5;
    profile.llc_misses = 1;
    profile.permission_upgrades = 2;
    profile.remote_supplies = 1;
    profile.llc_merged_misses = 1;
    profile.dtlb_accesses = 8;
    profile.dtlb_misses = 2;
    config.validate();

    const auto make_access = [](std::uint64_t pc, std::uint64_t address,
                                std::uint32_t token, bool write = false) {
        fastsim::TraceRecord load;
        load.pc = pc;
        load.address = address;
        load.size = 8;
        load.flags = fastsim::kRetires |
            (write ? fastsim::kStore : fastsim::kLoad) |
            fastsim::kPhysicalAddress | fastsim::kVirtualPageToken;
        load.reserved = token;
        return load;
    };
    const std::vector<fastsim::TraceRecord> records{
        make_access(0x1000, 0x1000, 1),
        make_access(0x1004, 0x1040, 1),
        make_access(0x1008, 0x2000, 2),
        make_access(0x100c, 0x2040, 2)};
    const auto run = [&records](fastsim::SimulatorConfig candidate) {
        std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
        traces.push_back(std::make_unique<VectorTraceSource>(records));
        fastsim::Simulator simulator(candidate, std::move(traces));
        return simulator.run().total_core();
    };
    const auto modeled = run(config);
    auto disabled = config;
    disabled.page_fault_event_model = false;
    const auto reference = run(disabled);
    check(modeled.retired_uops == reference.retired_uops &&
              modeled.retired_instructions ==
                  reference.retired_instructions &&
              modeled.memory_accesses == reference.memory_accesses,
          "page-fault model must preserve the functional user stream");
    check(modeled.page_fault_first_touch_candidates == 2 &&
              modeled.page_fault_first_touch_write_candidates == 0 &&
              modeled.page_fault_background_candidates == 2 &&
              modeled.page_fault_background_read_candidates == 2 &&
              modeled.page_fault_background_write_candidates == 0 &&
              modeled.page_fault_allocation_candidates == 0 &&
              modeled.page_fault_untracked_accesses == 0 &&
              modeled.page_fault_kernel.events == 1 &&
              modeled.page_fault_kernel.active_cycles == 20,
          "integer first-touch probability must select deterministically");
    check(reference.page_fault_first_touch_candidates == 2 &&
              reference.page_fault_background_candidates == 2 &&
              reference.page_fault_allocation_candidates == 0 &&
              reference.page_fault_untracked_accesses == 0 &&
              reference.page_fault_kernel.events == 0 &&
              reference.page_fault_kernel.active_cycles == 0,
          "model-off run must still expose first-touch calibration facts");
    check(modeled.page_fault_kernel.retired_instructions == 30 &&
              modeled.page_fault_kernel.memory_uops == 7 &&
              modeled.page_fault_kernel.line_requests == 10 &&
              modeled.page_fault_kernel.l1d.accesses == 10 &&
              modeled.page_fault_kernel.l1d.hits == 8 &&
              modeled.page_fault_kernel.permission_upgrades == 2 &&
              modeled.page_fault_kernel.remote_supplies == 1 &&
              modeled.page_fault_kernel.llc_merged_misses == 1 &&
              modeled.page_fault_kernel.dtlb.accesses == 8 &&
              modeled.page_fault_kernel.dtlb.hits == 6,
          "page-fault synthetic PMU bundle must conserve events");
    check(modeled.cycles >= reference.cycles + 20,
          "minor-fault active service must enter the core time line");

    auto state_only_config = config;
    state_only_config.page_fault_event_model = false;
    state_only_config.page_fault_cache_state_model = true;
    state_only_config.page_fault_probability_ppm = 1'000'000;
    state_only_config.validate();
    const auto state_only = run(state_only_config);
    check(state_only.page_fault_cache_state_pages == 2 &&
              state_only.page_fault_cache_state_lines == 128 &&
              state_only.page_fault_kernel.events == 0 &&
              state_only.page_fault_kernel.active_cycles == 0,
          "state-only page fills must remain outside kernel time and PMU");
    check(state_only.l1d.accesses == reference.l1d.accesses &&
              state_only.l1d.misses == 0 &&
              state_only.l2.accesses == 0,
          "state-only page fill must warm user-visible cache state before "
          "the first demand without creating user PMU accesses");

    auto state_missing_tokens = state_only_config;
    state_missing_tokens.require_virtual_page_token = false;
    bool state_rejected = false;
    try {
        state_missing_tokens.validate();
    } catch (const std::invalid_argument&) {
        state_rejected = true;
    }
    check(state_rejected,
          "page-fault cache-state model must require virtual-page tokens");

    auto missing_tokens = config;
    missing_tokens.require_virtual_page_token = false;
    bool rejected = false;
    try {
        missing_tokens.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected,
          "page-fault model must reject traces without required page tokens");

    auto allocation_config = config;
    allocation_config.page_fault_probability_ppm = 0;
    allocation_config.page_fault_allocation_syscalls.insert(9);
    allocation_config.page_fault_allocation_syscalls.insert(12);
    allocation_config.page_fault_allocation_probability_table[9] = {
        1'000'000, 0};
    allocation_config.page_fault_allocation_probability_table[12] = {
        0, 1'000'000};
    allocation_config.page_fault_allocation_window_records = 1;
    allocation_config.validate();
    fastsim::TraceRecord mmap_marker;
    mmap_marker.pc = 0x2000;
    mmap_marker.op_class = fastsim::kSyscallOpClass;
    mmap_marker.set_syscall_number(9);
    auto brk_marker = mmap_marker;
    brk_marker.pc = 0x2010;
    brk_marker.set_syscall_number(12);
    std::vector<std::unique_ptr<fastsim::TraceSource>> allocation_traces;
    allocation_traces.push_back(std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{
            mmap_marker,
            make_access(0x2004, 0x3000, 3),
            make_access(0x2008, 0x4000, 4),
            brk_marker,
            make_access(0x2014, 0x5000, 5),
            brk_marker,
            make_access(0x2018, 0x6000, 6, true)}));
    fastsim::Simulator allocation_simulator(
        allocation_config, std::move(allocation_traces));
    const auto allocation_total =
        allocation_simulator.run().total_core();
    check(allocation_total.page_fault_first_touch_candidates == 4 &&
              allocation_total.page_fault_first_touch_write_candidates == 1 &&
              allocation_total.page_fault_background_candidates == 1 &&
              allocation_total.page_fault_background_read_candidates == 1 &&
              allocation_total.page_fault_allocation_candidates == 3 &&
              allocation_total
                      .page_fault_allocation_recency_candidates[0] == 4 &&
              allocation_total.page_fault_allocation_by_syscall.at(9)
                      .recency_candidates[0] == 2 &&
              allocation_total.page_fault_allocation_by_syscall.at(12)
                      .recency_candidates[0] == 2 &&
              allocation_total.page_fault_allocation_by_syscall.at(12)
                      .recency_write_candidates[0] == 1 &&
              allocation_total.page_fault_kernel.events == 2,
          "allocation syscall identity, access type, and recency window must "
          "select independent deterministic channels");
}

void test_periodic_irq_kernel_event_model() {
    fastsim::SimulatorConfig base;
    base.cores = 1;
    base.core_model = "interval_bound";
    base.chunk_instructions = 128;
    std::vector<fastsim::TraceRecord> records(128);
    for (std::size_t index = 0; index < records.size(); ++index) {
        records[index].pc = 0x4000 + 4 * index;
        records[index].address = 0x1000;
        records[index].size = 8;
        records[index].flags = fastsim::kRetires | fastsim::kLoad |
            fastsim::kPhysicalAddress | fastsim::kVirtualPageToken;
        records[index].reserved = 1;
    }
    const auto run = [&records](fastsim::SimulatorConfig candidate) {
        std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
        traces.push_back(std::make_unique<VectorTraceSource>(records));
        fastsim::Simulator simulator(candidate, std::move(traces));
        return simulator.run().total_core();
    };
    const auto reference = run(base);

    auto modeled_config = base;
    modeled_config.irq_event_model = true;
    modeled_config.irq_period_cycles = 10;
    auto& profile = modeled_config.irq_event_profile;
    profile.service_cycles = 7;
    profile.retired_instructions = 9;
    profile.retired_uops = 12;
    profile.branches = 2;
    profile.branch_misses = 1;
    profile.l1d_accesses = 4;
    profile.l1d_misses = 1;
    profile.dtlb_accesses = 3;
    profile.dtlb_misses = 1;
    modeled_config.validate();
    const auto modeled = run(modeled_config);
    const auto expected_events = reference.cycles /
        modeled_config.irq_period_cycles;
    check(expected_events != 0 &&
              modeled.irq_kernel.events == expected_events &&
              modeled.irq_kernel.active_cycles == expected_events * 7 &&
              modeled.cycles == reference.cycles + expected_events * 7,
          "periodic IRQ must use foreground cycles and charge service once");
    check(modeled.retired_uops == reference.retired_uops &&
              modeled.retired_instructions ==
                  reference.retired_instructions,
          "periodic IRQ must not create functional user UOPs");
    check(modeled.irq_kernel.retired_uops == expected_events * 12 &&
              modeled.irq_kernel.l1d.hits == expected_events * 3 &&
              modeled.irq_kernel.dtlb.hits == expected_events * 2,
          "periodic IRQ PMU bundle must scale by deterministic event count");

    auto with_page_fault = modeled_config;
    with_page_fault.require_virtual_page_token = true;
    with_page_fault.page_fault_event_model = true;
    with_page_fault.page_fault_probability_ppm = 1'000'000;
    with_page_fault.page_fault_event_profile.service_cycles = 1'000;
    with_page_fault.validate();
    const auto faulted = run(with_page_fault);
    check(faulted.page_fault_kernel.events == 1 &&
              faulted.page_fault_kernel.active_cycles == 1'000 &&
              faulted.irq_kernel.events == expected_events,
          "page-fault service must not advance the foreground IRQ clock");

    auto invalid = modeled_config;
    invalid.irq_period_cycles = 0;
    bool rejected = false;
    try {
        invalid.validate();
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    check(rejected, "enabled periodic IRQ must require a nonzero period");
}

fastsim::TraceRecord branch_record(
    std::uint64_t pc, std::uint64_t next_pc, bool taken = true,
    bool conditional = false, bool indirect = false,
    bool call = false, bool is_return = false) {
    fastsim::TraceRecord record;
    record.pc = pc;
    record.target = taken ? next_pc : 0;
    record.next_pc = next_pc;
    record.flags = fastsim::kRetires | fastsim::kBranch |
                   fastsim::kBranchOutcomeValid;
    if (taken) record.flags = record.flags | fastsim::kTaken;
    if (conditional) record.flags = record.flags | fastsim::kConditional;
    if (indirect) record.flags = record.flags | fastsim::kIndirect;
    if (call) record.flags = record.flags | fastsim::kCall;
    if (is_return) record.flags = record.flags | fastsim::kReturn;
    return record;
}

fastsim::BranchConfig small_branch_config() {
    fastsim::BranchConfig config;
    config.type = "tournament";
    config.local_entries = 8;
    config.local_history_entries = 8;
    config.global_entries = 8;
    config.choice_entries = 8;
    config.btb_entries = 8;
    config.btb_associativity = 1;
    config.btb_tag_bits = 8;
    config.ras_entries = 4;
    config.indirect_sets = 8;
    config.indirect_ways = 1;
    config.indirect_tag_bits = 8;
    config.indirect_path_length = 2;
    config.indirect_speculative_path_length = 8;
    config.indirect_ghr_bits = 3;
    return config;
}

void test_branch_speculative_history_checkpoint() {
    auto config = small_branch_config();
    config.type = "gshare";
    config.global_entries = 1;
    config.global_counter_bits = 2;
    fastsim::BranchPredictor predictor(config);
    fastsim::BranchCounters counters;

    // Populate the direct target and leave the single direction counter weakly
    // not-taken. Immediate training would make the second conditional lookup
    // taken; the deferred model must keep both lookups on the pre-commit table.
    const auto warm = predictor.process(
        branch_record(0x10, 0x80), counters);
    check(warm.miss, "cold direct target must establish the BTB fixture");

    const auto branch = branch_record(0x10, 0x80, true, true);
    predictor.advance_to(1);
    const auto first = predictor.predict_speculative(branch, counters);
    predictor.schedule_commit(first.sequence, 10);
    predictor.advance_to(2);
    const auto second = predictor.predict_speculative(branch, counters);
    predictor.schedule_commit(second.sequence, 11);
    check(first.miss && second.miss && predictor.pending_commits() == 2,
          "younger Fetch must see stale direction counters while older "
          "branches remain uncommitted");

    predictor.advance_to(10);
    check(predictor.pending_commits() == 2,
          "same-cycle Fetch must precede Commit table visibility");
    predictor.advance_to(11);
    check(predictor.pending_commits() == 1,
          "the first direction update must become visible to the following "
          "Fetch cycle");
    const auto third = predictor.predict_speculative(branch, counters);
    predictor.schedule_commit(third.sequence, 12);
    check(!third.miss,
          "a later Fetch must observe the committed training");
    predictor.drain();

    check(predictor.pending_commits() == 0 &&
              counters.history_checkpoints == 3 &&
              counters.deferred_direction_commits == 3 &&
              counters.history_squashes == 2 &&
              counters.direction_only_misses == 2 &&
              counters.target_unavailable_misses == 1 &&
              counters.wrong_target_misses == 0 &&
              counters.miss_population_conserved(),
          "checkpoint/squash and mutually exclusive miss ledgers must "
          "conserve");
}

void test_branch_golden_direct_target() {
    fastsim::BranchPredictor predictor(small_branch_config());
    fastsim::BranchCounters counters;
    const auto first =
        predictor.process(branch_record(0x10, 0x80), counters);
    const auto second =
        predictor.process(branch_record(0x10, 0x80), counters);
    const auto changed =
        predictor.process(branch_record(0x10, 0x90), counters);
    const auto fourth =
        predictor.process(branch_record(0x10, 0x90), counters);
    check(first.miss, "cold direct branch must miss without BTB target");
    check(!second.miss, "warm direct branch must hit");
    check(changed.target_miss && changed.miss,
          "changed direct target must miss");
    check(!fourth.miss, "updated direct target must hit");
    check(counters.misses == 2, "direct target golden miss count");
}

void test_branch_golden_ras_learning() {
    fastsim::BranchPredictor predictor(small_branch_config());
    fastsim::BranchCounters counters;
    const auto first_call = predictor.process(
        branch_record(0x100, 0x500, true, false, false, true), counters);
    const auto first_return = predictor.process(
        branch_record(0x700, 0x105, true, false, true, false, true),
        counters);
    const auto second_call = predictor.process(
        branch_record(0x100, 0x500, true, false, false, true), counters);
    const auto second_return = predictor.process(
        branch_record(0x700, 0x105, true, false, true, false, true),
        counters);
    check(first_call.miss && first_return.miss,
          "cold call/return must expose unknown targets");
    check(!second_call.miss && !second_return.miss,
          "causally learned RAS target must hit");
    check(counters.ras_hits == 1 && counters.ras_pushes == 2 &&
              counters.ras_pops == 2 && counters.ras_predictions == 1 &&
              counters.ras_static_return_targets == 0 &&
              counters.ras_learned_return_targets == 1 &&
              counters.ras_unknown_return_targets == 1 &&
              counters.ras_source_conserved(),
          "RAS causal fallback must expose conserved source coverage");
}

void test_branch_golden_ras_static_fallthrough() {
    auto config = small_branch_config();
    fastsim::BranchPredictor predictor(config);
    fastsim::BranchCounters counters;

    fastsim::StaticInstructionInfo outer_call;
    outer_call.pc = 0x100;
    outer_call.size = 5;
    outer_call.fallthrough_pc = 0x105;
    outer_call.direct_target = 0x500;
    outer_call.flags = fastsim::kStaticBranch | fastsim::kStaticCall |
        fastsim::kStaticDirectTargetValid;
    fastsim::StaticInstructionInfo inner_call;
    inner_call.pc = 0x500;
    inner_call.size = 2;
    inner_call.fallthrough_pc = 0x502;
    inner_call.direct_target = 0x900;
    inner_call.flags = fastsim::kStaticBranch | fastsim::kStaticCall |
        fastsim::kStaticDirectTargetValid;
    StaticMapTraceSource static_map({outer_call, inner_call});

    const auto outer = predictor.process(
        branch_record(0x100, 0x500, true, false, false, true),
        counters, &static_map);
    const auto inner = predictor.process(
        branch_record(0x500, 0x900, true, false, false, true),
        counters, &static_map);
    const auto inner_return = predictor.process(
        branch_record(0xA00, 0x502, true, false, true, false, true),
        counters, &static_map);
    const auto outer_return = predictor.process(
        branch_record(0xA04, 0x105, true, false, true, false, true),
        counters, &static_map);

    check(outer.miss && inner.miss,
          "cold calls must still expose their independent BTB misses");
    check(!inner_return.miss && !outer_return.miss,
          "static architectural fallthrough PCs must make first nested "
          "returns predictable without future return outcomes");
    check(counters.ras_pushes == 2 && counters.ras_pops == 2 &&
              counters.ras_predictions == 2 && counters.ras_hits == 2 &&
              counters.ras_static_return_targets == 2 &&
              counters.ras_learned_return_targets == 0 &&
              counters.ras_unknown_return_targets == 0 &&
              counters.ras_source_conserved(),
          "static RAS source and prediction coverage must conserve");

    config.ras_static_return_target = false;
    fastsim::BranchPredictor disabled(config);
    fastsim::BranchCounters disabled_counters;
    (void)disabled.process(
        branch_record(0x100, 0x500, true, false, false, true),
        disabled_counters, &static_map);
    const auto disabled_return = disabled.process(
        branch_record(0xA00, 0x105, true, false, true, false, true),
        disabled_counters, &static_map);
    check(disabled_return.miss &&
              disabled_counters.ras_static_return_targets == 0 &&
              disabled_counters.ras_unknown_return_targets == 1 &&
              disabled_counters.ras_source_conserved(),
          "disabling static call fallthrough must preserve the explicit "
          "causal-learning fallback");
}

void test_branch_golden_ras_address_space_isolation() {
    auto config = small_branch_config();
    config.ras_static_return_target = false;
    fastsim::BranchPredictor predictor(config);
    fastsim::BranchCounters counters;
    AddressSpaceTraceSource source;
    const auto call = branch_record(
        0x100, 0x500, true, false, false, true);

    source.set_address_space_id(7);
    (void)predictor.process(call, counters, &source);
    const auto first_as7_return = predictor.process(
        branch_record(0xA00, 0x105, true, false, true, false, true),
        counters, &source);

    source.set_address_space_id(11);
    (void)predictor.process(call, counters, &source);
    const auto first_as11_return = predictor.process(
        branch_record(0xA00, 0x107, true, false, true, false, true),
        counters, &source);

    source.set_address_space_id(7);
    (void)predictor.process(call, counters, &source);
    const auto second_as7_return = predictor.process(
        branch_record(0xA00, 0x105, true, false, true, false, true),
        counters, &source);

    source.set_address_space_id(11);
    (void)predictor.process(call, counters, &source);
    const auto second_as11_return = predictor.process(
        branch_record(0xA00, 0x107, true, false, true, false, true),
        counters, &source);

    check(first_as7_return.miss && first_as11_return.miss &&
              !second_as7_return.miss && !second_as11_return.miss,
          "causal return targets must learn independently per address "
          "space");
    check(counters.ras_pushes == 4 &&
              counters.ras_static_return_targets == 0 &&
              counters.ras_learned_return_targets == 2 &&
              counters.ras_unknown_return_targets == 2 &&
              counters.ras_predictions == 2 && counters.ras_hits == 2 &&
              counters.ras_source_conserved(),
          "address-space-scoped RAS learning must conserve sources");
}

void test_branch_golden_indirect_learning() {
    fastsim::BranchPredictor predictor(small_branch_config());
    fastsim::BranchCounters counters;
    auto event =
        branch_record(0x20, 0xA0, true, false, true);
    const auto cold = predictor.process(event, counters);
    for (int index = 0; index < 3; ++index) {
        predictor.process(event, counters);
    }
    const auto warm = predictor.process(event, counters);
    check(cold.miss, "cold indirect branch must miss");
    check(warm.target_available && !warm.miss,
          "indirect predictor must reuse a causally learned target");
}

void test_trace_roundtrip() {
    const auto json_path = test_tmp_path("fastsim_test_trace.jsonl");
    const auto binary_path = test_tmp_path("fastsim_test_trace.fst");
    {
        std::ofstream output(json_path);
        output
            << "{\"macro_pc\":4096,\"address_space_id\":7,"
               "\"vaddr\":16384,"
               "\"paddr\":8192,\"size\":8,"
               "\"is_load\":1,\"is_store\":0,\"is_atomic\":0,"
               "\"is_branch\":0,\"is_branch_cond\":0,"
               "\"is_branch_indirect\":0,\"is_call\":0,"
               "\"is_return\":0,\"branch_taken\":0,"
               "\"is_microop\":0,\"is_last_microop\":1,"
               "\"initial_pte_state_valid\":1,"
               "\"initial_pte_present\":1,"
               "\"roi_entry_page_state_valid\":1,"
               "\"roi_entry_page_present\":0,"
               "\"roi_entry_inflight_page_fault\":1,"
               "\"op_class\":56,\"n_src\":2,\"n_dst\":1,"
               "\"producer_dists\":[1,7,0,0],"
               "\"producer_classes\":[0,1,255,255],"
               "\"destination_class_counts\":[1,0,0,0]}\n";
    }
    fastsim::convert_gem5_jsonl_to_binary(json_path, binary_path, 0);
    fastsim::BinaryTraceSource input(binary_path);
    fastsim::TraceRecord record;
    check(input.next(record), "binary trace has record");
    check(record.pc == 4096 && record.address == 8192 &&
              input.current_address_space_id() == 7 &&
              std::filesystem::file_size(binary_path + ".asmap") == 64,
          "trace values survive conversion");
    check(record.is_memory() &&
              fastsim::has_flag(record.flags, fastsim::kPhysicalAddress),
          "trace flags survive conversion");
    check(fastsim::has_flag(record.flags,
                            fastsim::kVirtualPageToken) &&
              record.reserved != 0,
          "current trace keeps virtual-page identity beside physical address");
    check(record.op_class == 56 && record.n_src == 2 &&
              record.n_dst == 1 && record.producer_dists[0] == 1 &&
              record.producer_dists[1] == 7 &&
              record.producer_class(1) == 1 &&
              record.has_destination_class_counts() &&
              record.destination_class_count(0) == 1 &&
              record.virtual_page_token() != 0,
          "interval-core functional fields survive conversion");
    const auto* page_mapping =
        input.virtual_page_mapping(record.virtual_page_token());
    check(page_mapping != nullptr && page_mapping->token ==
              record.virtual_page_token() &&
              page_mapping->first_record_ordinal == 0 &&
              page_mapping->virtual_page == 4 &&
              page_mapping->physical_page_valid &&
              page_mapping->physical_page == 2 &&
              page_mapping->initial_pte_state_valid &&
              page_mapping->initial_pte_present &&
              page_mapping->roi_entry_page_state_valid &&
              !page_mapping->roi_entry_page_present &&
              page_mapping->roi_entry_inflight_page_fault &&
              std::filesystem::file_size(binary_path + ".vmap") == 80,
          "FST companion map must recover token-to-virtual-page identity");
    check(!input.next(record), "binary trace record count");
    std::remove(json_path.c_str());
    std::remove(binary_path.c_str());
    std::remove((binary_path + ".vmap").c_str());
    std::remove((binary_path + ".asmap").c_str());
}

void test_trace_page_table_path_roundtrip() {
    const auto json_path =
        test_tmp_path("fastsim_test_trace_pte_path.jsonl");
    const auto binary_path =
        test_tmp_path("fastsim_test_trace_pte_path.fst");
    {
        std::ofstream output(json_path);
        output
            << "{\"macro_pc\":4096,\"address_space_id\":7,"
               "\"vaddr\":16384,\"paddr\":8192,\"size\":8,"
               "\"is_load\":1,\"op_class\":56,"
               "\"initial_page_table_path_valid\":1,"
               "\"initial_page_table_levels\":4,"
               "\"initial_page_size_bits\":12,"
               "\"initial_pte_physical_addresses\":"
               "[4104,8256,12672,18984],"
               "\"roi_entry_page_table_path_valid\":1,"
               "\"roi_entry_page_table_levels\":4,"
               "\"roi_entry_page_size_bits\":12,"
               "\"roi_entry_pte_physical_addresses\":"
               "[20488,24640,29056,35368]}\n";
    }

    fastsim::convert_gem5_jsonl_to_binary(json_path, binary_path, 0);
    fastsim::BinaryTraceSource input(binary_path);
    fastsim::TraceRecord record;
    check(input.next(record) && !input.next(record),
          "v2 PTE-path trace must preserve its hot record");
    const auto* mapping =
        input.virtual_page_mapping(record.virtual_page_token());
    check(mapping != nullptr &&
              mapping->initial_page_table_path.valid &&
              mapping->initial_page_table_path.levels == 4 &&
              mapping->initial_page_table_path.page_size_bits == 12 &&
              mapping->initial_page_table_path
                      .pte_physical_addresses[0] == 4104 &&
              mapping->initial_page_table_path
                      .pte_physical_addresses[3] == 18984 &&
              mapping->roi_entry_page_table_path.valid &&
              mapping->roi_entry_page_table_path.levels == 4 &&
              mapping->roi_entry_page_table_path.page_size_bits == 12 &&
              mapping->roi_entry_page_table_path
                      .pte_physical_addresses[0] == 20488 &&
              mapping->roi_entry_page_table_path
                      .pte_physical_addresses[3] == 35368 &&
              std::filesystem::file_size(binary_path + ".vmap") ==
                  48 + 168,
          "FST virtual-page map v2 must round-trip exact functional PTE "
          "paths without timing labels");

    std::remove(json_path.c_str());
    std::remove(binary_path.c_str());
    std::remove((binary_path + ".vmap").c_str());
    std::remove((binary_path + ".asmap").c_str());
}

void test_trace_late_roi_page_state_roundtrip() {
    const auto json_path =
        test_tmp_path("fastsim_test_late_roi_page_state.jsonl");
    const auto binary_path =
        test_tmp_path("fastsim_test_late_roi_page_state.fst");
    {
        std::ofstream output(json_path);
        output
            << "{\"macro_pc\":4096,\"address_space_id\":7,"
               "\"vaddr\":16384,\"paddr\":8192,\"size\":8,"
               "\"is_load\":1,\"op_class\":56}\n"
            << "{\"macro_pc\":4100,\"address_space_id\":7,"
               "\"vaddr\":16384,\"paddr\":8192,\"size\":8,"
               "\"is_load\":1,\"op_class\":56,"
               "\"measurement_pte_state_valid\":1,"
               "\"measurement_pte_present\":1,"
               "\"measurement_boundary_inflight_fault\":1}\n";
    }

    fastsim::convert_gem5_jsonl_to_binary(json_path, binary_path, 0);
    fastsim::BinaryTraceSource input(binary_path);
    fastsim::TraceRecord first;
    fastsim::TraceRecord second;
    check(input.next(first) && input.next(second) && !input.next(second) &&
              first.virtual_page_token() == second.virtual_page_token(),
          "late page-state enrichment must preserve one page identity");
    const auto* mapping =
        input.virtual_page_mapping(first.virtual_page_token());
    check(mapping != nullptr &&
              mapping->first_record_ordinal == 0 &&
              mapping->roi_entry_page_state_valid &&
              mapping->roi_entry_page_present &&
              mapping->roi_entry_inflight_page_fault,
          "JSONL conversion must retain page state learned after first use");

    std::remove(json_path.c_str());
    std::remove(binary_path.c_str());
    std::remove((binary_path + ".vmap").c_str());
    std::remove((binary_path + ".asmap").c_str());
}

void test_privilege_trace_roundtrip() {
    const auto json_path =
        test_tmp_path("fastsim_test_privilege_trace.jsonl");
    const auto binary_path =
        test_tmp_path("fastsim_test_privilege_trace.fst");
    {
        std::ofstream output(json_path);
        output << "{\"pc\":4096,\"cpl\":3,\"op_class\":7}\n"
               << "{\"pc\":8192,\"cpl\":0,\"op_class\":56,"
                  "\"paddr\":12288,\"size\":8,\"is_load\":1}\n";
    }
    fastsim::convert_gem5_jsonl_to_binary(
        json_path, binary_path, 0);

    std::ifstream header(binary_path, std::ios::binary);
    header.seekg(32);
    std::uint64_t feature_flags = 0;
    header.read(
        reinterpret_cast<char*>(&feature_flags), sizeof(feature_flags));
    check((feature_flags & (1ull << 4)) != 0,
          "privilege-tagged trace must declare the v7 feature bit");

    fastsim::BinaryTraceSource input(binary_path);
    fastsim::TraceRecord record;
    check(input.next(record) && !record.is_kernel() &&
              record.canonical_op_class() == 7,
          "CPL3 record must remain byte-compatible");
    check(input.next(record) && record.is_kernel() &&
              record.op_class == -58 &&
              record.canonical_op_class() == 56 && record.is_memory(),
          "CPL0 record must round-trip through negative OpClass encoding");
    check(!input.next(record),
          "privilege trace must conserve its record count");

    std::remove(json_path.c_str());
    std::remove(binary_path.c_str());

    const auto invalid_path =
        test_tmp_path("fastsim_test_privilege_without_feature.fst");
    fastsim::TraceRecord kernel;
    kernel.op_class = 1;
    kernel.set_kernel_mode(true);
    {
        fastsim::BinaryTraceWriter writer(invalid_path, 0);
        writer.append(kernel);
        writer.close();
    }
    {
        std::fstream file(
            invalid_path, std::ios::in | std::ios::out | std::ios::binary);
        file.seekg(32);
        std::uint64_t flags = 0;
        file.read(reinterpret_cast<char*>(&flags), sizeof(flags));
        flags &= ~(1ull << 4);
        file.seekp(32);
        file.write(reinterpret_cast<const char*>(&flags), sizeof(flags));
    }
    bool rejected = false;
    try {
        fastsim::BinaryTraceSource invalid(invalid_path);
        invalid.next(record);
    } catch (const std::runtime_error&) {
        rejected = true;
    }
    check(rejected,
          "kernel record without the privilege feature must fail closed");
    std::remove(invalid_path.c_str());

    const auto empty_native_path =
        test_tmp_path("fastsim_test_privilege_feature_user_only.fst");
    {
        fastsim::BinaryTraceWriter writer(empty_native_path, 0);
        fastsim::TraceRecord user;
        user.pc = 0x1000;
        writer.append(user);
        writer.close();
    }
    {
        std::fstream file(
            empty_native_path,
            std::ios::in | std::ios::out | std::ios::binary);
        file.seekg(32);
        std::uint64_t flags = 0;
        file.read(reinterpret_cast<char*>(&flags), sizeof(flags));
        flags |= 1ull << 4;
        file.seekp(32);
        file.write(reinterpret_cast<const char*>(&flags), sizeof(flags));
    }
    {
        fastsim::BinaryTraceSource empty_native(empty_native_path);
        check(empty_native.next(record) && !record.is_kernel() &&
                  !empty_native.next(record),
              "privilege capability must allow a per-core window with no "
              "kernel population");
    }
    fastsim::SimulatorConfig native_config;
    native_config.measurement_scope =
        fastsim::MeasurementScope::kUserPlusKernel;
    native_config.native_kernel_trace = true;
    native_config.syscall_restart_latency = 0;
    native_config.core_model = "scalar";
    std::vector<std::unique_ptr<fastsim::TraceSource>> empty_native_traces;
    empty_native_traces.push_back(
        std::make_unique<fastsim::BinaryTraceSource>(empty_native_path));
    fastsim::Simulator empty_native_simulator(
        native_config, std::move(empty_native_traces));
    const auto empty_native_stats = empty_native_simulator.run();
    check(empty_native_stats.total_core().native_kernel_records == 0,
          "declared native trace may have an empty kernel population");
    std::remove(empty_native_path.c_str());
}

void test_native_kernel_trace_replay() {
    fastsim::SimulatorConfig config;
    config.measurement_scope =
        fastsim::MeasurementScope::kUserPlusKernel;
    config.native_kernel_trace = true;
    config.syscall_restart_latency = 0;
    config.core_model = "scalar";
    config.validate();

    fastsim::TraceRecord user;
    user.pc = 0x1000;
    user.op_class = 1;

    fastsim::TraceRecord kernel_load;
    kernel_load.pc = 0xffff800000001000ull;
    kernel_load.address = 0x4000;
    kernel_load.size = 8;
    kernel_load.flags = fastsim::kRetires | fastsim::kLoad |
        fastsim::kPhysicalAddress;
    kernel_load.op_class = 56;
    kernel_load.set_kernel_mode(true);

    auto kernel_branch = branch_record(
        0xffff800000001004ull, 0xffff800000001100ull);
    kernel_branch.op_class = 7;
    kernel_branch.set_kernel_mode(true);

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{
            user, kernel_load, kernel_branch}));
    fastsim::Simulator simulator(config, std::move(traces));
    const auto stats = simulator.run();
    const auto total = stats.total_core();
    check(total.records == 3 && total.retired_uops == 3 &&
              total.native_kernel_records == 2 &&
              total.native_kernel_retired_uops == 2 &&
              total.native_kernel_retired_instructions == 2 &&
              total.native_kernel_memory_uops == 1 &&
              total.native_kernel_memory_accesses == 1 &&
              total.native_kernel_branch.branches == 1 &&
              stats.threads.size() == 1 &&
              stats.threads[0].native_kernel_retired_uops == 2,
          "native CPL0 records must replay in aggregate and conserve the "
          "privilege subset");
}

void test_address_space_map_roundtrip() {
    const auto binary_path =
        test_tmp_path("fastsim_test_address_space_map.fst");
    const auto upgraded_path =
        test_tmp_path("fastsim_test_address_space_map_upgraded.fst");
    {
        fastsim::BinaryTraceWriter output(binary_path, 3);
        fastsim::StaticInstructionInfo static_instruction;
        static_instruction.pc = 0x1000;
        static_instruction.fallthrough_pc = 0x1004;
        static_instruction.size = 4;
        output.register_static_instruction(static_instruction);
        output.set_static_instruction_map_complete();
        const auto append_load = [&](std::uint32_t token,
                                     std::uint64_t virtual_page) {
            fastsim::TraceRecord record;
            record.pc = 0x1000 + output.record_count() * 4;
            record.address = 0x20000;
            record.size = 8;
            record.flags = fastsim::kRetires | fastsim::kLoad |
                           fastsim::kPhysicalAddress |
                           fastsim::kVirtualPageToken;
            record.reserved = token;
            output.register_virtual_page_mapping(
                fastsim::VirtualPageMapping{
                    token, output.record_count(), virtual_page,
                    record.address >> 12, true});
            output.append(record);
        };
        output.set_address_space_id(7);
        append_load(1, 0x400);
        append_load(2, 0x401);
        output.set_address_space_id(11);
        append_load(3, 0x400);
        append_load(4, 0x401);
        output.close();
    }

    check(std::filesystem::file_size(binary_path + ".asmap") ==
              48 + 2 * 16,
          "sparse address-space map must use the frozen v1 header/row "
          "layout");
    {
        fastsim::BinaryTraceSource input(binary_path);
        const auto* transitions = input.address_space_transitions();
        check(transitions != nullptr && transitions->size() == 2 &&
                  (*transitions)[0].record_ordinal == 0 &&
                  (*transitions)[0].address_space_id == 7 &&
                  (*transitions)[1].record_ordinal == 2 &&
                  (*transitions)[1].address_space_id == 11 &&
                  input.address_space_id_for_record(0) == 7 &&
                  input.address_space_id_for_record(1) == 7 &&
                  input.address_space_id_for_record(2) == 11 &&
                  input.address_space_id_for_record(3) == 11,
              "binary trace must recover the exact address-space RLE");
        check(input.static_instruction(0x1000) == nullptr &&
                  !input.static_instruction_map_complete(),
              "PC-only imap facts must be disabled for multi-address-space "
              "streams");
        fastsim::TraceRecord record;
        check(input.next(record) && input.current_address_space_id() == 7 &&
                  input.next(record) &&
                  input.current_address_space_id() == 7 &&
                  input.next(record) &&
                  input.current_address_space_id() == 11 &&
                  input.next(record) &&
                  input.current_address_space_id() == 11 &&
                  !input.next(record),
              "streaming address-space identity must switch at the declared "
              "record ordinal");
    }
    {
        fastsim::SimulatorConfig config;
        config.cores = 1;
        config.validate();
        std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
        traces.push_back(
            std::make_unique<fastsim::BinaryTraceSource>(binary_path));
        fastsim::Simulator simulator(config, std::move(traces));
        const auto stats = simulator.run();
        check(stats.threads.size() == 1 &&
                  stats.threads[0].initial_effective_address_space_id == 7 &&
                  stats.threads[0].final_effective_address_space_id == 11 &&
                  stats.threads[0].distinct_address_spaces == 2 &&
                  stats.threads[0].address_space_switches == 1,
              "simulation stats must expose measured address-space coverage "
              "and transition count");
    }

    fastsim::upgrade_binary_trace_to_v7(
        binary_path, upgraded_path, fastsim::SyscallAbi::kUnknown);
    {
        fastsim::BinaryTraceSource upgraded(upgraded_path);
        const auto* transitions = upgraded.address_space_transitions();
        check(transitions != nullptr && transitions->size() == 2 &&
                  (*transitions)[0].address_space_id == 7 &&
                  (*transitions)[1].record_ordinal == 2 &&
                  (*transitions)[1].address_space_id == 11,
              "FST upgrade must preserve address-space transitions");
    }

    std::remove(binary_path.c_str());
    std::remove((binary_path + ".vmap").c_str());
    std::remove((binary_path + ".asmap").c_str());
    std::remove((binary_path + ".imap").c_str());
    std::remove(upgraded_path.c_str());
    std::remove((upgraded_path + ".vmap").c_str());
    std::remove((upgraded_path + ".asmap").c_str());
    std::remove((upgraded_path + ".imap").c_str());
}

void test_instruction_page_map_roundtrip() {
    const auto binary_path =
        test_tmp_path("fastsim_test_instruction_page_map.fst");
    const auto upgraded_path =
        test_tmp_path("fastsim_test_instruction_page_map_upgraded.fst");
    {
        fastsim::BinaryTraceWriter output(binary_path, 2);
        const auto append = [&](std::uint64_t address_space_id) {
            output.set_address_space_id(address_space_id);
            fastsim::TraceRecord record;
            record.pc = 0x1008;
            output.append(record);
        };
        append(7);
        append(7);
        append(11);
        append(7);
        append(7);
        output.register_instruction_page_mapping(
            fastsim::InstructionPageMapping{0, 7, 1, 2});
        output.register_instruction_page_mapping(
            fastsim::InstructionPageMapping{2, 11, 1, 9});
        output.register_instruction_page_mapping(
            fastsim::InstructionPageMapping{4, 7, 1, 3});
        output.close();
    }

    check(std::filesystem::file_size(binary_path + ".ifmap") ==
              48 + 3 * 32,
          "instruction-page companion must use the frozen v1 header/row "
          "layout");
    {
        fastsim::BinaryTraceSource input(binary_path);
        const auto* all = input.all_instruction_page_mappings();
        check(all != nullptr && all->size() == 3,
              "binary source must expose complete cold ifmap metadata");
        fastsim::TraceRecord record;
        check(input.next(record), "instruction-page record zero");
        const auto* first = input.instruction_page_mapping(record.pc);
        check(first != nullptr && first->address_space_id == 7 &&
                  first->physical_page == 2,
              "ifmap row must take effect before its anchor record");
        check(input.next(record) &&
                  input.instruction_page_mapping(record.pc) == first,
              "instruction-page mapping must remain active within one AS");
        check(input.next(record), "instruction-page AS switch record");
        const auto* second = input.instruction_page_mapping(record.pc);
        check(second != nullptr && second->address_space_id == 11 &&
                  second->physical_page == 9,
              "instruction-page lookup must be address-space scoped");
        check(input.next(record) &&
                  input.instruction_page_mapping(record.pc) == first,
              "returning to an AS must recover its prior mapping state");
        check(input.next(record), "instruction-page remap record");
        const auto* remapped = input.instruction_page_mapping(record.pc);
        check(remapped != nullptr && remapped->address_space_id == 7 &&
                  remapped->physical_page == 3 && remapped != first,
              "a later ifmap row must replace the prior physical page");
        check(!input.next(record), "instruction-page record count");
    }

    fastsim::upgrade_binary_trace_to_v7(
        binary_path, upgraded_path, fastsim::SyscallAbi::kUnknown);
    {
        fastsim::BinaryTraceSource input(upgraded_path);
        const auto* all = input.all_instruction_page_mappings();
        check(all != nullptr && all->size() == 3,
              "FST upgrade must preserve instruction-page mappings");
    }

    for (const auto& path : {binary_path, upgraded_path}) {
        std::remove(path.c_str());
        std::remove((path + ".asmap").c_str());
        std::remove((path + ".ifmap").c_str());
    }
}

void test_static_instruction_map_roundtrip() {
    const auto binary_path =
        test_tmp_path("fastsim_test_static_instruction_map.fst");
    {
        fastsim::BinaryTraceWriter output(binary_path, 3);
        fastsim::TraceRecord record;
        record.pc = 0x1000;
        output.append(record);

        fastsim::StaticInstructionInfo linear;
        linear.pc = 0x1000;
        linear.size = 5;
        linear.fallthrough_pc = 0x1005;
        linear.flags = fastsim::kStaticMemory;
        output.register_static_instruction(linear);

        fastsim::StaticInstructionInfo branch;
        branch.pc = 0x1005;
        branch.size = 2;
        branch.fallthrough_pc = 0x1007;
        branch.direct_target = 0x2000;
        branch.flags = fastsim::kStaticBranch |
                       fastsim::kStaticConditional |
                       fastsim::kStaticDirectTargetValid;
        output.register_static_instruction(branch);
        output.set_static_instruction_map_complete();
        output.close();
    }
    check(std::filesystem::file_size(binary_path + ".imap") ==
              48 + 2 * 32,
          "static instruction companion must use canonical fixed rows");
    fastsim::BinaryTraceSource input(binary_path);
    check(input.static_instruction_map_complete(),
          "static instruction companion completeness must survive");
    const auto* linear = input.static_instruction(0x1000);
    const auto* branch = input.static_instruction(0x1005);
    check(linear != nullptr && linear->size == 5 &&
              linear->fallthrough_pc == 0x1005 &&
              !linear->is_branch() && linear->is_memory(),
          "linear static instruction must survive the companion");
    check(branch != nullptr && branch->size == 2 &&
              branch->fallthrough_pc == 0x1007 &&
              branch->direct_target == 0x2000 &&
              branch->is_branch() && branch->is_conditional() &&
              branch->has_direct_target() &&
              input.static_instruction(0x3000) == nullptr,
          "direct branch facts must survive without speculative metadata");
    std::remove(binary_path.c_str());
    std::remove((binary_path + ".imap").c_str());
}

void test_static_instruction_operand_map_roundtrip() {
    const auto binary_path =
        test_tmp_path("fastsim_test_static_instruction_operand_map.fst");
    {
        fastsim::BinaryTraceWriter output(binary_path, 5);
        fastsim::TraceRecord record;
        record.pc = 0x4000;
        output.append(record);
        output.set_static_instruction_isa(
            fastsim::StaticInstructionIsa::kX86_64);

        fastsim::StaticInstructionInfo add;
        add.pc = 0x4000;
        add.size = 4;
        add.fallthrough_pc = 0x4004;
        add.operand_semantics_valid = true;
        add.read_register_mask[0] = (1ull << 0) | (1ull << 3);
        add.write_register_mask[0] = (1ull << 0) | (1ull << 17);
        output.register_static_instruction(add);

        fastsim::StaticInstructionInfo jump;
        jump.pc = 0x4004;
        jump.size = 2;
        jump.fallthrough_pc = 0x4006;
        jump.direct_target = 0x5000;
        jump.flags = fastsim::kStaticBranch |
                     fastsim::kStaticDirectTargetValid;
        jump.operand_semantics_valid = true;
        jump.read_register_mask[1] = 1ull << 6;
        output.register_static_instruction(jump);
        output.set_static_instruction_map_complete();
        output.close();
    }
    check(std::filesystem::file_size(binary_path + ".imap") ==
              48 + 2 * 64,
          "operand instruction companion must use canonical v2 rows");
    fastsim::BinaryTraceSource input(binary_path);
    check(input.static_instruction_map_complete() &&
              input.static_instruction_operands_complete() &&
              input.static_instruction_isa() ==
                  fastsim::StaticInstructionIsa::kX86_64,
          "v2 map must preserve ISA and complete operand coverage");
    const auto* add = input.static_instruction(0x4000);
    const auto* jump = input.static_instruction(0x4004);
    check(add != nullptr && add->operand_semantics_valid &&
              add->reads_register(0) && add->reads_register(3) &&
              !add->reads_register(4) && add->writes_register(0) &&
              add->writes_register(17),
          "v2 map must preserve read and write register masks");
    check(jump != nullptr && jump->operand_semantics_valid &&
              jump->reads_register(70) &&
              !jump->writes_register(70),
          "v2 map must preserve upper register-mask words");
    std::remove(binary_path.c_str());
    std::remove((binary_path + ".imap").c_str());
}

void test_static_instruction_ordering_map_roundtrip() {
    const auto binary_path =
        test_tmp_path("fastsim_test_static_instruction_ordering_map.fst");
    {
        fastsim::BinaryTraceWriter output(binary_path, 7);
        fastsim::TraceRecord record;
        record.pc = 0x6000;
        output.append(record);
        output.set_static_instruction_isa(
            fastsim::StaticInstructionIsa::kX86_64);

        fastsim::StaticInstructionInfo locked;
        locked.pc = record.pc;
        locked.size = 4;
        locked.fallthrough_pc = locked.pc + locked.size;
        locked.flags = fastsim::kStaticMemory |
                       fastsim::kStaticReadBarrier |
                       fastsim::kStaticWriteBarrier |
                       fastsim::kStaticLockedRmw;
        output.register_static_instruction(locked);
        output.set_static_instruction_map_complete();
        output.close();
    }
    check(std::filesystem::file_size(binary_path + ".imap") == 48 + 64,
          "memory-ordering companion must use the canonical v3 wide row");
    fastsim::BinaryTraceSource input(binary_path);
    const auto* locked = input.static_instruction(0x6000);
    check(input.static_instruction_map_complete() &&
              !input.static_instruction_operands_complete() &&
              input.static_instruction_isa() ==
                  fastsim::StaticInstructionIsa::kX86_64 &&
              locked != nullptr && locked->is_memory() &&
              locked->is_read_barrier() && locked->is_write_barrier() &&
              locked->is_memory_barrier() && locked->is_locked_rmw(),
          "v3 map must preserve producer-neutral locked-RMW semantics");
    std::remove(binary_path.c_str());
    std::remove((binary_path + ".imap").c_str());
}

void test_syscall_trace_roundtrip() {
    const auto json_path = test_tmp_path("fastsim_test_syscall.jsonl");
    const auto binary_path = test_tmp_path("fastsim_test_syscall.fst");
    const auto syscall_path =
        test_tmp_path("fastsim_test_syscall.syscalls.jsonl");
    {
        std::ofstream output(json_path);
        output << "{\"pc\":4092,\"op_class\":1}\n"
                  "{\"pc\":4096,\"is_syscall\":1,"
                  "\"op_class\":90,\"syscall_nr\":202,"
                  "\"syscall_args\":[8192,128,0,0,-1,1],"
                  "\"syscall_retval\":-11,"
                  "\"syscall_failed\":true,\"syscall_errno\":11,"
                  "\"syscall_pre_timestamp_us\":1000,"
                  "\"syscall_post_timestamp_us\":1578,"
                  "\"syscall_pre_cpu\":35,\"syscall_post_cpu\":37,"
                  "\"syscall_maybe_blocking\":true,"
                  "\"thread_id\":99}\n"
                  "{\"pc\":4100,\"is_syscall\":1,"
                  "\"syscall_number\":60,\"syscall_args\":[0]}\n";
    }
    fastsim::convert_gem5_jsonl_to_binary(
        json_path, binary_path, 0, syscall_path,
        fastsim::SyscallAbi::kLinuxX86_64);
    check(std::filesystem::file_size(binary_path) ==
              72 + 3 * sizeof(fastsim::TraceRecord) + 2 * 128,
          "FST v7 must append one fixed metadata row per syscall");
    fastsim::BinaryTraceSource input(binary_path);
    fastsim::TraceRecord record;
    check(input.syscall_abi() == fastsim::SyscallAbi::kLinuxX86_64,
          "FST v7 must retain the syscall ABI");
    check(input.next(record) && !record.is_syscall() &&
              input.current_syscall_metadata() == nullptr,
          "non-syscall record must not expose sparse metadata");
    check(input.next(record) && record.is_syscall() &&
              record.is_serializing() &&
              record.op_class == fastsim::kSyscallOpClass,
          "FST v7 must preserve an explicit functional syscall marker");
    check(record.syscall_number() == 202,
          "syscall_nr alias must survive JSONL to binary roundtrip");
    check(!has_flag(record.flags, fastsim::kPhysicalAddress),
          "syscall marker must not claim a physical memory address");
    const auto* metadata = input.current_syscall_metadata();
    check(metadata != nullptr && metadata->record_ordinal == 1 &&
              metadata->syscall_ordinal == 0 && metadata->number == 202 &&
              metadata->argument_count == 6 &&
              metadata->arguments[0] == 8192 &&
              metadata->arguments[4] == UINT64_MAX &&
              metadata->return_value_raw ==
                  static_cast<std::uint64_t>(-11) &&
              metadata->failed && metadata->errno_value == 11 &&
              metadata->pre_timestamp_us == 1000 &&
              metadata->post_timestamp_us == 1578 &&
              metadata->pre_cpu == 35 && metadata->post_cpu == 37 &&
              metadata->maybe_blocking && metadata->thread_id == 99,
          "portable DR-equivalent syscall values must survive FST v7");
    check(metadata->has(fastsim::kSyscallArgumentsValid) &&
              metadata->has(fastsim::kSyscallReturnValueValid) &&
              metadata->has(fastsim::kSyscallFailureValid) &&
              metadata->has(fastsim::kSyscallErrnoValid) &&
              metadata->has(fastsim::kSyscallPreTimestampValid) &&
              metadata->has(fastsim::kSyscallPostTimestampValid) &&
              metadata->has(fastsim::kSyscallPreCpuValid) &&
              metadata->has(fastsim::kSyscallPostCpuValid) &&
              metadata->has(fastsim::kSyscallMaybeBlockingValid) &&
              metadata->has(fastsim::kSyscallThreadIdValid),
          "FST v7 validity mask must preserve field availability");
    check(input.next(record) && record.syscall_number() == 60,
          "second syscall marker must remain aligned");
    metadata = input.current_syscall_metadata();
    check(metadata != nullptr && metadata->record_ordinal == 2 &&
              metadata->syscall_ordinal == 1 &&
              metadata->has(fastsim::kSyscallArgumentsValid) &&
              !metadata->has(fastsim::kSyscallReturnValueValid) &&
              !metadata->has(fastsim::kSyscallFailureValid),
          "missing DR fields must remain missing rather than becoming zero");
    check(!input.next(record), "syscall trace record count");
    {
        std::ifstream sidecar(syscall_path);
        const std::string text((std::istreambuf_iterator<char>(sidecar)),
                               std::istreambuf_iterator<char>());
        check(text.find("fastsim-functional-syscall-v2") !=
                      std::string::npos &&
                  text.find("\"retval_raw\":18446744073709551605") !=
                      std::string::npos &&
                  text.find("\"record_ordinal\":2") !=
                      std::string::npos,
              "syscall v2 audit sidecar must match embedded metadata");
    }
    std::remove(json_path.c_str());
    std::remove(binary_path.c_str());
    std::remove(syscall_path.c_str());
}

void test_syscall_semantic_page_fault_selection() {
    const auto binary_path =
        test_tmp_path("fastsim_test_syscall_semantic_fault.fst");
    {
        fastsim::BinaryTraceWriter output(
            binary_path, 0, fastsim::SyscallAbi::kLinuxX86_64);
        fastsim::TraceRecord mmap_record;
        mmap_record.pc = 0x1000;
        mmap_record.op_class = fastsim::kSyscallOpClass;
        mmap_record.flags = fastsim::kRetires | fastsim::kSerialize;
        mmap_record.set_syscall_number(9);
        fastsim::SyscallMetadata mmap_metadata;
        mmap_metadata.number = 9;
        mmap_metadata.arguments[1] = 8192;
        mmap_metadata.arguments[2] = 3;
        mmap_metadata.arguments[3] = 0x22;
        mmap_metadata.arguments[4] = UINT32_MAX;
        mmap_metadata.argument_count = 6;
        mmap_metadata.return_value_raw = 0x400000;
        mmap_metadata.valid_fields =
            fastsim::kSyscallArgumentsValid |
            fastsim::kSyscallReturnValueValid |
            fastsim::kSyscallFailureValid;
        output.append(mmap_record, &mmap_metadata);

        fastsim::TraceRecord mapped;
        mapped.pc = 0x1004;
        mapped.address = 0x10000;
        mapped.size = 8;
        mapped.flags = fastsim::kRetires | fastsim::kLoad |
                       fastsim::kPhysicalAddress |
                       fastsim::kVirtualPageToken;
        mapped.reserved = 1;
        output.register_virtual_page_mapping(
            fastsim::VirtualPageMapping{1, 1, 0x400, 0x10, true});
        output.append(mapped);

        fastsim::TraceRecord preexisting = mapped;
        preexisting.pc = 0x1008;
        preexisting.address = 0x20000;
        preexisting.flags &= ~fastsim::kLoad;
        preexisting.flags |= fastsim::kStore;
        preexisting.reserved = 2;
        output.register_virtual_page_mapping(
            fastsim::VirtualPageMapping{2, 2, 0x500, 0x20, true});
        output.append(preexisting);
        output.close();
    }
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.require_virtual_page_token = true;
    config.page_fault_event_model = true;
    config.page_fault_syscall_semantic_model = true;
    config.page_fault_event_profile.service_cycles = 100;
    config.validate();
    fastsim::CoreCounters total;
    {
        std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
        traces.push_back(
            std::make_unique<fastsim::BinaryTraceSource>(binary_path));
        fastsim::Simulator simulator(config, std::move(traces));
        total = simulator.run().total_core();
    }
    check(total.page_fault_first_touch_candidates == 2 &&
              total.page_fault_syscall_semantic_candidates == 1 &&
              total.page_fault_syscall_semantic_write_candidates == 0 &&
              total
                      .page_fault_syscall_semantic_fallback_write_candidates ==
                  1 &&
              total
                      .page_fault_syscall_semantic_fallback_write_selected ==
                  0 &&
              total.page_fault_virtual_page_map_misses == 0 &&
              total.page_fault_kernel.events == 1 &&
              total.page_fault_kernel.active_cycles == 100,
          "syscall-semantic page faults must select only first touches in a "
          "successful non-populated mmap range");
    config.page_fault_syscall_semantic_fallback_write_probability_ppm =
        1'000'000;
    config.validate();
    {
        std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
        traces.push_back(
            std::make_unique<fastsim::BinaryTraceSource>(binary_path));
        fastsim::Simulator simulator(config, std::move(traces));
        total = simulator.run().total_core();
    }
    check(total.page_fault_syscall_semantic_candidates == 1 &&
              total
                      .page_fault_syscall_semantic_fallback_write_candidates ==
                  1 &&
              total
                      .page_fault_syscall_semantic_fallback_write_selected ==
                  1 &&
              total.page_fault_kernel.events == 2 &&
              total.page_fault_kernel.active_cycles == 200,
          "semantic fallback must select only pre-existing first writes at "
          "the frozen probability");
    std::remove(binary_path.c_str());
    std::remove((binary_path + ".vmap").c_str());
}

void test_syscall_semantic_mapping_lifecycle() {
    const auto binary_path =
        test_tmp_path("fastsim_test_syscall_semantic_lifecycle.fst");
    {
        fastsim::BinaryTraceWriter output(
            binary_path, 0, fastsim::SyscallAbi::kLinuxX86_64);
        const auto append_syscall = [&output](
                                        std::uint64_t number,
                                        std::array<std::uint64_t, 6> arguments,
                                        std::uint8_t argument_count,
                                        std::uint64_t return_value,
                                        bool failed) {
            fastsim::TraceRecord record;
            record.pc = 0x1000 + output.record_count() * 4;
            record.op_class = fastsim::kSyscallOpClass;
            record.flags = fastsim::kRetires | fastsim::kSerialize;
            record.set_syscall_number(number);
            fastsim::SyscallMetadata metadata;
            metadata.number = number;
            metadata.arguments = arguments;
            metadata.argument_count = argument_count;
            metadata.return_value_raw = return_value;
            metadata.failed = failed;
            metadata.valid_fields =
                fastsim::kSyscallArgumentsValid |
                fastsim::kSyscallReturnValueValid |
                fastsim::kSyscallFailureValid;
            output.append(record, &metadata);
        };
        const auto append_load = [&output](std::uint32_t token,
                                           std::uint64_t virtual_page,
                                           std::uint64_t physical_page) {
            fastsim::TraceRecord record;
            record.pc = 0x1000 + output.record_count() * 4;
            record.address = physical_page << 12;
            record.size = 8;
            record.flags = fastsim::kRetires | fastsim::kLoad |
                           fastsim::kPhysicalAddress |
                           fastsim::kVirtualPageToken;
            record.reserved = token;
            output.register_virtual_page_mapping(
                fastsim::VirtualPageMapping{
                    token, output.record_count(), virtual_page,
                    physical_page, true});
            output.append(record);
        };

        append_syscall(9, {0, 8192, 3, 0x22, UINT32_MAX, 0}, 6,
                       0x400000, false);
        append_load(1, 0x400, 0x10);
        append_syscall(11, {0x401000, 4096, 0, 0, 0, 0}, 2, 0, false);
        append_load(2, 0x401, 0x11);
        append_syscall(9, {0, 4096, 3, 0x8022, UINT32_MAX, 0}, 6,
                       0x500000, false);
        append_load(3, 0x500, 0x12);
        append_syscall(9, {0, 4096, 3, 0x22, UINT32_MAX, 0}, 6,
                       UINT64_MAX, true);
        append_load(4, 0x600, 0x13);
        output.close();
    }

    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.require_virtual_page_token = true;
    config.page_fault_event_model = true;
    config.page_fault_syscall_semantic_model = true;
    config.page_fault_event_profile.service_cycles = 100;
    config.validate();
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(
        std::make_unique<fastsim::BinaryTraceSource>(binary_path));
    fastsim::Simulator simulator(config, std::move(traces));
    const auto total = simulator.run().total_core();
    check(total.page_fault_first_touch_candidates == 4 &&
              total.page_fault_syscall_semantic_candidates == 1 &&
              total.page_fault_kernel.events == 1 &&
              total.page_fault_kernel.active_cycles == 100,
          "syscall-semantic page faults must honor munmap, MAP_POPULATE, "
          "and failed mmap lifecycle semantics");
    std::remove(binary_path.c_str());
    std::remove((binary_path + ".vmap").c_str());
}

void test_initial_pte_page_fault_selection() {
    const auto path0 =
        test_tmp_path("fastsim_test_initial_pte_core0.fst");
    const auto path1 =
        test_tmp_path("fastsim_test_initial_pte_core1.fst");
    const auto write_trace = [](
                                 const std::string& path,
                                 std::uint32_t core_id,
                                 std::uint32_t token,
                                 bool state_valid,
                                 bool present) {
        fastsim::BinaryTraceWriter output(path, core_id);
        fastsim::TraceRecord record;
        record.pc = 0x1000 + core_id * 4;
        record.address = 0x10000;
        record.size = 8;
        record.flags = fastsim::kRetires | fastsim::kLoad |
                       fastsim::kPhysicalAddress |
                       fastsim::kVirtualPageToken;
        record.reserved = token;
        output.register_virtual_page_mapping(
            fastsim::VirtualPageMapping{
                token, 0, 0x400, 0x10, true, state_valid, present});
        output.append(record);
        output.close();
    };
    const auto run = [&]() {
        std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
        traces.push_back(
            std::make_unique<fastsim::BinaryTraceSource>(path0));
        traces.push_back(
            std::make_unique<fastsim::BinaryTraceSource>(path1));
        fastsim::SimulatorConfig config;
        config.cores = 2;
        config.require_virtual_page_token = true;
        config.page_fault_event_model = true;
        config.page_fault_roi_entry_page_state_model = true;
        config.page_fault_event_profile.service_cycles = 100;
        config.validate();
        fastsim::Simulator simulator(config, std::move(traces));
        return simulator.run().total_core();
    };

    write_trace(path0, 0, 1, true, false);
    write_trace(path1, 1, 2, true, false);
    auto total = run();
    check(total.page_fault_first_touch_candidates == 1 &&
              total.page_fault_initial_pte_known_pages == 1 &&
              total.page_fault_initial_pte_nonpresent_pages == 1 &&
              total.page_fault_initial_pte_selected == 1 &&
              total.page_fault_process_shared_duplicate_pages == 1 &&
              total.page_fault_kernel.events == 1 &&
              total.page_fault_kernel.active_cycles == 100,
          "one known non-present process page must select exactly one fault "
          "across trace streams");

    write_trace(path0, 0, 1, true, true);
    write_trace(path1, 1, 2, true, true);
    total = run();
    check(total.page_fault_first_touch_candidates == 1 &&
              total.page_fault_initial_pte_known_pages == 1 &&
              total.page_fault_initial_pte_present_pages == 1 &&
              total.page_fault_initial_pte_selected == 0 &&
              total.page_fault_process_shared_duplicate_pages == 1 &&
              total.page_fault_kernel.events == 0,
          "one known present process page must suppress the page-fault "
          "selector across trace streams");

    write_trace(path0, 0, 1, false, false);
    write_trace(path1, 1, 2, false, false);
    total = run();
    check(total.page_fault_first_touch_candidates == 1 &&
              total.page_fault_initial_pte_unknown_pages == 1 &&
              total.page_fault_initial_pte_known_pages == 0 &&
              total.page_fault_process_shared_duplicate_pages == 1 &&
              total.page_fault_kernel.events == 0,
          "legacy maps without initial PTE state must remain an explicit "
          "unknown fallback");

    write_trace(path0, 0, 1, true, false);
    write_trace(path1, 1, 2, true, true);
    bool conflict_rejected = false;
    try {
        std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
        traces.push_back(
            std::make_unique<fastsim::BinaryTraceSource>(path0));
        traces.push_back(
            std::make_unique<fastsim::BinaryTraceSource>(path1));
        fastsim::SimulatorConfig config;
        config.cores = 2;
        config.require_virtual_page_token = true;
        config.page_fault_event_model = true;
        config.page_fault_roi_entry_page_state_model = true;
        config.validate();
        fastsim::Simulator simulator(config, std::move(traces));
    } catch (const std::runtime_error&) {
        conflict_rejected = true;
    }
    check(conflict_rejected,
          "conflicting initial PTE states across process streams must be "
          "rejected");

    std::remove(path0.c_str());
    std::remove((path0 + ".vmap").c_str());
    std::remove(path1.c_str());
    std::remove((path1 + ".vmap").c_str());
}

void test_initial_pte_address_space_isolation() {
    const auto path0 =
        test_tmp_path("fastsim_test_initial_pte_as7.fst");
    const auto path1 =
        test_tmp_path("fastsim_test_initial_pte_as11.fst");
    const auto write_trace = [](
                                 const std::string& path,
                                 std::uint32_t core_id,
                                 std::uint64_t address_space_id,
                                 bool present) {
        fastsim::BinaryTraceWriter output(path, core_id);
        output.set_address_space_id(address_space_id);
        fastsim::TraceRecord record;
        record.pc = 0x1000 + core_id * 4;
        record.address = 0x10000;
        record.size = 8;
        record.flags = fastsim::kRetires | fastsim::kLoad |
                       fastsim::kPhysicalAddress |
                       fastsim::kVirtualPageToken;
        record.reserved = 1;
        output.register_virtual_page_mapping(
            fastsim::VirtualPageMapping{
                1, 0, 0x400, 0x10, true, true, present});
        output.append(record);
        output.close();
    };
    write_trace(path0, 0, 7, false);
    write_trace(path1, 1, 11, true);

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(
        std::make_unique<fastsim::BinaryTraceSource>(path0));
    traces.push_back(
        std::make_unique<fastsim::BinaryTraceSource>(path1));
    fastsim::SimulatorConfig config;
    config.cores = 2;
    config.require_virtual_page_token = true;
    config.page_fault_event_model = true;
    config.page_fault_roi_entry_page_state_model = true;
    config.page_fault_event_profile.service_cycles = 100;
    config.validate();
    fastsim::Simulator simulator(config, std::move(traces));
    const auto total = simulator.run().total_core();
    check(total.page_fault_first_touch_candidates == 2 &&
              total.page_fault_initial_pte_known_pages == 2 &&
              total.page_fault_initial_pte_nonpresent_pages == 1 &&
              total.page_fault_initial_pte_present_pages == 1 &&
              total.page_fault_initial_pte_selected == 1 &&
              total.page_fault_process_shared_duplicate_pages == 0 &&
              total.page_fault_kernel.events == 1,
          "equal virtual pages in different address spaces must retain "
          "independent PTE/residency state");

    std::remove(path0.c_str());
    std::remove((path0 + ".vmap").c_str());
    std::remove((path0 + ".asmap").c_str());
    std::remove(path1.c_str());
    std::remove((path1 + ".vmap").c_str());
    std::remove((path1 + ".asmap").c_str());
}

void test_roi_entry_page_state_page_fault_selection() {
    const auto path =
        test_tmp_path("fastsim_test_roi_entry_page_state.fst");
    const auto write_trace = [&](bool measurement_valid,
                                 bool measurement_present,
                                 bool boundary_inflight) {
        fastsim::BinaryTraceWriter output(path, 0);
        fastsim::TraceRecord warmup;
        warmup.pc = 0x1000;
        warmup.address = 0x10000;
        warmup.size = 8;
        warmup.flags = fastsim::kRetires | fastsim::kLoad |
                       fastsim::kPhysicalAddress |
                       fastsim::kVirtualPageToken;
        warmup.reserved = 1;
        output.register_virtual_page_mapping(
            fastsim::VirtualPageMapping{
                1, 0, 0x400, 0x10, true, true, true, true, true});
        output.append(warmup);

        fastsim::TraceRecord measurement = warmup;
        measurement.pc = 0x1004;
        measurement.address = 0x20000;
        measurement.reserved = 2;
        output.register_virtual_page_mapping(
            fastsim::VirtualPageMapping{
                2, 1, 0x500, 0x20, true, true, false,
                measurement_valid, measurement_present,
                boundary_inflight});
        output.append(measurement);
        output.close();
    };
    const auto run = [&]() {
        std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
        traces.push_back(
            std::make_unique<fastsim::WarmupInstructionTraceSource>(
                std::make_unique<fastsim::BinaryTraceSource>(path), 1, 1));
        fastsim::SimulatorConfig config;
        config.cores = 1;
        config.core_model = "interval_weave";
        config.interval_scheduler = "time_epoch";
        config.require_virtual_page_token = true;
        config.page_fault_event_model = true;
        config.page_fault_cache_state_model = true;
        config.page_fault_roi_entry_page_state_model = true;
        config.page_fault_event_profile.service_cycles = 100;
        config.validate();
        fastsim::Simulator simulator(config, std::move(traces));
        return simulator.run().total_core();
    };

    write_trace(true, true, false);
    auto total = run();
    check(total.page_fault_roi_entry_known_pages == 1 &&
              total.page_fault_roi_entry_present_pages == 1 &&
              total.page_fault_roi_entry_selected == 0 &&
              total.page_fault_initial_pte_known_pages == 0 &&
              total.page_fault_kernel.events == 0,
          "ROI-entry-present page state must override stale initial state "
          "snapshot after functional warmup");

    write_trace(true, false, false);
    total = run();
    check(total.page_fault_roi_entry_known_pages == 1 &&
              total.page_fault_roi_entry_nonpresent_pages == 1 &&
              total.page_fault_roi_entry_selected == 1 &&
              total.page_fault_roi_entry_inflight_suppressed ==
                  0 &&
              total.page_fault_kernel.events == 1 &&
              total.page_fault_cache_state_pages == 1,
          "ROI-entry-nonpresent state must select one measured page fault");

    write_trace(true, false, true);
    total = run();
    check(total.page_fault_roi_entry_known_pages == 1 &&
              total.page_fault_roi_entry_nonpresent_pages == 1 &&
              total.page_fault_roi_entry_selected == 0 &&
              total.page_fault_roi_entry_inflight_suppressed ==
                  1 &&
              total.page_fault_kernel.events == 0 &&
              total.page_fault_cache_state_pages == 1 &&
              total.page_fault_cache_state_lines == 64,
          "a page fault already in flight at ROI entry must "
          "retain page-fill state without charging a measured kernel event");

    write_trace(false, false, false);
    total = run();
    check(total.page_fault_roi_entry_unknown_pages == 1 &&
              total.page_fault_roi_entry_known_pages == 0 &&
              total.page_fault_initial_pte_known_pages == 0 &&
              total.page_fault_kernel.events == 0,
          "missing ROI-entry state must not reuse stale initial page state "
          "after functional warmup");

    std::remove(path.c_str());
    std::remove((path + ".vmap").c_str());
}

void test_legacy_syscall_trace_upgrade() {
    struct Header {
        std::array<char, 8> magic{};
        std::uint32_t version = 6;
        std::uint32_t header_size = sizeof(Header);
        std::uint32_t record_size = sizeof(fastsim::TraceRecord);
        std::uint32_t core_id = 7;
        std::uint64_t record_count = 2;
        std::uint64_t feature_flags = (1ull << 1);
        std::array<std::uint64_t, 4> reserved{};
    };
    static_assert(sizeof(Header) == 72, "test v6 header layout");

    const auto legacy_path =
        test_tmp_path("fastsim_test_legacy_syscall_v6.fst");
    const auto upgraded_path =
        test_tmp_path("fastsim_test_legacy_syscall_v7.fst");
    Header header;
    header.magic = {'F', 'S', 'T', 'R', 'C', '0', '1', '\0'};
    fastsim::TraceRecord compute;
    compute.pc = 0x1000;
    fastsim::TraceRecord syscall;
    syscall.pc = 0x1004;
    syscall.op_class = fastsim::kSyscallOpClass;
    syscall.flags = fastsim::kRetires | fastsim::kSerialize;
    syscall.set_syscall_number(202);
    {
        std::ofstream output(legacy_path, std::ios::binary);
        output.write(reinterpret_cast<const char*>(&header), sizeof(header));
        output.write(reinterpret_cast<const char*>(&compute), sizeof(compute));
        output.write(reinterpret_cast<const char*>(&syscall), sizeof(syscall));
    }

    fastsim::upgrade_binary_trace_to_v7(
        legacy_path, upgraded_path,
        fastsim::SyscallAbi::kLinuxX86_64);
    check(std::filesystem::file_size(upgraded_path) ==
              72 + 2 * sizeof(fastsim::TraceRecord) + 128,
          "legacy syscall upgrade must append one sparse metadata row");
    fastsim::BinaryTraceSource input(upgraded_path);
    fastsim::TraceRecord record;
    check(input.core_id() == 7 &&
              input.syscall_abi() == fastsim::SyscallAbi::kLinuxX86_64 &&
              input.next(record) && record.pc == compute.pc &&
              input.current_syscall_metadata() == nullptr,
          "legacy upgrade must preserve the source core and normal records");
    check(input.next(record) && record.is_syscall() &&
              record.syscall_number() == 202,
          "legacy upgrade must preserve the inline syscall number");
    const auto* metadata = input.current_syscall_metadata();
    check(metadata != nullptr && metadata->record_ordinal == 1 &&
              metadata->syscall_ordinal == 0 && metadata->number == 202 &&
              metadata->valid_fields == 0,
          "legacy syscall optional fields must remain invalid after upgrade");
    check(!input.next(record), "upgraded legacy trace record count");
    std::remove(legacy_path.c_str());
    std::remove(upgraded_path.c_str());
}

void test_binary_bulk_read_boundary() {
    const auto path = test_tmp_path("fastsim_test_bulk_trace.fst");
    constexpr std::uint64_t kRecords = 5003;
    {
        fastsim::BinaryTraceWriter output(path, 0);
        for (std::uint64_t index = 0; index < kRecords; ++index) {
            fastsim::TraceRecord record;
            record.pc = 0x1000 + index * 4;
            record.address = index * 64;
            record.op_class = static_cast<std::int16_t>(index % 100);
            output.append(record);
        }
        output.close();
    }
    fastsim::BinaryTraceSource input(path);
    fastsim::TraceRecord record;
    for (std::uint64_t index = 0; index < kRecords; ++index) {
        check(input.next(record), "bulk trace must cross refill boundary");
        check(record.pc == 0x1000 + index * 4 &&
                  record.address == index * 64 &&
                  record.op_class ==
                      static_cast<std::int16_t>(index % 100),
              "bulk trace refill must preserve record order");
    }
    check(!input.next(record), "bulk trace must stop at header count");
    std::remove(path.c_str());
}

void test_legacy_v2_trace_read() {
    struct Header {
        std::array<char, 8> magic{};
        std::uint32_t version = 2;
        std::uint32_t header_size = sizeof(Header);
        std::uint32_t record_size = 40;
        std::uint32_t core_id = 3;
        std::uint64_t record_count = 1;
        std::uint64_t feature_flags = 0;
        std::array<std::uint64_t, 4> reserved{};
    };
    struct Record {
        std::uint64_t pc = 0;
        std::uint64_t address = 0;
        std::uint64_t target = 0;
        std::uint64_t next_pc = 0;
        std::uint16_t size = 0;
        std::uint16_t flags = fastsim::kRetires;
        std::uint32_t reserved = 0;
    };
    static_assert(sizeof(Header) == 72, "test v2 header layout");
    static_assert(sizeof(Record) == 40, "test v2 record layout");

    const auto path = test_tmp_path("fastsim_test_v2_trace.fst");
    Header header;
    header.magic = {'F', 'S', 'T', 'R', 'C', '0', '1', '\0'};
    Record legacy;
    legacy.pc = 0x1234;
    legacy.address = 0x8000;
    legacy.size = 8;
    legacy.flags = fastsim::kRetires | fastsim::kLoad |
                   fastsim::kPhysicalAddress;
    {
        std::ofstream output(path, std::ios::binary);
        output.write(reinterpret_cast<const char*>(&header), sizeof(header));
        output.write(reinterpret_cast<const char*>(&legacy), sizeof(legacy));
    }
    fastsim::BinaryTraceSource input(path);
    fastsim::TraceRecord converted;
    check(input.core_id() == 3 && input.next(converted),
          "v2 binary trace remains readable");
    check(converted.pc == legacy.pc &&
              converted.address == legacy.address &&
              converted.op_class == 0 &&
              converted.producer_classes[0] == 255,
          "v2 trace defaults new interval fields safely");
    std::remove(path.c_str());
}

void test_legacy_v3_trace_read() {
    struct Header {
        std::array<char, 8> magic{};
        std::uint32_t version = 3;
        std::uint32_t header_size = sizeof(Header);
        std::uint32_t record_size = sizeof(fastsim::TraceRecord);
        std::uint32_t core_id = 5;
        std::uint64_t record_count = 1;
        std::uint64_t feature_flags = 0;
        std::array<std::uint64_t, 4> reserved{};
    };
    static_assert(sizeof(Header) == 72, "test v3 header layout");

    const auto path = test_tmp_path("fastsim_test_v3_trace.fst");
    Header header;
    header.magic = {'F', 'S', 'T', 'R', 'C', '0', '1', '\0'};
    fastsim::TraceRecord legacy;
    legacy.pc = 0x5678;
    legacy.address = 0x9000;
    legacy.size = 4;
    legacy.flags = fastsim::kRetires | fastsim::kLoad |
                   fastsim::kPhysicalAddress;
    {
        std::ofstream output(path, std::ios::binary);
        output.write(reinterpret_cast<const char*>(&header), sizeof(header));
        output.write(reinterpret_cast<const char*>(&legacy), sizeof(legacy));
    }
    fastsim::BinaryTraceSource input(path);
    fastsim::TraceRecord converted;
    check(input.core_id() == 5 && input.next(converted),
          "v3 64-byte binary trace remains readable");
    check(converted.pc == legacy.pc &&
              !fastsim::has_flag(converted.flags,
                                  fastsim::kVirtualPageToken),
          "v3 reserved field must not be mistaken for a virtual-page token");
    std::remove(path.c_str());
}

void test_binary_source_core_remap() {
    const auto binary_path =
        test_tmp_path("fastsim_test_remapped_trace.fst");
    const auto manifest_path =
        test_tmp_path("fastsim_test_remapped_manifest.txt");
    fastsim::TraceRecord expected;
    expected.pc = 0x1234;
    {
        fastsim::BinaryTraceWriter output(binary_path, 7);
        output.append(expected);
        output.close();
    }
    {
        std::ofstream manifest(manifest_path);
        manifest << "0 fastsim-binary " << binary_path << " 7\n";
    }
    auto sources = fastsim::open_trace_manifest(manifest_path, 4);
    fastsim::TraceRecord actual;
    check(sources[0]->next(actual) && actual.pc == expected.pc,
          "explicit source core remapping must preserve header validation");
    auto simulation_sources =
        fastsim::open_trace_manifest(manifest_path, 4);
    fastsim::SimulatorConfig undercommitted;
    undercommitted.cores = 4;
    undercommitted.core_model = "interval_bound";
    undercommitted.validate();
    fastsim::Simulator simulator(
        undercommitted, std::move(simulation_sources));
    const auto stats = simulator.run();
    check(stats.threads.size() == 1 &&
              stats.cores[0].retired_uops == 1 &&
              stats.cores[1].retired_uops == 0,
          "a short manifest must statically bind only its active streams");
    std::remove(manifest_path.c_str());
    std::remove(binary_path.c_str());
}

void test_binary_instruction_slice_manifest() {
    const auto binary_path =
        test_tmp_path("fastsim_test_sliced_trace.fst");
    const auto manifest_path =
        test_tmp_path("fastsim_test_sliced_manifest.txt");
    {
        fastsim::BinaryTraceWriter output(binary_path, 7);
        fastsim::TraceRecord first;
        first.pc = 0x1000;
        output.append(first);

        fastsim::TraceRecord second_a;
        second_a.pc = 0x2000;
        second_a.flags = fastsim::kRetires | fastsim::kMicroOp;
        output.append(second_a);
        fastsim::TraceRecord second_b = second_a;
        second_b.pc = 0x2001;
        second_b.flags = static_cast<std::uint16_t>(
            second_b.flags | fastsim::kLastMicroOp);
        output.append(second_b);

        fastsim::TraceRecord third;
        third.pc = 0x3000;
        output.append(third);
        fastsim::TraceRecord fourth;
        fourth.pc = 0x4000;
        output.append(fourth);
        output.close();
    }
    {
        std::ofstream manifest(manifest_path);
        manifest << "0 fastsim-binary-slice " << binary_path
                 << " 7 1 2\n";
    }
    auto sources = fastsim::open_trace_manifest(manifest_path, 1);
    fastsim::TraceRecord record;
    std::vector<std::uint64_t> pcs;
    while (sources[0]->next(record)) pcs.push_back(record.pc);
    check(pcs == std::vector<std::uint64_t>({0x2000, 0x2001, 0x3000}),
          "binary instruction slice must skip and take complete macro ops");
    std::remove(manifest_path.c_str());
    std::remove(binary_path.c_str());
}

void test_binary_functional_warmup_manifest() {
    const auto binary_path =
        test_tmp_path("fastsim_test_warmup_trace.fst");
    const auto manifest_path =
        test_tmp_path("fastsim_test_warmup_manifest.txt");
    const auto boundary_state_path =
        test_tmp_path("fastsim_test_boundary_memory_state.txt");
    {
        fastsim::BinaryTraceWriter output(binary_path, 9);
        for (std::uint64_t index = 0; index < 4; ++index) {
            fastsim::TraceRecord record;
            record.pc = 0x1000 + index * 4;
            output.append(record);
        }
        output.close();
    }
    {
        std::ofstream manifest(manifest_path);
        manifest << "0 fastsim-binary-warmup-slice " << binary_path
                 << " 9 2 2\n";
    }
    auto sources = fastsim::open_trace_manifest(manifest_path, 1);
    check(sources[0]->has_measurement_boundary(),
          "warmup manifest must expose a measurement boundary");
    fastsim::TraceRecord record;
    check(sources[0]->next(record) && record.pc == 0x1000 &&
              sources[0]->next(record) && record.pc == 0x1004 &&
              sources[0]->measurement_boundary_pending() &&
              !sources[0]->next(record),
          "functional warmup must pause after its exact macro boundary");
    sources[0]->start_measurement();
    check(sources[0]->next(record) && record.pc == 0x1008 &&
              sources[0]->next(record) && record.pc == 0x100c &&
              !sources[0]->next(record),
          "functional measurement must resume for its exact take range");
    {
        std::ofstream manifest(manifest_path);
        manifest << "0 fastsim-binary-warmup-slice " << binary_path
                 << " 9 0 2\n";
    }
    sources = fastsim::open_trace_manifest(manifest_path, 1);
    check(sources[0]->measurement_boundary_pending() &&
              !sources[0]->next(record),
          "zero-length per-core warmup must start at the common boundary");
    sources[0]->start_measurement();
    check(sources[0]->next(record) && record.pc == 0x1000 &&
              sources[0]->next(record) && record.pc == 0x1004 &&
              !sources[0]->next(record),
          "zero-length warmup must preserve the full measurement prefix");

    // A global serial marker can arrive after some UOPs of a macro operation
    // have committed on one core. Exact producer record counts must preserve
    // that boundary while instruction counts independently remain conserved.
    {
        fastsim::BinaryTraceWriter output(binary_path, 9);
        for (std::uint64_t index = 0; index < 4; ++index) {
            fastsim::TraceRecord split;
            split.pc = index < 2 ? 0x2000 : 0x2004;
            split.flags = fastsim::kRetires | fastsim::kMicroOp;
            if (index == 1 || index == 3) {
                split.flags = static_cast<std::uint16_t>(
                    split.flags | fastsim::kLastMicroOp);
            }
            output.append(split);
        }
        output.close();
    }
    {
        std::ofstream manifest(manifest_path);
        manifest << "0 fastsim-binary-warmup-slice " << binary_path
                 << " 9 1 1 3 1\n";
    }
    sources = fastsim::open_trace_manifest(manifest_path, 1);
    std::vector<std::uint64_t> warmup_pcs;
    while (sources[0]->next(record)) warmup_pcs.push_back(record.pc);
    check(warmup_pcs == std::vector<std::uint64_t>(
                              {0x2000, 0x2000, 0x2004}) &&
              sources[0]->measurement_boundary_pending(),
          "exact warmup record count must preserve a marker inside a macro");
    sources[0]->start_measurement();
    check(sources[0]->next(record) && record.pc == 0x2004 &&
              !sources[0]->next(record),
          "exact measurement record count must preserve the marker suffix");

    {
        std::ofstream state(boundary_state_path);
        state << "fastsim-boundary-memory-state-v1\n"
              << "0 0x1238 8 R\n"
              << "1 8190 4 W\n";
    }
    {
        std::ofstream manifest(manifest_path);
        manifest << "0 fastsim-binary-warmup-state-slice " << binary_path
                 << " 9 1 1 3 1 " << boundary_state_path << "\n";
    }
    sources = fastsim::open_trace_manifest(manifest_path, 1);
    const auto* boundary_accesses =
        sources[0]->measurement_boundary_memory_accesses();
    check(boundary_accesses != nullptr && boundary_accesses->size() == 2 &&
              (*boundary_accesses)[0].physical_address == 0x1238 &&
              (*boundary_accesses)[0].size == 8 &&
              !(*boundary_accesses)[0].write &&
              (*boundary_accesses)[1].physical_address == 8190 &&
              (*boundary_accesses)[1].size == 4 &&
              (*boundary_accesses)[1].write,
          "warmup state manifest must expose only ordered functional memory "
          "access fields");
    {
        std::ofstream state(boundary_state_path);
        state << "fastsim-boundary-memory-state-v1\n"
              << "0 0x1238 8 R 99\n";
    }
    bool rejected_oracle_shaped_row = false;
    try {
        (void)fastsim::open_trace_manifest(manifest_path, 1);
    } catch (const std::runtime_error&) {
        rejected_oracle_shaped_row = true;
    }
    check(rejected_oracle_shaped_row,
          "boundary state parser must reject fields outside its functional "
          "four-column schema");
    std::remove(boundary_state_path.c_str());
    std::remove(manifest_path.c_str());
    std::remove(binary_path.c_str());
}

void test_context_trace_contract() {
    const auto manifest_path = test_tmp_path("context_contract.manifest");
    const auto binary_path = test_tmp_path("context_contract.fst");
    fastsim::TraceRecord macro, first, last, auxiliary, record;
    first.flags = fastsim::kRetires | fastsim::kMicroOp;
    last.flags = first.flags | fastsim::kLastMicroOp;
    auxiliary.flags = 0;
    auxiliary.op_class = fastsim::kSyscallOpClass;
    auto retiring_syscall = auxiliary;
    retiring_syscall.flags = fastsim::kRetires;
    const auto write_manifest = [&](const std::string& format,
                                    const std::string& bounds) {
        std::ofstream manifest(manifest_path);
        manifest << "0 " << format << ' ' << binary_path << ' ' << bounds
                 << '\n';
    };
    const auto rejects = [](const auto& action, const std::string& message) {
        bool rejected = false;
        try {
            action();
        } catch (const std::runtime_error&) {
            rejected = true;
        }
        check(rejected, message);
    };
    const auto context_source = [&](std::vector<fastsim::TraceRecord> records,
                                    std::uint64_t score_records,
                                    std::uint64_t execution_records) {
        auto source = std::make_unique<fastsim::WarmupInstructionTraceSource>(
            std::make_unique<VectorTraceSource>(std::move(records)),
            0, 1, 0, score_records, true, std::nullopt, execution_records);
        source->start_measurement();
        return source;
    };

    // An asynchronous warmup cut preserves metadata; the later score marker
    // is sticky and does not introduce another pause before execution EOF.
    {
        fastsim::BinaryTraceWriter writer(binary_path, 0);
        writer.enable_complete_dependencies();
        writer.set_address_space_id(73);
        for (const auto& row : {macro, first, last, macro}) writer.append(row);
        writer.close();
    }
    write_manifest("fastsim-binary-context-v1", "0 1 1 2 1 2");
    auto sources = fastsim::open_trace_manifest(manifest_path, 1);
    auto& source = *sources[0];
    check(source.has_execution_context() && source.score_records() == 1 &&
              source.execution_records() == 2 &&
              !source.score_boundary_reached(),
          "explicit context bounds must be visible before measurement");
    check(source.next(record) && source.next(record) && !source.next(record) &&
              source.measurement_boundary_pending() &&
              !source.score_boundary_reached(),
          "context warmup must preserve an asynchronous cut inside a macro");
    source.start_measurement();
    check(source.next(record) && source.score_boundary_reached() &&
              source.complete_dependencies() &&
              source.current_address_space_id() == 73 &&
              source.address_space_transitions() != nullptr,
          "the score record must publish its marker and preserve source metadata");
    check(source.next(record) && !source.next(record) &&
              source.score_boundary_reached(),
          "the score marker must allow context records and remain sticky at EOF");
    for (const bool explicit_context : {false, true}) {
        write_manifest(explicit_context ? "fastsim-binary-context-v1"
                                        : "fastsim-binary-warmup-slice",
                       explicit_context ? "0 1 1 2 1 1" : "0 1 1 2 1");
        auto zero_tail = fastsim::open_trace_manifest(manifest_path, 1);
        check(zero_tail[0]->next(record) && zero_tail[0]->next(record) &&
                  !zero_tail[0]->next(record),
              "legacy and zero-tail sources must preserve identical warmup");
        zero_tail[0]->start_measurement();
        check(zero_tail[0]->next(record) && !zero_tail[0]->next(record) &&
                  zero_tail[0]->has_execution_context() == explicit_context &&
                  zero_tail[0]->score_boundary_reached() == explicit_context &&
                  zero_tail[0]->score_records() == (explicit_context ? 1u : 0u) &&
                  zero_tail[0]->execution_records() ==
                      (explicit_context ? 1u : 0u),
              "zero-tail execution must match legacy EOF with explicit bounds only");
    }
    // Retiring syscall markers already contribute to the manifest's macro
    // counts. Explicit context must preserve that convention in both phases.
    for (const bool explicit_context : {false, true}) {
        for (const bool syscall_in_warmup : {false, true}) {
            const std::uint64_t warmup = syscall_in_warmup ? 1 : 0;
            const std::uint64_t score = 2 - warmup;
            fastsim::WarmupInstructionTraceSource syscall_source(
                std::make_unique<VectorTraceSource>(
                    std::vector<fastsim::TraceRecord>{retiring_syscall, macro}),
                warmup, score, warmup, score, true, std::nullopt,
                explicit_context ? std::optional<std::uint64_t>(score)
                                 : std::nullopt);
            if (syscall_in_warmup) {
                check(syscall_source.next(record) && record.is_syscall() &&
                          !syscall_source.next(record),
                      "retiring syscall warmup must preserve legacy macro counts");
            }
            syscall_source.start_measurement();
            if (!syscall_in_warmup) {
                check(syscall_source.next(record) && record.is_syscall(),
                      "retiring syscall score population must preserve its record");
            }
            check(syscall_source.next(record) && !record.is_syscall() &&
                      !syscall_source.next(record) &&
                      syscall_source.score_boundary_reached() == explicit_context,
                  "retiring syscall counts must match legacy replay with zero tail");
        }
    }

    // Declared execution coverage and numeric bounds must fail closed.
    write_manifest("fastsim-binary-context-v1", "0 1 1 2 1 3");
    rejects([&] { (void)fastsim::open_trace_manifest(manifest_path, 1); },
            "context manifest must reject execution beyond the binary source");
    auto short_source = context_source({macro}, 1, 2);
    check(short_source->next(record), "short source must expose its valid score record");
    rejects([&] { (void)short_source->next(record); },
            "context source must reject physical EOF before execution completes");
    for (const auto& bounds : {
             "0 0 2 0 2 1", "0 0 1 0 1 -1",
             "0 0 1 0 1 18446744073709551616",
             "0 0 1 18446744073709551615 1 1"}) {
        write_manifest("fastsim-binary-context-v1", bounds);
        rejects([&] { (void)fastsim::read_trace_manifest(manifest_path); },
                "context manifest must reject short, negative or overflowing bounds");
    }

    // Score must end at a retiring macro; auxiliary records are allowed after
    // a complete execution macro but cannot disguise an unfinished one.
    auto split_score = context_source({macro, first, last}, 2, 3);
    check(split_score->next(record), "split-score fixture must begin with a macro");
    rejects([&] { (void)split_score->next(record); },
            "score boundary must reject a partially emitted macro instruction");
    check(!split_score->score_boundary_reached(),
          "a rejected score record must not publish the score marker");
    fastsim::WarmupInstructionTraceSource syscall_endpoint(
        std::make_unique<VectorTraceSource>(
            std::vector<fastsim::TraceRecord>{macro, retiring_syscall}),
        0, 2, 0, 2, true, std::nullopt, 2);
    syscall_endpoint.start_measurement();
    check(syscall_endpoint.next(record), "syscall endpoint fixture must begin with a macro");
    rejects([&] { (void)syscall_endpoint.next(record); },
            "matching macro counts must not permit a syscall as the score endpoint");
    for (const auto& tail : {auxiliary, retiring_syscall}) {
        auto split_execution = context_source({macro, first, tail}, 1, 3);
        check(split_execution->next(record) && split_execution->next(record),
              "execution-boundary fixture must reach its partial macro");
        rejects([&] { (void)split_execution->next(record); },
                "an auxiliary tail must not hide an incomplete execution macro");
        auto auxiliary_tail = context_source({macro, tail}, 1, 2);
        check(auxiliary_tail->next(record) && auxiliary_tail->score_boundary_reached() &&
                  auxiliary_tail->next(record) && record.is_syscall() &&
                  !auxiliary_tail->next(record),
              "execution EOF must allow auxiliary records after a complete macro");
    }

    std::remove(manifest_path.c_str());
    std::remove(binary_path.c_str());
    std::remove((binary_path + ".deps").c_str());
    std::remove((binary_path + ".asmap").c_str());
}

void test_context_execution_boundary() {
    const auto manifest_path = test_tmp_path("context_execution.manifest");
    const auto first_path = test_tmp_path("context_execution_core0.fst");
    const auto second_path = test_tmp_path("context_execution_core1.fst");
    for (std::uint32_t core = 0; core < 2; ++core) {
        fastsim::BinaryTraceWriter writer(core == 0 ? first_path : second_path, core);
        writer.enable_complete_dependencies();
        for (std::uint32_t index = 0; index < (core == 0 ? 800u : 128u); ++index) {
            fastsim::TraceRecord record;
            record.pc = 0x1000 + core * 0x100;
            record.address = index * 4096ull + core * 64;
            record.size = 8;
            record.flags = fastsim::kRetires | fastsim::kLoad | fastsim::kPhysicalAddress;
            record.n_dst = 1;
            if (index != 0) {
                record.n_src = 1;
                record.producer_dists[0] = 1;
            }
            writer.append(record);
        }
        writer.close();
    }
    fastsim::SimulatorConfig config;
    config.cores = 2;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.chunk_instructions = 37; // Score marker lies inside a producer chunk.
    config.lookahead_chunks = 2;
    config.interval_max_cycles = 128;
    config.interval_target_uops = 32;
    config.interval_private_preview = true;
    config.interval_parallel_feedback = true;
    config.domain_min_events = 1;
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.response_block_summary = true;
    config.response_memory_descriptor = true;
    config.response_monotone_iq_calendar = true;
    config.needs_tso = true;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.cha_count = 1;
    config.l1d.size_bytes = 64;
    config.l1d.associativity = 1;
    config.l2.size_bytes = 128;
    config.l2.associativity = 1;
    config.llc.size_bytes = 256;
    config.llc.associativity = 1;
    config.validate();
    const auto run = [&](bool context, std::uint32_t first_score) {
        {
            std::ofstream manifest(manifest_path);
            manifest << "0 " << (context ? "fastsim-binary-context-v1 "
                                          : "fastsim-binary-warmup-slice ")
                     << first_path << " 0 0 " << first_score << " 0 " << first_score;
            if (context) manifest << " 800";
            manifest << "\n1 fastsim-binary-warmup-slice " << second_path
                     << " 1 0 128 0 128\n";
        }
        auto sources = fastsim::open_trace_manifest(manifest_path, 2);
        fastsim::Simulator simulator(config, std::move(sources));
        return simulator.run();
    };
    const auto scored = run(true, 8);
    const auto full = run(false, 800);
    const auto truncated = run(false, 8);
    check(scored.total_core().retired_uops == 136 &&
              scored.total_core().retired_instructions == 136 &&
              scored.total_core().records == 136,
          "context records must not enter the scored population");
    check(scored.threads[0].retired_uops == 8 && scored.threads[1].retired_uops == 128,
          "thread score population must exclude producer lookahead and context");
    check(scored.cores[1].cycles == full.cores[1].cycles &&
              scored.cores[1].cycles > truncated.cores[1].cycles,
          "real context must preserve full-execution interference on the scored peer");
    check(scored.cores[0].cycles == truncated.cores[0].cycles,
          "score retirement must freeze inside the accepted producer chunk");
    check(scored.total_core().l1d.accesses == 136 &&
              scored.total_core().l2.accesses == 136 &&
              scored.llc.accesses == 136,
          "context cache requests must not pollute scored PMU");
    config.response_materialized_uop_fast_kernel = true;
    config.validate();
    const auto fast = run(true, 8);
    check(fast.cores[0].cycles == scored.cores[0].cycles &&
              fast.cores[1].cycles == scored.cores[1].cycles &&
              fast.total_core().l1d.accesses == 136 && fast.llc.accesses == 136,
          "materialized feedback must preserve the generic score marker and PMU");
    const auto zero_tail = run(true, 800);
    check(zero_tail.cores[0].cycles == full.cores[0].cycles &&
              zero_tail.cores[1].cycles == full.cores[1].cycles &&
              zero_tail.total_core().retired_uops == full.total_core().retired_uops &&
              zero_tail.llc.accesses == full.llc.accesses &&
              zero_tail.context_cores[0].records == 0,
          "zero-tail context bounds must reproduce the old full-execution format");

    // A score marker must not drain or reset an ordinary store. A one-slot SQ
    // makes the next context store observe the scored store's actual release.
    config.cores = 1;
    config.sq_entries = 1;
    config.chunk_instructions = 256;
    config.interval_target_uops = 256;
    config.interval_max_cycles = 1024;
    config.response_activity_certificate = true;
    config.validate();
    const auto single = [&](std::vector<fastsim::TraceRecord> records,
                            std::uint64_t score_count, bool context) {
        const auto executed = records.size();
        std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
        traces.push_back(std::make_unique<fastsim::WarmupInstructionTraceSource>(
            std::make_unique<VectorTraceSource>(std::move(records)),
            0, score_count, 0, score_count, true, std::nullopt,
            context ? std::optional<std::uint64_t>(executed) : std::nullopt));
        fastsim::Simulator simulator(config, std::move(traces));
        return simulator.run();
    };
    fastsim::TraceRecord store;
    store.pc = 0x2000;
    store.address = 0x4000;
    store.size = 8;
    store.flags = fastsim::kRetires | fastsim::kStore | fastsim::kPhysicalAddress;
    auto second_store = store;
    second_store.address += 4096;
    const std::vector<fastsim::TraceRecord> stores{store, second_store};
    const auto store_context = single(stores, 1, true);
    const auto store_full = single(stores, 2, false);
    const auto store_prefix = single(stores, 1, false);
    check(store_context.context_execution[0].execution_cycles == store_full.cores[0].cycles &&
              store_context.cores[0].cycles == store_prefix.cores[0].cycles &&
              store_context.cores[0].l1d.accesses == 1 &&
              store_context.context_cores[0].l1d.accesses == 1,
          "score marker must preserve the pending store and SQ release without draining it");

    // Exercise the certified memory-free path, whose feedback skips the
    // general per-UOP loop where ordinary score retirement is captured.
    std::vector<fastsim::TraceRecord> compute(192);
    const auto compute_context = single(compute, 96, true);
    const auto compute_prefix = single(compute, 96, false);
    check(compute_context.response_activity_certified_uops != 0 &&
              compute_context.cores[0].cycles == compute_prefix.cores[0].cycles &&
              compute_context.total_core().retired_uops == 96,
          "activity-certified feedback must freeze the exact score retirement");
    std::remove(manifest_path.c_str());
    std::remove(first_path.c_str());
    std::remove(second_path.c_str());
}

void test_two_phase_functional_warmup() {
    fastsim::SimulatorConfig config;
    config.cores = 2;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.chunk_instructions = 2;
    config.lookahead_chunks = 2;
    config.interval_target_uops = 2;
    config.interval_max_cycles = 16;
    config.interval_private_preview = true;
    config.interval_parallel_feedback = true;
    config.committed_pipeline_audit = true;
    config.rename_free_list = true;
    config.rename_int_free_entries = 1;
    config.domain_min_events = 1;
    config.l1d.size_bytes = 128;
    config.fetch_buffer_bytes = 64;
    config.l1i_enabled = true;
    config.l1i.size_bytes = 256;
    config.l1i.associativity = 2;
    config.l1i_miss_penalty = 7;
    config.l2.size_bytes = 256;
    config.llc.size_bytes = 512;
    config.l1d.associativity = 1;
    config.l2.associativity = 1;
    config.llc.associativity = 1;
    config.cha_count = 2;
    config.dram.channels = 2;
    config.dram.banks_per_channel = 1;
    config.validate();

    const auto load = [](std::uint64_t pc, std::uint64_t address) {
        fastsim::TraceRecord record;
        record.pc = pc;
        record.address = address;
        record.size = 8;
        record.flags = fastsim::kRetires | fastsim::kLoad |
                       fastsim::kPhysicalAddress;
        record.n_dst = 1;
        record.set_register_class_metadata(
            {255, 255, 255, 255}, {1, 0, 0, 0});
        return record;
    };
    fastsim::TraceRecord compute;
    compute.pc = 0x3000;
    compute.n_dst = 1;
    compute.set_register_class_metadata(
        {255, 255, 255, 255}, {1, 0, 0, 0});

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(
        std::make_unique<fastsim::WarmupInstructionTraceSource>(
            std::make_unique<VectorTraceSource>(
                std::vector<fastsim::TraceRecord>{
                    load(0x1000, 0x0000), load(0x1004, 0x0000)}),
            1, 1));
    traces.push_back(
        std::make_unique<fastsim::WarmupInstructionTraceSource>(
            std::make_unique<VectorTraceSource>(
                std::vector<fastsim::TraceRecord>{
                    load(0x2000, 0x0040), compute,
                    load(0x2004, 0x0040)}),
            2, 1));

    fastsim::Simulator simulator(config, std::move(traces));
    const auto stats = simulator.run();
    const auto total = stats.total_core();
    check(stats.functional_warmup_enabled &&
              stats.functional_warmup_instructions == 3 &&
              stats.functional_warmup_uops == 3 &&
              stats.functional_warmup_memory_events == 2 &&
              stats.functional_warmup_barrier_cycles > 0,
          "two-phase warmup must report only the functional prefix");
    check(total.retired_instructions == 2 && total.retired_uops == 2 &&
              total.memory_uops == 2 &&
              total.memory_accesses == 2 &&
              stats.interval_accepted_uops == 2 &&
              stats.batch_memory_events == 2,
          "measurement counters must exclude every warmup UOP and event");
    check(total.l1d.accesses == 2 && total.l1d.hits == 2 &&
              total.l1d.misses == 0,
          "measurement must retain private-cache state from warmup");
    check(total.l1i.accesses == 1 && total.l1i.hits == 1 &&
              total.l1i.misses == 0 &&
              total.l1i_miss_stall_cycles == 0,
          "measurement must retain L1I state while resetting its counters");
    check(stats.interval_private_memory_events +
              stats.interval_escape_memory_events ==
              stats.batch_memory_events,
          "measurement memory partition must remain conserved");
    auto committed_audit = stats.committed_pipeline_audit[0];
    committed_audit += stats.committed_pipeline_audit[1];
    check(committed_audit.uops == 2 &&
              committed_audit.destination_class_uops == 2 &&
              committed_audit.destination_class_tokens[0] == 2 &&
              committed_audit.destination_conserved() &&
              committed_audit.destination_classes_conserved(),
          "two-phase warmup must retain timing state while resetting the "
          "committed-pipeline audit at the measurement marker");

    auto page_fault_config = config;
    page_fault_config.cores = 1;
    page_fault_config.rename_free_list = false;
    page_fault_config.require_virtual_page_token = true;
    page_fault_config.page_fault_event_model = true;
    page_fault_config.page_fault_probability_ppm = 500'000;
    page_fault_config.page_fault_background_write_probability_ppm = 500'000;
    page_fault_config.page_fault_event_profile.service_cycles = 1;
    page_fault_config.validate();
    const auto token_load = [](std::uint64_t pc, std::uint64_t address,
                               std::uint32_t token) {
        fastsim::TraceRecord record;
        record.pc = pc;
        record.address = address;
        record.size = 8;
        record.flags = fastsim::kRetires | fastsim::kLoad |
                       fastsim::kPhysicalAddress |
                       fastsim::kVirtualPageToken;
        record.reserved = token;
        return record;
    };
    std::vector<std::unique_ptr<fastsim::TraceSource>> page_fault_traces;
    page_fault_traces.push_back(
        std::make_unique<fastsim::WarmupInstructionTraceSource>(
            std::make_unique<VectorTraceSource>(
                std::vector<fastsim::TraceRecord>{
                    token_load(0x4000, 0x1000, 1),
                    token_load(0x4004, 0x1000, 1),
                    token_load(0x4008, 0x2000, 2)}),
            1, 2));
    fastsim::Simulator page_fault_simulator(
        page_fault_config, std::move(page_fault_traces));
    const auto page_fault_total =
        page_fault_simulator.run().total_core();
    check(page_fault_total.page_fault_first_touch_candidates == 1 &&
              page_fault_total.page_fault_kernel.events == 0,
          "warmup must preserve page residency but reset deterministic "
          "probability phase at the measurement boundary");

    auto boundary_config = config;
    boundary_config.cores = 1;
    boundary_config.validate();
    const auto run_boundary_seed = [&](bool enabled) {
        std::optional<std::vector<fastsim::MeasurementBoundaryMemoryAccess>>
            state;
        if (enabled) {
            state = std::vector<fastsim::MeasurementBoundaryMemoryAccess>{
                {0, 0x3000, 8, false}};
        }
        std::vector<std::unique_ptr<fastsim::TraceSource>> seed_traces;
        seed_traces.push_back(
            std::make_unique<fastsim::WarmupInstructionTraceSource>(
                std::make_unique<VectorTraceSource>(
                    std::vector<fastsim::TraceRecord>{
                        load(0x5000, 0x3000)}),
                0, 1, 0, 0, false, std::move(state)));
        fastsim::Simulator seed_simulator(
            boundary_config, std::move(seed_traces));
        return seed_simulator.run();
    };
    const auto cold_boundary = run_boundary_seed(false);
    const auto seeded_boundary = run_boundary_seed(true);
    const auto cold_boundary_total = cold_boundary.total_core();
    const auto seeded_boundary_total = seeded_boundary.total_core();
    check(cold_boundary_total.l1d.misses == 1 &&
              seeded_boundary_total.l1d.hits == 1 &&
              seeded_boundary_total.l1d.misses == 0,
          "a boundary memory state read must make the matching measurement "
          "demand resident without prescribing a hit path");
    check(seeded_boundary.measurement_boundary_memory_state_enabled &&
              seeded_boundary.measurement_boundary_memory_state_accesses ==
                  1 &&
              seeded_boundary.measurement_boundary_memory_state_lines == 1,
          "measurement statistics must audit boundary state replay");
    check(cold_boundary_total.retired_uops ==
                  seeded_boundary_total.retired_uops &&
              cold_boundary_total.memory_accesses ==
                  seeded_boundary_total.memory_accesses &&
              cold_boundary.batch_memory_events ==
                  seeded_boundary.batch_memory_events,
          "boundary state replay must not add retired UOPs or measured "
          "memory requests");

    std::vector<std::unique_ptr<fastsim::TraceSource>> partial_state_traces;
    partial_state_traces.push_back(
        std::make_unique<fastsim::WarmupInstructionTraceSource>(
            std::make_unique<VectorTraceSource>(
                std::vector<fastsim::TraceRecord>{load(0x6000, 0x4000)}),
            0, 1, 0, 0, false,
            std::vector<fastsim::MeasurementBoundaryMemoryAccess>{}));
    partial_state_traces.push_back(
        std::make_unique<fastsim::WarmupInstructionTraceSource>(
            std::make_unique<VectorTraceSource>(
                std::vector<fastsim::TraceRecord>{load(0x7000, 0x5000)}),
            0, 1));
    bool rejected_partial_state = false;
    try {
        fastsim::Simulator partial_state_simulator(
            config, std::move(partial_state_traces));
    } catch (const std::invalid_argument&) {
        rejected_partial_state = true;
    }
    check(rejected_partial_state,
          "boundary state must be explicitly complete for every active "
          "trace");
}

void test_undercommitted_static_thread_bindings() {
    fastsim::SimulatorConfig config;
    config.cores = 4;
    config.core_model = "interval_bound";
    config.chunk_instructions = 2;
    config.validate();

    fastsim::TraceRecord uop;
    uop.pc = 0x1000;
    auto syscall = uop;
    syscall.pc = 0x1004;
    syscall.op_class = fastsim::kSyscallOpClass;
    std::vector<fastsim::ThreadTraceBinding> threads;
    fastsim::ThreadTraceBinding first;
    first.thread_id = 41;
    first.initial_core = 1;
    first.address_space_id = 7;
    first.trace = std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{uop, syscall, uop});
    threads.push_back(std::move(first));
    fastsim::ThreadTraceBinding second;
    second.thread_id = 99;
    second.initial_core = 3;
    second.address_space_id = 11;
    second.trace = std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{uop, uop});
    threads.push_back(std::move(second));

    fastsim::Simulator simulator(config, std::move(threads));
    const auto stats = simulator.run();
    check(stats.cores.size() == 4 && stats.threads.size() == 2 &&
              stats.trace_worker_threads == 2,
          "threads below cores must create only one trace worker per thread");
    check(stats.cores[0].retired_uops == 0 &&
              stats.cores[1].retired_uops == 3 &&
              stats.cores[2].retired_uops == 0 &&
              stats.cores[3].retired_uops == 2,
          "static thread bindings must leave unbound hardware cores idle");
    check(stats.threads[0].thread_id == 41 &&
              stats.threads[0].initial_core == 1 &&
              stats.threads[0].address_space_id == 7 &&
              stats.threads[0].retired_uops == 3 &&
              stats.threads[0].syscall_uops == 1 &&
              stats.cores[1].syscall_uops == 1 &&
              stats.threads[1].thread_id == 99 &&
              stats.threads[1].initial_core == 3 &&
              stats.threads[1].retired_uops == 2,
          "thread-owned identity and functional counters must be preserved");
}

void test_gem5_branch_contract() {
    const auto path = test_tmp_path("fastsim_test_branch_trace.jsonl");
    {
        std::ofstream output(path);
        output
            << "{\"macro_pc\":4096,\"is_branch\":1,"
               "\"is_branch_cond\":1,\"branch_taken\":1,"
               "\"branch_target\":8192,\"branch_next_pc\":8192}\n"
            << "{\"macro_pc\":4100,\"is_branch\":1,"
               "\"is_branch_cond\":1,\"branch_taken\":0,"
               "\"branch_target\":8192}\n";
    }
    fastsim::Gem5JsonlTraceSource input(path);
    fastsim::TraceRecord exact;
    fastsim::TraceRecord incomplete;
    check(input.next(exact) && input.next(incomplete),
          "gem5 branch contract records");
    check(
        fastsim::has_flag(
            exact.flags, fastsim::kBranchOutcomeValid) &&
            fastsim::has_flag(exact.flags, fastsim::kTaken) &&
            exact.next_pc == 8192,
        "explicit next PC must make branch outcome replayable");
    check(
        !fastsim::has_flag(
            incomplete.flags, fastsim::kBranchOutcomeValid),
        "missing next PC must not fabricate a branch outcome");
    std::remove(path.c_str());
}

void test_strict_physical_address_contract() {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.chunk_instructions = 1;
    config.lookahead_chunks = 1;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.strict_physical_address = true;
    config.validate();
    fastsim::TraceRecord virtual_load;
    virtual_load.pc = 0x1000;
    virtual_load.address = 0x2000;
    virtual_load.size = 8;
    virtual_load.flags = fastsim::kRetires | fastsim::kLoad;
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{virtual_load}));
    bool rejected = false;
    try {
        fastsim::Simulator simulator(config, std::move(traces));
        (void)simulator.run();
    } catch (const std::runtime_error&) {
        rejected = true;
    }
    check(rejected,
          "strict cache PMU mode must reject virtual-only memory records");
}

void test_strict_virtual_page_contract() {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.chunk_instructions = 1;
    config.lookahead_chunks = 1;
    config.core_model = "interval_bound";
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.dtlb.enabled = true;
    config.require_virtual_page_token = true;
    config.validate();
    fastsim::TraceRecord physical_load;
    physical_load.pc = 0x1000;
    physical_load.address = 0x2ffc;
    physical_load.size = 8;
    physical_load.flags = fastsim::kRetires | fastsim::kLoad |
                          fastsim::kPhysicalAddress;
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{physical_load}));
    bool rejected = false;
    try {
        fastsim::Simulator simulator(config, std::move(traces));
        (void)simulator.run();
    } catch (const std::runtime_error&) {
        rejected = true;
    }
    check(rejected,
          "DTLB timing mode must reject records without virtual-page tokens");

    config.allow_cross_page_without_virtual_token = true;
    std::vector<std::unique_ptr<fastsim::TraceSource>> cross_page_traces;
    cross_page_traces.push_back(std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{physical_load}));
    fastsim::Simulator cross_page_simulator(
        config, std::move(cross_page_traces));
    const auto cross_page_stats = cross_page_simulator.run();
    const auto cross_page_total = cross_page_stats.total_core();
    check(cross_page_total.retired_uops == 1 &&
              cross_page_total.dtlb.accesses == 1 &&
              cross_page_total.dtlb.untracked == 1,
          "explicit cross-page compatibility must expose untracked DTLB state");

    auto non_crossing_load = physical_load;
    non_crossing_load.address = 0x2000;
    std::vector<std::unique_ptr<fastsim::TraceSource>> non_crossing_traces;
    non_crossing_traces.push_back(std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{non_crossing_load}));
    bool non_crossing_rejected = false;
    try {
        fastsim::Simulator non_crossing_simulator(
            config, std::move(non_crossing_traces));
        (void)non_crossing_simulator.run();
    } catch (const std::runtime_error&) {
        non_crossing_rejected = true;
    }
    check(non_crossing_rejected,
          "cross-page compatibility must not admit other missing tokens");
}

void test_dram_capacity_contract() {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.chunk_instructions = 1;
    config.lookahead_chunks = 1;
    config.cha_count = 1;
    config.dram.size_bytes = 64;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();
    fastsim::TraceRecord load;
    load.pc = 0x1000;
    load.address = 64;
    load.size = 8;
    load.flags = fastsim::kRetires | fastsim::kLoad |
                 fastsim::kPhysicalAddress;
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{load}));
    bool rejected = false;
    try {
        fastsim::Simulator simulator(config, std::move(traces));
        (void)simulator.run();
    } catch (const std::runtime_error&) {
        rejected = true;
    }
    check(rejected,
          "physical access beyond configured DRAM must be rejected");

    config.allow_mmio_escape = true;
    std::vector<std::unique_ptr<fastsim::TraceSource>> mmio_traces;
    mmio_traces.push_back(std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{load}));
    fastsim::Simulator mmio_simulator(config, std::move(mmio_traces));
    const auto stats = mmio_simulator.run();
    const auto total = stats.total_core();
    check(total.retired_uops == 1 && total.memory_accesses == 0 &&
              total.mmio_escape_accesses == 1 &&
              stats.batch_memory_events == 0,
          "explicit MMIO escape must retain the UOP but bypass memory state");
}

fastsim::SimulationStats run_dram_bank_group_case(
    std::uint32_t t_ccd_l) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.chunk_instructions = 32;
    config.interval_target_uops = 32;
    config.interval_max_cycles = 4096;
    config.lookahead_chunks = 2;
    config.l1d.size_bytes = 64;
    config.l2.size_bytes = 64;
    config.llc.size_bytes = 64;
    config.l1d.associativity = 1;
    config.l2.associativity = 1;
    config.llc.associativity = 1;
    config.l1d.hit_latency = 1;
    config.l2.hit_latency = 2;
    config.llc.hit_latency = 2;
    config.cha_count = 1;
    config.noc_one_way_latency = 0;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 4;
    config.dram.ranks_per_channel = 1;
    config.dram.bank_groups_per_rank = 2;
    config.dram.row_bytes = 8192;
    config.dram.t_cl = 2;
    config.dram.t_rcd = 2;
    config.dram.t_rp = 2;
    config.dram.burst_cycles = 2;
    config.dram.t_ccd_l = t_ccd_l;
    config.dram.frontend_latency = 0;
    config.dram.backend_latency = 0;
    config.dram.scheduler = "fcfs";
    config.validate();

    constexpr std::uint64_t kLinesPerRow = 8192 / 64;
    std::vector<fastsim::TraceRecord> records;
    records.reserve(32);
    for (std::uint64_t index = 0; index < 32; ++index) {
        // Banks zero and two are distinct but share bank group zero. Keep
        // each bank's row open while issuing unique columns.
        const auto bank = (index & 1u) == 0 ? 0u : 2u;
        const auto column = index / 2;
        fastsim::TraceRecord load;
        load.address = (bank * kLinesPerRow + column) * 64;
        load.size = 8;
        load.flags = fastsim::kRetires | fastsim::kLoad |
                     fastsim::kPhysicalAddress;
        records.push_back(load);
    }
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_dram_bank_group_column_spacing() {
    const auto unconstrained = run_dram_bank_group_case(0);
    const auto constrained = run_dram_bank_group_case(8);
    check(constrained.total_core().cycles >
              unconstrained.total_core().cycles &&
              constrained.cha[0].queue_cycles >
                  unconstrained.cha[0].queue_cycles,
          "tCCD_L must delay same-bank-group columns across distinct banks");
}

fastsim::SimulationStats run_dram_activation_case(
    std::uint32_t t_rrd, std::uint32_t t_rrd_l,
    std::uint32_t t_xaw, std::uint32_t activation_limit) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.chunk_instructions = 64;
    config.interval_target_uops = 16;
    config.interval_max_cycles = 4096;
    config.lookahead_chunks = 2;
    config.l1d.size_bytes = 64;
    config.l2.size_bytes = 64;
    config.llc.size_bytes = 64;
    config.l1d.associativity = 1;
    config.l2.associativity = 1;
    config.llc.associativity = 1;
    config.l1d.hit_latency = 1;
    config.l2.hit_latency = 2;
    config.llc.hit_latency = 2;
    config.cha_count = 1;
    config.noc_one_way_latency = 0;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 8;
    config.dram.ranks_per_channel = 1;
    config.dram.bank_groups_per_rank = 4;
    config.dram.row_bytes = 8192;
    config.dram.t_cl = 2;
    config.dram.t_rcd = 2;
    config.dram.t_rp = 2;
    config.dram.t_rrd = t_rrd;
    config.dram.t_rrd_l = t_rrd_l;
    config.dram.t_xaw = t_xaw;
    config.dram.activation_limit = activation_limit;
    config.dram.burst_cycles = 2;
    config.dram.frontend_latency = 0;
    config.dram.backend_latency = 0;
    config.dram.scheduler = "fcfs";
    config.validate();

    constexpr std::uint64_t kLinesPerRow = 8192 / 64;
    constexpr std::uint64_t kBanks = 8;
    std::vector<fastsim::TraceRecord> records;
    records.reserve(64);
    for (std::uint64_t index = 0; index < 64; ++index) {
        const auto bank = index % kBanks;
        const auto row = index / kBanks;
        fastsim::TraceRecord load;
        load.address =
            ((row * kBanks + bank) * kLinesPerRow) * 64;
        load.size = 8;
        load.flags = fastsim::kRetires | fastsim::kLoad |
                     fastsim::kPhysicalAddress;
        records.push_back(load);
    }
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_dram_activation_spacing() {
    const auto unconstrained = run_dram_activation_case(0, 0, 0, 0);
    const auto rrd = run_dram_activation_case(8, 12, 0, 0);
    const auto xaw = run_dram_activation_case(0, 0, 32, 4);
    check(rrd.total_core().cycles > unconstrained.total_core().cycles &&
              rrd.cha[0].queue_cycles >
                  unconstrained.cha[0].queue_cycles,
          "tRRD/tRRD_L must serialize row activations within a rank");
    check(xaw.total_core().cycles > unconstrained.total_core().cycles &&
              xaw.cha[0].queue_cycles >
                  unconstrained.cha[0].queue_cycles,
          "tXAW must bound the number of row activations per rank window");
}

fastsim::DramConfig dram_page_policy_test_config() {
    fastsim::DramConfig config;
    config.size_bytes = 1ull << 20;
    config.channels = 1;
    config.banks_per_channel = 2;
    config.ranks_per_channel = 1;
    config.bank_groups_per_rank = 1;
    config.row_bytes = 64;
    config.t_cl = 2;
    config.t_rcd = 3;
    config.t_rp = 5;
    config.burst_cycles = 2;
    config.frontend_latency = 0;
    config.backend_latency = 0;
    config.read_buffer_size = 8;
    return config;
}

void test_dram_causal_selection_matches_gem5_future_hit() {
    // Exact ticks from the baseline gem5 binary, using the captured DDR4
    // configuration and tools/gem5_dram_trace_probe.py. A future row hit
    // must not undo the earlier cold-bank command reservation.
    fastsim::DramConfig config;
    config.size_bytes = 3ull << 30;
    config.channels = 8;
    config.banks_per_channel = 16;
    config.ranks_per_channel = 2;
    config.bank_groups_per_rank = 4;
    config.row_bytes = 8192;
    config.t_cl = config.t_rcd = config.t_rp = 14160;
    config.t_ras = 32000;
    config.t_rtp = 7500;
    config.t_rrd = 3332;
    config.t_rrd_l = 4900;
    config.t_xaw = 21000;
    config.activation_limit = 4;
    config.burst_cycles = 3332;
    config.t_ccd_l = 5000;
    config.t_cs = 1666;
    config.frontend_latency = config.backend_latency = 10000;
    const std::vector<fastsim::testing::DramScheduleRequest> requests{
        // Start after gem5's initial nextBurstAt = tRP + tRCD. That
        // controller startup state is outside this selection differential.
        {50000, 1024, 0}, {100000, 0, 1}, {110000, 1024, 2}};
    check(!config.frfcfs_causal_selection,
          "causal selection remains experimental and disabled by default");
    const auto legacy = fastsim::testing::run_dram_schedule_probe(
        config, 64, requests, 8);
    check(legacy.command_cycles[2] == 110000,
          "the prior path must preserve the observed future-hit counterexample");
    config.frfcfs_causal_selection = true;
    for (const auto window : {8u, 64u}) {
        const auto aligned = fastsim::testing::run_dram_schedule_probe(
            config, 64, requests, window);
        check(aligned.command_cycles ==
                  std::vector<std::uint64_t>({64160, 114160, 117492}) &&
              aligned.completions ==
                  std::vector<std::uint64_t>({101652, 151652, 154984}) &&
              aligned.service_order == std::vector<std::size_t>({0, 1, 2}),
              "selection and response timing must match the 3-request gem5 oracle");
    }
}

void test_dram_page_policy_full_queue_visibility() {
    auto config = dram_page_policy_test_config();
    config.frfcfs_full_queue_page_policy = true;
    const auto same_row_beyond_window =
        fastsim::testing::run_dram_schedule_probe(
            config, 64,
            {
                {0, 0, 0},  // bank 0, row 0: serviced first
                {0, 2, 1},  // bank 0, row 1: in-queue conflict
                {0, 0, 2},  // bank 0, row 0: hit beyond window
            },
            1);
    check(same_row_beyond_window.service_order ==
              std::vector<std::size_t>({0, 1, 2}) &&
              same_row_beyond_window.max_selection_candidates == 1 &&
              same_row_beyond_window.max_admitted_pending == 3,
          "selection window must bound service candidates, not admission");
    check(same_row_beyond_window.outside_window_row_hits > 0 &&
              same_row_beyond_window.adaptive_precharges == 1,
          "an admitted same-row hit beyond the service window must keep the "
          "row open despite an earlier bank conflict");

    auto bounded_config = config;
    bounded_config.frfcfs_full_queue_page_policy = false;
    const auto bounded_same_row =
        fastsim::testing::run_dram_schedule_probe(
            bounded_config, 64,
            {
                {0, 0, 0},
                {0, 2, 1},
                {0, 0, 2},
            },
            1);
    check(bounded_same_row.outside_window_row_hits == 0 &&
              bounded_same_row.page_policy_scanned_requests <
                  same_row_beyond_window.page_policy_scanned_requests &&
              bounded_same_row.adaptive_precharges == 2,
          "disabled full-queue visibility must preserve the bounded "
          "production comparison path");

    const auto conflict_beyond_window =
        fastsim::testing::run_dram_schedule_probe(
            config, 64,
            {
                {0, 0, 0},  // bank 0, row 0: serviced first
                {0, 1, 1},  // bank 1: irrelevant to bank 0 policy
                {0, 2, 2},  // bank 0, row 1: conflict beyond window
            },
            1);
    check(conflict_beyond_window.max_selection_candidates == 1 &&
              conflict_beyond_window.max_admitted_pending == 3 &&
              conflict_beyond_window.outside_window_bank_conflicts > 0 &&
              conflict_beyond_window.adaptive_precharges == 1,
          "an admitted conflict beyond the service window must close the "
          "open row when no same-row hit remains");
}

void test_dram_page_policy_row_cap_single_precharge() {
    auto config = dram_page_policy_test_config();
    config.frfcfs_full_queue_page_policy = true;
    config.frfcfs_row_cap_single_precharge = true;
    config.max_accesses_per_row = 2;
    const auto result = fastsim::testing::run_dram_schedule_probe(
        config, 64,
        {
            {0, 0, 0},  // bank 0, row 0
            {0, 1, 1},  // bank 1, leaves bank 0 open
            {0, 0, 2},  // second bank 0 row 0 access reaches row cap
            {0, 2, 3},  // pending bank 0 conflict
        },
        1);
    check(result.row_hits.size() == 4 && result.row_hits[2] == 1,
          "row-cap test must reach the configured limit on a row hit");
    check(result.row_cap_precharges == 1 &&
              result.adaptive_precharges == 0,
          "row-cap and adaptive decisions must not precharge one access "
          "twice");

    config.frfcfs_row_cap_single_precharge = false;
    const auto legacy = fastsim::testing::run_dram_schedule_probe(
        config, 64,
        {
            {0, 0, 0},
            {0, 1, 1},
            {0, 0, 2},
            {0, 2, 3},
        },
        1);
    check(legacy.row_cap_precharges == 1 &&
              legacy.adaptive_precharges == 1,
          "disabled single-precharge correction must preserve the explicit "
          "production comparison path");
}

void test_dram_separate_write_queue_read_priority() {
    auto config = dram_page_policy_test_config();
    config.banks_per_channel = 1;
    config.row_bytes = 128;
    config.scheduler = "frfcfs";
    config.separate_write_queue = true;
    config.write_buffer_size = 8;
    config.write_high_threshold_percent = 75;
    config.write_low_threshold_percent = 50;
    config.min_reads_per_switch = 2;
    config.min_writes_per_switch = 2;

    const std::vector<fastsim::testing::DramControllerProbeEvent> events{
        {0, 0, true}, {0, 1, true}, {0, 2, true},
        {0, 3, true}, {0, 4, true}, {0, 5, true},
        {10, 100, false},
        {10, 6, true},
        {11, 102, false},
        {12, 104, false},
    };
    const auto buffered = fastsim::testing::run_dram_controller_probe(
        config, 64, events);
    check(buffered.write_enqueues == 7 &&
              buffered.high_watermark_switches == 1 &&
              buffered.turnarounds == 1 &&
              buffered.writes_drained == 2 &&
              buffered.read_bypasses == 2 &&
              buffered.max_pending == 7 &&
              buffered.pending_final == 5,
          "write controller must preserve read priority and drain one "
          "minimum burst after crossing the high watermark");
    check(buffered.write_row_hits == 1 &&
              buffered.write_row_misses == 1,
          "write drain must apply FR-FCFS row-hit selection within the "
          "buffered write queue");

    auto immediate_config = config;
    immediate_config.separate_write_queue = false;
    const auto immediate = fastsim::testing::run_dram_controller_probe(
        immediate_config, 64,
        {{0, 0, true}, {0, 8, false}});
    const auto read_priority = fastsim::testing::run_dram_controller_probe(
        config, 64,
        {{0, 0, true}, {0, 8, false}});
    check(immediate.completions[1] > read_priority.completions[1] &&
              immediate.write_enqueues == 0 &&
              read_priority.write_enqueues == 1 &&
              read_priority.writes_drained == 0,
          "disabled mode must preserve immediate dirty-writeback timing, "
          "while enabled mode lets a demand read bypass a buffered write");

    auto capacity_config = config;
    capacity_config.write_buffer_size = 4;
    capacity_config.write_high_threshold_percent = 75;
    capacity_config.write_low_threshold_percent = 50;
    capacity_config.min_writes_per_switch = 1;
    const auto capacity = fastsim::testing::run_dram_controller_probe(
        capacity_config, 64,
        {{0, 0, true}, {0, 1, true}, {0, 2, true},
         {0, 3, true}, {0, 4, true}});
    check(capacity.write_enqueues == 5 &&
              capacity.forced_capacity_drains == 1 &&
              capacity.writes_drained == 4 &&
              capacity.pending_final == 1,
          "a full write queue must drain through the low-watermark "
          "hysteresis before admitting another dirty victim");
}

void test_dram_parallel_channel_write_state_isolation() {
    auto config = dram_page_policy_test_config();
    config.channels = 8;
    config.scheduler = "frfcfs";
    config.separate_write_queue = true;
    config.write_buffer_size = 8;
    config.write_high_threshold_percent = 75;
    config.write_low_threshold_percent = 50;
    config.min_reads_per_switch = 1;
    config.min_writes_per_switch = 2;

    std::vector<std::uint64_t> buffered_writes;
    std::vector<fastsim::testing::DramScheduleRequest> reads;
    for (std::uint32_t channel = 0; channel < config.channels; ++channel) {
        for (std::uint32_t index = 0; index < 7; ++index) {
            buffered_writes.push_back(
                channel + config.channels * (index * 4ull));
        }
        reads.push_back({10, channel + config.channels * 128ull,
                         channel * 2ull});
        reads.push_back({11, channel + config.channels * 132ull,
                         channel * 2ull + 1});
    }

    const auto serial = fastsim::testing::run_dram_schedule_probe(
        config, 64, reads, 4, buffered_writes, false);
    check(serial.writes_drained == config.channels * 2ull &&
              serial.high_watermark_switches == config.channels &&
              serial.turnarounds == config.channels &&
              serial.pending_writes_final == config.channels * 5ull,
          "DRAM write-state test must switch and drain every channel");

    const auto equivalent = [&serial](const auto& parallel) {
        return parallel.completions == serial.completions &&
            parallel.command_cycles == serial.command_cycles &&
            parallel.row_hits == serial.row_hits &&
            parallel.service_order == serial.service_order &&
            parallel.max_selection_candidates ==
                serial.max_selection_candidates &&
            parallel.max_admitted_pending ==
                serial.max_admitted_pending &&
            parallel.page_policy_scanned_requests ==
                serial.page_policy_scanned_requests &&
            parallel.outside_window_row_hits ==
                serial.outside_window_row_hits &&
            parallel.outside_window_bank_conflicts ==
                serial.outside_window_bank_conflicts &&
            parallel.row_cap_precharges == serial.row_cap_precharges &&
            parallel.adaptive_precharges == serial.adaptive_precharges &&
            parallel.writes_drained == serial.writes_drained &&
            parallel.high_watermark_switches ==
                serial.high_watermark_switches &&
            parallel.turnarounds == serial.turnarounds &&
            parallel.pending_writes_final ==
                serial.pending_writes_final;
    };
    for (std::uint32_t iteration = 0; iteration < 16; ++iteration) {
        const auto parallel = fastsim::testing::run_dram_schedule_probe(
            config, 64, reads, 4, buffered_writes, true);
        check(equivalent(parallel),
              "parallel DRAM channels must isolate dirty-write mode state");
    }
}

void test_resident_chunk_ring() {
    const auto result =
        fastsim::testing::run_resident_buffer_probe();
    check(result.initial_mapping_conserved,
          "resident ring must preserve cross-chunk UOP/memory mapping");
    check(result.rebased_mapping_conserved,
          "resident ring must rebase logical cursors without moving data");
    check(result.descriptor_indices_unchanged,
          "resident ring must not rewrite producer descriptor indices");
    check(result.ring_wrap_conserved,
          "resident ring must preserve mapping after index wrap-around");
    check(result.released_chunks == 34,
          "resident ring must release every consumed producer chunk");
}

void test_simulator() {
    fastsim::SimulatorConfig config;
    config.cores = 2;
    config.chunk_instructions = 256;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 2;
    config.dram.channels = 2;
    config.dram.banks_per_channel = 2;
    config.validate();
    auto traces =
        fastsim::make_synthetic_traces(2, 2000, 40, 20, 1024, 7);
    fastsim::Simulator simulator(config, std::move(traces));
    const auto stats = simulator.run();
    const auto total = stats.total_core();
    check(total.retired_instructions == 4000,
          "simulator must retire all synthetic instructions");
    check(total.memory_accesses > 0, "simulator must see memory accesses");
    check(stats.llc.accesses > 0, "simulator must access LLC");
    check(total.memory_penalty_cycles > 0,
          "simulator must expose memory penalty cycles");
    check(total.branch_penalty_cycles > 0,
          "simulator must expose branch penalty cycles");
    check(total.cycles >=
              total.memory_penalty_cycles +
                  total.branch_penalty_cycles,
          "cycle total must contain the reported penalty components");
    check(stats.wall_time_ns > 0, "simulator must measure wall time");
}

void test_interval_weave_scheduler() {
    fastsim::SimulatorConfig config;
    config.cores = 2;
    config.core_model = "interval_weave";
    config.chunk_instructions = 64;
    config.interval_target_uops = 64;
    config.interval_max_cycles = 128;
    config.lookahead_chunks = 2;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 2;
    config.dram.channels = 2;
    config.dram.banks_per_channel = 2;
    config.validate();

    auto traces =
        fastsim::make_synthetic_traces(2, 2048, 50, 25, 1024, 19);
    fastsim::Simulator simulator(config, std::move(traces));
    const auto stats = simulator.run();
    const auto total = stats.total_core();
    check(total.retired_instructions == 4096,
          "interval weave must retire every UOP exactly once");
    check(stats.interval_steps > 0,
          "interval weave must advance global-time steps");
    check(stats.batch_memory_events == total.memory_accesses,
          "interval weave must batch every memory event exactly once");
    check(stats.max_batch_memory_events > 1,
          "interval weave must amortize more than one memory event");
    check(total.memory_penalty_cycles > 0,
          "interval weave must feed memory delay back to the core");
}

fastsim::SimulationStats run_ruby_sequencer_case(
    std::uint32_t max_outstanding) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.chunk_instructions = 64;
    config.interval_target_uops = 64;
    config.interval_max_cycles = 4096;
    config.lookahead_chunks = 2;
    config.ruby_sequencer_max_outstanding = max_outstanding;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    fastsim::TraceRecord load;
    load.address = 0x8000;
    load.size = 8;
    load.flags = fastsim::kRetires | fastsim::kLoad |
                 fastsim::kPhysicalAddress;
    std::vector<fastsim::TraceRecord> records(64, load);
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_ruby_sequencer_capacity() {
    const auto one = run_ruby_sequencer_case(1);
    const auto sixteen = run_ruby_sequencer_case(16);
    check(one.sequencer.size() == 1 &&
              one.sequencer[0].requests == 64 &&
              sixteen.sequencer[0].requests == 64,
          "Ruby Sequencer must account for every CPU memory request");
    check(one.sequencer[0].max_outstanding == 1 &&
              sixteen.sequencer[0].max_outstanding > 1 &&
              sixteen.sequencer[0].max_outstanding <= 16,
          "Ruby Sequencer occupancy must respect its independent capacity");
    check(one.sequencer[0].buffer_full_stalls > 0 &&
              one.sequencer[0].stall_cycles >
                  sixteen.sequencer[0].stall_cycles &&
              one.total_core().cycles > sixteen.total_core().cycles,
          "a one-entry Sequencer must apply visible request backpressure");
}

fastsim::SimulationStats run_ruby_line_coalescing_case(bool second_write) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.chunk_instructions = 8;
    config.interval_target_uops = 8;
    config.interval_max_cycles = 1024;
    config.lookahead_chunks = 2;
    config.ruby_sequencer_max_outstanding = 16;
    config.ruby_sequencer_line_coalescing = true;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    const auto access = [](bool write) {
        fastsim::TraceRecord record;
        record.address = 0x8000;
        record.size = 8;
        record.flags = fastsim::kRetires |
            (write ? fastsim::kStore : fastsim::kLoad) |
            fastsim::kPhysicalAddress;
        return record;
    };
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{
            access(false), access(second_write)}));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_pending_fill_generations() {
    fastsim::PendingFillTable table;
    table.publish(7, 10, 100, 1, false, 100);
    table.expire_before(5);
    // Visiting an independent UOP issued at 150 is not permission to expire
    // the parent: a younger UOP can issue at 30 from the same dispatch window.
    check(table.find(7) != nullptr && table.find(7)->response == 100,
          "OOO issue order must not expire a fill before the dispatch watermark");
    auto proposal = table;
    proposal.publish(7, 20, 180, 2, true, 100);
    proposal.expire_before(100);
    check(proposal.find(7) != nullptr && proposal.find(7)->response == 180 &&
              proposal.find(7)->read_response == 100,
          "old fill expiry must not erase a new write-permission generation");
    check(table.find(7)->response == 100,
          "an uncommitted timing proposal must not publish a fill");
    table = std::move(proposal);
    table.expire_before(180);
    check(table.find(7) == nullptr,
          "a committed fill generation must expire at its own response");
    table.publish(9, 190, 220, 3, true, 0);
    check(table.find(9)->read_response == 0,
          "a write permission upgrade must not invent a data-fill dependency");
}

void test_line_generation_same_line_reads_and_visibility() {
    using Ledger = fastsim::LineGenerationLedger;
    Ledger ledger(2, 10);
    const auto leader = ledger.try_admit(
        Ledger::Request::read(7, 41, 10, 100, 100));
    check(leader.outcome == Ledger::Outcome::kNewGeneration &&
              leader.response_cycle == 100 &&
              leader.leader_sequence == 41 && ledger.size() == 1,
          "a first line request must own one new generation");

    ledger.advance_to(20);
    const auto follower = ledger.try_admit(
        Ledger::Request::read(7, 42, 20, 250, 250));
    check(follower.outcome == Ledger::Outcome::kAttached &&
              follower.generation == leader.generation &&
              follower.leader_sequence == 41 &&
              follower.response_cycle == 100 && ledger.size() == 1 &&
              ledger.find(7)->read_followers == 1,
          "same-line reads must share their leader generation and response");

    const auto upgrade = ledger.try_admit(
        Ledger::Request::write(7, 43, 20, 260, 260));
    check(upgrade.outcome == Ledger::Outcome::kPermissionBlocked &&
              upgrade.retry_cycle == 100 && !upgrade.admitted(),
          "a read generation must not invent write permission");

    Ledger write_ledger(2, 20);
    const auto store = write_ledger.try_admit(
        Ledger::Request::write(9, 51, 20, 80, 20));
    const auto load = write_ledger.try_admit(
        Ledger::Request::read(9, 52, 20, 300, 300));
    const auto write = write_ledger.try_admit(
        Ledger::Request::write(9, 53, 20, 300, 300));
    check(store.response_cycle == 80 &&
              load.outcome == Ledger::Outcome::kAttached &&
              load.response_cycle == 20 &&
              load.read_visible_at_admission &&
              write.outcome == Ledger::Outcome::kAttached &&
              write.response_cycle == 80 &&
              !write.write_visible_at_admission,
          "a write-permission generation must expose resident read data "
          "separately from its write callback");

    Ledger visibility_extension(2, 20);
    const auto callback_only = visibility_extension.try_admit(
        Ledger::Request::read(11, 61, 20, 80, 80));
    const auto callback_and_data = visibility_extension.try_admit(
        Ledger::Request::read(12, 62, 20, 90, 90));
    visibility_extension.extend_callback(
        11, callback_only.generation, 100,
        Ledger::ReadVisibilityUpdate::kKeep);
    visibility_extension.extend_callback(
        12, callback_and_data.generation, 110,
        Ledger::ReadVisibilityUpdate::kExtendToCallback);
    check(visibility_extension.find(11)->callback_cycle == 100 &&
              visibility_extension.find(11)->read_visible_cycle == 80 &&
              visibility_extension.find(12)->callback_cycle == 110 &&
              visibility_extension.find(12)->read_visible_cycle == 110,
          "callback extension must explicitly choose whether read "
          "visibility moves with the callback");
    check(ledger.invariants_hold() && write_ledger.invariants_hold() &&
              visibility_extension.invariants_hold() &&
              visibility_extension.expiry_accounting_conserved(),
          "line-generation visibility state must preserve its bounds");
}

void test_line_generation_exact_callback_boundary() {
    using Ledger = fastsim::LineGenerationLedger;
    Ledger ledger(1, 10);
    const auto first = ledger.try_admit(
        Ledger::Request::read(3, 10, 10, 40, 40));
    const auto immediate = ledger.try_admit(
        Ledger::Request::read(4, 12, 10, 10, 10));
    check(immediate.outcome == Ledger::Outcome::kNewGeneration &&
              immediate.response_cycle == 10 && ledger.size() == 1,
          "a zero-duration generation must not wait behind full capacity");
    ledger.advance_to(40);
    const auto second = ledger.try_admit(
        Ledger::Request::read(3, 11, 40, 70, 70));
    check(second.outcome == Ledger::Outcome::kNewGeneration &&
              second.generation != first.generation &&
              second.leader_sequence == 11 && ledger.size() == 1 &&
              ledger.counters().expired_generations == 1,
          "a request at the callback boundary must start a new generation");
    check(ledger.invariants_hold(),
          "callback-boundary replacement must retain one live expiry");
}

void test_line_generation_ordered_and_stale_expiry() {
    using Ledger = fastsim::LineGenerationLedger;
    Ledger ledger(2, 10);
    const auto extended = ledger.try_admit(
        Ledger::Request::read(1, 20, 10, 40, 40));
    (void)ledger.try_admit(
        Ledger::Request::read(2, 21, 10, 30, 30));
    ledger.extend_callback(
        1, extended.generation, 60,
        Ledger::ReadVisibilityUpdate::kExtendToCallback);

    ledger.advance_to(30);
    check(ledger.find(2) == nullptr && ledger.find(1) != nullptr &&
              ledger.size() == 1,
          "callback expiry must process the earliest live generation first");
    ledger.advance_to(40);
    check(ledger.find(1) != nullptr &&
              ledger.find(1)->callback_cycle == 60 &&
              ledger.counters().stale_expiry_events == 1,
          "a stale callback record must not erase an extended generation");
    ledger.advance_to(60);
    check(ledger.size() == 0 &&
              ledger.counters().expired_generations == 2 &&
              ledger.counters().max_expiry_entries <= 2 * ledger.capacity() &&
              ledger.expiry_accounting_conserved() &&
              ledger.invariants_hold(),
          "ordered callback state and its stale records must remain bounded");

    Ledger compacted(1, 10);
    const auto compacted_leader = compacted.try_admit(
        Ledger::Request::read(8, 30, 10, 20, 20));
    for (const auto callback : {30ull, 40ull, 50ull, 60ull}) {
        compacted.extend_callback(
            8, compacted_leader.generation, callback,
            Ledger::ReadVisibilityUpdate::kExtendToCallback);
    }
    check(compacted.counters().expiry_compactions == 2 &&
              compacted.counters().stale_expiry_events == 4 &&
              compacted.counters().expiry_records_scheduled == 5 &&
              compacted.expiry_entries() == 1 &&
              compacted.expiry_entries() <= 2 * compacted.capacity() &&
              compacted.expiry_accounting_conserved() &&
              compacted.invariants_hold(),
          "capacity-one repeated extension must compact stale records while "
          "conserving scheduled expiry accounting");
    compacted.advance_to(60);
    check(compacted.size() == 0 && compacted.expiry_entries() == 0 &&
              compacted.counters().expired_generations == 1 &&
              compacted.expiry_accounting_conserved(),
          "a compacted generation must expire once at its final callback");
}

void test_line_generation_capacity_without_future_store_reservation() {
    using Ledger = fastsim::LineGenerationLedger;
    Ledger ledger(2, 468);

    // A is the old miss.  B is next in program order but cannot really enter
    // until cycle 656, so its early attempt must leave the cycle-468 ledger
    // unchanged.  C must be able to use the otherwise empty slot.
    const auto miss_a = ledger.try_admit(
        Ledger::Request::read(0xa, 256, 468, 654, 654));
    const auto future_store_b = ledger.try_admit(
        Ledger::Request::write(0xb, 257, 656, 0, 0));
    const auto load_c = ledger.try_admit(
        Ledger::Request::read(0xc, 258, 468, 470, 470));
    check(miss_a.admitted() &&
              future_store_b.outcome ==
                  Ledger::Outcome::kAdmissionDeferred &&
              future_store_b.retry_cycle == 656 && load_c.admitted() &&
              ledger.find(0xb) == nullptr && ledger.size() == 2 &&
              load_c.response_cycle == 470,
          "a future store must not reserve the slot needed by an earlier load");

    const auto blocked = ledger.try_admit(
        Ledger::Request::read(0xd, 259, 468, 472, 472));
    check(blocked.outcome == Ledger::Outcome::kCapacityBlocked &&
              blocked.retry_cycle == 470 && ledger.size() == 2,
          "capacity failure must report the earliest callback without "
          "creating a future reservation");

    ledger.advance_to(470);
    check(ledger.size() == 1 && ledger.find(0xa) != nullptr,
          "the short C request must release its slot before A");
    ledger.advance_to(656);
    const auto store_b = ledger.try_admit(
        Ledger::Request::write(0xb, 257, 656, 846, 656));
    check(store_b.outcome == Ledger::Outcome::kNewGeneration &&
              store_b.leader_sequence == 257 &&
              store_b.response_cycle == 846 && ledger.size() == 1 &&
              ledger.counters().max_active_generations == 2 &&
              ledger.invariants_hold(),
          "B must occupy capacity only at its actual admission after C");
}

void test_line_generation_copy_isolation() {
    using Ledger = fastsim::LineGenerationLedger;
    Ledger original(2, 10);
    const auto leader = original.try_admit(
        Ledger::Request::read(5, 90, 10, 50, 50));
    auto proposal = original;
    proposal.extend_callback(
        5, leader.generation, 70,
        Ledger::ReadVisibilityUpdate::kExtendToCallback);
    (void)proposal.try_admit(
        Ledger::Request::read(6, 91, 10, 30, 30));
    proposal.advance_to(50);

    check(proposal.find(5) != nullptr &&
              proposal.find(5)->callback_cycle == 70 &&
              proposal.find(6) == nullptr && proposal.size() == 1,
          "a copied proposal must own independent entries and expiry state");
    check(original.find(5) != nullptr &&
              original.find(5)->callback_cycle == 50 &&
              original.find(6) == nullptr && original.size() == 1 &&
              original.counters().callback_extensions == 0,
          "mutating a proposal must not publish into the original ledger");
    original.advance_to(50);
    check(original.size() == 0 && proposal.size() == 1 &&
              original.invariants_hold() && proposal.invariants_hold(),
          "copied ledgers must expire their transactions independently");
}

fastsim::SimulationStats run_pending_fill_case(
    bool enabled, bool first_store = false, bool cross_epoch = false,
    bool fast_kernel = false, bool ablate_all = false,
    bool boundary_stream = false) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.interval_max_cycles = 1024;
    config.chunk_instructions = 64;
    config.interval_target_uops = 64;
    config.lookahead_chunks = 2;
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.response_block_summary = true;
    config.response_memory_descriptor = true;
    config.response_monotone_iq_calendar = true;
    config.response_pending_fill = enabled;
    config.response_pending_fill_wait = !ablate_all;
    config.response_pending_fill_load_admission = !ablate_all;
    config.response_pending_fill_store_commit = !ablate_all;
    config.response_materialized_uop_fast_kernel = fast_kernel;
    config.cpi_attribution = !fast_kernel;
    config.response_frontier_audit_stride_uops = fast_kernel ? 0 : 1;
    config.ruby_sequencer_max_outstanding = 64;
    config.l1d.hit_latency = 2;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.llc_fill_response_latency = cross_epoch ? 2048 : 100;
    config.validate();
    const auto load_index = boundary_stream ? 8192u : (cross_epoch ? 1200u : 4u);
    std::vector<fastsim::TraceRecord> records(load_index + 2);
    for (std::size_t index = 0; index < records.size(); ++index) {
        auto& record = records[index];
        record.pc = 0x1000 + index * 4;
        record.flags = fastsim::kRetires | fastsim::kPhysicalAddress;
        if (boundary_stream) {
            record.flags |= fastsim::kLoad;
            record.address = 0x100000 + (index % 128) * 64;
            record.size = 8;
        }
        if (cross_epoch && index > 1 && index < load_index) {
            record.producer_dists[0] = 1;
        }
    }
    records[0].flags |= first_store ? fastsim::kStore : fastsim::kLoad;
    records[0].address = 0x8000;
    records[0].size = 8;
    records[load_index].flags |= fastsim::kLoad;
    records[load_index].address = 0x8000;
    records[load_index].size = 8;
    records[load_index + 1].producer_dists[0] = 1;
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_pending_fill_response_closure() {
    const auto original = run_pending_fill_case(false);
    const auto corrected = run_pending_fill_case(true);
    const auto sample = [](const fastsim::SimulationStats& stats,
                           std::uint64_t sequence)
        -> const fastsim::ResponseFrontierAuditSample& {
        const auto& samples = stats.response_frontier_audit[0];
        const auto found = std::find_if(samples.begin(), samples.end(),
            [sequence](const auto& value) { return value.sequence == sequence; });
        if (found == samples.end()) throw std::runtime_error("missing pending-fill audit sample");
        return *found;
    };
    check(sample(original, 4).selected_memory_response_cycle <
              sample(original, 0).selected_memory_response_cycle,
          "directed input must expose the baseline premature L1 hit");
    check(sample(corrected, 4).selected_memory_response_cycle >=
              sample(corrected, 0).selected_memory_response_cycle,
          "a cold-line follower cannot return before its actual parent response");
    check(sample(corrected, 5).actual_issue_cycle >=
              sample(corrected, 4).actual_completion_cycle,
          "the corrected fill must propagate to the load's consumer");
    const auto& producer = sample(corrected, 4);
    const auto& consumer = sample(corrected, 5);
    check(consumer.issue_gate_kind == static_cast<std::uint32_t>(
                  fastsim::ResponseIssueGateKind::kRegisterProducer) &&
              consumer.issue_gate_owner_valid != 0 &&
              consumer.issue_gate_owner_sequence == 4 &&
              consumer.issue_gate_dependency_slot == 0 &&
              consumer.issue_gate_cross_checkpoint == 0 &&
              consumer.issue_gate_ready_cycle ==
                  producer.actual_completion_cycle &&
              consumer.issue_gate_owner_completion_cause ==
                  producer.completion_cause,
          "frontier audit must retain the exact register producer and the "
          "producer's response-side completion cause");
    check(producer.checkpoint_extra_cycles ==
                  consumer.checkpoint_extra_cycles &&
              producer.checkpoint_critical_cause ==
                  consumer.checkpoint_critical_cause,
          "frontier samples in one checkpoint must expose one committed "
          "interval displacement and cause");
    check(corrected.pending_fill[0].followers > 0 &&
              corrected.pending_fill[0].wait_cycles > 0,
          "response-time parent waits must be observable");
    check(corrected.total_core().retired_uops == original.total_core().retired_uops &&
              corrected.total_core().l1d.misses == original.total_core().l1d.misses &&
              corrected.sequencer_functional_replay_passes == 0,
          "pending response closure must use one canonical functional pass");
    const auto fast = run_pending_fill_case(true, false, false, true);
    check(fast.total_core().cycles == corrected.total_core().cycles,
          "pending fill fast kernel must match the auditable kernel");
    const auto ablated = run_pending_fill_case(true, false, false, false, true);
    check(ablated.total_core().cycles == original.total_core().cycles &&
              ablated.total_core().l1d.misses == original.total_core().l1d.misses,
          "disabling all three mechanisms must reproduce baseline timing");

    const auto boundary = run_pending_fill_case(true, false, false, true, false, true);
    check(boundary.total_core().retired_uops == 8194 &&
              boundary.time_epoch_request_boundary_deferred_uops > 0,
          "loads admitted after the fixed-Q horizon must defer without losing UOPs");

    const auto cross = run_pending_fill_case(true, true, true);
    const auto& store = sample(cross, 0);
    const auto& load = sample(cross, 1200);
    check(store.selected_memory_corrected_issue_cycle >= store.actual_retire_cycle + 2 &&
              store.selected_memory_response_cycle > store.actual_retire_cycle,
          "a regular store response admission must follow commit");
    check(load.selected_memory_response_cycle >= store.selected_memory_response_cycle &&
              cross.pending_fill[0].carried_parents > 0 && cross.interval_steps > 1,
          "a store fill must remain visible to a load across a fixed-Q boundary");
}

void test_ruby_sequencer_line_coalescing() {
    const auto reads = run_ruby_line_coalescing_case(false);
    const auto reads_core = reads.total_core();
    const auto reads_seq = reads.total_sequencer();
    fastsim::ChaCounters reads_cha;
    for (const auto& cha : reads.cha) reads_cha += cha;
    check(reads_core.memory_accesses == 2 &&
              reads_core.l1d.accesses == 1 &&
              reads_core.l1d.misses == 1 &&
              reads_core.l2.accesses == 1 &&
              reads.llc.accesses == 1 && reads_cha.requests == 1,
          "a same-line read follower must not probe private tags or issue "
          "a second hierarchy request");
    check(reads_seq.requests == 2 &&
              reads_seq.coalesced_requests == 1 &&
              reads_seq.coalesced_reads == 1 &&
              reads_seq.write_aliases == 0 &&
              reads_seq.coalesced_l1_parents == 0 &&
              reads_seq.coalesced_l2_parents == 0 &&
              reads_seq.coalesced_escape_parents == 1 &&
              reads_seq.coalesced_wait_cycles > 0 &&
              reads_seq.max_line_table_entries == 1,
          "the Sequencer line table must retain both CPU admissions while "
          "identifying the single coalesced read and its cold parent");

    const auto write = run_ruby_line_coalescing_case(true);
    const auto write_core = write.total_core();
    const auto write_seq = write.total_sequencer();
    fastsim::ChaCounters write_cha;
    for (const auto& cha : write.cha) write_cha += cha;
    check(write_core.memory_accesses == 2 &&
              write_core.l1d.accesses == 1 &&
              write_core.l2.accesses == 1 &&
              write.llc.accesses == 1 && write_cha.requests == 1 &&
              write_cha.upgrades == 0,
          "a write queued behind its cold read must reissue against the "
          "returned Exclusive line rather than fabricate an LLC upgrade");
    check(write_seq.coalesced_requests == 1 &&
              write_seq.write_aliases == 1 &&
              write_seq.coalesced_escape_parents == 1 &&
              write_seq.reissued_writes == 1 &&
              write_seq.reissued_write_permission_conflicts == 0,
          "the read-to-write line transition must be explicit and "
          "uncontended in the directed case");
}

fastsim::SimulationStats run_line_generation_admission_audit_case(
    bool enabled) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.ruby_sequencer_max_outstanding = 16;
    config.ruby_line_generation_admission_audit = enabled;
    config.chunk_instructions = 8;
    config.interval_target_uops = 8;
    config.interval_max_cycles = 1024;
    config.lookahead_chunks = 2;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    const auto load = [] {
        fastsim::TraceRecord record;
        record.address = 0x8000;
        record.size = 8;
        record.flags = fastsim::kRetires | fastsim::kLoad |
            fastsim::kPhysicalAddress;
        return record;
    };
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{load(), load()}));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_line_generation_admission_audit() {
    const auto baseline = run_line_generation_admission_audit_case(false);
    const auto audited = run_line_generation_admission_audit_case(true);
    const auto& ledger = audited.line_generation_admission_audit;
    const auto gate_proposals = std::accumulate(
        ledger.proposals_by_issue_gate.begin(),
        ledger.proposals_by_issue_gate.end(), std::uint64_t{0});
    check(ledger.population_conserved() && ledger.admitted_conserved() &&
              ledger.memory_events == 2 &&
              ledger.ordinary_load_proposals == 2 &&
              gate_proposals == ledger.ordinary_load_proposals &&
              ledger.actual_leaders == 1 &&
              ledger.actual_followers == 1 &&
              ledger.actual_capacity_blocks == 0 &&
              ledger.follower_response_changed == 1 &&
              ledger.follower_response_later_cycles > 0 &&
              ledger.max_actual_active == 1,
          "the final-admission shadow ledger must attach a same-line load "
          "to one bounded generation and inherit its response");
    check(audited.total_core().cycles == baseline.total_core().cycles &&
              audited.total_core().l1d.accesses ==
                  baseline.total_core().l1d.accesses &&
              audited.total_core().l1d.hits ==
                  baseline.total_core().l1d.hits &&
              audited.total_core().l1d.misses ==
                  baseline.total_core().l1d.misses &&
              audited.llc.accesses == baseline.llc.accesses &&
              audited.sequencer_functional_replay_passes == 0,
          "the admission audit must not mutate cache, response, replay, or "
          "CPI state");
}

fastsim::SimulationStats run_response_iq_case(
    bool response_feedback, bool monotone_iq_calendar = false,
    std::uint32_t iq_entries = 4) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.response_queue_feedback = response_feedback;
    config.response_monotone_iq_calendar = monotone_iq_calendar;
    config.chunk_instructions = 64;
    config.interval_target_uops = 64;
    config.interval_max_cycles = 4096;
    config.lookahead_chunks = 2;
    config.iq_entries = iq_entries;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    std::vector<fastsim::TraceRecord> records;
    records.reserve(64);
    for (std::uint64_t index = 0; index < 64; ++index) {
        fastsim::TraceRecord load;
        load.address = 0x8000 + index * 64;
        load.size = 8;
        load.flags = fastsim::kRetires | fastsim::kLoad |
                     fastsim::kPhysicalAddress;
        records.push_back(load);
    }
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_response_driven_iq_lifetime() {
    const auto lower_bound = run_response_iq_case(false);
    const auto response_driven = run_response_iq_case(true);
    check(response_driven.o3.size() == 1 &&
              response_driven.o3[0].iq_full_events > 0 &&
              response_driven.o3[0].iq_stall_cycles > 0 &&
              response_driven.o3[0].iq_max_occupancy <= 4,
          "load responses must extend the checkpointed IQ lifetime");
    check(response_driven.total_core().cycles >
              lower_bound.total_core().cycles,
          "response-held IQ entries must backpressure younger dispatch");
}

void test_response_monotone_iq_calendar() {
    const auto heap = run_response_iq_case(true, false, 64);
    const auto radix = run_response_iq_case(true, true, 64);
    const auto small_heap = run_response_iq_case(true, false, 4);
    const auto small_fallback = run_response_iq_case(true, true, 4);
    const auto& heap_o3 = heap.o3[0];
    const auto& radix_o3 = radix.o3[0];
    check(heap.total_core().cycles == radix.total_core().cycles &&
              heap.total_core().retired_uops ==
                  radix.total_core().retired_uops &&
              heap_o3.iq_full_events == radix_o3.iq_full_events &&
              heap_o3.iq_stall_cycles == radix_o3.iq_stall_cycles &&
              heap_o3.iq_max_occupancy ==
                  radix_o3.iq_max_occupancy &&
              heap.cores[0].l1d.accesses ==
                  radix.cores[0].l1d.accesses &&
              heap.cores[0].l1d.misses ==
                  radix.cores[0].l1d.misses &&
              heap.llc.accesses == radix.llc.accesses &&
              heap.llc.misses == radix.llc.misses,
          "monotone IQ calendar must preserve cycles, IQ PMU, and cache "
          "outcomes");
    check(radix.response_iq_radix_checkpoints > 0 &&
              radix.response_iq_radix_updates ==
                  radix.total_core().retired_uops,
          "monotone IQ calendar must audit every response IQ update");
    check(small_heap.total_core().cycles ==
                  small_fallback.total_core().cycles &&
              small_heap.o3[0].iq_full_events ==
                  small_fallback.o3[0].iq_full_events &&
              small_fallback.response_iq_radix_checkpoints == 0 &&
              small_fallback.response_iq_radix_updates == 0,
          "small response IQs must preserve the exact binary-heap fallback");
}

fastsim::SimulationStats run_shared_transient_fill_case(
    bool same_line, std::uint32_t fill_response_latency = 0) {
    fastsim::SimulatorConfig config;
    config.cores = 4;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.chunk_instructions = 8;
    config.interval_target_uops = 8;
    config.interval_max_cycles = 1024;
    config.lookahead_chunks = 2;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.llc_mshrs = 4;
    config.llc_fill_response_latency = fill_response_latency;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    for (std::uint32_t core = 0; core < config.cores; ++core) {
        fastsim::TraceRecord load;
        load.address = 0x200000 + (same_line ? 0 : core * 64);
        load.size = 8;
        load.flags = fastsim::kRetires | fastsim::kLoad |
                     fastsim::kPhysicalAddress;
        std::vector<fastsim::TraceRecord> records;
        records.push_back(load);
        traces.push_back(std::make_unique<VectorTraceSource>(
            std::move(records)));
    }
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_shared_transient_fill_merge() {
    const auto merged = run_shared_transient_fill_case(true);
    fastsim::ChaCounters merged_cha;
    for (const auto& cha : merged.cha) merged_cha += cha;
    check(merged.llc.accesses == 4 && merged.llc.misses == 1 &&
              merged.llc.hits == 0,
          "only the parent transient request may retain LLC tag-miss "
          "semantics");
    check(merged_cha.llc_unique_fills == 1 &&
              merged_cha.llc_merged_misses == 3 &&
              merged_cha.llc_misses == 1 &&
              merged_cha.dram_reads == 1 &&
              merged_cha.llc_outcomes_conserved(),
          "one unique LLC fill must serve all same-line secondary misses");
    check(merged_cha.llc_merged_wait_cycles > 0 &&
              merged_cha.llc_merged_wait_max_cycles > 0,
          "secondary misses must account for time waiting on the parent "
          "fill");

    const auto delayed_fill = run_shared_transient_fill_case(true, 17);
    fastsim::ChaCounters delayed_cha;
    for (const auto& cha : delayed_fill.cha) delayed_cha += cha;
    check(delayed_cha.llc_unique_fills == 1 &&
              delayed_cha.llc_merged_misses == 3 &&
              delayed_cha.llc_outcomes_conserved() &&
              delayed_fill.total_core().cycles >
                  merged.total_core().cycles,
          "Ruby fill-response latency must extend the transient/TBE "
          "lifetime without allocating another DRAM request");

    const auto independent = run_shared_transient_fill_case(false);
    fastsim::ChaCounters independent_cha;
    for (const auto& cha : independent.cha) independent_cha += cha;
    check(independent_cha.llc_unique_fills == 4 &&
              independent_cha.llc_merged_misses == 0 &&
              independent_cha.dram_reads == 4 &&
              independent_cha.llc_outcomes_conserved(),
          "different lines must allocate independent LLC fills");
}

fastsim::SimulationStats run_shared_outcome_case(bool remote) {
    fastsim::SimulatorConfig config;
    config.cores = remote ? 2 : 1;
    config.chunk_instructions = 4;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    const auto access = [](bool write) {
        fastsim::TraceRecord record;
        record.pc = write ? 0x1004 : 0x1000;
        record.address = 0x400000;
        record.size = 8;
        record.flags = fastsim::kRetires |
            (write ? fastsim::kStore : fastsim::kLoad) |
            fastsim::kPhysicalAddress;
        return record;
    };

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    if (remote) {
        traces.push_back(std::make_unique<VectorTraceSource>(
            std::vector<fastsim::TraceRecord>{access(true)}));
        traces.push_back(std::make_unique<VectorTraceSource>(
            std::vector<fastsim::TraceRecord>{access(false)}));
    } else {
        traces.push_back(std::make_unique<VectorTraceSource>(
            std::vector<fastsim::TraceRecord>{
                access(false), access(true)}));
    }
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_shared_cache_outcome_conservation() {
    const auto remote = run_shared_outcome_case(true);
    fastsim::ChaCounters remote_cha;
    for (const auto& cha : remote.cha) remote_cha += cha;
    check(remote.llc.accesses == 2 && remote.llc.hits == 0 &&
              remote.llc.misses == 1 && remote_cha.remote_supplies == 1 &&
              remote_cha.llc_hits == 0 && remote_cha.llc_misses == 1 &&
              remote_cha.llc_outcomes_conserved(),
          "a remote supply must not also count the internal LLC state "
          "lookup as a tag hit or miss");

    const auto upgrade = run_shared_outcome_case(false);
    fastsim::ChaCounters upgrade_cha;
    for (const auto& cha : upgrade.cha) upgrade_cha += cha;
    check(upgrade.llc.accesses == 2 && upgrade.llc.hits == 0 &&
              upgrade.llc.misses == 1 && upgrade_cha.upgrades == 1 &&
              upgrade_cha.llc_hits == 0 && upgrade_cha.llc_misses == 1 &&
              upgrade_cha.llc_outcomes_conserved(),
          "a permission upgrade must be one shared-cache outcome rather "
          "than an unaccounted request");
}

fastsim::SimulationStats run_dependency_slack_case(bool dependent) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.response_queue_feedback = true;
    config.chunk_instructions = 64;
    config.interval_target_uops = 64;
    config.interval_max_cycles = 4096;
    config.lookahead_chunks = 2;
    config.rob_entries = 64;
    config.iq_entries = 64;
    config.integer_divide_latency = 64;
    config.integer_divide_pipelined = false;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    std::vector<fastsim::TraceRecord> records;
    fastsim::TraceRecord load;
    load.address = 0x400000;
    load.size = 8;
    load.flags = fastsim::kRetires | fastsim::kLoad |
                 fastsim::kPhysicalAddress;
    records.push_back(load);
    for (std::uint32_t index = 0; index < 6; ++index) {
        fastsim::TraceRecord divide;
        divide.op_class = 3;
        if (dependent && index == 5) {
            divide.producer_dists[0] = 6;
        }
        records.push_back(divide);
    }

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_dependency_feedback_consumes_existing_slack() {
    const auto independent = run_dependency_slack_case(false);
    const auto dependent = run_dependency_slack_case(true);
    check(independent.llc.misses > 0,
          "dependency-slack regression requires a delayed shared-memory "
          "response");
    check(dependent.total_core().cycles ==
              independent.total_core().cycles,
          "producer response delay fully hidden by existing FU slack must "
          "not be charged again to the consumer");
}

enum class ResponseCapacityMode {
    kIqOnly,
    kDense,
    kSparse,
};

fastsim::SimulationStats run_persistent_rob_lsq_case(
    ResponseCapacityMode mode, bool needs_tso,
    bool memory_descriptor = false, bool pending_fill = false,
    bool fast_kernel = false) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.response_queue_feedback = true;
    config.response_rob_lsq_feedback =
        mode == ResponseCapacityMode::kDense;
    config.response_sparse_scoreboard =
        mode == ResponseCapacityMode::kSparse;
    config.response_memory_descriptor = memory_descriptor;
    config.response_pending_fill = pending_fill;
    config.response_pending_fill_load_admission = pending_fill;
    config.response_pending_fill_store_commit = pending_fill;
    config.response_materialized_uop_fast_kernel = fast_kernel;
    config.response_block_summary = pending_fill;
    config.response_monotone_iq_calendar = pending_fill;
    config.needs_tso = needs_tso;
    config.chunk_instructions = 64;
    config.interval_target_uops = 64;
    config.interval_max_cycles = pending_fill ? 1024 : 4096;
    config.lookahead_chunks = 2;
    config.rob_entries = 8;
    config.iq_entries = 8;
    config.lq_entries = 4;
    config.sq_entries = 4;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    std::vector<fastsim::TraceRecord> records;
    records.reserve(128);
    for (std::uint64_t index = 0; index < 128; ++index) {
        fastsim::TraceRecord memory;
        memory.address = 0x10000 + index * 64;
        memory.size = 8;
        const auto memory_flag =
            index % 7 == 0
                ? fastsim::kAtomic
                : index % 2 == 0 ? fastsim::kLoad
                                 : fastsim::kStore;
        memory.flags = fastsim::kRetires | memory_flag |
                       fastsim::kPhysicalAddress;
        if (pending_fill && index % 19 == 0) {
            memory.flags |= fastsim::kSerialize;
        }
        records.push_back(memory);
    }
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_pending_fill_bounded_resources() {
    const auto scalar = run_persistent_rob_lsq_case(
        ResponseCapacityMode::kSparse, true, true, true, false);
    const auto fast = run_persistent_rob_lsq_case(
        ResponseCapacityMode::kSparse, true, true, true, true);
    const auto& queues = scalar.o3[0];
    check(scalar.total_core().retired_uops == 128 &&
              scalar.total_core().serializing_uops > 0 &&
              scalar.pending_fill[0].requests > 0 &&
              queues.rob_max_occupancy <= 8 &&
              queues.iq_max_occupancy <= 8 &&
              queues.lq_max_occupancy <= 4 &&
              queues.sq_max_occupancy <= 4,
          "pending fills, atomic requests and serialization must preserve bounded queues");
    check(scalar.total_core().cycles == fast.total_core().cycles &&
              scalar.total_core().memory_accesses == fast.total_core().memory_accesses,
          "atomic and serialization traffic must agree across response kernels");
}

void test_persistent_rob_lsq_tso_feedback() {
    const auto iq_only = run_persistent_rob_lsq_case(
        ResponseCapacityMode::kIqOnly, true);
    const auto relaxed = run_persistent_rob_lsq_case(
        ResponseCapacityMode::kDense, false);
    const auto tso = run_persistent_rob_lsq_case(
        ResponseCapacityMode::kDense, true);
    check(relaxed.o3[0].rob_full_events > 0 &&
              relaxed.o3[0].sq_full_events > 0 &&
              relaxed.o3[0].rob_max_occupancy <= 8 &&
              relaxed.o3[0].lq_max_occupancy > 0 &&
              relaxed.o3[0].lq_max_occupancy <= 4 &&
              relaxed.o3[0].sq_max_occupancy <= 4,
          "response calendars must persist ROB/LQ/SQ capacity and stay "
          "within configured bounds");
    check(tso.o3[0].tso_store_stall_cycles > 0 &&
              tso.total_core().cycles >= relaxed.total_core().cycles &&
              relaxed.total_core().cycles > iq_only.total_core().cycles,
          "x86 TSO must retain post-commit stores until the single in-flight "
          "request completes");
    check(tso.total_core().memory_accesses == 128 &&
              tso.total_core().l1d.accesses == 128,
          "persistent queue feedback must not duplicate functional memory "
          "events");
}

void test_sparse_response_scoreboard_capacity() {
    const auto iq_only = run_persistent_rob_lsq_case(
        ResponseCapacityMode::kIqOnly, true);
    const auto relaxed = run_persistent_rob_lsq_case(
        ResponseCapacityMode::kSparse, false);
    const auto tso = run_persistent_rob_lsq_case(
        ResponseCapacityMode::kSparse, true);
    check(relaxed.sparse_scoreboard_seeds > 0 &&
              relaxed.sparse_scoreboard_materialized_uops > 0 &&
              relaxed.sparse_scoreboard_rob_crossings > 0 &&
              relaxed.sparse_scoreboard_lq_crossings > 0 &&
              relaxed.sparse_scoreboard_sq_crossings > 0,
          "sparse response scoreboard must expose response seeds and only "
          "the crossed ROB/LSQ capacity boundaries: seeds=" +
              std::to_string(relaxed.sparse_scoreboard_seeds) +
              " materialized=" + std::to_string(
                  relaxed.sparse_scoreboard_materialized_uops) +
              " rob=" + std::to_string(
                  relaxed.sparse_scoreboard_rob_crossings) +
              " lq=" + std::to_string(
                  relaxed.sparse_scoreboard_lq_crossings) +
              " sq=" + std::to_string(
                  relaxed.sparse_scoreboard_sq_crossings));
    check(relaxed.total_core().cycles > iq_only.total_core().cycles &&
              tso.total_core().cycles >= relaxed.total_core().cycles &&
              tso.o3[0].tso_store_stall_cycles > 0,
          "sparse ROB/LSQ state and x86 TSO drain must backpressure younger "
          "memory UOPs");
    check(tso.total_core().memory_accesses == 128 &&
              tso.total_core().l1d.accesses == 128,
          "sparse response repair must conserve functional memory events");
}

void test_response_memory_descriptor_equivalence() {
    const auto reference = run_persistent_rob_lsq_case(
        ResponseCapacityMode::kSparse, true, false);
    const auto descriptor = run_persistent_rob_lsq_case(
        ResponseCapacityMode::kSparse, true, true);
    const auto reference_total = reference.total_core();
    const auto descriptor_total = descriptor.total_core();
    const auto reference_o3 = reference.total_o3();
    const auto descriptor_o3 = descriptor.total_o3();

    check(reference_total.cycles == descriptor_total.cycles &&
              reference_total.retired_uops ==
                  descriptor_total.retired_uops &&
              reference_total.memory_accesses ==
                  descriptor_total.memory_accesses &&
              reference_total.l1d.accesses ==
                  descriptor_total.l1d.accesses &&
              reference_total.l2.accesses ==
                  descriptor_total.l2.accesses &&
              reference.llc.accesses == descriptor.llc.accesses &&
              reference.llc.misses == descriptor.llc.misses &&
              reference_o3.iq_full_events ==
                  descriptor_o3.iq_full_events &&
              reference_o3.iq_stall_cycles ==
                  descriptor_o3.iq_stall_cycles &&
              reference_o3.lq_full_events ==
                  descriptor_o3.lq_full_events &&
              reference_o3.sq_full_events ==
                  descriptor_o3.sq_full_events &&
              reference_o3.tso_store_stall_cycles ==
                  descriptor_o3.tso_store_stall_cycles &&
              reference.sparse_scoreboard_materialized_uops ==
                  descriptor.sparse_scoreboard_materialized_uops &&
              reference.sparse_scoreboard_rob_crossings ==
                  descriptor.sparse_scoreboard_rob_crossings,
          "producer memory descriptors must preserve load/store/atomic "
          "response admission state");
}

fastsim::SimulationStats run_sparse_cross_epoch_case(bool dependent) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.chunk_instructions = 64;
    config.interval_target_uops = 8;
    config.interval_max_cycles = 8;
    config.lookahead_chunks = 2;
    config.rob_entries = 16;
    config.iq_entries = 16;
    config.lq_entries = 4;
    config.sq_entries = 4;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    std::vector<fastsim::TraceRecord> records;
    records.reserve(32);
    fastsim::TraceRecord load;
    load.address = 0x400000;
    load.size = 8;
    load.flags = fastsim::kRetires | fastsim::kLoad |
                 fastsim::kPhysicalAddress;
    records.push_back(load);
    for (std::uint32_t index = 1; index < 32; ++index) {
        fastsim::TraceRecord alu;
        alu.op_class = 1;
        if (dependent && index == 12) {
            alu.producer_dists[0] = 12;
        }
        records.push_back(alu);
    }
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_sparse_cross_epoch_dependency() {
    const auto independent = run_sparse_cross_epoch_case(false);
    const auto dependent = run_sparse_cross_epoch_case(true);
    check(dependent.sparse_scoreboard_cross_epoch_edges > 0,
          "sequence-tagged ROB ring must carry producer completion across "
          "time-epoch boundaries");
    check(dependent.total_core().cycles >=
              independent.total_core().cycles,
          "cross-epoch dependency repair must not make a delayed producer "
          "complete earlier");
}

fastsim::SimulationStats run_response_rename_case(
    bool rename_free_list, bool response_rename_feedback,
    std::uint32_t fill_response_latency) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.rename_free_list = rename_free_list;
    config.response_rename_feedback = response_rename_feedback;
    config.rename_int_free_entries = 4;
    config.chunk_instructions = 16;
    config.interval_target_uops = 16;
    config.interval_max_cycles = 128;
    config.lookahead_chunks = 2;
    config.rob_entries = 16;
    config.iq_entries = 16;
    config.lq_entries = 4;
    config.sq_entries = 4;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.llc_fill_response_latency = fill_response_latency;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    std::vector<fastsim::TraceRecord> records(128);
    for (std::size_t index = 0; index < records.size(); ++index) {
        auto& record = records[index];
        record.op_class = 1;
        record.n_dst = 1;
        record.set_register_class_metadata(
            {255, 255, 255, 255}, {1, 0, 0, 0});
    }
    records[0].address = 0x600000;
    records[0].size = 8;
    records[0].flags = fastsim::kRetires | fastsim::kLoad |
                       fastsim::kPhysicalAddress;

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_response_aware_rename_free_list() {
    const auto no_free_list = run_response_rename_case(false, false, 128);
    const auto lower_response =
        run_response_rename_case(true, false, 128);
    const auto response_aware =
        run_response_rename_case(false, true, 128);
    const auto rename = response_aware.total_response_rename();
    const auto lower_rename = lower_response.total_response_rename();
    check(rename.destination_uops == 128 &&
              rename.allocated[0] == 128 &&
              rename.free_list_stall_uops > 0 &&
              rename.free_list_stall_cycles > 0 &&
              rename.max_live[0] == 4 &&
              rename.conserved(),
          "response-aware rename must stall at the per-class capacity and "
          "conserve mappings across resident chunks and time epochs: "
          "destination=" +
              std::to_string(rename.destination_uops) +
              " allocated=" + std::to_string(rename.allocated[0]) +
              " released=" + std::to_string(rename.released[0]) +
              " live=" + std::to_string(rename.live[0]) +
              " max_live=" + std::to_string(rename.max_live[0]) +
              " stalls=" +
              std::to_string(rename.free_list_stall_uops) +
              " stall_cycles=" +
              std::to_string(rename.free_list_stall_cycles) +
              " conserved=" +
              std::to_string(rename.conserved()));
    check(response_aware.total_core().cycles >
              lower_response.total_core().cycles &&
              response_aware.total_core().cycles >
                  no_free_list.total_core().cycles,
          "response-delayed retirement must postpone physical-register "
          "release and backpressure younger committed rename");
    check(rename.free_list_stall_cycles > 0 &&
              lower_rename.free_list_stall_cycles == 0,
          "increasing only the target Ruby fill-response stage must increase "
          "response-aware rename pressure");
    check(response_aware.total_core().retired_uops ==
              no_free_list.total_core().retired_uops &&
              response_aware.total_core().memory_accesses ==
                  no_free_list.total_core().memory_accesses &&
              response_aware.llc.accesses == no_free_list.llc.accesses,
          "rename timing repair must preserve functional and cache events");
}

fastsim::SimulationStats run_response_residual_ledger_case(
    bool rob_head_suffix_replay = false,
    bool small_rob = false,
    bool block_summary = false,
    bool store_post_commit_request = false,
    std::uint32_t response_frontier_audit_stride_uops = 0,
    bool response_paired_frontier = false,
    std::uint32_t interval_max_cycles = 8,
    std::uint64_t response_frontier_audit_begin_sequence = 0,
    std::uint64_t response_frontier_audit_end_sequence = 0,
    std::uint32_t response_frontier_audit_core =
        std::numeric_limits<std::uint32_t>::max(),
    bool ruby_sequencer_load_admission = false) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.cpi_attribution = true;
    config.committed_pipeline_audit = true;
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.response_block_summary = block_summary;
    config.response_frontier_audit_stride_uops =
        response_frontier_audit_stride_uops;
    config.response_frontier_audit_begin_sequence =
        response_frontier_audit_begin_sequence;
    config.response_frontier_audit_end_sequence =
        response_frontier_audit_end_sequence;
    config.response_frontier_audit_core =
        response_frontier_audit_core;
    config.response_paired_frontier = response_paired_frontier;
    config.ruby_sequencer_load_admission =
        ruby_sequencer_load_admission;
    if (ruby_sequencer_load_admission) {
        config.ruby_sequencer_max_outstanding = 16;
    }
    config.interval_rob_head_suffix_replay = rob_head_suffix_replay;
    config.store_post_commit_request = store_post_commit_request;
    config.chunk_instructions = 64;
    config.interval_target_uops = 8;
    config.interval_max_cycles = interval_max_cycles;
    config.lookahead_chunks = 2;
    config.rob_entries = 16;
    config.iq_entries = 16;
    config.lq_entries = 4;
    config.sq_entries = 4;
    if (small_rob) {
        config.rob_entries = rob_head_suffix_replay ? 1 : 4;
    }
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    if (rob_head_suffix_replay) {
        config.llc_fill_response_latency = 64;
    }
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    std::vector<fastsim::TraceRecord> records(32);
    records[0].address = 0x400000;
    records[0].size = 8;
    records[0].flags = fastsim::kRetires | fastsim::kLoad |
                       fastsim::kPhysicalAddress;
    for (std::size_t index = 1; index < records.size(); ++index) {
        records[index].op_class = 1;
    }
    records[12].address = 0x500000;
    records[12].size = 8;
    records[12].flags = fastsim::kRetires | fastsim::kLoad |
                        fastsim::kPhysicalAddress;
    records[12].producer_dists[0] = 12;
    records[20].address = 0x600000;
    records[20].size = 8;
    records[20].flags = fastsim::kRetires | fastsim::kStore |
                        fastsim::kPhysicalAddress;
    records[24].address = 0x700000;
    records[24].size = 8;
    records[24].flags = fastsim::kRetires | fastsim::kLoad |
                        fastsim::kPhysicalAddress;

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_ruby_sequencer_load_admission() {
    const auto baseline = run_response_residual_ledger_case(
        false, false, false, false, 1, false, 1024, 0, 0, 0, false);
    const auto admitted = run_response_residual_ledger_case(
        false, false, false, false, 1, false, 1024, 0, 0, 0, true);
    const auto find_first_load = [](const auto& stats) {
        for (const auto& sample : stats.response_frontier_audit[0]) {
            for (const auto& event : sample.memory_events) {
                if (!event.write && !event.instruction_fetch) {
                    return event;
                }
            }
        }
        throw std::runtime_error(
            "load-admission audit did not contain an ordinary load");
    };
    const auto baseline_load = find_first_load(baseline);
    const auto admitted_load = find_first_load(admitted);
    check(baseline_load.shared_stage_issue_cycle ==
              baseline_load.corrected_issue_cycle &&
              admitted_load.shared_stage_issue_cycle ==
                  admitted_load.corrected_issue_cycle + 1,
          "an ordinary load must move from IQ issue to Ruby admission by "
          "exactly the source-defined one-cycle execute edge");
    check(admitted_load.response_cycle ==
                  admitted_load.corrected_issue_cycle +
                      admitted_load.latency_cycles &&
              admitted_load.shared_response_cycle ==
                  admitted_load.response_cycle &&
              admitted_load.latency_cycles ==
                  admitted_load.shared_response_cycle -
                      admitted_load.shared_stage_issue_cycle + 1,
          "load admission replay must conserve one end-to-end producer "
          "response without double-counting the admission edge");
    const auto lifecycle = admitted.total_dram_request_lifecycle();
    check(lifecycle.population_conserved() &&
              lifecycle.timing_conserved() &&
              lifecycle.shared_stage_projection_events > 0 &&
              lifecycle.shared_stage_projection_forward_cycles > 0,
          "the DRAM lifecycle must expose and conserve the new admission "
          "stage");
    check(admitted.total_core().retired_uops ==
                  baseline.total_core().retired_uops &&
              admitted.total_core().memory_accesses ==
                  baseline.total_core().memory_accesses &&
              admitted.llc.accesses == baseline.llc.accesses &&
              admitted.llc.misses == baseline.llc.misses,
          "source load admission must preserve architectural and cache "
          "populations in the directed no-reordering case");
}

void test_response_residual_ledger_conservation() {
    const auto stats = run_response_residual_ledger_case();
    const auto residual = stats.total_response_residuals();
    const auto dram_lifecycle = stats.total_dram_request_lifecycle();
    check(residual.response_seed_events > 0 &&
              residual.response_seed_uops > 0 &&
              residual.completion_extension_cycles > 0,
          "response residual ledger must observe delayed load seeds");
    check(residual.dependency_edges > 0 &&
              residual.dependency_input_cycles > 0 &&
              residual.dependency_propagated_cycles > 0 &&
              residual.dependency_conserved(),
          "response residual ledger must conserve producer delay across "
          "dependency slack");
    check(residual.retire_input_cycles > 0 &&
              residual.retire_propagated_cycles > 0 &&
              residual.retire_conserved() &&
              residual.ordered_retire_moved_uops > 0,
          "response residual ledger must conserve completion delay at "
          "ordered retirement");
    check(residual.memory_issue_moved_events > 0 &&
              residual.escape_issue_moved_events > 0,
          "a response-dependent younger miss must expose its corrected "
          "shared-queue arrival");
    check(residual.store_uops == 1 &&
              residual.store_commit_to_sq_release_cycles > 0 &&
              residual.store_send_to_response_cycles > 0 &&
              residual.store_lifecycle_conserved(),
          "the response store ledger must conserve commit-to-send and "
          "send-to-response intervals through SQ release");
    check(residual.stage_uops == stats.total_core().retired_uops &&
              residual.stage_memory_uops > 0 &&
              residual.stage_non_memory_uops == 28 &&
              residual.stage_load_uops == 3 &&
              residual.stage_non_memory_corrected_fetch_to_issue_cycles >=
                  residual.stage_non_memory_base_fetch_to_issue_cycles &&
              residual.stage_load_corrected_fetch_to_issue_cycles >=
                  residual.stage_load_base_fetch_to_issue_cycles &&
              residual.stage_conserved(),
          "response-corrected committed issue/completion/retire stages "
          "must conserve every audited UOP");
    check(dram_lifecycle.requests > 0 &&
              dram_lifecycle.candidate_creates == dram_lifecycle.requests &&
              dram_lifecycle.corrected_issues == dram_lifecycle.requests &&
              dram_lifecycle.controller_arrivals ==
                  dram_lifecycle.requests &&
              dram_lifecycle.controller_services ==
                  dram_lifecycle.requests &&
              dram_lifecycle.responses == dram_lifecycle.requests &&
              dram_lifecycle.retires == dram_lifecycle.requests &&
              dram_lifecycle.controller_arrival_to_service_cycles > 0 &&
              dram_lifecycle.controller_service_to_response_cycles > 0 &&
              dram_lifecycle.population_conserved() &&
              dram_lifecycle.timing_conserved(),
          "unique data-DRAM requests must conserve candidate, corrected "
          "issue, controller arrival/service, response, and retire stages");
    fastsim::DramRequestLifecycleCounters projected_lifecycle;
    projected_lifecycle.record(false, 10, 20, 10, 15, 30, 50, 60);
    check(projected_lifecycle
                  .unprojected_issue_after_controller_arrival_events == 1 &&
              projected_lifecycle
                      .corrected_issue_after_controller_arrival_events == 0 &&
              projected_lifecycle.shared_stage_projection_events == 1 &&
              projected_lifecycle.shared_stage_projection_forward_cycles ==
                  10 &&
              projected_lifecycle.adjacent_backward_cycles == 0 &&
              projected_lifecycle.population_conserved() &&
              projected_lifecycle.timing_conserved(),
          "the DRAM lifecycle must distinguish a relative-latency stage "
          "projection from an effective causal inversion");
    fastsim::DramRequestLifecycleCounters inverted_lifecycle;
    inverted_lifecycle.record(false, 10, 20, 20, 15, 30, 50, 60);
    check(inverted_lifecycle.corrected_issue_after_controller_arrival_events ==
                  1 &&
              inverted_lifecycle.adjacent_backward_cycles == 5 &&
              inverted_lifecycle.population_conserved() &&
              inverted_lifecycle.timing_conserved(),
          "the projected DRAM lifecycle must retain genuine causal "
          "inversions and its signed adjacent-stage identity");
    const auto post_commit = run_response_residual_ledger_case(
        false, false, false, true);
    const auto post_commit_residual =
        post_commit.total_response_residuals();
    check(post_commit.store_post_commit_request_events > 0 &&
              post_commit.store_post_commit_request_delay_cycles > 0 &&
              post_commit_residual.store_uops == 1 &&
              post_commit_residual.store_commit_to_admission_cycles == 2 &&
              post_commit_residual.store_lifecycle_conserved() &&
              post_commit.total_core().retired_uops ==
                  stats.total_core().retired_uops &&
              post_commit.total_core().memory_accesses ==
                  stats.total_core().memory_accesses,
          "post-commit store requests must move cache-visible requests "
          "without changing functional UOP or memory populations");
    const auto epoch = stats.total_committed_epoch_audit();
    check(epoch.accepted_uops == stats.total_core().retired_uops &&
              epoch.memory_events == stats.batch_memory_events &&
              epoch.memory_events_conserved(),
          "committed epoch ledger must conserve accepted UOPs and memory "
          "events");
}

void test_rob_head_local_suffix_checkpoint() {
    const auto canonical = run_response_residual_ledger_case(false, true);
    const auto repaired = run_response_residual_ledger_case(true, true);
    const auto canonical_total = canonical.total_core();
    const auto repaired_total = repaired.total_core();
    const bool suffix_opened =
        repaired.rob_head_suffix_anchors != 0;
    check((suffix_opened &&
               repaired.rob_head_suffix_uops > 0) ||
              (!suffix_opened &&
               repaired.rob_head_suffix_uops == 0 &&
               repaired.rob_head_suffix_open_checkpoints == 0),
          "ROB-head suffix bookkeeping must be empty or consistently "
          "materialized after source-aligned backward capacity edges: anchors=" +
              std::to_string(repaired.rob_head_suffix_anchors) +
              " uops=" +
              std::to_string(repaired.rob_head_suffix_uops) +
              " open=" +
              std::to_string(
                  repaired.rob_head_suffix_open_checkpoints) +
              " recoveries=" +
              std::to_string(repaired.rob_head_suffix_recoveries));
    check(repaired.rob_head_suffix_candidate_epochs +
              repaired.rob_head_suffix_noop_epochs +
              repaired.rob_head_suffix_fallback_epochs > 0,
          "the local suffix must reach an explicit replay/noop/fallback "
          "certificate outcome");
    check(repaired_total.retired_uops == canonical_total.retired_uops &&
              repaired_total.memory_accesses ==
                  canonical_total.memory_accesses &&
              repaired_total.l1d.accesses ==
                  canonical_total.l1d.accesses &&
              repaired_total.l2.accesses ==
                  canonical_total.l2.accesses &&
              repaired.llc.accesses == canonical.llc.accesses &&
              repaired.llc.misses == canonical.llc.misses,
          "ROB-head-local timing replay must conserve functional and "
          "cache PMU counts");
}

void test_response_block_summary_equivalence() {
    const auto reference =
        run_response_residual_ledger_case(false, false, false);
    const auto summarized =
        run_response_residual_ledger_case(false, false, true);
    const auto reference_total = reference.total_core();
    const auto summarized_total = summarized.total_core();
    const auto reference_o3 = reference.total_o3();
    const auto summarized_o3 = summarized.total_o3();
    const auto reference_residual =
        reference.total_response_residuals();
    const auto summarized_residual =
        summarized.total_response_residuals();

    check(summarized.response_block_summary_checkpoints > 0 &&
              summarized.response_block_summary_uops ==
                  summarized_total.retired_uops &&
              summarized.response_block_summary_rob_writes > 0,
          "block summary must commit a compact ROB exit state");
    check(reference_total.cycles == summarized_total.cycles &&
              reference_total.retired_uops ==
                  summarized_total.retired_uops &&
              reference_total.memory_accesses ==
                  summarized_total.memory_accesses &&
              reference_total.l1d.accesses ==
                  summarized_total.l1d.accesses &&
              reference_total.l2.accesses ==
                  summarized_total.l2.accesses &&
              reference.llc.accesses == summarized.llc.accesses &&
              reference.llc.misses == summarized.llc.misses &&
              reference_o3.iq_full_events ==
                  summarized_o3.iq_full_events &&
              reference_o3.iq_stall_cycles ==
                  summarized_o3.iq_stall_cycles &&
              reference_o3.rob_full_events ==
                  summarized_o3.rob_full_events &&
              reference_o3.rob_stall_cycles ==
                  summarized_o3.rob_stall_cycles &&
              reference.sparse_scoreboard_seeds ==
                  summarized.sparse_scoreboard_seeds &&
              reference.sparse_scoreboard_materialized_uops ==
                  summarized.sparse_scoreboard_materialized_uops &&
              reference.sparse_scoreboard_rob_crossings ==
                  summarized.sparse_scoreboard_rob_crossings &&
              reference_residual.response_seed_events ==
                  summarized_residual.response_seed_events &&
              reference_residual.dependency_input_cycles ==
                  summarized_residual.dependency_input_cycles &&
              reference_residual.retire_input_cycles ==
                  summarized_residual.retire_input_cycles,
          "incremental ROB block summary must match per-UOP ring writes");
}

void test_response_frontier_audit() {
    const auto unaudited =
        run_response_residual_ledger_case(false, false, true);
    const auto audited =
        run_response_residual_ledger_case(false, false, true, false, 4);
    const auto per_uop_ring =
        run_response_residual_ledger_case(false, false, false, false, 4);
    const auto targeted = run_response_residual_ledger_case(
        false, false, false, false, 1, true, 8, 12, 24, 0);
    check(unaudited.total_core().cycles == audited.total_core().cycles &&
              unaudited.total_core().retired_uops ==
                  audited.total_core().retired_uops &&
              unaudited.total_core().memory_accesses ==
                  audited.total_core().memory_accesses &&
              unaudited.llc.accesses == audited.llc.accesses &&
              unaudited.llc.misses == audited.llc.misses,
          "response frontier auditing must not change timing or PMU state");
    check(audited.response_frontier_audit.size() == 1 &&
              !audited.response_frontier_audit[0].empty(),
          "response frontier auditing must emit per-core milestones");
    const auto& summarized_samples = audited.response_frontier_audit[0];
    const auto& materialized_samples =
        per_uop_ring.response_frontier_audit[0];
    check(summarized_samples.size() == materialized_samples.size(),
          "block-summary and per-UOP ROB paths must sample the same "
          "milestones");
    for (std::size_t index = 0; index < summarized_samples.size(); ++index) {
        const auto& summarized = summarized_samples[index];
        const auto& materialized = materialized_samples[index];
        check((index == 0 || summarized.sequence % 4 == 3) &&
                  (index == 0 ||
                   summarized_samples[index - 1].sequence <
                       summarized.sequence),
              "response frontier milestones must include measurement entry "
              "and ordered fixed-sequence samples");
        check(summarized.sequence == materialized.sequence &&
                  summarized.interval_gap_cycles ==
                      materialized.interval_gap_cycles &&
                  summarized.actual_fetch_cycle ==
                      materialized.actual_fetch_cycle &&
                  summarized.actual_dispatch_cycle ==
                      materialized.actual_dispatch_cycle &&
                  summarized.actual_issue_cycle ==
                      materialized.actual_issue_cycle &&
                  summarized.actual_completion_cycle ==
                      materialized.actual_completion_cycle &&
                  summarized.actual_retire_cycle ==
                      materialized.actual_retire_cycle &&
                  summarized.sequencer_min_release_cycle ==
                      materialized.sequencer_min_release_cycle &&
                  summarized.iq_min_release_cycle ==
                      materialized.iq_min_release_cycle &&
                  summarized.rob_head_retire_cycle ==
                      materialized.rob_head_retire_cycle &&
                  summarized.lq_head_release_cycle ==
                      materialized.lq_head_release_cycle &&
                  summarized.sq_head_release_cycle ==
                      materialized.sq_head_release_cycle &&
                  summarized.dispatch_cause ==
                      materialized.dispatch_cause &&
                  summarized.completion_cause ==
                      materialized.completion_cause &&
                  summarized.retire_cause == materialized.retire_cause &&
                  summarized.sequencer_digest ==
                      materialized.sequencer_digest &&
                  summarized.iq_digest == materialized.iq_digest &&
                  summarized.rob_digest == materialized.rob_digest &&
                  summarized.lq_digest == materialized.lq_digest &&
                  summarized.sq_digest == materialized.sq_digest &&
                  summarized.fetch_queue_digest ==
                      materialized.fetch_queue_digest &&
                  summarized.rename_release_digest ==
                      materialized.rename_release_digest,
              "frontier audit must reconstruct the block-summary ROB state "
              "without changing its semantic snapshot");
    }
    const auto& targeted_samples = targeted.response_frontier_audit[0];
    check(!targeted_samples.empty() &&
              targeted_samples.front().sequence >= 12 &&
              targeted_samples.back().sequence <= 24,
          "response frontier sequence filters must bound fine-grained "
          "audit output");
    const auto load_sample = std::find_if(
        targeted_samples.begin(), targeted_samples.end(),
        [](const auto& sample) { return sample.sequence == 12; });
    check(load_sample != targeted_samples.end() &&
              load_sample->producer_dists[0] == 12 &&
              load_sample->memory_event_count != 0 &&
              load_sample->selected_memory_valid != 0 &&
              load_sample->selected_memory_response_cycle != 0 &&
              load_sample->checkpoint_begin_sequence <=
                  load_sample->sequence &&
              load_sample->checkpoint_end_sequence >=
                  load_sample->sequence,
          "fine-grained frontier samples must identify producer and memory "
          "response context");
    const auto rob_sample = std::find_if(
        targeted_samples.begin(), targeted_samples.end(),
        [](const auto& sample) {
            return sample.rob_capacity_predecessor_valid != 0;
        });
    check(rob_sample != targeted_samples.end() &&
              rob_sample->rob_capacity_predecessor_sequence + 16 ==
                  rob_sample->sequence,
          "fine-grained frontier samples must expose the direct ROB "
          "capacity predecessor");
}

fastsim::SimulationStats run_sq_owner_audit_case(bool audit) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.cpi_attribution = true;
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.response_block_summary = true;
    config.response_frontier_audit_stride_uops = audit ? 1 : 0;
    config.chunk_instructions = 8;
    config.interval_target_uops = 2;
    config.interval_max_cycles = 8;
    config.lookahead_chunks = 2;
    config.rob_entries = 4;
    config.iq_entries = 4;
    config.lq_entries = 1;
    config.sq_entries = 1;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    std::vector<fastsim::TraceRecord> records(8);
    for (std::size_t index = 0; index < records.size(); ++index) {
        records[index].pc = 0x1000 + 4 * index;
        records[index].flags = fastsim::kRetires;
    }
    for (const auto index : {0u, 4u}) {
        records[index].address = 0x400000 + 64 * index;
        records[index].size = 8;
        records[index].flags |=
            fastsim::kStore | fastsim::kPhysicalAddress;
    }
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_response_frontier_sq_owner_audit() {
    const auto unaudited = run_sq_owner_audit_case(false);
    const auto audited = run_sq_owner_audit_case(true);
    check(unaudited.total_core().cycles == audited.total_core().cycles &&
              unaudited.total_core().memory_accesses ==
                  audited.total_core().memory_accesses &&
              unaudited.llc.accesses == audited.llc.accesses,
          "SQ owner auditing must not change timing or cache population");
    const auto& samples = audited.response_frontier_audit[0];
    const auto second_store = std::find_if(
        samples.begin(), samples.end(),
        [](const auto& sample) { return sample.sequence == 4; });
    check(second_store != samples.end() &&
              second_store->incoming_sq_release_valid != 0 &&
              second_store->incoming_sq_release_sequence == 0 &&
              second_store->incoming_sq_release_cycle != 0 &&
              second_store
                      ->incoming_sq_release_displacement_cycles == 0,
          "ordinary frontier audit must expose the direct SQ slot owner "
          "without enabling paired-frontier timing");
}

void test_paired_response_frontier_settlement() {
    const auto q4 = run_response_residual_ledger_case(
        false, false, true, false, 4, true, 4);
    const auto q16 = run_response_residual_ledger_case(
        false, false, true, false, 4, true, 16);
    const auto verify = [](const fastsim::SimulationStats& stats) {
        const auto critical = stats.total_response_critical_cycles();
        const auto frontier =
            stats.total_response_frontier_settlement();
        check(frontier.checkpoints > 0 &&
                  frontier.open_checkpoints > 0 &&
                  frontier.open_frontier_cycles >=
                      frontier.final_open_frontier_cycles &&
                  frontier.final_open_frontier_cycles >=
                      frontier.final_tail_cycles &&
                  frontier.total_critical_cycles() ==
                      critical.total_cycles &&
                  frontier.conserved(critical.total_cycles),
              "paired response frontier must separately conserve closed "
              "gap, resident open displacement, and the final ordered "
              "retire tail");
    };
    verify(q4);
    verify(q16);
    check(q4.total_core().retired_uops ==
              q16.total_core().retired_uops &&
              q4.total_core().memory_accesses ==
                  q16.total_core().memory_accesses &&
              q4.llc.accesses == q16.llc.accesses &&
              q4.llc.misses == q16.llc.misses,
          "paired frontier checkpoint partitions must preserve functional "
          "and cache populations");
    check(q4.total_core().cycles == q16.total_core().cycles,
          "paired lower-bound/open-response frontier must make the directed "
          "single-core retirement path independent of Q: q4=" +
              std::to_string(q4.total_core().cycles) + " q16=" +
              std::to_string(q16.total_core().cycles));
}

fastsim::SimulationStats run_response_causal_block_case(
    bool causal_block_transfer, bool serializing_tail,
    bool sequencer_tail = false,
    bool materialized_fast_kernel = false,
    bool event_only_approximation = false) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.response_block_summary = true;
    config.response_memory_descriptor = true;
    config.response_monotone_iq_calendar = true;
    config.response_causal_block_transfer = causal_block_transfer;
    config.response_materialized_uop_fast_kernel =
        materialized_fast_kernel;
    config.response_event_only_approximation =
        event_only_approximation;
    if (event_only_approximation) {
        config.response_event_only_calibration_checkpoints = 1;
        config.response_event_only_teacher_stride = 0;
    }
    config.chunk_instructions = 512;
    config.interval_target_uops = 256;
    config.interval_max_cycles =
        event_only_approximation ? 32 : 1024;
    config.lookahead_chunks = 2;
    config.rob_entries = 192;
    config.iq_entries = 64;
    config.lq_entries = 72;
    config.sq_entries = 56;
    if (sequencer_tail) {
        config.ruby_sequencer_max_outstanding = 1;
        config.float_sqrt_latency = 1024;
    }
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    std::vector<fastsim::TraceRecord> records(512);
    for (std::size_t index = 0; index < records.size(); ++index) {
        records[index].pc = 0x1000 + index * 4;
        records[index].op_class = 1;
    }
    if (serializing_tail) {
        // Exercise the read-only static preflight fallback.
        records[63].flags |= fastsim::kSerialize;
    }
    if (sequencer_tail) {
        const auto make_load = [](fastsim::TraceRecord& record) {
            record.address = 0x400000;
            record.size = 8;
            record.flags = fastsim::kRetires | fastsim::kLoad |
                fastsim::kPhysicalAddress;
        };
        // The first access warms the line.  A burst near the end of an
        // otherwise eligible block passes static preflight, advances most
        // private candidate state, then dynamically rejects on the one-entry
        // Sequencer.  Scalar replay must start from an untouched entry state.
        make_load(records[0]);
        // A long lower-bound operation absorbs the initial compulsory miss,
        // allowing later blocks to re-enter the exact baseline certificate.
        records[64].op_class = 11;
        for (std::size_t index = 248; index < 256; ++index) {
            make_load(records[index]);
        }
    }
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void check_response_causal_block_equivalence(
    const fastsim::SimulationStats& reference,
    const fastsim::SimulationStats& transferred,
    const std::string& context) {
    const auto ref_total = reference.total_core();
    const auto got_total = transferred.total_core();
    const auto ref_o3 = reference.total_o3();
    const auto got_o3 = transferred.total_o3();
    check(ref_total.cycles == got_total.cycles &&
              ref_total.retired_instructions ==
                  got_total.retired_instructions &&
              ref_total.retired_uops == got_total.retired_uops &&
              ref_total.memory_accesses == got_total.memory_accesses &&
              ref_total.l1d.accesses == got_total.l1d.accesses &&
              ref_total.l2.accesses == got_total.l2.accesses &&
              reference.llc.accesses == transferred.llc.accesses &&
              reference.llc.misses == transferred.llc.misses &&
              ref_o3.iq_full_events == got_o3.iq_full_events &&
              ref_o3.iq_stall_cycles == got_o3.iq_stall_cycles &&
              ref_o3.iq_max_occupancy == got_o3.iq_max_occupancy &&
              ref_o3.rob_full_events == got_o3.rob_full_events &&
              ref_o3.rob_stall_cycles == got_o3.rob_stall_cycles &&
              ref_o3.rob_max_occupancy == got_o3.rob_max_occupancy &&
              ref_o3.lq_full_events == got_o3.lq_full_events &&
              ref_o3.lq_stall_cycles == got_o3.lq_stall_cycles &&
              ref_o3.lq_max_occupancy == got_o3.lq_max_occupancy &&
              ref_o3.sq_full_events == got_o3.sq_full_events &&
              ref_o3.sq_stall_cycles == got_o3.sq_stall_cycles &&
              ref_o3.sq_max_occupancy == got_o3.sq_max_occupancy &&
              ref_o3.tso_store_stall_cycles ==
                  got_o3.tso_store_stall_cycles &&
              reference.sparse_scoreboard_seeds ==
                  transferred.sparse_scoreboard_seeds &&
              reference.sparse_scoreboard_materialized_uops ==
                  transferred.sparse_scoreboard_materialized_uops &&
              reference.sparse_scoreboard_absorbed_edges ==
                  transferred.sparse_scoreboard_absorbed_edges &&
              reference.sparse_scoreboard_cross_epoch_edges ==
                  transferred.sparse_scoreboard_cross_epoch_edges &&
              reference.sparse_scoreboard_rob_crossings ==
                  transferred.sparse_scoreboard_rob_crossings &&
              reference.sparse_scoreboard_lq_crossings ==
                  transferred.sparse_scoreboard_lq_crossings &&
              reference.sparse_scoreboard_sq_crossings ==
                  transferred.sparse_scoreboard_sq_crossings &&
              reference.response_block_summary_rob_writes ==
                  transferred.response_block_summary_rob_writes &&
              reference.response_block_summary_rob_writes_avoided ==
                  transferred.response_block_summary_rob_writes_avoided &&
              reference.sequencer[0].requests ==
                  transferred.sequencer[0].requests &&
              reference.sequencer[0].buffer_full_stalls ==
                  transferred.sequencer[0].buffer_full_stalls &&
              reference.sequencer[0].stall_cycles ==
                  transferred.sequencer[0].stall_cycles &&
              reference.sequencer[0].max_outstanding ==
                  transferred.sequencer[0].max_outstanding,
          context + " must preserve timing, PMU, queue state, and sparse "
                    "scoreboard accounting");
}

void test_response_causal_block_transfer() {
    const auto reference = run_response_causal_block_case(false, false);
    const auto transferred = run_response_causal_block_case(true, false);
    check_response_causal_block_equivalence(
        reference, transferred, "64-UOP causal block transfer");
    check(transferred.response_causal_block_transfers[0] > 0 &&
              transferred.response_causal_block_transferred_uops >= 64,
          "an aligned inactive interval must commit at least one exact "
          "64-UOP response transfer");

    const auto serial_reference =
        run_response_causal_block_case(false, true);
    const auto serial_fallback =
        run_response_causal_block_case(true, true);
    check_response_causal_block_equivalence(
        serial_reference, serial_fallback,
        "late serializing causal-block fallback");
    check(serial_fallback.response_causal_block_transferred_uops > 0,
          "a late serialize edge must leave other certified blocks "
          "transferable after its exact scalar fallback");

    const auto dynamic_reference =
        run_response_causal_block_case(false, false, true);
    const auto dynamic_fallback =
        run_response_causal_block_case(true, false, true);
    check_response_causal_block_equivalence(
        dynamic_reference, dynamic_fallback,
        "late Sequencer causal-block rollback");
    check(dynamic_fallback.response_causal_block_candidates[0] +
                  dynamic_fallback.response_causal_block_candidates[1] +
                  dynamic_fallback.response_causal_block_candidates[2] >
              dynamic_fallback.response_causal_block_transfers[0] +
                  dynamic_fallback.response_causal_block_transfers[1] +
                  dynamic_fallback.response_causal_block_transfers[2],
          "a late dynamic capacity failure must discard the private block "
          "state before exact scalar replay: candidates64=" +
              std::to_string(
                  dynamic_fallback.response_causal_block_candidates[0]) +
              " transfers64=" +
              std::to_string(
                  dynamic_fallback.response_causal_block_transfers[0]) +
              " candidates32=" +
              std::to_string(
                  dynamic_fallback.response_causal_block_candidates[1]) +
              " transfers32=" +
              std::to_string(
                  dynamic_fallback.response_causal_block_transfers[1]) +
              " candidates16=" +
              std::to_string(
                  dynamic_fallback.response_causal_block_candidates[2]) +
              " transfers16=" +
              std::to_string(
                  dynamic_fallback.response_causal_block_transfers[2]));
}

void test_materialized_uop_fast_kernel() {
    const auto reference = run_response_causal_block_case(
        false, false, true, false);
    const auto specialized = run_response_causal_block_case(
        false, false, true, true);
    check_response_causal_block_equivalence(
        reference, specialized,
        "materialized-UOP maintained-profile fast kernel");
    check(specialized.response_materialized_fast_kernel_checkpoints > 0 &&
              specialized.response_materialized_fast_kernel_uops ==
                  specialized.interval_accepted_uops,
          "the maintained-profile fast kernel must account for every "
          "accepted UOP exactly once");
}

void test_response_event_only_approximation() {
    const auto reference = run_response_causal_block_case(
        false, false, true, true, false);
    const auto approximate = run_response_causal_block_case(
        false, false, true, true, true);
    const auto reference_total = reference.total_core();
    const auto approximate_total = approximate.total_core();
    check(reference_total.retired_uops == approximate_total.retired_uops &&
              reference_total.retired_instructions ==
                  approximate_total.retired_instructions &&
              reference_total.memory_accesses ==
                  approximate_total.memory_accesses &&
              reference_total.l1d.accesses ==
                  approximate_total.l1d.accesses &&
              reference_total.l1d.misses ==
                  approximate_total.l1d.misses &&
              reference.llc.accesses == approximate.llc.accesses &&
              reference.llc.misses == approximate.llc.misses,
          "event-only response approximation must retain the complete "
          "functional memory/PMU event population");
    check(approximate.response_event_only_calibration_checkpoints > 0 &&
              approximate.response_event_only_calibration_uops > 0 &&
              approximate.response_event_only_approximation_checkpoints > 0 &&
              approximate.response_event_only_anchor_uops > 0 &&
              approximate.response_event_only_skipped_uops > 0 &&
              approximate.response_event_only_calibration_uops +
                      approximate.response_event_only_anchor_uops +
                      approximate.response_event_only_skipped_uops ==
                  approximate.interval_accepted_uops,
          "event-only response approximation must partition every accepted "
          "UOP into calibration, event anchors, or aggregated ordinary "
          "UOPs");
}

void test_event_only_calibration_starts_at_measurement() {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.response_block_summary = true;
    config.response_memory_descriptor = true;
    config.response_monotone_iq_calendar = true;
    config.response_materialized_uop_fast_kernel = true;
    config.response_event_only_approximation = true;
    config.response_event_only_calibration_checkpoints = 1;
    config.response_event_only_teacher_stride = 0;
    config.chunk_instructions = 128;
    config.interval_target_uops = 64;
    config.interval_max_cycles = 16;
    config.lookahead_chunks = 2;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    std::vector<fastsim::TraceRecord> records(1024);
    for (std::size_t index = 0; index < records.size(); ++index) {
        auto& record = records[index];
        record.pc = 0x1000 + index * 4;
        record.op_class = 1;
        record.flags = fastsim::kRetires;
        if ((index & 31) == 0) {
            record.address = 0x400000 + (index & 255) * 64;
            record.size = 8;
            record.flags = static_cast<std::uint16_t>(
                record.flags | fastsim::kLoad |
                fastsim::kPhysicalAddress);
        }
    }
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(
        std::make_unique<fastsim::WarmupInstructionTraceSource>(
            std::make_unique<VectorTraceSource>(std::move(records)),
            512, 512));
    fastsim::Simulator simulator(config, std::move(traces));
    const auto stats = simulator.run();
    check(stats.functional_warmup_enabled &&
              stats.functional_warmup_uops == 512 &&
              stats.interval_accepted_uops == 512 &&
              stats.response_event_only_calibration_checkpoints == 1 &&
              stats.response_event_only_calibration_uops > 0 &&
              stats.response_event_only_approximation_checkpoints > 0 &&
              stats.response_event_only_calibration_uops +
                      stats.response_event_only_anchor_uops +
                      stats.response_event_only_skipped_uops ==
                  stats.interval_accepted_uops,
          "event-only calibration must begin at the measurement boundary "
          "without freezing sparse response state during warmup");
}

void test_event_only_teacher_partition() {
    fastsim::SimulatorConfig config;
    config.cores = 3;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.response_block_summary = true;
    config.response_memory_descriptor = true;
    config.response_monotone_iq_calendar = true;
    config.response_materialized_uop_fast_kernel = true;
    config.response_event_only_approximation = true;
    config.response_event_only_calibration_checkpoints = 1;
    config.response_event_only_teacher_stride = 3;
    config.response_event_only_teacher_offset = 1;
    config.response_event_only_teacher_window_epochs = 4;
    config.chunk_instructions = 128;
    config.interval_target_uops = 64;
    config.interval_max_cycles = 16;
    config.lookahead_chunks = 2;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    for (std::uint32_t core = 0; core < config.cores; ++core) {
        std::vector<fastsim::TraceRecord> records(512);
        for (std::size_t index = 0; index < records.size(); ++index) {
            auto& record = records[index];
            record.pc = 0x1000 + core * 0x10000 + index * 4;
            record.op_class = 1;
            record.flags = fastsim::kRetires;
            if ((index & 31) == 0) {
                record.address = 0x400000 + core * 0x100000 +
                    (index & 255) * 64;
                record.size = 8;
                record.flags = static_cast<std::uint16_t>(
                    record.flags | fastsim::kLoad |
                    fastsim::kPhysicalAddress);
            }
        }
        traces.push_back(std::make_unique<VectorTraceSource>(
            std::move(records)));
    }

    fastsim::Simulator simulator(config, std::move(traces));
    const auto stats = simulator.run();
    check(stats.response_event_only_teacher_checkpoints > 0 &&
              stats.response_event_only_teacher_uops > 0 &&
              stats.response_event_only_teacher_exact_cycles > 0 &&
              stats.response_event_only_teacher_reference_checkpoints > 0 &&
              stats.response_event_only_teacher_reference_uops > 0 &&
              stats.response_event_only_teacher_reference_exact_cycles > 0 &&
              stats.response_event_only_calibration_checkpoints > 0 &&
              stats.response_event_only_approximation_checkpoints > 0 &&
              stats.response_event_only_skipped_uops > 0 &&
              stats.response_event_only_calibration_uops +
                      stats.response_event_only_teacher_uops +
                      stats.response_event_only_anchor_uops +
                      stats.response_event_only_skipped_uops ==
                  stats.interval_accepted_uops,
          "event-only teacher sampling must retain exact sentinel work and "
          "partition every accepted UOP exactly once: teacher_cp=" +
              std::to_string(
                  stats.response_event_only_teacher_checkpoints) +
              " teacher_uops=" +
              std::to_string(stats.response_event_only_teacher_uops) +
              " teacher_cycles=" +
              std::to_string(
                  stats.response_event_only_teacher_exact_cycles) +
              " reference_cp=" +
              std::to_string(
                  stats.response_event_only_teacher_reference_checkpoints) +
              " reference_uops=" +
              std::to_string(
                  stats.response_event_only_teacher_reference_uops) +
              " reference_cycles=" +
              std::to_string(
                  stats.response_event_only_teacher_reference_exact_cycles) +
              " calibration_cp=" +
              std::to_string(
                  stats.response_event_only_calibration_checkpoints) +
              " approximation_cp=" +
              std::to_string(
                  stats.response_event_only_approximation_checkpoints) +
              " skipped=" +
              std::to_string(stats.response_event_only_skipped_uops) +
              " accepted=" +
              std::to_string(stats.interval_accepted_uops));
}

fastsim::SimulationStats run_response_activity_case(
    bool activity_certificate, bool serializing_uop,
    bool timing_ledger = false) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.response_activity_certificate = activity_certificate;
    config.cpi_attribution = timing_ledger;
    config.committed_pipeline_audit = timing_ledger;
    config.chunk_instructions = 512;
    config.interval_target_uops = 256;
    config.interval_max_cycles = 1024;
    config.lookahead_chunks = 2;
    config.rob_entries = 32;
    config.iq_entries = 16;
    config.lq_entries = 8;
    config.sq_entries = 8;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    std::vector<fastsim::TraceRecord> records(512);
    for (std::size_t index = 0; index < records.size(); ++index) {
        auto& alu = records[index];
        alu.pc = 0x1000 + index * 4;
        alu.op_class = 1;
        if (index != 0) alu.producer_dists[0] = 1;
    }
    if (serializing_uop) {
        records[256].flags = fastsim::kRetires | fastsim::kSerialize;
    }
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void check_response_activity_equivalence(
    const fastsim::SimulationStats& reference,
    const fastsim::SimulationStats& certified,
    const std::string& context) {
    const auto& ref_o3 = reference.o3[0];
    const auto& got_o3 = certified.o3[0];
    check(reference.total_core().cycles ==
                  certified.total_core().cycles &&
              reference.total_core().retired_uops ==
                  certified.total_core().retired_uops &&
              ref_o3.iq_full_events == got_o3.iq_full_events &&
              ref_o3.iq_stall_cycles == got_o3.iq_stall_cycles &&
              ref_o3.iq_max_occupancy == got_o3.iq_max_occupancy &&
              ref_o3.rob_full_events == got_o3.rob_full_events &&
              ref_o3.rob_stall_cycles == got_o3.rob_stall_cycles &&
              reference.sparse_scoreboard_absorbed_edges ==
                  certified.sparse_scoreboard_absorbed_edges &&
              reference.sparse_scoreboard_cross_epoch_edges ==
                  certified.sparse_scoreboard_cross_epoch_edges &&
              reference.sparse_scoreboard_materialized_uops ==
                  certified.sparse_scoreboard_materialized_uops,
          context + " must preserve cycles, queue PMU, and sparse "
                    "dependency accounting");
}

void test_response_activity_certificate() {
    const auto full = run_response_activity_case(false, false);
    const auto certified = run_response_activity_case(true, false);
    check_response_activity_equivalence(
        full, certified, "response-inactive fast path");
    check(certified.response_activity_candidates > 0 &&
              certified.response_activity_certified_segments > 0 &&
              certified.response_activity_certified_uops == 512 &&
              certified.response_activity_fallback_segments == 0,
          "a long memory-free checkpoint must pass the activity entry/exit "
          "certificate");

    const auto serial_full = run_response_activity_case(false, true);
    const auto serial_fallback = run_response_activity_case(true, true);
    check_response_activity_equivalence(
        serial_full, serial_fallback,
        "serializing activity-certificate fallback");
    check(serial_fallback.response_activity_candidates > 0 &&
              serial_fallback.response_activity_certified_segments == 0 &&
              serial_fallback.response_activity_fallback_segments > 0,
          "a serialize edge must reject tentative inactive execution and "
          "fall back to the complete response loop");

    const auto audited = run_response_activity_case(true, false, true);
    const auto residual = audited.total_response_residuals();
    check(audited.response_activity_candidates == 0 &&
              residual.stage_uops ==
                  audited.total_core().retired_uops &&
              residual.stage_conserved(),
          "timing-ledger mode must bypass the host fast path and account "
          "for every response-inactive committed UOP");
}

fastsim::SimulationStats run_corrected_arrival_case(
    bool cross_core_lines, std::uint32_t reweave_passes,
    bool causal_timing = false,
    std::uint32_t causal_max_closure_events = 4096,
    std::uint32_t sequencer_capacity = 1,
    bool corrected_suffix_carry = false,
    bool response_retime = false,
    bool same_line_order_audit = true,
    bool cpi_attribution = false) {
    fastsim::SimulatorConfig config;
    config.cores = 2;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.interval_full_order_audit = false;
    config.interval_reweave_passes = reweave_passes;
    config.interval_causal_timing = causal_timing;
    config.interval_response_retime = response_retime;
    config.interval_causal_passes = 8;
    config.interval_causal_max_closure_events =
        causal_max_closure_events;
    config.interval_corrected_suffix_carry =
        corrected_suffix_carry;
    config.interval_same_line_order_audit =
        same_line_order_audit;
    config.cpi_attribution = cpi_attribution;
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = cpi_attribution;
    config.ruby_sequencer_max_outstanding = sequencer_capacity;
    config.chunk_instructions = 128;
    config.interval_target_uops = 64;
    config.interval_max_cycles = corrected_suffix_carry ? 64 : 4096;
    config.lookahead_chunks = 2;
    config.iq_entries = 4;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    for (std::uint32_t core = 0; core < config.cores; ++core) {
        std::vector<fastsim::TraceRecord> records;
        records.reserve(128);
        for (std::uint64_t index = 0; index < 128; ++index) {
            fastsim::TraceRecord load;
            load.pc = 0x4000 + index * 4;
            load.op_class = 1;
            const auto private_offset =
                cross_core_lines ? 0 : core * 0x100000;
            load.address = 0x10000 + private_offset +
                           (index % 8) * 64;
            load.size = 8;
            load.flags = fastsim::kRetires | fastsim::kLoad |
                         fastsim::kPhysicalAddress;
            records.push_back(load);
        }
        traces.push_back(std::make_unique<VectorTraceSource>(
            std::move(records)));
    }
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_corrected_epoch_suffix_transaction() {
    const auto stats = run_corrected_arrival_case(
        true, 1, false, 4096, 1, true);
    const auto total = stats.total_core();
    check(stats.corrected_suffix_candidate_epochs > 0 &&
              stats.corrected_suffix_stable_epochs > 0 &&
              stats.corrected_suffix_passes > 0 &&
              stats.corrected_suffix_deferred_uops > 0 &&
              stats.corrected_suffix_deferred_memory_events > 0,
          "response-corrected requests beyond the horizon must defer their "
          "per-core suffix from one epoch-entry transaction");
    check(stats.corrected_suffix_conservative_epochs > 0,
          "hit/merge suffixes without a persistent fill witness must retain "
          "their canonical epoch instead of carrying uncertified services");
    check(total.retired_uops == 256 &&
              total.memory_accesses == 256 &&
              total.l1d.accesses == total.memory_accesses &&
              stats.batch_memory_events == total.memory_accesses,
          "suffix rollback must conserve UOP and cache/PMU events");

    const auto audited = run_corrected_arrival_case(
        true, 1, false, 4096, 1, true, false, true, true);
    const auto epoch = audited.total_committed_epoch_audit();
    check(epoch.accepted_uops == audited.total_core().retired_uops &&
              epoch.memory_events == audited.batch_memory_events &&
              epoch.memory_events_conserved(),
          "suffix rollback must commit attribution populations only after "
          "selecting its final prefix");

}

void test_corrected_arrival_no_conflict_fast_path() {
    const auto canonical = run_corrected_arrival_case(false, 1);
    const auto enabled = run_corrected_arrival_case(false, 2);
    const auto canonical_total = canonical.total_core();
    const auto enabled_total = enabled.total_core();
    check(enabled.corrected_arrival_candidate_epochs == 0 &&
              enabled.corrected_arrival_replay_epochs == 0 &&
              enabled.corrected_arrival_replayed_events == 0,
          "disjoint per-core lines must bypass corrected-arrival replay");
    check(enabled_total.cycles == canonical_total.cycles &&
              enabled_total.l1d.misses == canonical_total.l1d.misses &&
              enabled_total.l2.misses == canonical_total.l2.misses &&
              enabled.llc.misses == canonical.llc.misses &&
              enabled.total_o3().iq_full_events ==
                  canonical.total_o3().iq_full_events &&
              enabled.total_sequencer().stall_cycles ==
                  canonical.total_sequencer().stall_cycles,
          "no-conflict corrected-arrival gate must be bit-identical");
}

void test_corrected_arrival_same_line_transaction() {
    const auto canonical = run_corrected_arrival_case(true, 1);
    const auto two_pass = run_corrected_arrival_case(true, 2);
    const auto two_pass_total = two_pass.total_core();
    check(two_pass.corrected_arrival_stable_epochs == 0 &&
              two_pass.corrected_arrival_fallback_epochs > 0 &&
              two_pass.timing_certificate_failures > 0 &&
              two_pass_total.cycles == canonical.total_core().cycles &&
              two_pass.llc.misses == canonical.llc.misses &&
              two_pass.cha.at(0).llc_merged_misses == canonical.cha.at(0).llc_merged_misses &&
              two_pass_total.l1d.accesses ==
                  two_pass_total.memory_accesses,
          "same-order requests whose arrivals still change must roll back "
          "to the canonical cycles and cache outcomes");

    const auto stats = run_corrected_arrival_case(true, 8);
    const auto total = stats.total_core();
    check(stats.corrected_arrival_candidate_epochs > 0 &&
              stats.corrected_arrival_conflict_components > 0 &&
              stats.corrected_arrival_component_events > 0,
          "cross-core same-line accesses must form path-risk components");
    check(stats.corrected_arrival_replay_epochs > 0 &&
              stats.corrected_arrival_replayed_events > 0 &&
              stats.corrected_arrival_stable_epochs +
                  stats.corrected_arrival_fallback_epochs ==
                  stats.corrected_arrival_candidate_epochs,
          "every bounded functional candidate must certify or roll back");
    check(total.retired_uops == 256 &&
              total.memory_accesses == 256 &&
              total.l1d.accesses == total.memory_accesses &&
              stats.batch_memory_events == total.memory_accesses,
          "transaction retries must not duplicate functional/cache counters");
}

void test_same_line_order_audit_equivalence() {
    const auto audited = run_corrected_arrival_case(
        true, 1, false, 4096, 1, false, false, true);
    const auto production = run_corrected_arrival_case(
        true, 1, false, 4096, 1, false, false, false);
    const auto audited_total = audited.total_core();
    const auto production_total = production.total_core();
    const auto audited_o3 = audited.total_o3();
    const auto production_o3 = production.total_o3();
    const auto audited_sequencer = audited.total_sequencer();
    const auto production_sequencer = production.total_sequencer();

    check(production.same_line_reordered_pairs == 0 &&
              audited.same_line_reordered_pairs >=
                  production.same_line_reordered_pairs,
          "production must skip same-line order diagnostics");
    check(production_total.cycles == audited_total.cycles &&
              production_total.retired_uops ==
                  audited_total.retired_uops &&
              production_total.memory_accesses ==
                  audited_total.memory_accesses &&
              production_total.l1d.accesses ==
                  audited_total.l1d.accesses &&
              production_total.l1d.misses ==
                  audited_total.l1d.misses &&
              production_total.l2.accesses ==
                  audited_total.l2.accesses &&
              production_total.l2.misses ==
                  audited_total.l2.misses &&
              production.llc.accesses == audited.llc.accesses &&
              production.llc.misses == audited.llc.misses &&
              production_o3.iq_full_events ==
                  audited_o3.iq_full_events &&
              production_o3.iq_stall_cycles ==
                  audited_o3.iq_stall_cycles &&
              production_o3.rob_full_events ==
                  audited_o3.rob_full_events &&
              production_o3.rob_stall_cycles ==
                  audited_o3.rob_stall_cycles &&
              production_sequencer.requests ==
                  audited_sequencer.requests &&
              production_sequencer.buffer_full_stalls ==
                  audited_sequencer.buffer_full_stalls &&
              production_sequencer.stall_cycles ==
                  audited_sequencer.stall_cycles &&
              production.interval_steps == audited.interval_steps &&
              production.batch_memory_events ==
                  audited.batch_memory_events,
          "disabling same-line diagnostics must preserve every target-state "
          "transition");
}

void test_causal_timing_sparse_closure() {
    const auto canonical = run_corrected_arrival_case(
        true, 1, false, 4096, 4);
    const auto repaired = run_corrected_arrival_case(
        true, 1, true, 4096, 4);
    const auto canonical_total = canonical.total_core();
    const auto repaired_total = repaired.total_core();
    const bool certified_closure =
        repaired.causal_timing_candidate_epochs > 0 &&
        repaired.causal_timing_stable_epochs > 0 &&
        repaired.causal_closure_components > 0 &&
        repaired.causal_closure_events > 0 &&
        repaired.causal_timing_replayed_events > 0;
    const bool transient_merge_noop =
        repaired.causal_timing_candidate_epochs == 0 &&
        repaired.causal_timing_noop_epochs > 0 &&
        repaired.causal_closure_components == 0 &&
        repaired.causal_closure_events == 0;
    check(certified_closure || transient_merge_noop,
          "corrected shared arrivals must either certify a sparse timing "
          "closure or prove that same-fill waiters need no reorder");
    check(repaired_total.retired_uops == canonical_total.retired_uops &&
              repaired_total.memory_accesses ==
                  canonical_total.memory_accesses &&
              repaired_total.l1d.accesses ==
                  canonical_total.l1d.accesses &&
              repaired_total.l1d.misses == canonical_total.l1d.misses &&
              repaired_total.l2.accesses == canonical_total.l2.accesses &&
              repaired_total.l2.misses == canonical_total.l2.misses &&
              repaired.llc.accesses == canonical.llc.accesses &&
              repaired.llc.misses == canonical.llc.misses,
          "timing-only causal repair must preserve canonical cache state "
          "and PMU counts");

    const auto budgeted = run_corrected_arrival_case(
        true, 1, true, 1, 4);
    check(budgeted.causal_timing_deferred_epochs > 0 ||
              budgeted.causal_timing_noop_epochs > 0,
          "an oversized path closure must defer, unless transient-fill "
          "coalescing proves that no closure remains");
}

void test_response_timing_retime_transaction() {
    const auto canonical = run_corrected_arrival_case(
        true, 1, false, 4096, 4);
    const auto retimed = run_corrected_arrival_case(
        true, 1, false, 4096, 4, false, true);
    const auto canonical_total = canonical.total_core();
    const auto retimed_total = retimed.total_core();
    check(retimed.response_retime_candidate_epochs > 0 &&
              retimed.response_retime_stable_epochs > 0 &&
              retimed.response_retime_replayed_events > 0,
          "response-delayed shared requests must complete a certified "
          "single retime pass");
    check(retimed_total.retired_uops == canonical_total.retired_uops &&
              retimed_total.memory_accesses ==
                  canonical_total.memory_accesses &&
              retimed_total.l1d.accesses ==
                  canonical_total.l1d.accesses &&
              retimed_total.l1d.misses == canonical_total.l1d.misses &&
              retimed_total.l2.accesses ==
                  canonical_total.l2.accesses &&
              retimed_total.l2.misses == canonical_total.l2.misses &&
              retimed.llc.accesses == canonical.llc.accesses &&
              retimed.llc.misses == canonical.llc.misses,
          "response retime must preserve canonical functional/cache PMU "
          "counts");
}

fastsim::SimulationStats run_time_epoch_scheduler_case() {
    fastsim::SimulatorConfig config;
    config.cores = 4;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.chunk_instructions = 32;
    config.interval_target_uops = 32;
    config.interval_max_cycles = 64;
    config.lookahead_chunks = 2;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 2;
    config.dram.channels = 2;
    config.dram.banks_per_channel = 2;
    config.validate();

    auto traces =
        fastsim::make_synthetic_traces(4, 4096, 35, 10, 2048, 29);
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_time_epoch_scheduler() {
    const auto stats = run_time_epoch_scheduler_case();
    const auto total = stats.total_core();
    check(total.retired_uops == 16384,
          "time epoch must consume every functional UOP");
    check(stats.interval_accepted_uops == total.retired_uops,
          "time-epoch accepted-UOP accounting must be exact");
    check(stats.batch_memory_events == total.memory_accesses,
          "time epoch must weave every memory event exactly once");
    check(stats.max_interval_accepted_uops > 32,
          "decode microbatch size must not be an epoch barrier");
    check(stats.epoch_lookahead_chunks > 4,
          "time epoch must pull more than one producer chunk per core");
    check(stats.epoch_advanced_cycles > 0,
          "time epoch must advance simulated global time");
}

fastsim::SimulatorConfig make_windowed_dvfs_config(
    std::uint32_t cores) {
    fastsim::SimulatorConfig config;
    config.cores = cores;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.interval_full_order_audit = false;
    config.interval_same_line_order_audit = false;
    config.chunk_instructions = 64;
    config.interval_target_uops = 64;
    config.interval_max_cycles = 32;
    config.lookahead_chunks = 2;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 2;
    config.validate();
    return config;
}

void test_windowed_dvfs_time_control() {
    auto config = make_windowed_dvfs_config(2);
    config.interval_full_order_audit = true;
    config.interval_same_line_order_audit = true;
    auto traces = fastsim::make_synthetic_traces(
        2, 100000, 0, 0, 1024, 91);
    fastsim::Simulator simulator(config, std::move(traces));
    simulator.set_core_frequencies(
        {4'500'000'000ull, 1'500'000'000ull});

    const auto first = simulator.advance(
        fastsim::SimulationWindow::simulated_time_ns(100));
    check(first.window_id == 1 && first.start_time_fs == 0 &&
              first.end_time_fs == 100'000'000ull &&
              !first.finished && first.cores.size() == 2,
          "a simulated-time window must pause at the exact requested target "
          "time");
    check(first.cores[0].cycles == 450 &&
              first.cores[1].cycles == 150 &&
              first.cores[0].retired_instructions >
                  first.cores[1].retired_instructions &&
              first.cores[0].cpi_available &&
              first.cores[1].cpi_available,
          "per-core frequency must scale local cycles, forward progress, and "
          "window CPI in one common target-time interval");

    simulator.set_core_frequencies(
        {1'500'000'000ull, 4'500'000'000ull});
    const auto second = simulator.advance(
        fastsim::SimulationWindow::simulated_time_ns(100));
    check(second.window_id == 2 &&
              second.start_time_fs == first.end_time_fs &&
              second.end_time_fs == 200'000'000ull &&
              second.cores[0].cycles == 150 &&
              second.cores[1].cycles == 450,
          "a paused DVFS update must change only the next window's clock "
          "slope and preserve a continuous simulated-time timeline");

    simulator.set_core_frequencies(
        {3'000'000'000ull, 3'000'000'000ull});
    const auto instruction = simulator.advance(
        fastsim::SimulationWindow::retired_instructions(1000));
    check(instruction.retired_instructions >= 1000 &&
              instruction.instruction_overshoot ==
                  instruction.retired_instructions - 1000,
          "an instruction window must stop at the first committed epoch and "
          "report its deterministic overshoot");
}

void test_windowed_pmu_conservation() {
    auto config = make_windowed_dvfs_config(1);
    auto traces = fastsim::make_synthetic_traces(
        1, 3000, 45, 5, 256, 117);
    fastsim::Simulator simulator(config, std::move(traces));

    std::uint64_t retired = 0;
    std::uint64_t memory_accesses = 0;
    std::uint64_t l1d_accesses = 0;
    std::uint64_t windows = 0;
    while (!simulator.finished()) {
        const auto result = simulator.advance(
            fastsim::SimulationWindow::retired_instructions(173));
        ++windows;
        retired += result.retired_instructions;
        memory_accesses += result.cores[0].memory_accesses;
        l1d_accesses += result.cores[0].l1d.accesses;
        check(result.cores[0].retired_instructions == 0 ||
                  result.cores[0].cpi_available,
              "every nonempty retirement window must publish CPI");
    }
    check(windows > 1 && retired == 3000 &&
              memory_accesses != 0 &&
              memory_accesses == l1d_accesses,
          "window retirement and private-cache PMU deltas must conserve the "
          "complete trace across pause/resume boundaries");
}

void test_windowed_time_tail_does_not_overrun() {
    auto config = make_windowed_dvfs_config(1);
    fastsim::TraceRecord load;
    load.pc = 0x1000;
    load.address = 0x8000;
    load.size = 8;
    load.flags = fastsim::kRetires | fastsim::kLoad |
        fastsim::kPhysicalAddress;
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{load}));
    fastsim::Simulator simulator(config, std::move(traces));

    constexpr std::uint64_t kWindowFs = 1'000'000;
    std::uint64_t previous_end = 0;
    std::uint64_t windows = 0;
    while (!simulator.finished() && windows < 1000) {
        const auto result = simulator.advance(
            fastsim::SimulationWindow::simulated_time_ns(1));
        ++windows;
        check(result.start_time_fs == previous_end &&
                  result.end_time_fs >= result.start_time_fs &&
                  result.end_time_fs - result.start_time_fs <= kWindowFs,
              "a time window must not overrun while draining retirement "
              "events after the input stream is exhausted");
        if (!result.finished) {
            check(result.end_time_fs - result.start_time_fs == kWindowFs,
                  "every nonterminal time window must stop at its exact "
                  "target boundary");
        }
        previous_end = result.end_time_fs;
    }
    check(simulator.finished() && windows > 1,
          "the exhausted input tail must eventually drain across bounded "
          "time windows");
}

fastsim::SimulationStats run_parallel_feedback_case(
    bool parallel, bool batch_timing_encode = false) {
    fastsim::SimulatorConfig config;
    config.cores = 4;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.interval_full_order_audit = false;
    config.interval_same_line_order_audit = false;
    config.interval_parallel_feedback = parallel;
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.response_batch_timing_encode = batch_timing_encode;
    config.response_rename_feedback = true;
    config.rename_int_free_entries = 16;
    config.ruby_sequencer_max_outstanding = 4;
    config.chunk_instructions = 64;
    config.interval_target_uops = 32;
    config.interval_max_cycles = 128;
    config.lookahead_chunks = 2;
    config.iq_entries = 8;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 2;
    config.dram.channels = 2;
    config.dram.banks_per_channel = 2;
    config.validate();

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    for (std::uint32_t core = 0; core < config.cores; ++core) {
        std::vector<fastsim::TraceRecord> records;
        records.reserve(8192);
        for (std::uint64_t index = 0; index < 8192; ++index) {
            fastsim::TraceRecord record;
            record.op_class = 1;
            record.n_dst = 1;
            record.set_register_class_metadata(
                {255, 255, 255, 255}, {1, 0, 0, 0});
            if (index % 2 == 0) {
                record.address =
                    0x1000000 + core * 0x100000 + index * 64;
                record.size = 8;
                record.flags = fastsim::kRetires | fastsim::kLoad |
                               fastsim::kPhysicalAddress;
            }
            records.push_back(record);
        }
        traces.push_back(std::make_unique<VectorTraceSource>(
            std::move(records)));
    }
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_parallel_feedback_equivalence() {
    const auto serial = run_parallel_feedback_case(false);
    const auto parallel = run_parallel_feedback_case(true);
    const auto batch_encoded = run_parallel_feedback_case(true, true);
    const auto serial_total = serial.total_core();
    const auto parallel_total = parallel.total_core();
    const auto serial_o3 = serial.total_o3();
    const auto parallel_o3 = parallel.total_o3();
    const auto serial_rename = serial.total_response_rename();
    const auto parallel_rename = parallel.total_response_rename();
    const auto serial_seq = serial.total_sequencer();
    const auto parallel_seq = parallel.total_sequencer();

    check(serial.timing_feedback_calls > 0 &&
              serial.timing_feedback_parallel_calls == 0 &&
              parallel.timing_feedback_calls ==
                  serial.timing_feedback_calls &&
              parallel.timing_feedback_parallel_calls > 0 &&
              parallel.timing_feedback_parallel_calls <=
                  parallel.timing_feedback_calls &&
              parallel.timing_feedback_core_tasks ==
                  serial.timing_feedback_core_tasks,
          "hybrid parallel feedback must execute the same per-core task set");
    check(parallel_total.retired_uops == serial_total.retired_uops &&
              parallel_total.cycles == serial_total.cycles &&
              parallel_total.memory_penalty_cycles ==
                  serial_total.memory_penalty_cycles &&
              parallel_total.l1d.accesses == serial_total.l1d.accesses &&
              parallel_total.l1d.misses == serial_total.l1d.misses &&
              parallel_total.l2.accesses == serial_total.l2.accesses &&
              parallel_total.l2.misses == serial_total.l2.misses &&
              parallel.llc.accesses == serial.llc.accesses &&
              parallel.llc.misses == serial.llc.misses &&
              parallel_o3.iq_full_events == serial_o3.iq_full_events &&
              parallel_o3.iq_stall_cycles == serial_o3.iq_stall_cycles &&
              parallel_rename.allocated == serial_rename.allocated &&
              parallel_rename.released == serial_rename.released &&
              parallel_rename.live == serial_rename.live &&
              parallel_rename.free_list_stall_uops ==
                  serial_rename.free_list_stall_uops &&
              parallel_rename.free_list_stall_cycles ==
                  serial_rename.free_list_stall_cycles &&
              parallel_rename.conserved() &&
              parallel_seq.requests == serial_seq.requests &&
              parallel_seq.buffer_full_stalls ==
                  serial_seq.buffer_full_stalls &&
              parallel_seq.stall_cycles == serial_seq.stall_cycles &&
              parallel.interval_steps == serial.interval_steps &&
              parallel.batch_memory_events == serial.batch_memory_events &&
              parallel.epoch_corrected_horizon_violations ==
                  serial.epoch_corrected_horizon_violations,
          "parallel per-core feedback must be target-state bit-equivalent");
    check(parallel.cores.size() == serial.cores.size(),
          "parallel feedback must preserve the core count");
    for (std::size_t core = 0; core < serial.cores.size(); ++core) {
        check(parallel.cores[core].cycles == serial.cores[core].cycles &&
                  parallel.o3[core].iq_full_events ==
                      serial.o3[core].iq_full_events &&
                  parallel.response_rename[core].allocated ==
                      serial.response_rename[core].allocated &&
                  parallel.response_rename[core].released ==
                      serial.response_rename[core].released &&
                  parallel.response_rename[core].live ==
                      serial.response_rename[core].live &&
                  parallel.sequencer[core].stall_cycles ==
                      serial.sequencer[core].stall_cycles,
              "parallel feedback must preserve every core's timing state");
    }
    check(batch_encoded.total_core().cycles == parallel_total.cycles &&
              batch_encoded.total_core().retired_uops ==
                  parallel_total.retired_uops &&
              batch_encoded.total_core().memory_accesses ==
                  parallel_total.memory_accesses &&
              batch_encoded.total_o3().iq_full_events ==
                  parallel_o3.iq_full_events &&
              batch_encoded.total_o3().rob_full_events ==
                  parallel_o3.rob_full_events &&
              batch_encoded.total_sequencer().requests ==
                  parallel_seq.requests &&
              batch_encoded.total_sequencer().stall_cycles ==
                  parallel_seq.stall_cycles &&
              batch_encoded.interval_steps == parallel.interval_steps &&
              batch_encoded.batch_memory_events ==
                  parallel.batch_memory_events &&
              batch_encoded.sparse_scoreboard_materialized_uops ==
                  parallel.sparse_scoreboard_materialized_uops &&
              batch_encoded.sparse_scoreboard_rob_crossings ==
                  parallel.sparse_scoreboard_rob_crossings,
          "batched producer timing encoding must match per-field checks");
}

fastsim::SimulationStats run_topology_frfcfs_case(
    std::uint32_t cores, bool enable_frfcfs,
    std::uint32_t domain_workers = 0,
    bool parallel_feedback = true) {
    fastsim::SimulatorConfig config;
    config.cores = cores;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.interval_parallel_feedback = parallel_feedback;
    config.domain_workers = domain_workers;
    config.response_queue_feedback = true;
    config.chunk_instructions = 128;
    config.interval_target_uops = 64;
    config.interval_max_cycles = 256;
    config.lookahead_chunks = 2;
    config.iq_entries = 16;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 8;
    config.dram.channels = 8;
    config.dram.banks_per_channel = 16;
    config.dram.ranks_per_channel = 2;
    config.dram.read_buffer_size = 64;
    config.dram.frfcfs_selection_window = 8;
    config.dram.frfcfs_topology_scaled_window = true;
    config.dram.scheduler = enable_frfcfs ? "frfcfs" : "fcfs";
    config.validate();

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.reserve(cores);
    for (std::uint32_t core = 0; core < cores; ++core) {
        std::vector<fastsim::TraceRecord> records;
        records.reserve(2048);
        for (std::uint64_t index = 0; index < 2048; ++index) {
            fastsim::TraceRecord load;
            load.pc = 0x4000 + index * 4;
            load.address = 0x100000 +
                static_cast<std::uint64_t>(core) * 0x1000000 +
                index * 64;
            load.size = 8;
            load.flags = fastsim::kRetires | fastsim::kLoad |
                         fastsim::kPhysicalAddress;
            records.push_back(load);
        }
        traces.push_back(std::make_unique<VectorTraceSource>(
            std::move(records)));
    }
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_topology_scaled_frfcfs_sparse_repair() {
    const auto c8 = run_topology_frfcfs_case(8, true);
    check(c8.dram_frfcfs_effective_selection_window == 1 &&
              c8.dram_frfcfs_candidate_epochs == 0 &&
              c8.dram_frfcfs_bypass_epochs > 0 &&
              c8.dram_frfcfs_bypass_requests > 0,
          "one topology candidate must take the certified FCFS bypass");

    const auto canonical = run_topology_frfcfs_case(16, false);
    const auto repaired = run_topology_frfcfs_case(16, true);
    check(repaired.dram_frfcfs_effective_selection_window == 3,
          "core-rank lanes must select a three-request frontier");
    check(repaired.dram_frfcfs_candidate_epochs > 0 &&
              repaired.dram_frfcfs_requests > 0,
          "topology-scaled FR-FCFS must discover repair candidates");
    check(repaired.dram_frfcfs_stable_epochs > 0,
          "FR-FCFS candidates must reach a certified fixed point");
    check(repaired.dram_frfcfs_stable_epochs +
                  repaired.dram_frfcfs_fallback_epochs ==
              repaired.dram_frfcfs_candidate_epochs,
          "every FR-FCFS candidate must certify or explicitly fall back");
    check(repaired.dram_frfcfs_max_pending <= 3,
          "FR-FCFS pending frontier must respect the topology bound");
    check(repaired.dram_frfcfs_row_hits +
                  repaired.dram_frfcfs_row_misses <=
              repaired.dram_frfcfs_requests &&
              repaired.dram_frfcfs_reordered_requests <=
                  repaired.dram_frfcfs_requests,
          "FR-FCFS PMU accounting must be request-conservative");

    const auto canonical_total = canonical.total_core();
    const auto repaired_total = repaired.total_core();
    check(repaired_total.retired_uops == canonical_total.retired_uops &&
              repaired_total.memory_accesses ==
                  canonical_total.memory_accesses &&
              repaired_total.l1d.accesses ==
                  canonical_total.l1d.accesses &&
              repaired_total.l1d.misses == canonical_total.l1d.misses &&
              repaired_total.l2.accesses ==
                  canonical_total.l2.accesses &&
              repaired_total.l2.misses == canonical_total.l2.misses &&
              repaired.llc.accesses == canonical.llc.accesses &&
              repaired.llc.misses == canonical.llc.misses,
          "timing-only DRAM repair must preserve canonical cache paths and "
          "functional PMU counts");
}

fastsim::SimulationStats run_frfcfs_atomic_fallback_case(
    bool enable_frfcfs) {
    fastsim::SimulatorConfig config;
    config.cores = 2;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.interval_reweave_passes = 1;
    config.interval_private_preview = false;
    config.interval_parallel_feedback = false;
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.chunk_instructions = 64;
    config.interval_target_uops = 64;
    config.interval_max_cycles = 256;
    config.lookahead_chunks = 2;
    config.iq_entries = 16;
    config.rob_entries = 32;
    config.lq_entries = 8;
    config.sq_entries = 8;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 8ull << 10;
    config.llc.size_bytes = 16ull << 10;
    config.cha_count = 2;
    config.dram.channels = 2;
    config.dram.banks_per_channel = 2;
    config.dram.frfcfs_selection_window = 8;
    config.dram.frfcfs_topology_scaled_window = false;
    config.dram.scheduler = enable_frfcfs ? "frfcfs" : "fcfs";
    config.validate();

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    for (std::uint32_t core = 0; core < config.cores; ++core) {
        std::vector<fastsim::TraceRecord> records;
        records.reserve(256);
        for (std::uint64_t index = 0; index < 256; ++index) {
            fastsim::TraceRecord atomic;
            atomic.pc = 0x8000 + index * 4;
            atomic.address = 0x1000000 +
                static_cast<std::uint64_t>(core) * 0x1000000 +
                index * 64;
            atomic.size = 8;
            atomic.flags = fastsim::kRetires | fastsim::kAtomic |
                           fastsim::kPhysicalAddress;
            records.push_back(atomic);
        }
        traces.push_back(std::make_unique<VectorTraceSource>(
            std::move(records)));
    }
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_frfcfs_nonreplayable_fail_fast() {
    const auto canonical = run_frfcfs_atomic_fallback_case(false);
    const auto repaired = run_frfcfs_atomic_fallback_case(true);
    const auto canonical_total = canonical.total_core();
    const auto repaired_total = repaired.total_core();
    check(repaired.dram_frfcfs_candidate_epochs > 0 &&
              repaired.dram_frfcfs_fallback_epochs ==
                  repaired.dram_frfcfs_candidate_epochs &&
              repaired.dram_frfcfs_stable_epochs == 0 &&
              repaired.dram_frfcfs_requests > 0,
          "atomic DRAM epochs must take the explicit non-replayable "
          "FR-FCFS fallback");
    check(repaired_total.retired_uops ==
                  canonical_total.retired_uops &&
              repaired_total.memory_accesses ==
                  canonical_total.memory_accesses &&
              repaired_total.l1d.accesses ==
                  canonical_total.l1d.accesses &&
              repaired_total.l2.accesses ==
                  canonical_total.l2.accesses &&
              repaired.llc.accesses == canonical.llc.accesses &&
              repaired.llc.misses == canonical.llc.misses,
          "early non-replayable detection must preserve canonical "
          "functional and PMU state");
}

void test_zero_progress_epoch_collapse() {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.interval_private_preview = false;
    config.interval_parallel_feedback = false;
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.chunk_instructions = 32;
    config.interval_target_uops = 32;
    config.interval_max_cycles = 1;
    config.lookahead_chunks = 2;
    config.rob_entries = 16;
    config.iq_entries = 8;
    config.lq_entries = 4;
    config.sq_entries = 4;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 8ull << 10;
    config.llc.size_bytes = 16ull << 10;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    std::vector<fastsim::TraceRecord> records(32);
    records.front().address = 0x400000;
    records.front().size = 8;
    records.front().flags = fastsim::kRetires | fastsim::kLoad |
                            fastsim::kPhysicalAddress;
    for (auto& record : records) record.flags |= fastsim::kRetires;
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    const auto stats = simulator.run();
    check(stats.interval_zero_progress_steps > 0 &&
              stats.timing_feedback_calls == stats.interval_steps &&
              stats.interval_accepted_uops == 32 &&
              stats.total_core().retired_uops == 32,
          "collapsed empty epochs must restore the legacy step/feedback "
          "counts and conserve every accepted UOP");
}

void test_frfcfs_channel_parallel_equivalence() {
    // Disable the other domain phases so worker count changes only the host
    // execution of independent DRAM channels.
    const auto serial = run_topology_frfcfs_case(
        16, true, 1, false);
    const auto parallel = run_topology_frfcfs_case(
        16, true, 8, false);
    const auto serial_total = serial.total_core();
    const auto parallel_total = parallel.total_core();

    check(parallel.domain_worker_threads == 8 &&
              parallel.dram_frfcfs_candidate_epochs > 0,
          "FR-FCFS channel test must exercise the domain worker pool");
    check(parallel_total.retired_uops == serial_total.retired_uops &&
              parallel_total.cycles == serial_total.cycles &&
              parallel_total.memory_penalty_cycles ==
                  serial_total.memory_penalty_cycles &&
              parallel.llc.accesses == serial.llc.accesses &&
              parallel.llc.misses == serial.llc.misses &&
              parallel.interval_steps == serial.interval_steps &&
              parallel.dram_frfcfs_passes ==
                  serial.dram_frfcfs_passes &&
              parallel.dram_frfcfs_stable_epochs ==
                  serial.dram_frfcfs_stable_epochs &&
              parallel.dram_frfcfs_reordered_requests ==
                  serial.dram_frfcfs_reordered_requests &&
              parallel.dram_frfcfs_row_hits ==
                  serial.dram_frfcfs_row_hits &&
              parallel.dram_frfcfs_row_misses ==
                  serial.dram_frfcfs_row_misses &&
              parallel.dram_frfcfs_max_admitted_pending ==
                  serial.dram_frfcfs_max_admitted_pending &&
              parallel.dram_frfcfs_page_policy_scanned_requests ==
                  serial.dram_frfcfs_page_policy_scanned_requests &&
              parallel.dram_frfcfs_outside_window_row_hits ==
                  serial.dram_frfcfs_outside_window_row_hits &&
              parallel.dram_frfcfs_outside_window_bank_conflicts ==
                  serial.dram_frfcfs_outside_window_bank_conflicts &&
              parallel.dram_frfcfs_row_cap_precharges ==
                  serial.dram_frfcfs_row_cap_precharges &&
              parallel.dram_frfcfs_adaptive_precharges ==
                  serial.dram_frfcfs_adaptive_precharges,
          "parallel DRAM channels must preserve target timing and PMU "
          "state");
    check(parallel.cores.size() == serial.cores.size() &&
              parallel.cha.size() == serial.cha.size(),
          "parallel DRAM channels must preserve topology dimensions");
    for (std::size_t core = 0; core < serial.cores.size(); ++core) {
        check(parallel.cores[core].cycles == serial.cores[core].cycles &&
                  parallel.cores[core].memory_penalty_cycles ==
                      serial.cores[core].memory_penalty_cycles,
              "parallel DRAM channels must preserve every core timeline");
    }
    for (std::size_t cha = 0; cha < serial.cha.size(); ++cha) {
        check(parallel.cha[cha].requests == serial.cha[cha].requests &&
                  parallel.cha[cha].queue_cycles ==
                      serial.cha[cha].queue_cycles,
              "parallel DRAM channels must preserve every CHA timeline");
    }
}

void test_time_epoch_inflight_memory() {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.chunk_instructions = 1;
    config.interval_target_uops = 1;
    config.interval_max_cycles = 8;
    config.lookahead_chunks = 2;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 2;
    config.dram.channels = 2;
    config.dram.banks_per_channel = 2;
    config.validate();

    fastsim::TraceRecord older_long_latency;
    older_long_latency.op_class = 11;
    fastsim::TraceRecord younger_load;
    younger_load.pc = 4;
    younger_load.address = 0x8000;
    younger_load.size = 8;
    younger_load.flags = fastsim::kRetires | fastsim::kLoad |
                         fastsim::kPhysicalAddress;

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{
            older_long_latency, younger_load}));
    fastsim::Simulator simulator(config, std::move(traces));
    const auto stats = simulator.run();
    check(stats.total_core().retired_uops == 2,
          "time epoch must retain both ROB-ordered UOPs");
    check(stats.batch_memory_events == 1,
          "in-flight memory event must be woven exactly once");
    check(stats.epoch_inflight_memory_uops == 1,
          "early-issued, late-retiring load must cross the epoch as in-flight");
}

fastsim::SimulationStats run_private_preview_case(
    bool preview, bool response_feedback = false) {
    fastsim::SimulatorConfig config;
    config.cores = 2;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.interval_private_preview = preview;
    config.domain_min_events = 1;
    config.interval_reweave_passes = 1;
    config.chunk_instructions = 64;
    config.interval_target_uops = 32;
    config.interval_max_cycles = 128;
    config.lookahead_chunks = 2;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 2;
    config.dram.channels = 2;
    config.dram.banks_per_channel = 2;
    if (response_feedback) {
        config.response_queue_feedback = true;
        config.ruby_sequencer_max_outstanding = 4;
        config.iq_entries = 8;
    }
    config.validate();

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    for (std::uint32_t core = 0; core < config.cores; ++core) {
        fastsim::SyntheticTraceConfig trace;
        trace.core_id = core;
        trace.instructions = 4096;
        trace.memory_percent = 60;
        trace.write_percent = 0;
        trace.shared_percent = 0;
        trace.working_set_lines = 16;
        trace.seed = 41;
        traces.push_back(
            std::make_unique<fastsim::SyntheticTraceSource>(trace));
    }
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_private_preview_equivalence() {
    const auto canonical = run_private_preview_case(false);
    const auto preview = run_private_preview_case(true);
    const auto canonical_total = canonical.total_core();
    const auto preview_total = preview.total_core();
    check(preview.private_preview_events == preview.batch_memory_events &&
              preview.private_preview_epochs > 0 &&
              preview.state_certificate_failures == 0,
          "read-only epochs must use certified private preview");
    check(preview.materialized_escape_events <
              preview.batch_memory_events,
          "private preview must filter warm private hits");
    check(preview_total.cycles == canonical_total.cycles &&
              preview_total.l1d.misses == canonical_total.l1d.misses &&
              preview_total.l2.misses == canonical_total.l2.misses &&
              preview.llc.misses == canonical.llc.misses,
          "certified private preview must match canonical timing and PMU");
}

void test_private_preview_response_feedback_equivalence() {
    const auto canonical = run_private_preview_case(false, true);
    const auto preview = run_private_preview_case(true, true);
    const auto canonical_total = canonical.total_core();
    const auto preview_total = preview.total_core();
    const auto canonical_o3 = canonical.total_o3();
    const auto preview_o3 = preview.total_o3();
    const auto canonical_sequencer = canonical.total_sequencer();
    const auto preview_sequencer = preview.total_sequencer();
    check(preview.private_preview_epochs > 0 &&
              preview.state_certificate_failures == 0,
          "Sequencer/response feedback must permit certified preview");
    check(preview_total.cycles == canonical_total.cycles &&
              preview_total.l1d.misses == canonical_total.l1d.misses &&
              preview_total.l2.misses == canonical_total.l2.misses &&
              preview.llc.misses == canonical.llc.misses &&
              preview_o3.iq_full_events == canonical_o3.iq_full_events &&
              preview_o3.iq_stall_cycles == canonical_o3.iq_stall_cycles &&
              preview_sequencer.requests == canonical_sequencer.requests &&
              preview_sequencer.buffer_full_stalls ==
                  canonical_sequencer.buffer_full_stalls &&
              preview_sequencer.stall_cycles ==
                  canonical_sequencer.stall_cycles,
          "certified preview must preserve response-IQ and Sequencer timing");
}

void test_private_preview_sparse_set_repair() {
    fastsim::SimulatorConfig config;
    config.cores = 2;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.domain_min_events = 1;
    config.chunk_instructions = 4;
    config.interval_target_uops = 4;
    config.interval_max_cycles = 64;
    config.lookahead_chunks = 2;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 2;
    config.dram.channels = 2;
    config.dram.banks_per_channel = 2;
    config.validate();

    fastsim::TraceRecord store;
    store.address = 0;
    store.size = 8;
    store.flags = fastsim::kRetires | fastsim::kStore |
                  fastsim::kPhysicalAddress;
    fastsim::TraceRecord read = store;
    read.flags = fastsim::kRetires | fastsim::kLoad |
                 fastsim::kPhysicalAddress;
    fastsim::TraceRecord same_set = read;
    same_set.address = 32 * 64;
    fastsim::TraceRecord different_set = read;
    different_set.address = 64;
    const auto run = [&](bool preview) {
        auto run_config = config;
        run_config.interval_private_preview = preview;
        std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
        traces.push_back(std::make_unique<VectorTraceSource>(
            std::vector<fastsim::TraceRecord>{store}));
        traces.push_back(std::make_unique<VectorTraceSource>(
            std::vector<fastsim::TraceRecord>{
                read, same_set, different_set}));
        fastsim::Simulator simulator(run_config, std::move(traces));
        return simulator.run();
    };
    const auto canonical = run(false);
    const auto sparse = run(true);
    const auto canonical_total = canonical.total_core();
    const auto sparse_total = sparse.total_core();
    check(sparse.state_certificate_failures > 0 &&
              sparse.private_preview_partial_epochs > 0 &&
              sparse.private_preview_events == 2 &&
              sparse.private_preview_unsafe_events == 2 &&
              sparse.canonical_fallback_epochs == 0,
          "one conflicting set must repair only its affected suffix");
    check(sparse_total.cycles == canonical_total.cycles &&
              sparse_total.l1d.misses == canonical_total.l1d.misses &&
              sparse_total.l2.misses == canonical_total.l2.misses &&
              sparse.llc.misses == canonical.llc.misses,
          "sparse private preview must match canonical timing and PMU");
}

void test_causal_frontier_skew() {
    fastsim::SimulatorConfig config;
    config.cores = 2;
    config.chunk_instructions = 1;
    config.lookahead_chunks = 2;
    config.l1d.size_bytes = 4ull << 10;
    config.l2.size_bytes = 16ull << 10;
    config.llc.size_bytes = 64ull << 10;
    config.cha_count = 2;
    config.dram.channels = 2;
    config.dram.banks_per_channel = 2;
    config.validate();

    fastsim::TraceRecord delayed_write =
        branch_record(0x1000, 0x2000);
    delayed_write.address = 0x8000;
    delayed_write.size = 8;
    delayed_write.flags =
        delayed_write.flags | fastsim::kStore |
        fastsim::kPhysicalAddress;

    fastsim::TraceRecord compute;
    compute.pc = 0x3000;
    fastsim::TraceRecord early_read;
    early_read.pc = 0x3004;
    early_read.address = 0x8000;
    early_read.size = 8;
    early_read.flags =
        fastsim::kRetires | fastsim::kLoad |
        fastsim::kPhysicalAddress;

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{delayed_write}));
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::vector<fastsim::TraceRecord>{compute, early_read}));
    fastsim::Simulator simulator(config, std::move(traces));
    const auto stats = simulator.run();

    std::uint64_t invalidations = 0;
    std::uint64_t remote_supplies = 0;
    for (const auto& cha : stats.cha) {
        invalidations += cha.invalidations;
        remote_supplies += cha.remote_supplies;
    }
    check(invalidations == 1,
          "earlier cross-chunk read must precede delayed write");
    check(remote_supplies == 0,
          "causal frontier must not process delayed writer first");
}

fastsim::SimulationStats run_modeled_instruction_hierarchy_case(
    bool lower_hierarchy) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.chunk_instructions = 8;
    config.interval_target_uops = 8;
    config.interval_max_cycles = 256;
    config.lookahead_chunks = 2;
    config.fetch_buffer_bytes = 64;
    config.fetch_supply_model = true;
    config.l1i_enabled = true;
    config.l1i.size_bytes = 64;
    config.l1i.associativity = 1;
    config.l1i_miss_penalty = 0;
    config.fetch_supply_physical_request_ledger = true;
    config.fetch_supply_lower_hierarchy = lower_hierarchy;
    config.instruction_address_mode = "modeled";
    config.instruction_physical_address_bits = 38;
    config.instruction_mapping_seed = 23;
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.l1d.size_bytes = 64;
    config.l1d.associativity = 1;
    config.l2.size_bytes = 64;
    config.l2.associativity = 1;
    config.llc.size_bytes = 4 * 64;
    config.llc.associativity = 1;
    config.cha_count = 1;
    config.dram.channels = 1;
    config.dram.banks_per_channel = 1;
    config.validate();

    std::vector<fastsim::TraceRecord> records;
    for (const auto pc : {0x1000ull, 0x1040ull, 0x1080ull, 0x1000ull}) {
        fastsim::TraceRecord record;
        record.pc = pc;
        record.flags = fastsim::kRetires;
        records.push_back(record);
    }
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_modeled_instruction_lower_hierarchy() {
    const auto ledger_only =
        run_modeled_instruction_hierarchy_case(false);
    const auto replayed =
        run_modeled_instruction_hierarchy_case(true);
    const auto ledger_core = ledger_only.total_core();
    const auto replayed_core = replayed.total_core();
    check(ledger_core.physical_instruction_fetch_requests == 4 &&
              ledger_core.modeled_instruction_fetch_requests == 4 &&
              ledger_core.instruction_page_map_lookups == 0 &&
              ledger_core.instruction_l2.accesses == 0 &&
              ledger_core.l2.accesses == 0 &&
              ledger_only.instruction_llc.accesses == 0 &&
              ledger_only.llc.accesses == 0,
          "modeled ledger must generate physical requests without requiring "
          "ifmap or mutating lower cache state");
    check(replayed_core.instruction_fetch_lower_hierarchy_requests == 4 &&
              replayed_core.l1d.accesses == 0 &&
              replayed_core.l2.accesses == 0 &&
              replayed_core.instruction_l2.accesses == 4 &&
              replayed_core.instruction_l2.misses == 4 &&
              replayed.llc.accesses == 0 &&
              replayed.instruction_llc.accesses == 4 &&
              replayed.instruction_llc.misses == 3 &&
              replayed.instruction_cha.size() == 1 &&
              replayed.instruction_cha[0].llc_hits +
                      replayed.instruction_cha[0].llc_merged_misses == 1 &&
              replayed.instruction_cha[0].llc_outcomes_conserved(),
          "committed L1I misses must bypass L1D, share L2/LLC, and conserve "
          "one LLC outcome per request (requests=" +
              std::to_string(
                  replayed_core
                      .instruction_fetch_lower_hierarchy_requests) +
              ", l1d=" + std::to_string(replayed_core.l1d.accesses) +
              ", i_l2=" +
              std::to_string(replayed_core.instruction_l2.accesses) +
              "/" +
              std::to_string(replayed_core.instruction_l2.misses) +
              ", i_llc=" +
              std::to_string(replayed.instruction_llc.accesses) +
              "/" + std::to_string(replayed.instruction_llc.hits) +
              "/" + std::to_string(replayed.instruction_llc.misses) +
              ")");
    check(replayed_core.cycles > ledger_core.cycles,
          "lower-hierarchy I-fetch responses must extend the committed "
          "frontend beyond the local L1I baseline");
}

void test_modeled_instruction_epoch_boundary() {
    auto config = fastsim::load_simulator_config(
        std::string(FASTSIM_PROJECT_ROOT) +
        "/configs/gem5-v28_1-time-epoch.cfg");
    config.cores = 1;
    config.fetch_supply_model = true;
    config.l1i_enabled = true;
    config.fetch_supply_physical_request_ledger = true;
    config.fetch_supply_lower_hierarchy = true;
    config.instruction_address_mode = "modeled";
    config.instruction_physical_address_bits = 38;
    config.instruction_mapping_seed = 1;
    config.validate();

    auto traces = fastsim::make_synthetic_traces(
        1, 2000, 30, 5, 1ull << 18, 1);
    fastsim::Simulator simulator(config, std::move(traces));
    const auto stats = simulator.run();
    const auto total = stats.total_core();
    check(stats.time_epoch_request_boundary_deferred_uops > 0 &&
              total.instruction_fetch_lower_hierarchy_requests > 0 &&
              total.l2.accesses == total.l1d.misses &&
              total.instruction_l2.accesses ==
                  total.instruction_fetch_lower_hierarchy_requests,
          "a cache request after its owner retire lower bound must defer the "
          "UOP to a later time epoch without dropping I-side L2 requests "
          "(deferred=" +
              std::to_string(
                  stats.time_epoch_request_boundary_deferred_uops) +
              ", lower=" +
              std::to_string(
                  total.instruction_fetch_lower_hierarchy_requests) +
              ", l2=" + std::to_string(total.l2.accesses) +
              "/" + std::to_string(total.l1d.misses) +
              ", i_l2=" +
              std::to_string(total.instruction_l2.accesses) + "/" +
              std::to_string(
                  total.instruction_fetch_lower_hierarchy_requests) +
              ")");
}

void test_projected_dram_capture() {
    const auto maintained = fastsim::load_simulator_config(
        std::string(FASTSIM_PROJECT_ROOT) + "/configs/gem5-fs-native-kernel.cfg");
    const auto rejected_profile = fastsim::load_simulator_config(
        std::string(FASTSIM_PROJECT_ROOT) + "/configs/gem5-exp-projected-dram-feedback.cfg");
    check(!maintained.projected_dram_feedback && !rejected_profile.projected_dram_feedback,
          "maintained and rejected projected profiles must keep feedback disabled");
    auto config = fastsim::load_simulator_config(
        std::string(FASTSIM_PROJECT_ROOT) + "/configs/gem5-v28_1-time-epoch.cfg");
    config.cores = 1;
    config.require_virtual_page_token = false;
    config.dtlb.enabled = false;
    config.l1d.size_bytes = config.l2.size_bytes = config.llc.size_bytes = 128;
    config.l1d.associativity = config.l2.associativity = config.llc.associativity = 1;
    const auto run = [&](const fastsim::SimulatorConfig& c) {
        std::vector<fastsim::TraceRecord> records;
        for (unsigned i = 0; i < 200; ++i) {
            fastsim::TraceRecord r;
            r.pc = 0x1000 + 4 * i;
            r.address = 0x10000 + 128 * i;
            r.size = 8;
            r.flags = fastsim::kRetires | fastsim::kStore | fastsim::kPhysicalAddress;
            records.push_back(r);
        }
        std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
        traces.push_back(std::make_unique<VectorTraceSource>(records));
        fastsim::Simulator simulator(c, std::move(traces));
        return simulator.run();
    };
    const auto baseline = run(config);
    config.projected_dram_audit_path = test_tmp_path("projected-dram.csv");
    config.validate();
    const auto captured = run(config);
    check(baseline.total_core().cycles == captured.total_core().cycles &&
              baseline.total_core().retired_instructions == captured.total_core().retired_instructions,
          "projected capture must not change target timing or instructions");
    std::ifstream input(config.projected_dram_audit_path);
    std::string row;
    std::getline(input, row);
    check(row == "phase,batch,kind,arrival,line,core,sequence,ordinal,command,response",
          "projected capture has versioned columns");
    std::uint64_t reads = 0, writes = 0;
    while (std::getline(input, row)) {
        reads += row.find(",R,") != std::string::npos;
        writes += row.find(",W,") != std::string::npos;
    }
    std::uint64_t expected_reads = 0, expected_writes = 0;
    for (const auto& cha : captured.cha) {
        expected_reads += cha.dram_reads;
        expected_writes += cha.dram_writes;
    }
    check(reads == expected_reads && writes == expected_writes && writes > 0,
          "projected capture includes real reads and dirty evictions exactly once");
    config.projected_dram_audit_path.clear();
    config.projected_dram_feedback = true;
    config.validate();
    const auto projected = run(config);
    std::uint64_t projected_reads = 0, projected_writes = 0;
    for (const auto& cha : projected.cha) {
        projected_reads += cha.dram_reads;
        projected_writes += cha.dram_writes;
    }
    check(projected.projected_dram_feedback_enabled &&
              projected.projected_dram_feedback_events == projected_reads &&
              projected.projected_dram_reads == projected_reads &&
              projected.projected_dram_writes == projected_writes &&
              projected.projected_dram_feedback_stores == projected_reads &&
              projected.projected_dram_writes_serviced +
                  projected.projected_dram_pending_final ==
                  projected.projected_dram_pending_initial + projected_writes &&
              projected.projected_dram_faster + projected.projected_dram_slower > 0 &&
              projected.total_core().cycles != baseline.total_core().cycles &&
              projected.total_core().retired_instructions ==
                  baseline.total_core().retired_instructions,
          "enabled projected responses must reach core timing with conserved RD/WB owners");
    check(projected.dram_frfcfs_candidate_epochs == 0 &&
              projected.timing_feedback_calls == projected.interval_steps,
          "projected feedback replaces FRFCFS without a second core feedback pass");
    const auto repeated = run(config);
    check(repeated.total_core().cycles == projected.total_core().cycles &&
              repeated.projected_dram_saved_cycles == projected.projected_dram_saved_cycles &&
              repeated.projected_dram_added_cycles == projected.projected_dram_added_cycles,
          "projected feedback is deterministic");
    config.projected_dram_audit_path = test_tmp_path("projected-feedback-capture.csv");
    const auto projected_captured = run(config);
    check(projected_captured.total_core().cycles == projected.total_core().cycles,
          "capture also preserves enabled projected timing");
    config.core_frequency_hz = config.reference_frequency_hz / 2;
    bool frequency_rejected = false;
    try { config.validate(); } catch (const std::invalid_argument&) { frequency_rejected = true; }
    check(frequency_rejected, "projected feedback rejects unsupported frequency conversion");
    config.core_frequency_hz = config.reference_frequency_hz;
    config.projected_dram_feedback = false;
    config.interval_corrected_suffix_carry = true;
    bool rejected = false;
    try { config.validate(); } catch (const std::invalid_argument&) { rejected = true; }
    check(rejected, "capture rejects unsupported timing replay combinations");
}

}  // namespace

void test_cache_probe_fill();
void test_cross_q_config();
void test_cross_q_runtime();
void test_shared_pending_fill();
void test_shared_mixed_service();
void test_pending_resources();
void test_line_generation_coordinator();
void test_load_component_preflight();
void test_causal_read();
void test_interval_dependencies();
void test_response_completion();
void test_mixed_dram();

int main(int argc, char** argv) {
    try {
        if (argc == 2 && std::string(argv[1]) == "--context-only") {
            test_context_trace_contract();
            test_context_execution_boundary();
            std::cout << "context execution tests passed\n";
            return 0;
        }
        test_context_trace_contract();
        test_context_execution_boundary();
        test_response_completion();
        test_mixed_dram();
        test_projected_dram_capture();
        test_interval_dependencies();
        test_causal_read();
        test_config();
        test_cache_transaction();
        test_private_dirty_victim_merge();
        test_predictor();
        test_interval_core_dependency_and_width();
        test_branch_shadow_rob();
        test_branch_population_audit();
        test_committed_pipeline_audit();
        test_static_memory_ordering();
        test_interval_dtlb();
        test_dtlb_hierarchy_walk();
        test_interval_syscall_serialization();
        test_syscall_cost_model();
        test_syscall_kernel_event_model();
        test_page_fault_kernel_event_model();
        test_periodic_irq_kernel_event_model();
        test_branch_speculative_history_checkpoint();
        test_branch_golden_direct_target();
        test_branch_golden_ras_learning();
        test_branch_golden_ras_static_fallthrough();
        test_branch_golden_ras_address_space_isolation();
        test_branch_golden_indirect_learning();
        test_trace_roundtrip();
        test_trace_page_table_path_roundtrip();
        test_trace_late_roi_page_state_roundtrip();
        test_privilege_trace_roundtrip();
        test_native_kernel_trace_replay();
        test_address_space_map_roundtrip();
        test_instruction_page_map_roundtrip();
        test_static_instruction_map_roundtrip();
        test_static_instruction_operand_map_roundtrip();
        test_static_instruction_ordering_map_roundtrip();
        test_syscall_trace_roundtrip();
        test_syscall_semantic_page_fault_selection();
        test_syscall_semantic_mapping_lifecycle();
        test_initial_pte_page_fault_selection();
        test_initial_pte_address_space_isolation();
        test_roi_entry_page_state_page_fault_selection();
        test_legacy_syscall_trace_upgrade();
        test_binary_bulk_read_boundary();
        test_legacy_v2_trace_read();
        test_legacy_v3_trace_read();
        test_binary_source_core_remap();
        test_binary_instruction_slice_manifest();
        test_binary_functional_warmup_manifest();
        test_two_phase_functional_warmup();
        test_undercommitted_static_thread_bindings();
        test_gem5_branch_contract();
        test_strict_physical_address_contract();
        test_strict_virtual_page_contract();
        test_dram_capacity_contract();
        test_dram_bank_group_column_spacing();
        test_dram_activation_spacing();
        test_dram_page_policy_full_queue_visibility();
        test_dram_causal_selection_matches_gem5_future_hit();
        test_dram_page_policy_row_cap_single_precharge();
        test_dram_separate_write_queue_read_priority();
        test_dram_parallel_channel_write_state_isolation();
        test_resident_chunk_ring();
        test_simulator();
        test_interval_weave_scheduler();
        test_ruby_sequencer_capacity();
        test_ruby_sequencer_line_coalescing();
        test_line_generation_admission_audit();
        test_pending_fill_generations();
        test_line_generation_same_line_reads_and_visibility();
        test_line_generation_exact_callback_boundary();
        test_line_generation_ordered_and_stale_expiry();
        test_line_generation_capacity_without_future_store_reservation();
        test_line_generation_copy_isolation();
        test_pending_fill_response_closure();
        test_pending_fill_bounded_resources();
        test_ruby_sequencer_load_admission();
        test_response_driven_iq_lifetime();
        test_response_monotone_iq_calendar();
        test_shared_transient_fill_merge();
        test_shared_cache_outcome_conservation();
        test_dependency_feedback_consumes_existing_slack();
        test_persistent_rob_lsq_tso_feedback();
        test_sparse_response_scoreboard_capacity();
        test_response_memory_descriptor_equivalence();
        test_sparse_cross_epoch_dependency();
        test_response_aware_rename_free_list();
        test_response_residual_ledger_conservation();
        test_rob_head_local_suffix_checkpoint();
        test_response_block_summary_equivalence();
        test_response_frontier_audit();
        test_response_frontier_sq_owner_audit();
        test_paired_response_frontier_settlement();
        test_response_causal_block_transfer();
        test_materialized_uop_fast_kernel();
        test_response_event_only_approximation();
        test_event_only_calibration_starts_at_measurement();
        test_event_only_teacher_partition();
        test_response_activity_certificate();
        test_corrected_arrival_no_conflict_fast_path();
        test_corrected_arrival_same_line_transaction();
        test_same_line_order_audit_equivalence();
        test_corrected_epoch_suffix_transaction();
        test_causal_timing_sparse_closure();
        test_response_timing_retime_transaction();
        test_time_epoch_scheduler();
        test_windowed_dvfs_time_control();
        test_windowed_pmu_conservation();
        test_windowed_time_tail_does_not_overrun();
        test_parallel_feedback_equivalence();
        test_topology_scaled_frfcfs_sparse_repair();
        test_frfcfs_nonreplayable_fail_fast();
        test_zero_progress_epoch_collapse();
        test_frfcfs_channel_parallel_equivalence();
        test_time_epoch_inflight_memory();
        test_private_preview_equivalence();
        test_private_preview_response_feedback_equivalence();
        test_private_preview_sparse_set_repair();
        test_causal_frontier_skew();
        test_modeled_instruction_lower_hierarchy();
        test_modeled_instruction_epoch_boundary();
        test_cache_probe_fill();
        test_cross_q_config();
        test_cross_q_runtime();
        test_shared_pending_fill();
        test_shared_mixed_service();
        test_pending_resources();
        test_line_generation_coordinator();
        test_load_component_preflight();
        std::cout << "all FastSim tests passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "test failure: " << error.what() << '\n';
        return 1;
    }
}
