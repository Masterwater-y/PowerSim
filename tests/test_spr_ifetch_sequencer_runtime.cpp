// Real Simulator fixture for opt-in I-fetch/data Sequencer sharing.
// Uses only retired functional instructions and modeled I-fetch addresses.
#include "fastsim/simulator.hpp"

#include <cstdint>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {
using namespace fastsim;
void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error("I-fetch Sequencer: " + message);
}

// Before the implementation lands these adapters leave the new switch off
// and expose zero witnesses, so the RED run fails on the intended behavior.
template <typename T>
auto enable_ifetch(T& config, int) -> decltype(config.ruby_sequencer_ifetch_admission = true, void()) {
    config.ruby_sequencer_ifetch_admission = true;
}
template <typename T> void enable_ifetch(T&, long) {}
template <typename T>
auto ifetch_requests(const T& counters, int) -> decltype(counters.ifetch_requests) {
    return counters.ifetch_requests;
}
template <typename T> std::uint64_t ifetch_requests(const T&, long) { return 0; }
template <typename T>
auto ifetch_stalls(const T& counters, int) -> decltype(counters.ifetch_buffer_full_stalls) {
    return counters.ifetch_buffer_full_stalls;
}
template <typename T> std::uint64_t ifetch_stalls(const T&, long) { return 0; }

class MixedSource final : public TraceSource {
 public:
    bool next(TraceRecord& record) override {
        if (index_ == 17) return false;
        record = {};
        record.flags = kRetires;
        // Sixteen distinct independent data loads from one I-cache block,
        // then a cold second instruction block while loads can remain live.
        record.pc = index_ < 16 ? 0x1000 : 0x2000;
        record.op_class = index_ < 16 ? 56 : 1;
        if (index_ < 16) {
            record.flags |= kLoad | kPhysicalAddress;
            record.address = 0x40000 + 64 * index_;
            record.size = 8;
        }
        ++index_;
        return true;
    }
    std::string description() const override { return "retired I-fetch/data credit fixture"; }
 private:
    unsigned index_ = 0;
};

SimulationStats run(bool enabled, std::uint32_t chunk, bool fast, bool preview,
                    std::uint32_t capacity = 16) {
    SimulatorConfig c;
    c.cores = 1;
    c.cha_count = c.dram.channels = c.dram.banks_per_channel = 1;
    c.core_model = "interval_weave";
    c.interval_scheduler = "time_epoch";
    c.response_queue_feedback = c.response_sparse_scoreboard = true;
    c.cpi_attribution = !fast;
    c.response_frontier_audit_stride_uops = 1;
    c.interval_private_preview = preview;
    c.response_materialized_uop_fast_kernel = fast;
    c.response_interval_tail_audit = fast;
    c.response_block_summary = fast;
    c.response_memory_descriptor = fast;
    c.response_monotone_iq_calendar = fast;
    c.interval_reweave_passes = 1;
    c.interval_target_uops = c.chunk_instructions = chunk;
    c.interval_max_cycles = 64;
    c.lookahead_chunks = 2;
    c.fetch_buffer_bytes = 64;
    c.fetch_supply_model = c.l1i_enabled = true;
    c.fetch_supply_physical_request_ledger = c.fetch_supply_lower_hierarchy = true;
    c.instruction_address_mode = "modeled";
    c.instruction_physical_address_bits = 38;
    c.l1i.size_bytes = 64;
    c.l1i.associativity = 1;
    c.l1i_miss_penalty = 0;
    c.l1d.size_bytes = 32768;
    c.l2.size_bytes = 262144;
    c.llc.size_bytes = 1048576;
    c.llc_fill_response_latency = 1000;
    c.ruby_sequencer_max_outstanding = capacity;
    c.needs_tso = true;
    c.tso_store = TsoStoreMode::kBuffer;
    if (enabled) enable_ifetch(c, 0);
    c.validate();
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::make_unique<MixedSource>());
    Simulator simulator(c, std::move(traces));
    return simulator.run();
}

void nonbinding_capacity_keeps_population() {
    const auto off = run(false, 128, false, false, 64);
    const auto on = run(true, 128, false, false, 64);
    const auto os = off.total_sequencer(), ns = on.total_sequencer();
    require(ifetch_requests(ns, 0) == on.total_core().instruction_fetch_lower_hierarchy_requests &&
            ns.requests == os.requests + ifetch_requests(ns, 0),
            "nonbinding capacity must still account for every I-fetch once");
    require(ifetch_stalls(ns, 0) == 0 && ns.buffer_full_stalls == 0,
            "a genuinely nonbinding credit pool must not create buffer-full stalls");
    require(on.total_core().retired_uops == off.total_core().retired_uops &&
            on.total_core().l1d.accesses == off.total_core().l1d.accesses &&
            on.total_core().instruction_l2.accesses == off.total_core().instruction_l2.accesses,
            "nonbinding credit must preserve retirement and real hierarchy population");
}

SequencerCounters shared_credit_and_feedback(std::uint32_t chunk, bool fast, bool preview) {
    const auto off = run(false, chunk, fast, preview);
    const auto on = run(true, chunk, fast, preview);
    const auto off_seq = off.total_sequencer(), on_seq = on.total_sequencer();
    const auto instruction_requests = on.total_core().instruction_fetch_lower_hierarchy_requests;
    require(instruction_requests >= 2, "fixture must issue both cold instruction blocks");
    require(off_seq.max_outstanding == 16,
            "fixture must actually saturate all sixteen existing demand slots; peak=" +
                std::to_string(off_seq.max_outstanding));
    require(ifetch_requests(on_seq, 0) == instruction_requests &&
            on_seq.requests == off_seq.requests + instruction_requests,
            "every real I-fetch must join the existing demand credit population once");
    require(ifetch_stalls(on_seq, 0) > 0,
            "fixture must show an I-fetch delayed by the shared data-occupied credit pool");
    require(on.total_core().retired_uops == off.total_core().retired_uops &&
            on.total_core().retired_instructions == off.total_core().retired_instructions &&
            on.total_core().l1d.accesses == off.total_core().l1d.accesses &&
            on.total_core().instruction_l2.accesses == off.total_core().instruction_l2.accesses,
            "the credit gate must not change retirement or issue new hierarchy requests");
    const auto find_final_fetch = [](const SimulationStats& stats) {
        for (const auto& sample : stats.response_frontier_audit.at(0))
            if (sample.sequence == 16) return sample.actual_fetch_cycle;
        throw std::runtime_error("missing final fetch feedback witness");
    };
    require(find_final_fetch(on) > find_final_fetch(off),
            "the shared-credit wait must reach the final UOP fetch edge");
    return on_seq;
}
}

int main() {
    try {
        for (const std::uint32_t chunk : {8u, 128u}) {
            const auto reference = shared_credit_and_feedback(chunk, false, false);
            for (const auto mode : {std::pair<bool,bool>{false,true}, {true,false}, {true,true}}) {
                const auto other = shared_credit_and_feedback(chunk, mode.first, mode.second);
                require(ifetch_requests(other, 0) == ifetch_requests(reference, 0) &&
                        ifetch_stalls(other, 0) == ifetch_stalls(reference, 0) &&
                        other.requests == reference.requests,
                        "fast-kernel and private-preview combinations must preserve credit populations");
            }
        }
        nonbinding_capacity_keeps_population();
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n'; return 1;
    }
    std::cout << "SPR I-fetch Sequencer runtime tests passed\n";
}
