#include <array>
#include <cstdio>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <unistd.h>

#include "fastsim/cache.hpp"
#include "fastsim/config.hpp"
#include "fastsim/interval_core.hpp"
#include "fastsim/predictor.hpp"
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
    const auto source = fastsim::KeyValueConfig::parse(
        "sim.cores = 8\n"
        "cache.l2.size = 2MiB\n"
        "uncore.coherence = false\n");
    check(source.get_u32("sim.cores", 0) == 8, "config integer");
    check(source.get_u64("cache.l2.size", 0) == (2ull << 20),
          "config size suffix");
    check(!source.get_bool("uncore.coherence", true), "config bool");

    const auto config_path = test_tmp_path(
        "fastsim_test_dram_topology.cfg");
    {
        std::ofstream output(config_path);
        output
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
            << "sim.interval_same_line_order_audit = false\n"
            << "core.minimum_load_latency = 4\n"
            << "uncore.directory_memory_latency = 18\n"
            << "syscall.service_latency = 9\n"
            << "syscall.restart_latency = 3\n";
    }
    const auto loaded = fastsim::load_simulator_config(config_path);
    check(loaded.dram.ranks_per_channel == 2 &&
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
              !loaded.interval_same_line_order_audit &&
              loaded.minimum_load_latency == 4 &&
              loaded.directory_memory_latency == 18 &&
              loaded.syscall_service_latency == 9 &&
              loaded.syscall_restart_latency == 3,
          "DRAM topology-scaled FR-FCFS config must round-trip");

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
}

void test_interval_core_dependency_and_width() {
    fastsim::SimulatorConfig config;
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
    check(after_switch.fetch_cycle >= before_switch.fetch_cycle + 2,
          "a gem5 fetch-buffer block switch must expose its refill bubble");

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
    shadow_config.validate();

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
    const auto reference_branch = reference.schedule(branch, true);
    const auto shadow_branch = shadow.schedule(branch, true);
    const auto reference_target = reference.schedule(target, false);
    const auto shadow_target = shadow.schedule(target, false);

    check(shadow_branch.branch_shadow_uops > 0 &&
              shadow_branch.branch_shadow_uops <=
                  shadow_config.rob_entries &&
              shadow_branch.branch_shadow_cycles ==
                  (shadow_branch.branch_shadow_uops +
                   shadow_config.commit_width - 1) /
                      shadow_config.commit_width,
          "branch shadow occupancy must be derived from the target pipeline "
          "and bounded by the ROB");
    check(shadow_target.rename_cycle > reference_target.rename_cycle &&
              shadow_branch.fetch_cycle == reference_branch.fetch_cycle &&
              shadow_branch.completion_cycle ==
                  reference_branch.completion_cycle,
          "anonymous wrong-path occupancy must delay only correct-path "
          "rename, not the resolving branch or functional execution");
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
    check(cold.dtlb_miss && !cold.dtlb_merged_miss &&
              cold.translation_delay_cycles >= 10,
          "cold virtual page must allocate a page walk");
    check(queued.dtlb_miss && !queued.dtlb_merged_miss &&
              queued.translation_delay_cycles >
                  cold.translation_delay_cycles,
          "gem5 x86 follower must queue without walk coalescing");
    check(warm.dtlb_hit && !warm.dtlb_miss,
          "completed page walk must fill the DTLB");

    auto coalescing_config = config;
    coalescing_config.dtlb.coalesce_misses = true;
    fastsim::IntervalCoreModel coalescing(coalescing_config);
    (void)coalescing.schedule(load, false);
    const auto merged = coalescing.schedule(load, false);
    check(merged.dtlb_miss && merged.dtlb_merged_miss,
          "optional coalescing mode must retain its explicit behavior");
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
    check(counters.ras_hits == 1, "RAS golden hit count");
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
            << "{\"macro_pc\":4096,\"vaddr\":16384,"
               "\"paddr\":8192,\"size\":8,"
               "\"is_load\":1,\"is_store\":0,\"is_atomic\":0,"
               "\"is_branch\":0,\"is_branch_cond\":0,"
               "\"is_branch_indirect\":0,\"is_call\":0,"
               "\"is_return\":0,\"branch_taken\":0,"
               "\"is_microop\":0,\"is_last_microop\":1,"
               "\"op_class\":56,\"n_src\":2,\"n_dst\":1,"
               "\"producer_dists\":[1,7,0,0],"
               "\"producer_classes\":[0,1,255,255],"
               "\"destination_class_counts\":[1,0,0,0]}\n";
    }
    fastsim::convert_gem5_jsonl_to_binary(json_path, binary_path, 0);
    fastsim::BinaryTraceSource input(binary_path);
    fastsim::TraceRecord record;
    check(input.next(record), "binary trace has record");
    check(record.pc == 4096 && record.address == 8192,
          "trace values survive conversion");
    check(record.is_memory() &&
              fastsim::has_flag(record.flags, fastsim::kPhysicalAddress),
          "trace flags survive conversion");
    check(fastsim::has_flag(record.flags,
                            fastsim::kVirtualPageToken) &&
              record.reserved != 0,
          "v5 trace keeps virtual-page identity beside physical address");
    check(record.op_class == 56 && record.n_src == 2 &&
              record.n_dst == 1 && record.producer_dists[0] == 1 &&
              record.producer_dists[1] == 7 &&
              record.producer_class(1) == 1 &&
              record.has_destination_class_counts() &&
              record.destination_class_count(0) == 1 &&
              record.virtual_page_token() != 0,
          "interval-core functional fields survive conversion");
    check(!input.next(record), "binary trace record count");
    std::remove(json_path.c_str());
    std::remove(binary_path.c_str());
}

