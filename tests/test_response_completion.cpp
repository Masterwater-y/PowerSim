#include "fastsim/simulator.hpp"
#include "fastsim/shared_fill.hpp"
#include "fastsim/interval_core.hpp"
#include "fastsim/fixed_read_services.hpp"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
#include <unistd.h>

namespace {
using namespace fastsim;
void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error("response completion: " + message);
}
class Records final : public TraceSource {
  public:
    explicit Records(std::vector<TraceRecord> records) : records_(std::move(records)) {}
    bool next(TraceRecord& record) override {
        if (cursor_ == records_.size()) return false;
        record = records_[cursor_++];
        return true;
    }
    std::string description() const override { return "response-completion-test"; }
  private:
    std::vector<TraceRecord> records_;
    std::size_t cursor_ = 0;
};
TraceRecord alu(unsigned distance = 0, std::int16_t op = 1) {
    TraceRecord r;
    r.flags = kRetires;
    r.pc = 0x1000;
    r.op_class = op;
    r.producer_dists[0] = distance;
    r.n_src = distance != 0;
    return r;
}
TraceRecord load(std::uint64_t address, unsigned distance = 0, unsigned size = 8) {
    auto r = alu(distance, 56);
    r.flags |= kLoad | kPhysicalAddress;
    r.address = address;
    r.size = size;
    return r;
}
SimulatorConfig config(unsigned width = 8) {
    SimulatorConfig c;
    c.core_model = "interval_weave";
    c.cores = 1;
    c.interval_scheduler = "time_epoch";
    c.interval_max_cycles = 1024;
    c.chunk_instructions = c.interval_target_uops = 64;
    c.lookahead_chunks = 2;
    c.fetch_width = c.decode_width = c.rename_width = c.dispatch_width = 8;
    c.issue_width = c.commit_width = 8;
    c.writeback_width = width;
    c.integer_divide_latency = 24;
    c.response_queue_feedback = true;
    c.response_sparse_scoreboard = true;
    c.response_block_summary = true;
    c.response_memory_descriptor = true;
    c.response_monotone_iq_calendar = true;
    c.response_activity_certificate = true;
    c.l1d.size_bytes = 4096;
    c.l2.size_bytes = 16384;
    c.llc.size_bytes = 65536;
    c.cha_count = c.dram.channels = c.dram.banks_per_channel = 1;
    return c;
}
SimulationStats run(std::vector<TraceRecord> records, SimulatorConfig c, bool fast = false,
                    bool preserve_pcs = false) {
    c.cpi_attribution = !fast;
    c.response_frontier_audit_stride_uops = fast ? 0 : 1;
    c.response_materialized_uop_fast_kernel = fast;
    c.validate();
    if (!preserve_pcs) {
        for (std::size_t i = 0; i < records.size(); ++i) records[i].pc += i * 4;
    }
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::make_unique<Records>(std::move(records)));
    Simulator simulator(c, std::move(traces));
    return simulator.run();
}
const ResponseFrontierAuditSample& sample(const SimulationStats& s, unsigned seq) {
    const auto& rows = s.response_frontier_audit.at(0);
    const auto found = std::find_if(rows.begin(), rows.end(), [&](const auto& row) {
        return row.sequence == seq;
    });
    require(found != rows.end(), "missing audit UOP " + std::to_string(seq));
    return *found;
}
void verify(const SimulationStats& s, const std::vector<TraceRecord>& records, unsigned width,
            unsigned response_to_ready = 0) {
    std::map<std::uint64_t, unsigned> writebacks;
    for (unsigned seq = 0; seq < records.size(); ++seq) {
        const auto& row = sample(s, seq);
        require(++writebacks[row.actual_completion_cycle] <= width,
                "writeback capacity exceeded at cycle " + std::to_string(row.actual_completion_cycle));
        for (const auto distance : records[seq].producer_dists) {
            if (!distance || distance > seq) continue;
            require(row.actual_issue_cycle >= sample(s, seq - distance).actual_completion_cycle,
                    "RAW consumer issued before producer writeback");
        }
        if (!has_flag(records[seq].flags, kLoad) || records[seq].is_write()) continue;
        for (const auto& memory : row.memory_events) {
            if (memory.instruction_fetch || memory.write) continue;
            require(row.actual_completion_cycle >= memory.response_cycle + response_to_ready,
                    "load readiness precedes fragment response plus wakeup");
        }
    }
}
void compare(const SimulationStats& a, const SimulationStats& b) {
    require(a.total_core().cycles == b.total_core().cycles &&
                a.total_core().retired_uops == b.total_core().retired_uops &&
                a.total_core().memory_accesses == b.total_core().memory_accesses &&
                a.llc.misses == b.llc.misses &&
                a.response_load_data_ready_repairs == b.response_load_data_ready_repairs &&
                a.sparse_resource_writeback_collision_cycles == b.sparse_resource_writeback_collision_cycles,
            "generic and materialized kernels disagree");
    require(b.response_materialized_fast_kernel_uops > 0, "fast kernel was not exercised");
}

void test_post_commit_service_certificate() {
    // Two ordinary stores converge in the post-commit solver, but a later
    // independent DRAM repair must not replace their accepted send schedule
    // with the earlier AGU proposal. This catches loss of the accepted batch
    // and a stale response/SQ certificate, not a particular CPI value.
    auto c = config();
    c.store_post_commit_request = true;
    c.interval_private_preview = false;
    c.interval_reweave_passes = 1;
    c.dram.scheduler = "frfcfs";
    c.dram.frfcfs_selection_window = 4;
    c.dram.frfcfs_passes = 8;
    std::vector<TraceRecord> stores;
    for (const auto address : {0x8000ull, 0x8400ull}) {
        auto store = load(address);
        store.flags = kRetires | kStore | kPhysicalAddress;
        store.op_class = 57;
        stores.push_back(store);
    }
    const auto s = run(stores, c);
    require(s.store_post_commit_request_stable_epochs > 0,
            "fixture did not enter a stable post-commit store epoch");
    require(s.dram_frfcfs_stable_epochs > 0,
            "post-commit certificate bypassed the FR-FCFS controller");
    for (unsigned seq = 0; seq < stores.size(); ++seq) {
        const auto& row = sample(s, seq);
        require(row.memory_events.size() == 1,
                "post-commit fixture unexpectedly split its store");
        const auto& event = row.memory_events.front();
        const auto actual_send = row.store_drain_ready_cycle - event.latency_cycles;
        require(event.shared_stage_issue_cycle == actual_send,
                "accepted store service origin differs from final send for " +
                std::to_string(seq) + ": origin=" +
                std::to_string(event.shared_stage_issue_cycle) + " send=" +
                std::to_string(actual_send));
        require(event.shared_response_cycle == row.store_drain_ready_cycle,
                "accepted shared response differs from SQ/store drain release");
    }
    compare(s, run(stores, c, true));

    // These independent repairs are not participants in the post-commit
    // fixed point. Reject their combination rather than silently skipping
    // one or letting it invalidate an accepted send/response certificate.
    for (unsigned variant = 0; variant < 3; ++variant) {
        auto incompatible = c;
        incompatible.interval_response_retime = variant == 0;
        incompatible.interval_causal_timing = variant == 1;
        incompatible.interval_rob_head_suffix_replay = variant == 2;
        bool rejected = false;
        try {
            incompatible.validate();
        } catch (const std::invalid_argument& error) {
            rejected = std::string(error.what()).find(
                           "store_post_commit_request") != std::string::npos;
        }
        require(rejected, "post-commit accepted an independent timing repair");
    }
}

