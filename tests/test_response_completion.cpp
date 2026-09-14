#include "fastsim/simulator.hpp"
#include "fastsim/shared_fill.hpp"
#include "fastsim/interval_core.hpp"

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
    for (const auto model : {"scalar", "causal_read"}) {
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
        const auto simulate_pair = [](bool shared_channel, bool parallel) {
            auto c = config();
            c.cores = 2;
            c.cha_count = 2;
            c.dram.channels = shared_channel ? 1 : 2;
            c.response_shared_service_constraints = true;
            c.interval_parallel_feedback = parallel;
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
        const auto serial = simulate_pair(false, false);
        const auto parallel = simulate_pair(false, true);
        require(parallel.shared_service_constraints.committed_requests == 4 &&
                    parallel.shared_service_constraints.committed_components == 2 &&
                    parallel.total_core().cycles == serial.total_core().cycles,
                "independent shared resource components lost parallel equivalence");
        const auto conflicting = simulate_pair(true, true);
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
    c.interval_parallel_feedback = false;
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