void test_syscall_trace_roundtrip() {
    const auto json_path = test_tmp_path("fastsim_test_syscall.jsonl");
    const auto binary_path = test_tmp_path("fastsim_test_syscall.fst");
    {
        std::ofstream output(json_path);
        output << "{\"pc\":4096,\"is_syscall\":1,"
                  "\"op_class\":90,\"syscall_number\":202}\n";
    }
    fastsim::convert_gem5_jsonl_to_binary(json_path, binary_path, 0);
    fastsim::BinaryTraceSource input(binary_path);
    fastsim::TraceRecord record;
    check(input.next(record) && record.is_syscall() &&
              record.is_serializing() &&
              record.op_class == fastsim::kSyscallOpClass,
          "v5 trace must preserve an explicit functional syscall marker");
    check(record.syscall_number() == 202,
          "syscall number must survive JSONL to binary roundtrip");
    check(!has_flag(record.flags, fastsim::kPhysicalAddress),
          "syscall marker must not claim a physical memory address");
    check(!input.next(record), "syscall trace record count");
    std::remove(json_path.c_str());
    std::remove(binary_path.c_str());
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
    std::remove(manifest_path.c_str());
    std::remove(binary_path.c_str());
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
    config.domain_min_events = 1;
    config.l1d.size_bytes = 128;
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
        return record;
    };
    fastsim::TraceRecord compute;
    compute.pc = 0x3000;

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
              total.memory_accesses == 2 &&
              stats.interval_accepted_uops == 2 &&
              stats.batch_memory_events == 2,
          "measurement counters must exclude every warmup UOP and event");
    check(total.l1d.accesses == 2 && total.l1d.hits == 2 &&
              total.l1d.misses == 0,
          "measurement must retain private-cache state from warmup");
    check(stats.interval_private_memory_events +
              stats.interval_escape_memory_events ==
              stats.batch_memory_events,
          "measurement memory partition must remain conserved");
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

fastsim::SimulationStats run_response_iq_case(bool response_feedback) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.response_queue_feedback = response_feedback;
    config.chunk_instructions = 64;
    config.interval_target_uops = 64;
    config.interval_max_cycles = 4096;
    config.lookahead_chunks = 2;
    config.iq_entries = 4;
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
    check(merged.llc.accesses == 4 && merged.llc.misses == 4 &&
              merged.llc.hits == 0,
          "all concurrent demand accesses to a transient line must retain "
          "LLC-miss semantics");
    check(merged_cha.llc_unique_fills == 1 &&
              merged_cha.llc_merged_misses == 3 &&
              merged_cha.dram_reads == 1,
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
              delayed_fill.total_core().cycles >
                  merged.total_core().cycles,
          "Ruby fill-response latency must extend the transient/TBE "
          "lifetime without allocating another DRAM request");

    const auto independent = run_shared_transient_fill_case(false);
    fastsim::ChaCounters independent_cha;
    for (const auto& cha : independent.cha) independent_cha += cha;
    check(independent_cha.llc_unique_fills == 4 &&
              independent_cha.llc_merged_misses == 0 &&
              independent_cha.dram_reads == 4,
          "different lines must allocate independent LLC fills");
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
    bool memory_descriptor = false) {
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
    config.needs_tso = needs_tso;
    config.chunk_instructions = 64;
    config.interval_target_uops = 64;
    config.interval_max_cycles = 4096;
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
        records.push_back(memory);
    }
    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
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
    bool block_summary = false) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.cpi_attribution = true;
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.response_block_summary = block_summary;
    config.interval_rob_head_suffix_replay = rob_head_suffix_replay;
    config.chunk_instructions = 64;
    config.interval_target_uops = 8;
    config.interval_max_cycles = 8;
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

    std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
    traces.push_back(std::make_unique<VectorTraceSource>(
        std::move(records)));
    fastsim::Simulator simulator(config, std::move(traces));
    return simulator.run();
}