void test_load_ready_contract() {
    for (const auto model : {"scalar"}) {
        SimulatorConfig unsupported;
        unsupported.core_model = model;
        unsupported.validate();
        unsupported.ordinary_load_latency = 3;
        bool rejected = false;
        try { unsupported.validate(); } catch (const std::invalid_argument& error) {
            rejected = std::string(error.what()).find("ordinary_load_latency") != std::string::npos;
        }
        require(rejected, "ordinary-load override silently ignored by " + std::string(model));
    }
    // Current gem5: issue -> data return = 2, then consumer issue at +3.
    // Preserve the real same-PC store edge: a load/add/store iteration is
    // five cycles, not six. This catches a stale four-cycle load-ready floor,
    // not just a change to the text of the profile.
    const auto directory = std::filesystem::path(FASTSIM_PROJECT_ROOT) /
                           "tmp/fastsim-tests" / std::to_string(::getpid());
    std::filesystem::create_directories(directory);
    const auto path = directory / "load-ready-contract.cfg";
    {
        std::ofstream file(path);
        file << "config.include = " << FASTSIM_PROJECT_ROOT
             << "/configs/gem5-v28_1-time-epoch.cfg\n"
             << "core.ordinary_load_latency = 3\n"
             << "core.load_response_to_ready = 1\n";
        require(bool(file), "cannot write load-ready fixture");
    }
    auto c = load_simulator_config(path.string());
    // The maintained native-FS alias must activate this timing contract.
    // Replay its load-ready parameters through the synthetic core fixture;
    // keep guest/kernel/I-side setup out of this RAM-only witness.
    const auto maintained = load_simulator_config(
        (std::filesystem::path(FASTSIM_PROJECT_ROOT) /
         "configs/gem5-fs-native-kernel.cfg").string());
    c.ordinary_load_latency = maintained.ordinary_load_latency;
    c.load_response_to_ready = maintained.load_response_to_ready;
    // This synthetic RAM-only fixture has no guest address-space metadata.
    c.require_virtual_page_token = false;
    c.dtlb.enabled = false;
    c.store_set_same_pc_feedback = true;
    c.response_monotone_iq_calendar = true;
    c.cores = c.cha_count = c.dram.channels = 1;
    c.integer_divide_latency = 600;
    c.chunk_instructions = c.interval_target_uops = 64;
    c.lookahead_chunks = 2;
    std::vector<TraceRecord> chain{load(0x8000), alu(0, 3)};
    for (unsigned i = 0; i < 12; ++i) {
        auto read = load(0x8000 + i * 2, i == 0 ? 1 : 0, 2);
        auto add = alu(1);
        auto store = read;
        read.pc = add.pc = store.pc = 0x4000;
        read.flags |= kMicroOp;
        add.flags |= kMicroOp;
        store.flags = kRetires | kPhysicalAddress | kStore | kMicroOp | kLastMicroOp;
        store.op_class = 57;
        store.producer_dists[0] = 1;
        chain.insert(chain.end(), {read, add, store});
    }
    const auto s = run(chain, c, false, true);
    for (unsigned seq = 2; seq < chain.size(); seq += 3) {
        const auto& read = sample(s, seq);
        const auto& add = sample(s, seq + 1);
        const auto& store = sample(s, seq + 2);
        require(read.selected_memory_path == 0,
                "RMW witness must hit a line filled before the delayed loop");
        require(add.actual_issue_cycle == read.actual_issue_cycle + 3,
                "resident load consumer must issue at +3, observed +" +
                std::to_string(add.actual_issue_cycle - read.actual_issue_cycle));
        require(store.actual_completion_cycle == read.actual_issue_cycle + 5,
                "load/add/store address-generation chain must take five cycles");
        if (seq == 2) continue;
        require(read.issue_gate_kind == static_cast<unsigned>(ResponseIssueGateKind::kStoreSetProducer) &&
                    read.issue_gate_owner_sequence == seq - 1 &&
                    read.actual_issue_cycle == sample(s, seq - 1).actual_completion_cycle,
                "next RMW load must retain the preceding non-aliasing store edge");
    }
    verify(s, chain, c.writeback_width, 1);
    compare(s, run(chain, c, true, true));

    // A regular-load repair must not silently retime atomics or store AGU.
    IntervalCoreModel core(c);
    const auto ordinary = core.schedule(load(0xa000), false);
    require(ordinary.completion_cycle - ordinary.execute_cycle == 3,
            "maintained ordinary-load core lower bound must be three cycles before response feedback");
    auto atomic = load(0x9000);
    atomic.flags |= kAtomic;
    const auto a = core.schedule(atomic, false);
    const auto w = core.schedule(chain[4], false);
    require(a.completion_cycle - a.execute_cycle == 4 &&
                w.completion_cycle - w.execute_cycle == 1,
            "ordinary-load profile changed legacy atomic/store latency");

    // A clamped request returns much later than its UOP issue. Readiness
    // must be based on the actual response plus wakeup, not only the L1 floor.
    const std::vector<TraceRecord> late{
        alu(0, 3), alu(1), load(0x8000, 1), load(0x10000), alu(1)};
    const auto delayed = run(late, c);
    const auto& read = sample(delayed, 3);
    require(read.selected_memory_corrected_issue_cycle > read.actual_issue_cycle,
            "late-response witness must have distinct request and UOP origins");
    require(read.actual_completion_cycle >= read.selected_memory_response_cycle + 1 &&
                sample(delayed, 4).actual_issue_cycle >= read.selected_memory_response_cycle + 1,
            "late data response must pass through consumer wakeup before use");
    compare(delayed, run(late, c, true));

    for (const auto delay : {0u, 1u, 3u}) {
        auto edges = config(1);
        edges.minimum_load_latency = 4;
        edges.ordinary_load_latency = 3;
        edges.load_response_to_ready = delay;
        const std::vector<TraceRecord> split{load(0x8038, 0, 80), alu(1)};
        const auto fragments = run(split, edges);
        require(sample(fragments, 0).memory_events.size() == 3,
                "load-ready witness must contain three fragments");
        verify(fragments, split, 1, delay);
        compare(fragments, run(split, edges, true));

        // Same-time responses still share the existing producer-ready port.
        std::vector<TraceRecord> collision{load(0x8000), alu(0, 3), load(0x8000, 1)};
        for (unsigned i = 0; i < 12; ++i) collision.push_back(load(0x8000));
        collision.push_back(alu(1));
        const auto narrow = run(collision, edges);
        require(narrow.sparse_resource_writeback_collision_cycles > 0,
                "load-ready witness did not exercise ready/WB contention");
        verify(narrow, collision, 1, delay);
        compare(narrow, run(collision, edges, true));

        edges.chunk_instructions = edges.interval_target_uops = 7;
        edges.interval_max_cycles = 8;
        auto across = late;
        across.insert(across.end(), 90, alu());
        across.push_back(load(0x18000));
        across.push_back(alu(1));
        const auto carried = run(across, edges);
        verify(carried, across, 1, delay);
        compare(carried, run(across, edges, true));
    }

    // Wakeup is a consumer edge, not a longer memory-resource lifetime.
    for (const bool sequencer : {false, true}) {
        auto edges = config();
        edges.ordinary_load_latency = 3;
        edges.load_response_to_ready = 20;
        edges.ruby_sequencer_max_outstanding = sequencer ? 1 : 16;
        edges.l1d_mshrs = sequencer ? 16 : 1;
        const std::vector<TraceRecord> requests{load(0x8000), load(0x18000)};
        const auto released = run(requests, edges);
        const auto& first = sample(released, 0);
        const auto& next = sample(released, 1);
        require(next.selected_memory_corrected_issue_cycle >= first.selected_memory_response_cycle &&
                    next.selected_memory_corrected_issue_cycle < first.actual_completion_cycle,
                "memory slot release incorrectly waits for consumer wakeup");
        verify(released, requests, edges.writeback_width, 20);
        compare(released, run(requests, edges, true));
    }

    auto legacy = c;
    legacy.ordinary_load_latency.reset();
    legacy.load_response_to_ready = 0;
    for (const auto write : {atomic, chain[4]}) {
        const std::vector<TraceRecord> records{write, alu(1)};
        require(run(records, c).total_core().cycles == run(records, legacy).total_core().cycles,
                "load-ready repair changed a standalone atomic/store path");
    }
    IntervalCoreModel legacy_core(legacy);
    const auto old_load = legacy_core.schedule(load(0xa000), false);
    require(old_load.completion_cycle - old_load.execute_cycle == 4,
            "unset ordinary-load override must preserve legacy latency");

    for (unsigned invalid = 0; invalid < 5; ++invalid) {
        auto bad = config();
        bad.load_response_to_ready = 1;
        if (invalid == 0) bad.ordinary_load_latency = 0;
        if (invalid == 1) bad.ordinary_load_latency = (1u << 20) + 1;
        if (invalid == 2) bad.load_response_to_ready = (1u << 20) + 1;
        if (invalid == 3) bad.interval_scheduler = "frontier";
        if (invalid == 4) bad.response_causal_block_transfer = true;
        bool rejected = false;
        try { bad.validate(); } catch (const std::invalid_argument& error) {
            const auto key = invalid < 2 ? "ordinary_load_latency" : "load_response_to_ready";
            rejected = std::string(error.what()).find(key) != std::string::npos;
        }
        require(rejected, "unsupported load-ready configuration was accepted");
    }
}

void test_request_origins() {
    {
        // The first channel's request is already admissible. A dependent
        // store on another channel crosses the other core's next epoch.
        // Rejecting that store must not discard the independent first service.
        const auto simulate = [](bool fast) {
            auto c = config();
            c.cores = c.cha_count = c.dram.channels = 2;
            c.dram.t_cl = 2000;
            c.response_shared_service_constraints = true;
            c.response_materialized_uop_fast_kernel = fast;
            c.cpi_attribution = !fast;
            c.response_frontier_audit_stride_uops = fast ? 0 : 1;
            c.validate();
            auto store = load(0x8040, 1);
            store.flags = kRetires | kPhysicalAddress | kStore;
            std::vector<std::unique_ptr<TraceSource>> traces;
            traces.push_back(std::make_unique<Records>(std::vector<TraceRecord>{
                load(0x8000), store}));
            traces.push_back(std::make_unique<Records>(std::vector<TraceRecord>(20000, alu())));
            Simulator simulator(c, std::move(traces));
            return simulator.run();
        };
        const auto s = simulate(false);
        require(s.shared_service_constraints.committed_requests == 1 &&
                    s.shared_service_constraints.committed_store_requests == 0,
                "late independent resource discarded the admissible load service");
        compare(s, simulate(true));
    }
    {
        // A cold ordinary store acquires its line through the same read
        // resources as loads, but only after commit and the older TSO store.
        // Rejecting every mixed component hides the load owners as well.
        auto c = config();
        c.response_shared_service_constraints = true;
        auto first_store = load(0x8080, 1);
        first_store.flags = kRetires | kPhysicalAddress | kStore;
        auto second_store = first_store;
        second_store.address = 0x80c0;
        const std::vector<TraceRecord> records{
            load(0x8000), load(0x8040, 1), first_store, second_store};
        const auto s = run(records, c);
        require(s.shared_service_constraints.committed_requests == 4,
                "ordinary stores excluded the dependent load service component");
        for (unsigned seq = 0; seq < records.size(); ++seq) {
            const auto& row = sample(s, seq);
            const auto& event = row.memory_events.at(0);
            require(event.shared_stage_issue_cycle == event.corrected_issue_cycle,
                    "mixed component used inconsistent shared and core request origins");
            if (seq < 2) continue;
            require(event.corrected_issue_cycle >= row.actual_retire_cycle + 2 &&
                        row.store_drain_ready_cycle == event.response_cycle &&
                        row.actual_retire_cycle < event.response_cycle,
                    "ordinary store reserved service before commit or released SQ before response");
        }
        require(sample(s, 3).memory_events.at(0).corrected_issue_cycle >=
                    sample(s, 2).memory_events.at(0).response_cycle + 1,
                "next TSO store bypassed the prior store response");
        verify(s, records, c.writeback_width);
        compare(s, run(records, c, true));
    }
    // The second miss cannot arrive until the first load has returned. Its
    // old, early-arrival queue wait must not be charged again after that RAW
    // dependency. The final ALU makes this a retirement-path counterexample.
    {
        const std::vector<TraceRecord> records{
            load(0x8000), load(0x8040, 1), alu(1)};
        auto c = config();
        const auto old = run(records, c);
        c.response_shared_service_constraints = true;
        const auto repaired = run(records, c);
        verify(repaired, records, c.writeback_width);
        const auto& memory = sample(repaired, 1).memory_events.at(0);
        require(memory.shared_stage_issue_cycle == memory.corrected_issue_cycle &&
                    memory.canonical_dram_arrival_cycle >= memory.corrected_issue_cycle,
                "retimed service retained an earlier shared request origin");
        require(sample(repaired, 1).memory_events.at(0).response_cycle <
                    sample(old, 1).memory_events.at(0).response_cycle &&
                    repaired.total_core().cycles < old.total_core().cycles,
                "moved arrival reused the stale DRAM queue wait");
        compare(repaired, run(records, c, true));
    }
    {
        // A younger independent request overtakes the retimed RAW consumer.
        // Reject the whole resource component, including earlier trial queue
        // updates, then reproduce the canonical core result exactly.
        const std::vector<TraceRecord> records{
            load(0x8000), load(0x8040, 1), load(0x8080), alu(1)};
        auto c = config();
        const auto old = run(records, c);
        c.response_shared_service_constraints = true;
        const auto rejected = run(records, c);
        require(rejected.shared_service_constraints.order_fallback_components == 1 &&
                    rejected.shared_service_constraints.committed_requests == 0 &&
                    rejected.total_core().cycles == old.total_core().cycles &&
                    rejected.cha.at(0).queue_cycles == old.cha.at(0).queue_cycles,
                "reversed service order leaked part of its rejected transaction");
        verify(rejected, records, c.writeback_width);
        compare(rejected, run(records, c, true));
    }
    {
        auto c = config();
        c.l1d.size_bytes = c.l2.size_bytes = 64;
        c.l1d.associativity = c.l2.associativity = 1;
        const std::vector<TraceRecord> records{
            load(0x8000), load(0x8040, 1), load(0x8000, 1), alu(1)};
        const auto old = run(records, c);
        c.response_shared_service_constraints = true;
        const auto rejected = run(records, c);
        require(rejected.shared_service_constraints.visibility_fallback_components == 1 &&
                    rejected.shared_service_constraints.committed_requests == 0 &&
                    rejected.total_core().cycles == old.total_core().cycles &&
                    rejected.cha.at(0).llc_merged_wait_cycles == old.cha.at(0).llc_merged_wait_cycles,
                "arrival crossed a fill boundary without rolling back its service component");
        compare(rejected, run(records, c, true));
    }
    {
        const auto simulate_pair = [](bool shared_channel) {
            auto c = config();
            c.cores = 2;
            c.cha_count = 2;
            c.dram.channels = shared_channel ? 1 : 2;
            c.response_shared_service_constraints = true;
            c.response_materialized_uop_fast_kernel = true;
            c.validate();
            std::vector<std::unique_ptr<TraceSource>> traces;
            traces.push_back(std::make_unique<Records>(std::vector<TraceRecord>{
                load(0x8000), load(0x8080, 1), alu(1)}));
            traces.push_back(std::make_unique<Records>(std::vector<TraceRecord>{
                load(0x8040), load(0x80c0, 1), alu(1)}));
            Simulator simulator(c, std::move(traces));
            return simulator.run();
        };
        const auto parallel = simulate_pair(false);
        require(parallel.shared_service_constraints.committed_requests == 4 &&
                    parallel.shared_service_constraints.committed_components == 2,
                "independent shared resource components must commit in parallel");
        const auto conflicting = simulate_pair(true);
        require(conflicting.shared_service_constraints.conflict_components == 2 &&
                    conflicting.shared_service_constraints.committed_requests == 0,
                "two core workers mutated the same DRAM channel");
    }
    {
        // A store on another CHA/channel does not belong to the read service
        // component, even though the same CPU executes both instructions.
        auto c = config();
        c.cha_count = c.dram.channels = 2;
        auto store = load(0x8040);
        store.flags = kRetires | kPhysicalAddress | kStore;
        const std::vector<TraceRecord> records{
            load(0x8000), load(0x8080, 1), alu(1), store};
        const auto old = run(records, c);
        c.response_shared_service_constraints = true;
        const auto s = run(records, c);
        require(s.shared_service_constraints.committed_store_requests == 1 &&
                    s.shared_service_constraints.committed_requests == 3 &&
                    sample(s, 1).memory_events.at(0).response_cycle <
                        sample(old, 1).memory_events.at(0).response_cycle,
                "an unrelated shared store disabled the read service component");
        compare(s, run(records, c, true));
    }
    {
        // Force several epoch-entry checkpoints. Replaying their admitted
        // DRAM arrivals as one continuous controller stream must reproduce
        // every committed service; host boundaries cannot reset row/bus state.
        auto c = config();
        c.response_shared_service_constraints = true;
        c.chunk_instructions = c.interval_target_uops = 1;
        c.interval_max_cycles = 1;
        c.fetch_width = c.decode_width = c.rename_width = c.dispatch_width = 1;
        c.rob_entries = 1;
        const std::vector<TraceRecord> records{
            load(0x8000), load(0x8040, 1), load(0x10000, 1), load(0x10040, 1), alu(1)};
        const auto s = run(records, c);
        require(s.shared_service_constraints.committed_components >= 2,
                "cross-epoch service witness did not cross a committed boundary");
        std::vector<testing::DramControllerProbeEvent> requests;
        std::vector<std::uint64_t> completions;
        for (unsigned seq = 0; seq < 4; ++seq) {
            const auto& event = sample(s, seq).memory_events.at(0);
            requests.push_back({event.canonical_dram_arrival_cycle,
                                event.descriptor_memory_line, false});
            completions.push_back(event.canonical_dram_completion_cycle);
        }
        const auto continuous = testing::run_dram_controller_probe(c.dram, c.llc.line_size, requests);
        require(continuous.completions == completions,
                "shared service commit lost cross-epoch DRAM state");
        verify(s, records, c.writeback_width);
        compare(s, run(records, c, true));
    }
    // A long-latency ALU delays an older request in the functional ordering
    // envelope. A different, earlier miss delays the younger request's RAW
    // readiness. Those two independent floors overlap; they are not serial.
    for (const auto divide_latency : {24u, 240u}) {
        for (const auto size : {8u, 80u}) {
            const auto address = size == 8 ? 0x8000u : 0x8038u;
            const std::vector<TraceRecord> records{
                load(address, 0, size), alu(0, 3), load(0x10000, 1),
                load(address, 3, size), alu(1)};
            auto c = config();
            c.integer_divide_latency = divide_latency;
            const auto s = run(records, c);
            verify(s, records, c.writeback_width);
            const auto& row = sample(s, 3);
            const auto& parent = sample(s, 0);
            require(row.actual_issue_cycle > row.base_issue_cycle &&
                        row.actual_issue_cycle >= parent.actual_completion_cycle,
                    "origin witness must inherit a delayed RAW producer");
            require(row.memory_events.size() == (size == 8 ? 1 : 3),
                    "origin witness lost load fragments");
            for (const auto& event : row.memory_events) {
                const auto origin = event.base_issue_cycle + row.interval_gap_cycles;
                require(origin > row.base_issue_cycle,
                        "origin witness must also exercise functional clamping");
                require(event.corrected_issue_cycle == std::max(origin, row.actual_issue_cycle),
                        "request charged both overlapping issue-ready and ordering delays");
                require(event.response_cycle == event.corrected_issue_cycle + event.latency_cycles,
                        "saved service latency was rebased to a different request origin");
            }
            compare(s, run(records, c, true));
        }
    }

    // A real request-capacity wait must still constrain the UOP. After the
    // event displacement is normalized, copying it back to the UOP origin
    // would lose the clamp-sized part of this resource gate.
    for (const bool sequencer : {false, true}) {
        const std::vector<TraceRecord> records{
            load(0x8000), alu(0, 3), load(0x10000, 1), load(0x18000, 3), alu(1)};
        auto c = config();
        c.ruby_sequencer_max_outstanding = sequencer ? 1 : 16;
        c.l1d_mshrs = sequencer ? 16 : 1;
        const auto s = run(records, c);
        verify(s, records, c.writeback_width);
        const auto& row = sample(s, 3);
        const auto expected_gate = sequencer ? ResponseIssueGateKind::kSequencer
                                             : ResponseIssueGateKind::kL1Mshr;
        require(row.issue_gate_kind == static_cast<std::uint32_t>(expected_gate),
                "directed origin test did not reach the intended resource gate");
        require(row.actual_issue_cycle == row.selected_memory_corrected_issue_cycle,
                "request resource-ready edge was not projected back to the UOP origin");
        compare(s, run(records, c, true));
    }

    // Stores and atomic requests use the same data-request origin conversion;
    // store address generation remains distinct from its post-commit send.
    for (const bool atomic : {false, true}) {
        auto target = load(0x8000, 3);
        target.flags = kRetires | kPhysicalAddress | (atomic ? kAtomic : kStore);
        const std::vector<TraceRecord> records{
            load(0x8000), alu(0, 3), load(0x10000, 1), target, alu(1)};
        auto c = config();
        const auto s = run(records, c);
        verify(s, records, c.writeback_width);
        const auto& row = sample(s, 3);
        for (const auto& event : row.memory_events) {
            const auto origin = event.base_issue_cycle + row.interval_gap_cycles;
            require(origin > row.base_issue_cycle,
                    "write origin witness must exercise functional clamping");
            require(event.corrected_issue_cycle == std::max(origin, row.actual_issue_cycle),
                    "write request mixed the UOP and service origins");
        }
        compare(s, run(records, c, true));
    }
}