void test_response_residual_ledger_conservation() {
    const auto stats = run_response_residual_ledger_case();
    const auto residual = stats.total_response_residuals();
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

fastsim::SimulationStats run_response_activity_case(
    bool activity_certificate, bool serializing_uop) {
    fastsim::SimulatorConfig config;
    config.cores = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.response_activity_certificate = activity_certificate;
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
}

fastsim::SimulationStats run_corrected_arrival_case(
    bool cross_core_lines, std::uint32_t reweave_passes,
    bool causal_timing = false,
    std::uint32_t causal_max_closure_events = 4096,
    std::uint32_t sequencer_capacity = 1,
    bool corrected_suffix_carry = false,
    bool response_retime = false,
    bool same_line_order_audit = true) {
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
    config.response_queue_feedback = true;
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
    check(stats.corrected_suffix_conservative_epochs == 0,
          "the bounded suffix transaction must converge on the small "
          "deterministic case");
    check(total.retired_uops == 256 &&
              total.memory_accesses == 256 &&
              total.l1d.accesses == total.memory_accesses &&
              stats.batch_memory_events == total.memory_accesses,
          "suffix rollback must conserve UOP and cache/PMU events");
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
    const auto two_pass = run_corrected_arrival_case(true, 2);
    const auto two_pass_total = two_pass.total_core();
    check(two_pass.corrected_arrival_stable_epochs > 0 &&
              two_pass.corrected_arrival_fallback_epochs == 0 &&
              two_pass.timing_certificate_failures == 0 &&
              two_pass_total.l1d.accesses ==
                  two_pass_total.memory_accesses,
          "same-line transient merges must make the two-pass corrected "
          "schedule stable without duplicating cache events");

    const auto stats = run_corrected_arrival_case(true, 8);
    const auto total = stats.total_core();
    check(stats.corrected_arrival_candidate_epochs > 0 &&
              stats.corrected_arrival_conflict_components > 0 &&
              stats.corrected_arrival_component_events > 0,
          "cross-core same-line accesses must form path-risk components");
    check(stats.corrected_arrival_replay_epochs > 0 &&
              stats.corrected_arrival_replayed_events > 0 &&
              stats.corrected_arrival_stable_epochs > 0,
          "response feedback must trigger a stable corrected-arrival pass");
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

}  // namespace

int main() {
    try {
        test_config();
        test_cache_transaction();
        test_private_dirty_victim_merge();
        test_predictor();
        test_interval_core_dependency_and_width();
        test_branch_shadow_rob();
        test_committed_pipeline_audit();
        test_interval_dtlb();
        test_interval_syscall_serialization();
        test_syscall_cost_model();
        test_branch_golden_direct_target();
        test_branch_golden_ras_learning();
        test_branch_golden_indirect_learning();
        test_trace_roundtrip();
        test_syscall_trace_roundtrip();
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
        test_dram_page_policy_row_cap_single_precharge();
        test_dram_separate_write_queue_read_priority();
        test_simulator();
        test_interval_weave_scheduler();
        test_ruby_sequencer_capacity();
        test_response_driven_iq_lifetime();
        test_shared_transient_fill_merge();
        test_dependency_feedback_consumes_existing_slack();
        test_persistent_rob_lsq_tso_feedback();
        test_sparse_response_scoreboard_capacity();
        test_response_memory_descriptor_equivalence();
        test_sparse_cross_epoch_dependency();
        test_response_aware_rename_free_list();
        test_response_residual_ledger_conservation();
        test_rob_head_local_suffix_checkpoint();
        test_response_block_summary_equivalence();
        test_response_activity_certificate();
        test_corrected_arrival_no_conflict_fast_path();
        test_corrected_arrival_same_line_transaction();
        test_same_line_order_audit_equivalence();
        test_corrected_epoch_suffix_transaction();
        test_causal_timing_sparse_closure();
        test_response_timing_retime_transaction();
        test_time_epoch_scheduler();
        test_parallel_feedback_equivalence();
        test_topology_scaled_frfcfs_sparse_repair();
        test_frfcfs_channel_parallel_equivalence();
        test_time_epoch_inflight_memory();
        test_private_preview_equivalence();
        test_private_preview_response_feedback_equivalence();
        test_private_preview_sparse_set_repair();
        test_causal_frontier_skew();
        std::cout << "all FastSim tests passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "test failure: " << error.what() << '\n';
        return 1;
    }
}