SimulationStats run_fill_boundary(bool retime) {
    auto c = config();
    c.cores = 2;
    c.interval_private_preview = false;
    c.interval_response_retime = retime;
    c.iq_entries = 16;
    c.ruby_sequencer_max_outstanding = 4;
    c.validate();
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::make_unique<Records>(std::vector<TraceRecord>{
        load(0x10000), alu(1)}));
    traces.push_back(std::make_unique<Records>(std::vector<TraceRecord>{
        load(0x20000), load(0x10000, 1), alu(1)}));
    Simulator simulator(c, std::move(traces));
    return simulator.run();
}
} // namespace

void test_private_read_service_boundaries() {
    // The protocol callback removes the transaction one cycle before CPU
    // response. A request at that callback must take a new local-hit service.
    PrivateReadServices services;
    services.reset(8);
    services.observe(0, 32, 10, true, false);
    services.observe(1, 32, 11, true, true);
    services.observe(2, 32, 12, true, true);
    services.seal(1000);
    const auto owner = services.resolve(0, 32, 10, 100);
    const auto follower = services.resolve(1, 32, 98, 110);
    const auto after_callback = services.resolve(2, 32, 99, 101);
    require(owner.certified && owner.callback == 99 &&
                follower.follower && follower.response == 100 &&
                !after_callback.follower && after_callback.response == 101,
            "callback boundary or parent response reference is incorrect");
    require(services.counters.response_removed_cycles == 10,
            "service references must allow shorter responses, not only append waits");

    for (const bool future_parent : {false, true}) {
        services.reset(8);
        services.observe(0, 32, 10, true, false);
        services.observe(1, 32, 11, true, true);
        services.seal(future_parent ? 1000 : 90);
        const auto rejected = services.resolve(0, 32, future_parent ? 20 : 10, 100);
        const auto child = services.resolve(1, 32, 11, 13);
        require(!rejected.certified && !child.follower && child.response == 13 &&
                    services.counters.owners == 0,
                "unproved admission order or cross-batch owner leaked a service reference");
    }
    services.reset(8);
    services.observe(0, 32, 10, true, false);
    services.observe(1, 32, 11, true, true);
    services.seal(1000, 50);
    require(!services.resolve(0, 32, 10, 100).certified &&
                services.counters.entry_rejected_loads == 2,
            "untracked incoming transactions were ignored by the component proof");

    // A slot may be reused, but the preceding batch's completed service may
    // not survive in it. Conflicting lines and unsupported accesses reject
    // the complete replacement component before response changes.
    for (const bool write : {false, true}) {
        services.reset(8);
        services.observe(0, 32, 10, true, false);
        services.observe(1, write ? 32 : 40, 11, !write, true);
        services.seal(1000);
        require(!services.active() && !services.resolve(0, 32, 10, 20).certified &&
                    services.counters.guarded_events == 2,
                "replacement or write component was not rejected atomically");
    }

    // The existing Sequencer heap counts CPU requests, including followers.
    // With capacity two the third request cannot join the first callback.
    const std::vector<TraceRecord> reads{
        load(0x8000), load(0x8008), load(0x8010), alu(1)};
    auto c = config();
    c.response_private_read_services = true;
    c.ruby_sequencer_max_outstanding = 2;
    const auto s = run(reads, c);
    const auto response = sample(s, 0).memory_events.at(0).response_cycle;
    require(s.private_read_services.owners == 1 && s.private_read_services.followers == 1 &&
                s.private_read_services.completed_hits == 1 &&
                sample(s, 2).memory_events.at(0).corrected_issue_cycle == response - 1 &&
                s.sequencer.at(0).max_outstanding == 2,
            "same-line followers did not consume request capacity until callback");
    verify(s, reads, c.writeback_width);
    compare(s, run(reads, c, true));

    // A RAW-delayed candidate owner cannot claim an independent younger
    // request whose lower bound precedes that owner's actual admission.
    const std::vector<TraceRecord> reversal{
        load(0x8000), load(0x8040, 1), load(0x8048), alu(1)};
    c = config();
    c.response_private_read_services = true;
    const auto rejected = run(reversal, c);
    require(rejected.private_read_services.admission_rejected_loads == 2 &&
                rejected.private_read_services.followers == 0,
            "a future owner was treated as already admitted");
    c.response_private_read_services = false;
    const auto original = run(reversal, c);
    require(rejected.total_core().cycles == original.total_core().cycles,
            "rejected component leaked a timing change");
}

void test_response_completion() {
    {
        const auto path = std::filesystem::temp_directory_path() /
            ("fastsim-fixed-reads-" + std::to_string(getpid()) + ".cfg");
        {
            std::ofstream out(path);
            out << "ruby.fixed_read_services = true\n"
                << "core.model = interval_weave\nsim.interval_scheduler = time_epoch\n"
                << "sim.interval_reweave_passes = 1\ncore.needs_tso = true\n"
                << "core.tso_store = buffer\ncore.tso_store_buffer_size = 114\n"
                << "core.response_queue_feedback = true\ncore.response_sparse_scoreboard = true\n"
                << "ruby.sequencer_max_outstanding = 16\n";
        }
        auto c = load_simulator_config(path.string());
        std::filesystem::remove(path);
        c.core_model = "interval_weave";
        c.cores = 1;
        c.interval_scheduler = "time_epoch";
        c.interval_max_cycles = 1024;
        c.chunk_instructions = c.interval_target_uops = 64;
        c.response_queue_feedback = true;
        c.response_sparse_scoreboard = true;
        c.response_block_summary = true;
        c.response_memory_descriptor = true;
        c.response_monotone_iq_calendar = true;
        c.response_activity_certificate = true;
        c.needs_tso = true;
        c.tso_store = TsoStoreMode::kBuffer;
        c.tso_store_buffer_size = 114;
        c.ruby_sequencer_max_outstanding = 16;
        const std::vector<TraceRecord> reads{
            load(0x8000), load(0x8008), load(0x8010), load(0x8018)};
        const auto fixed = run(reads, c);
        require(fixed.total_sequencer().coalesced_requests == 3 &&
                    fixed.total_core().l1d.accesses == 1 &&
                    fixed.total_sequencer().requests == 4,
                "fixed read services must suppress duplicate lookups but retain CPU requests");
        for (unsigned i = 1; i < 4; ++i)
            require(sample(fixed, i).memory_events.at(0).response_cycle ==
                        sample(fixed, 0).memory_events.at(0).response_cycle,
                    "fixed read followers must reference the owner's final response");
        verify(fixed, reads, c.writeback_width);
        require(fixed.fixed_read_committed_epochs == 1 && fixed.fixed_read_followers == 3,
                "fixed services must classify once per epoch");
        compare(fixed, run(reads, c, true));
        // Use the maintained mechanism-test pipeline for the remaining
        // fixtures (the parser smoke above starts from config-file defaults).
        c = config();
        c.needs_tso = true;
        c.tso_store = TsoStoreMode::kBuffer;
        c.tso_store_buffer_size = 114;
        c.ruby_sequencer_max_outstanding = 16;
        c.ruby_fixed_read_services = true;
        c.ruby_sequencer_max_outstanding = 1;
        const auto late = run(reads, c);
        require(late.fixed_read_late_references > 0 && late.total_core().l1d.accesses == 1,
                "late references must retain fixed classification and be diagnosed");
        for (unsigned i = 0; i < reads.size(); ++i) {
            const auto& m = sample(late, i).memory_events.at(0);
            require(m.response_cycle >= m.corrected_issue_cycle,
                    "late fixed follower responded before its own issue");
        }
        verify(late, reads, c.writeback_width);
        c.ruby_sequencer_max_outstanding = 16;
        const std::vector<TraceRecord> reversal{
            load(0x9000), load(0x8000, 1), load(0x8008), alu(1)};
        const auto reversed = run(reversal, c);
        verify(reversed, reversal, c.writeback_width);

        c.cha_count = c.dram.channels = c.dram.banks_per_channel = 1;
        c.dram.scheduler = "frfcfs";
        c.l1d.size_bytes = 4096;
        c.l2.size_bytes = 16384;
        c.llc.size_bytes = 65536;
        const std::vector<TraceRecord> reordered_reads{
            load(0x8000), load(0x10000), load(0x8040), load(0x8048), alu(1)};
        const auto reordered = run(reordered_reads, c);
        require(reordered.dram_frfcfs_reordered_requests > 0 &&
                    reordered.fixed_read_committed_epochs == 1 &&
                    reordered.total_core().l1d.accesses == 3 &&
                    sample(reordered, 3).memory_events.at(0).response_cycle ==
                        sample(reordered, 2).memory_events.at(0).response_cycle,
                "FR-FCFS must update the parent response without repeating fixed classification");
        verify(reordered, reordered_reads, c.writeback_width);
        compare(reordered, run(reordered_reads, c, true));

        c.interval_private_preview = true;
        c.domain_min_events = 1;
        const auto preview = run(reads, c);
        require(preview.fixed_read_followers == 3 && preview.fixed_read_committed_epochs == 1,
                "private preview silently bypassed fixed read classification");
        c.interval_private_preview = false;

        c.dram.scheduler = "fcfs";
        c.chunk_instructions = c.interval_target_uops = 8;
        c.interval_max_cycles = 8;
        const std::vector<TraceRecord> repeated(128, load(0x8000));
        const auto run_checked = [](const auto& records, auto cfg, const char* label, bool fast = false) {
            try { return run(records, cfg, fast); }
            catch (const std::exception& error) {
                throw std::runtime_error(std::string(label) + ": " + error.what());
            }
        };
        const auto prefix = run_checked(std::vector<TraceRecord>(8, load(0x8000)), c, "fixed prefix");
        const auto crossing = run_checked(repeated, c, "fixed crossing");
        require(crossing.fixed_read_committed_epochs > 1 &&
                    crossing.total_core().l1d.accesses +
                        crossing.total_sequencer().coalesced_requests == repeated.size(),
                "fixed services lost requests across epoch compaction");
        for (unsigned i = 0; i < 8; ++i)
            require(sample(prefix, i).memory_events.at(0).response_cycle ==
                        sample(crossing, i).memory_events.at(0).response_cycle,
                    "later epochs changed a committed prefix response");
        verify(crossing, repeated, c.writeback_width);

        c.chunk_instructions = c.interval_target_uops = 64;
        c.interval_max_cycles = 1024;
        auto store = load(0x8000);
        store.flags = kRetires | kStore | kPhysicalAddress;
        store.op_class = 57;
        c.store_forwarding = false;
        c.ruby_fixed_read_services = false;
        // The existing buffered-store synthetic trace fails residual CPI
        // attribution even with fixed services disabled. Exercise its request
        // semantics through the maintained fast kernel, as other store fixtures do.
        (void)run_checked(std::vector<TraceRecord>{load(0x8000), store, load(0x8008)}, c, "baseline write", true);
        c.ruby_fixed_read_services = true;
        const auto written = run_checked(std::vector<TraceRecord>{load(0x8000), store, load(0x8008)}, c, "fixed write", true);
        require(written.fixed_read_followers == 0 && written.total_core().l1d.accesses == 3,
                "write conflict reused a read generation");
        c.l1d.size_bytes = c.l2.size_bytes = 64;
        c.l1d.associativity = c.l2.associativity = 1;
        const auto replaced = run_checked(std::vector<TraceRecord>{load(0x8000), load(0x8040), load(0x8008)}, c, "fixed replacement");
        require(replaced.fixed_read_followers == 0 && replaced.total_core().l1d.accesses == 3,
                "eviction/refill reused a stale read generation");

        std::vector<std::unique_ptr<TraceSource>> traces;
        traces.push_back(std::make_unique<Records>(reads));
        Simulator simulator(c, std::move(traces));
        bool rejected = false;
        try { simulator.set_core_frequencies({c.reference_frequency_hz / 2}); }
        catch (const std::invalid_argument&) { rejected = true; }
        require(rejected, "fixed read services accepted runtime DVFS");
        for (bool resource_repair : {false, true}) {
            auto unsupported = c;
            unsupported.response_paired_frontier = !resource_repair;
            unsupported.response_sparse_resource_repair = resource_repair;
            rejected = false;
            try { (void)run(reads, unsupported); }
            catch (const std::invalid_argument&) { rejected = true; }
            require(rejected, "unsupported fixed read timing kernel was silently accepted");
        }
    }
    {
        FixedReadServices table;
        const auto first = table.insert(7, {0, 10, 0, 3, 10, 99, 100, 0});
        table.publish(7, first.generation, 20, 120, 1);
        require(table.find(7, 25)->current_index == FixedReadServices::incoming,
                "published fixed read retained an epoch-local index");
        table.begin_epoch(30);
        require(table.find(7, 40)->response == 120 &&
                    table.find(7, 40)->current_index == FixedReadServices::incoming,
                "incoming service lost its absolute committed response");
        require(table.find(7, 118) != nullptr && table.find(7, 119) == nullptr,
                "callback equality must terminate alias eligibility");
        table.invalidate(7);
        const auto second = table.insert(7, {0, 11, 0, 4, 35, 139, 140, 0});
        table.publish(7, first.generation, 0, 999, 1);
        require(second.generation != first.generation && table.find(7, 40)->response == 140,
                "stale generation changed the replacement service");
        table.insert(100, {0, 12, 0, 5, 40, 149, 150, 0});
        const auto before = table;
        table.invalidate(7);
        table = before; // failed speculative transaction
        table.invalidate_range(0, 64); // committed baseline page update
        require(table.find(7, 40) == nullptr && table.find(100, 40) != nullptr,
                "fallback invalidation must preserve unrelated incoming services");
    }
    {
        auto c = config();
        c.needs_tso = true;
        c.tso_store = TsoStoreMode::kBuffer;
        c.tso_store_buffer_size = 114;
        c.ruby_sequencer_max_outstanding = 16;
        c.ruby_buffer_service_repair = true;
        c.ruby_buffer_service_local_fallback = true;
        c.ruby_buffer_service_work_percent = 1000;
        auto store = load(0x8000);
        store.flags = kRetires | kStore | kPhysicalAddress;
        store.op_class = 57;
        const auto mixed = run({load(0x8040), load(0x8000), store, load(0x8048)}, c, true);
        require(mixed.buffer_service_committed_epochs > 0 &&
                    mixed.total_sequencer().coalesced_requests == 1 &&
                    mixed.total_core().l1d.accesses == 3 &&
                    mixed.buffer_service_local_modeled_events == 2 &&
                    mixed.buffer_service_local_baseline_events == 2 &&
                    mixed.buffer_service_local_broad_fallback_epochs == 0,
                "local fallback must retain Y coalescing when X has a read/write conflict");
        // The fourth read's RAW dependency moves it beyond X's callback.
        // Only that event loses its service decision; Y keeps its follower.
        const std::vector<TraceRecord> dynamic{
            load(0x8000), load(0x8040), load(0x8048), load(0x8008, 3)};
        const auto repaired = run(dynamic, c);
        require(repaired.buffer_service_local_degraded_events == 1 &&
                    repaired.buffer_service_local_baseline_events == 1 &&
                    repaired.buffer_service_local_modeled_events == 3 &&
                    repaired.buffer_service_local_retained_aliases == 1 &&
                    repaired.total_core().l1d.accesses == 3 &&
                    repaired.buffer_service_passes == 2,
                "a moved follower must not discard unrelated service decisions");
        compare(repaired, run(dynamic, c, true));
        testing::set_buffer_service_baseline_events({3});
        SimulationStats reference;
        try { reference = run(dynamic, c); }
        catch (...) { testing::set_buffer_service_baseline_events({}); throw; }
        testing::set_buffer_service_baseline_events({});
        require(reference.buffer_service_passes == 1 &&
                    reference.buffer_service_local_degraded_events == 0 &&
                    reference.total_core().cycles == repaired.total_core().cycles &&
                    reference.total_core().l1d.accesses == repaired.total_core().l1d.accesses &&
                    reference.total_core().l1d.misses == repaired.total_core().l1d.misses &&
                    reference.total_core().l2.misses == repaired.total_core().l2.misses &&
                    reference.llc.misses == repaired.llc.misses &&
                    reference.cha.at(0).dram_reads == repaired.cha.at(0).dram_reads,
                "local retry must match full replay with the final decisions fixed upfront");
        for (unsigned i = 0; i < dynamic.size(); ++i)
            require(sample(reference, i).memory_events.at(0).response_cycle ==
                        sample(repaired, i).memory_events.at(0).response_cycle,
                    "masked full replay and dynamic local downgrade callbacks differ");
        // Same cache set, one transaction: do not stitch per-line cache states.
        const auto same_set = run({load(0x9000), load(0x8000), store, load(0x9008)}, c, true);
        require(same_set.buffer_service_local_retained_aliases == 1 &&
                    same_set.total_core().l1d.accesses == 3,
                "same-set interactions must preserve eligible merges in one replay");
        auto cross_config = c;
        cross_config.chunk_instructions = cross_config.interval_target_uops = 8;
        cross_config.interval_max_cycles = 8;
        const auto prefix = run(std::vector<TraceRecord>(8, load(0x8000)), cross_config);
        const auto crossing = run(std::vector<TraceRecord>(128, load(0x8000)), cross_config);
        require(crossing.buffer_service_local_committed_epochs > 1 &&
                    crossing.buffer_service_local_broad_fallback_epochs == 0 &&
                    crossing.buffer_service_discarded_services == 0 &&
                    crossing.total_core().l1d.accesses +
                        crossing.total_sequencer().coalesced_requests == 128,
                "accepted cross-epoch services must conserve lookups and retain identities");
        for (unsigned i = 0; i < 8; ++i)
            require(sample(prefix, i).memory_events.at(0).response_cycle ==
                        sample(crossing, i).memory_events.at(0).response_cycle,
                    "later epochs changed an already committed service response");
        c.dram.scheduler = "frfcfs";
        const std::vector<TraceRecord> controller_reads{
            load(0x8000), load(0x10000), load(0x8040), load(0x8048),
            alu(), alu(), alu(), alu()};
        const auto controller = run(controller_reads, c);
        require(controller.dram_frfcfs_reordered_requests > 0 &&
                    controller.buffer_service_local_committed_epochs == 1 &&
                    controller.buffer_service_local_degraded_events > 0 &&
                    controller.buffer_service_local_broad_fallback_epochs == 0,
                "controller response changes must reclassify services before a mixed commit");
        c.dram.scheduler = "fcfs";
        c.ruby_buffer_service_max_passes = 1;
        const auto exhausted = run(dynamic, c, true);
        require(exhausted.buffer_service_local_committed_epochs == 0 &&
                    exhausted.buffer_service_local_broad_fallback_epochs == 1 &&
                    exhausted.total_core().l1d.accesses == dynamic.size() &&
                    exhausted.total_sequencer().coalesced_requests == 0,
                "an exhausted local retry must not retain speculative PMU suppression");
        c.ruby_buffer_service_max_passes = 3;
        c.fetch_supply_model = true;
        c.fetch_buffer_bytes = 64;
        c.l1i_enabled = true;
        c.fetch_supply_physical_request_ledger = true;
        c.fetch_supply_lower_hierarchy = true;
        c.instruction_address_mode = "modeled";
        c.instruction_physical_address_bits = 38;
        const auto special = run({load(0x8000), load(0x8008)}, c, true);
        require(special.total_core().instruction_fetch_lower_hierarchy_requests > 0 &&
                    special.buffer_service_local_committed_epochs > 0 &&
                    special.buffer_service_special_fallback_epochs == 0 &&
                    special.buffer_service_local_baseline_events > 0 &&
                    special.buffer_service_local_all_baseline_epochs == 1 &&
                    special.buffer_service_local_modeled_events == 0,
                "unsupported I-side must be counted as baseline events, not an epoch rejection");
        c.fetch_supply_model = c.l1i_enabled = c.fetch_supply_physical_request_ledger =
            c.fetch_supply_lower_hierarchy = false;
        c.ruby_buffer_service_max_work = 12;
        const auto unreserved = run(dynamic, c, true);
        require(unreserved.buffer_service_local_skipped_epochs == 1 &&
                    unreserved.buffer_service_passes == 0 &&
                    unreserved.buffer_service_work == 0,
                "local mode must reserve a downgrade replay before spending the first pass");
        c.ruby_buffer_service_max_work = 0;
        const auto skipped = run(dynamic, c, true);
        require(skipped.buffer_service_local_skipped_epochs > 0 &&
                    skipped.buffer_service_local_broad_fallback_epochs == 0 &&
                    skipped.buffer_service_passes == 0,
                "budget skip must not be reported as a trial rollback");
        c.ruby_buffer_service_no_fallback = true;
        bool invalid = false;
        try { c.validate(); } catch (const std::invalid_argument&) { invalid = true; }
        require(invalid, "local and forced no-fallback modes must be mutually exclusive");
    }
    {
        auto c = config();
        c.needs_tso = true;
        c.tso_store = TsoStoreMode::kBuffer;
        c.tso_store_buffer_size = 114;
        c.ruby_sequencer_max_outstanding = 16;
        c.ruby_buffer_service_repair = true;
        c.ruby_buffer_service_no_fallback = true;
        c.ruby_buffer_service_max_work = 0;
        c.ruby_buffer_service_work_percent = 0;
        const std::vector<TraceRecord> reads{load(0x8000), load(0x8008)};
        const auto forced = run(reads, c);
        require(forced.buffer_service_committed_epochs == forced.buffer_service_epochs &&
                    forced.buffer_service_passes == forced.buffer_service_epochs &&
                    forced.buffer_service_no_fallback_epochs == forced.buffer_service_epochs &&
                    forced.buffer_service_ignored_budget_epochs == forced.buffer_service_epochs &&
                    forced.buffer_service_budget_fallback_epochs == 0 &&
                    forced.total_sequencer().coalesced_requests == 1 &&
                    forced.total_core().l1d.accesses == 1,
                "no-fallback must commit a complete service pass even with zero budget");
        c.ruby_sequencer_max_outstanding = 1;
        const auto unsteady = run(reads, c);
        require(unsteady.buffer_service_uncertified_epochs > 0 &&
                    unsteady.buffer_service_ignored_unstable_epochs > 0 &&
                    unsteady.buffer_service_committed_epochs == unsteady.buffer_service_epochs,
                "forced capacity-delayed alias decisions must be labeled uncertified");
        c.ruby_sequencer_max_outstanding = 16;
        auto store = load(0x8000);
        store.flags = kRetires | kStore | kPhysicalAddress;
        store.op_class = 57;
        const auto mixed = run({load(0x8000), store, load(0x8008)}, c, true);
        require(mixed.buffer_service_committed_epochs == mixed.buffer_service_epochs &&
                    mixed.buffer_service_conflict_fallback_epochs == 0 &&
                    mixed.buffer_service_ignored_conflict_epochs > 0 &&
                    mixed.total_sequencer().requests == 3,
                "no-fallback must replay every event after a write conflict");
        c.fetch_supply_model = true;
        c.fetch_buffer_bytes = 64;
        c.l1i_enabled = true;
        c.fetch_supply_physical_request_ledger = true;
        c.fetch_supply_lower_hierarchy = true;
        c.instruction_address_mode = "modeled";
        c.instruction_physical_address_bits = 38;
        const auto special = run(reads, c, true);
        require(special.total_core().instruction_fetch_lower_hierarchy_requests > 0 &&
                    special.buffer_service_committed_epochs == special.buffer_service_epochs &&
                    special.buffer_service_ignored_special_epochs > 0 &&
                    special.buffer_service_special_fallback_epochs == 0,
                "no-fallback must retain I-side work without reverting the epoch");
        c.ruby_buffer_service_repair = false;
        bool invalid = false;
        try { c.validate(); } catch (const std::invalid_argument&) { invalid = true; }
        require(invalid, "no-fallback without service repair must reject its configuration");
    }
    {
        auto c = config();
        c.needs_tso = true;
        c.tso_store = TsoStoreMode::kBuffer;
        c.tso_store_buffer_size = 114;
        c.ruby_sequencer_max_outstanding = 16;
        const std::vector<TraceRecord> reads{
            load(0x8000), load(0x8008), load(0x8010), load(0x8018)};
        const auto baseline = run(reads, c);
        c.ruby_buffer_service_repair = true;
        c.ruby_buffer_service_work_percent = 1000;
        const auto repaired = run(reads, c);
        require(repaired.total_sequencer().coalesced_requests == 3 &&
                    repaired.total_core().l1d.accesses == 1 &&
                    repaired.total_sequencer().requests == 4,
                "service admission must jointly suppress follower PMU/MSHR work while retaining CPU slots");
        for (unsigned i = 1; i < 4; ++i)
            require(sample(repaired, i).memory_events.at(0).response_cycle ==
                        sample(repaired, 0).memory_events.at(0).response_cycle,
                    "followers must share a service response");
        compare(repaired, run(reads, c, true));
        c.ruby_buffer_service_max_passes = 8;
        for (unsigned capacity : {1u, 2u}) {
            c.ruby_sequencer_max_outstanding = capacity;
            const auto limited = run(reads, c);
            require(limited.total_sequencer().requests == 4 &&
                        limited.total_sequencer().coalesced_requests == 4 - 4 / capacity &&
                        limited.total_core().l1d.accesses == 4 / capacity &&
                        limited.total_sequencer().max_outstanding <= capacity,
                    "capacity-delayed arrivals must be reclassified after the callback");
            require(sample(limited, capacity).memory_events.at(0).response_cycle >
                        sample(limited, 0).memory_events.at(0).response_cycle,
                    "a post-callback arrival reused an expired response");
            compare(limited, run(reads, c, true));
        }
        c.ruby_sequencer_max_outstanding = 16;
        c.ruby_buffer_service_max_passes = 1;
        c.ruby_buffer_service_max_work = 1;
        const auto bounded = run(reads, c);
        require(bounded.buffer_service_work == 0 &&
                    bounded.buffer_service_budget_fallback_epochs > 0,
                "work budget must be checked before trial replay");
        c.ruby_buffer_service_max_work = 0;
        const auto fallback = run(reads, c);
        require(fallback.total_core().cycles == baseline.total_core().cycles &&
                    fallback.total_core().l1d.accesses == baseline.total_core().l1d.accesses &&
                    fallback.total_core().l2.misses == baseline.total_core().l2.misses &&
                    fallback.llc.misses == baseline.llc.misses &&
                    fallback.total_sequencer().coalesced_requests == 0,
                "zero repair budget must preserve baseline timing and PMU");
        auto store = load(0x8000);
        store.flags = kRetires | kStore | kPhysicalAddress;
        store.op_class = 57;
        c.ruby_buffer_service_max_work = 4096;
        c.ruby_buffer_service_max_passes = 8;
        for (const auto& mixed : {std::vector<TraceRecord>{load(0x8000), store, load(0x8008)},
                                  std::vector<TraceRecord>{store, load(0x8000), load(0x8008)}}) {
            c.ruby_buffer_service_repair = false;
            const auto original = run(mixed, c, true);
            c.ruby_buffer_service_repair = true;
            const auto rejected = run(mixed, c, true);
            require(rejected.buffer_service_conflict_fallback_epochs > 0 &&
                        rejected.total_core().cycles == original.total_core().cycles &&
                        rejected.total_core().l1d.accesses == original.total_core().l1d.accesses &&
                        rejected.total_core().l2.misses == original.total_core().l2.misses &&
                        rejected.llc.misses == original.llc.misses &&
                        rejected.cha.at(0).dram_reads == original.cha.at(0).dram_reads,
                    "write/read overlap trial must roll back cache PMU and controller state");
        }
        c.dram.scheduler = "frfcfs";
        c.ruby_buffer_service_repair = false;
        c.ruby_buffer_service_repair = true;
        const auto controller_fallback = run(reads, c);
        require(controller_fallback.buffer_service_committed_epochs > 0 &&
                    controller_fallback.total_sequencer().coalesced_requests == 3 &&
                    controller_fallback.total_core().l1d.accesses == 1,
                "FR-FCFS and read admission must commit one shared service and one PMU lookup");
        for (unsigned i = 1; i < 4; ++i)
            require(sample(controller_fallback, i).memory_events.at(0).response_cycle ==
                        sample(controller_fallback, 0).memory_events.at(0).response_cycle,
                    "FR-FCFS parent and follower responses diverged");
        // Row A, row B, row A: the controller must change a parent's
        // response, then the next transaction must rebuild its follower.
        const std::vector<TraceRecord> reordered_reads{
            load(0x8000), load(0x10000), load(0x8040), load(0x8048)};
        const auto reordered = run(reordered_reads, c);
        require(reordered.buffer_service_committed_epochs == 1 &&
                    reordered.buffer_service_passes >= 2 &&
                    reordered.dram_frfcfs_reordered_requests > 0 &&
                    reordered.total_sequencer().coalesced_requests == 1 &&
                    reordered.total_core().l1d.accesses == 3 &&
                    sample(reordered, 2).memory_events.at(0).response_cycle <
                        sample(reordered, 1).memory_events.at(0).response_cycle &&
                    sample(reordered, 3).memory_events.at(0).response_cycle ==
                        sample(reordered, 2).memory_events.at(0).response_cycle,
                "controller reorder must reconverge admission, follower response and PMU");
        compare(reordered, run(reordered_reads, c, true));
        c.ruby_buffer_service_repair = false;
        const auto reorder_base = run(reordered_reads, c);
        c.ruby_buffer_service_repair = true;
        c.ruby_buffer_service_max_work = 42;
        const auto rolled_back = run(reordered_reads, c);
        require(rolled_back.buffer_service_passes == 1 &&
                    rolled_back.buffer_service_committed_epochs == 0 &&
                    rolled_back.buffer_service_budget_fallback_epochs > 0 &&
                    rolled_back.total_sequencer().coalesced_requests == 0 &&
                    rolled_back.total_core().l1d.accesses == reorder_base.total_core().l1d.accesses &&
                    rolled_back.total_core().l2.misses == reorder_base.total_core().l2.misses &&
                    rolled_back.llc.misses == reorder_base.llc.misses &&
                    rolled_back.cha.at(0).dram_reads == reorder_base.cha.at(0).dram_reads,
                "budget exhaustion after controller repair must roll back resources and PMU");
        require(rolled_back.total_core().cycles == reorder_base.total_core().cycles,
                "controller rollback changed baseline cycles");
        for (unsigned i = 0; i < reordered_reads.size(); ++i)
            require(sample(rolled_back, i).memory_events.at(0).response_cycle ==
                        sample(reorder_base, i).memory_events.at(0).response_cycle,
                    "controller rollback retained a speculative callback");
        c.ruby_buffer_service_max_work = 4096;
        c.dram.scheduler = "fcfs";
        c.fetch_supply_model = true;
        c.fetch_buffer_bytes = 64;
        c.l1i_enabled = true;
        c.fetch_supply_physical_request_ledger = true;
        c.fetch_supply_lower_hierarchy = true;
        c.instruction_address_mode = "modeled";
        c.instruction_physical_address_bits = 38;
        c.ruby_buffer_service_repair = false;
        const auto instruction_base = run(reads, c, true);
        c.ruby_buffer_service_repair = true;
        const auto instruction_fallback = run(reads, c, true);
        require(instruction_fallback.total_core().instruction_fetch_lower_hierarchy_requests > 0 &&
                    instruction_fallback.buffer_service_committed_epochs == 0 &&
                    instruction_fallback.buffer_service_special_fallback_epochs > 0 &&
                    instruction_fallback.buffer_service_work > 0 &&
                    instruction_fallback.total_core().cycles == instruction_base.total_core().cycles &&
                    instruction_fallback.total_core().instruction_l2.accesses == instruction_base.total_core().instruction_l2.accesses,
                "I-side events without an admission ledger must use baseline, never time zero");
        c.fetch_supply_lower_hierarchy = false;
        c.fetch_supply_physical_request_ledger = false;
        c.fetch_supply_model = false;
        c.l1i_enabled = false;
        c.ruby_buffer_service_work_percent = 10;
        std::vector<TraceRecord> long_reads(128, load(0x8000));
        c.chunk_instructions = c.interval_target_uops = 8;
        c.interval_max_cycles = 8;
        const auto budgeted = run(long_reads, c, true);
        require(budgeted.buffer_service_work <= 25 &&
                    budgeted.buffer_service_budget_fallback_epochs > 0,
                "failed attempts must also obey cumulative ten-percent UOP+event budget");
    }
    test_post_commit_service_certificate();
    test_load_ready_contract();
    test_private_read_service_boundaries();
    // One cold line followed by independent reads: tag allocation must not
    // make the data available while its unique fill is still outstanding.
    {
        const std::vector<TraceRecord> reads{
            load(0x8000), load(0x8008), load(0x8010), alu(1)};
        auto c = config();
        c.response_private_read_services = true;
        const auto s = run(reads, c);
        const auto response = sample(s, 0).memory_events.at(0).response_cycle;
        require(sample(s, 1).memory_events.at(0).response_cycle == response &&
                    sample(s, 2).memory_events.at(0).response_cycle == response,
                "same-line reads observed data before the unique fill response");
        verify(s, reads, c.writeback_width);
        compare(s, run(reads, c, true));
    }
    test_request_origins();
    // Reuse is tied to the fill generation and a half-open visibility
    // interval, including when the parent came from a preceding checkpoint.
    const SharedFill carried_fill{100, 7};
    require(validate_fill_service(FillServiceKind::kMerge, 7, 95, &carried_fill) ==
                FillServiceValidity::kValid, "same carried fill must remain reusable before completion");
    for (const auto arrival : {100u, 105u}) {
        require(validate_fill_service(FillServiceKind::kMerge, 7, arrival, &carried_fill) ==
                    FillServiceValidity::kVisibilityChanged,
                "merge must expire at completion even if request order is unchanged");
    }
    const SharedFill replacement{100, 8};
    require(validate_fill_service(FillServiceKind::kMerge, 7, 95, &replacement) ==
                FillServiceValidity::kReplacedParent,
            "same line and timestamp cannot identify a replacement transaction");
    require(validate_fill_service(FillServiceKind::kMerge, 7, 95, nullptr) ==
                FillServiceValidity::kMissingParent, "missing parent cannot reuse a saved timestamp");
    const SharedFill retimed{120, 7};
    require(validate_fill_service(FillServiceKind::kMerge, 7, 105, &retimed) ==
                FillServiceValidity::kValid, "retiming preserves the parent's identity");
    require(validate_fill_service(FillServiceKind::kHit, 0, 105, &retimed) ==
                FillServiceValidity::kVisibilityChanged,
            "a delayed fill must invalidate a previously resident hit");
    require(validate_fill_service(FillServiceKind::kHit, 0, 120, &retimed) ==
                FillServiceValidity::kValid, "completion precedes equal-time lookup");
    require(validate_fill_service(FillServiceKind::kMiss, 9, 105, &retimed) ==
                FillServiceValidity::kVisibilityChanged,
            "new miss cannot silently replace an outstanding generation");

    // The second core's dependent X request initially merges with core 0's
    // fill, then arrives after that fill once Y's response is propagated.
    // Even though request order is unchanged, timing-only reuse must fail.
    const auto canonical_boundary = run_fill_boundary(false);
    const auto rejected_boundary = run_fill_boundary(true);
    require(rejected_boundary.service_fill_visibility_changed > 0 &&
                rejected_boundary.response_retime_stable_epochs == 0 &&
                rejected_boundary.response_retime_fallback_epochs == 1,
            "fill-crossing retime must reject its stale merge descriptor");
    require(rejected_boundary.total_core().cycles == canonical_boundary.total_core().cycles &&
                rejected_boundary.total_core().memory_accesses == 3 &&
                rejected_boundary.total_core().l1d.accesses == canonical_boundary.total_core().l1d.accesses &&
                rejected_boundary.llc.misses == canonical_boundary.llc.misses &&
                rejected_boundary.llc.hits == canonical_boundary.llc.hits &&
                rejected_boundary.cha.at(0).queue_cycles == canonical_boundary.cha.at(0).queue_cycles &&
                rejected_boundary.cha.at(0).dram_reads == canonical_boundary.cha.at(0).dram_reads &&
                rejected_boundary.cha.at(0).llc_merged_misses == canonical_boundary.cha.at(0).llc_merged_misses,
            "rejected retime leaked candidate feedback, queues, DRAM requests or cache PMU");

    // Program-order request clamping delays the independent load's request
    // while leaving its original UOP issue early. The RAW edge must use WB
    // after the response, not base completion plus a mismatched latency delta.
    const std::vector<TraceRecord> clamped{
        alu(0, 3), alu(1), load(0x8000, 1), load(0x10000), alu(1)};
    auto c = config();
    auto s = run(clamped, c);
    verify(s, clamped, c.writeback_width);
    require(s.total_core().memory_order_clamp_events > 0 &&
                s.response_load_data_ready_repairs > 0 &&
                sample(s, 3).selected_memory_corrected_issue_cycle > sample(s, 3).base_issue_cycle,
            "directed case must exercise the two request origins");
    compare(s, run(clamped, c, true));

    // Three line fragments of one load consume one WB, at the latest return.
    const std::vector<TraceRecord> split{load(0x8038, 0, 80), alu(1)};
    s = run(split, c);
    verify(s, split, c.writeback_width);
    require(sample(s, 0).memory_events.size() == 3, "split load lost a fragment");

    // Warm functional tags make the later loads local hits. Their clamped
    // request timestamps coincide, so data readiness now exposes WB contention.
    std::vector<TraceRecord> collision{load(0x8000), alu(0, 3), load(0x8000, 1)};
    for (unsigned i = 0; i < 12; ++i) collision.push_back(load(0x8000));
    collision.push_back(alu(1));
    c = config(1);
    s = run(collision, c);
    verify(s, collision, 1);
    require(s.sparse_resource_writeback_collision_cycles > 0,
            "simultaneous responses must exercise WB arbitration");
    compare(s, run(collision, c, true));

    // An older future WB must leave earlier slots available to independent work.
    std::vector<TraceRecord> gaps{alu(0, 3), alu()};
    c.integer_divide_latency = 200;
    s = run(gaps, c);
    verify(s, gaps, 1);
    require(sample(s, 1).actual_completion_cycle < sample(s, 0).actual_completion_cycle,
            "future writeback reservation blocked a legal earlier slot");

    // Cross-epoch import uses the existing bounded ROB history. This also
    // exercises chunk retirement and fast-kernel certificate fallback.
    c = config(1);
    c.chunk_instructions = c.interval_target_uops = 7;
    c.interval_max_cycles = 8;
    std::vector<TraceRecord> across = clamped;
    across.insert(across.end(), 90, alu());
    across.push_back(load(0x18000));
    across.push_back(alu(1));
    s = run(across, c);
    verify(s, across, 1);
    compare(s, run(across, c, true));
}
