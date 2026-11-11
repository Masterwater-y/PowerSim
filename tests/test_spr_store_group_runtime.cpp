// Real Simulator acceptance tests for the bounded same-line store pair.
// FST carries retired operations/addresses, not store data values: tests can
// check model service and retirement order, not byte-value correctness.
#include "fastsim/simulator.hpp"

#include <algorithm>
#include <cstdint>
#include <memory>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {
using namespace fastsim;

void check(bool ok, const std::string& why) {
    if (!ok) throw std::runtime_error("store group runtime: " + why);
}

enum class Kind { kStore, kUnknownStore, kSecureStore, kOtherContext, kFence, kAlu, kLoad, kAtomic };
struct Step { Kind kind; std::uint64_t address; bool depends_on_prior = false; };

class FunctionalStoreSource final : public TraceSource {
 public:
    explicit FunctionalStoreSource(std::vector<Step> steps) : steps_(std::move(steps)) {}
    bool next(TraceRecord& record) override {
        if (cursor_ == steps_.size()) return false;
        const auto& step = steps_[cursor_++];
        record = {};
        record.pc = 0x4000 + 4 * (cursor_ - 1);
        record.flags = kRetires;
        facts_available_ = false;
        if (step.kind == Kind::kFence) {
            record.flags |= kSerialize;
            record.op_class = 1;
            return true;
        }
        if (step.kind == Kind::kAlu) {
            record.op_class = 1;
            record.n_dst = 1;
            if (step.depends_on_prior) {
                record.n_src = 1;
                record.producer_dists[0] = 1;
            }
            return true;
        }
        if (step.kind == Kind::kAtomic) {
            record.flags |= kStore | kLoad | kAtomic | kPhysicalAddress;
            record.op_class = 57;
            record.address = step.address;
            record.size = 8;
            return true;
        }
        if (step.kind == Kind::kLoad) {
            record.flags |= kLoad | kPhysicalAddress;
            record.op_class = 56;
            record.address = step.address;
            record.size = 8;
            if (step.depends_on_prior) {
                record.n_src = 1;
                record.producer_dists[0] = 1;
            }
            return true;
        }
        record.flags |= kStore | kPhysicalAddress;
        record.op_class = 57;
        record.address = step.address;
        record.size = 8;
        if (step.depends_on_prior) {
            record.n_src = 1;
            record.producer_dists[0] = 1;
        }
        facts_ = {};
        facts_.address = record.address;
        facts_.size = record.size;
        facts_.byte_enable = 0xff;
        facts_.attributes = fst::kStoreFactsComplete | fst::kStoreOrdinary;
        if (step.kind == Kind::kSecureStore) facts_.attributes |= fst::kStoreSecure;
        if (step.kind == Kind::kOtherContext) facts_.context = 7;
        facts_available_ = step.kind != Kind::kUnknownStore;
        return true;
    }
    std::optional<std::uint64_t> current_source_record_ordinal() const override {
        return cursor_ ? std::optional<std::uint64_t>(cursor_ - 1) : std::nullopt;
    }
    const fst::StoreRequestFacts* current_store_facts() const override {
        return facts_available_ ? &facts_ : nullptr;
    }
    const fst::StoreRequestFacts* current_memory_facts() const override {
        return current_store_facts();
    }
    std::string description() const override { return "functional store group fixture"; }
 private:
    std::vector<Step> steps_;
    std::size_t cursor_ = 0;
    bool facts_available_ = false;
    fst::StoreRequestFacts facts_{};
};

SimulatorConfig group_config(bool enabled, std::uint32_t chunk, std::uint32_t budget) {
    SimulatorConfig c;
    c.cores = 1;
    c.cha_count = c.dram.channels = c.dram.banks_per_channel = 1;
    c.core_model = "interval_weave";
    c.interval_scheduler = "time_epoch";
    c.response_queue_feedback = true;
    c.response_sparse_scoreboard = true;
    c.cpi_attribution = true; // exposes committed dispatch/retire/send witnesses
    c.response_frontier_audit_stride_uops = 1;
    c.interval_private_preview = false;
    c.interval_reweave_passes = 1;
    c.interval_max_cycles = 4096;
    c.interval_target_uops = chunk;
    c.chunk_instructions = chunk;
    c.lookahead_chunks = 2;
    c.needs_tso = true;
    c.tso_store = TsoStoreMode::kBuffer;
    c.tso_store_buffer_size = budget;
    c.sq_entries = budget;
    c.store_commit_to_iew_delay = 1;
    c.store_wait_relief = false;
    c.cache_store_ports = 2;
    c.ruby_sequencer_max_outstanding = 16;
    c.l1d.size_bytes = 4096;
    c.l2.size_bytes = 16384;
    c.llc.size_bytes = 65536;
    c.llc_fill_response_latency = 100;
    c.dram.scheduler = "fcfs";
    c.spr_store_group_merge = enabled; // proposed default-off option
    return c;
}

SimulationStats run(const std::vector<Step>& steps, bool enabled,
                    std::uint32_t chunk = 16, std::uint32_t budget = 16,
                    std::uint32_t quantum = 4096) {
    auto config = group_config(enabled, chunk, budget);
    config.interval_max_cycles = quantum;
    config.validate();
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::make_unique<FunctionalStoreSource>(steps));
    Simulator simulator(config, std::move(traces));
    return simulator.run();
}

std::vector<Step> same_line(unsigned count) {
    std::vector<Step> result;
    for (unsigned i = 0; i < count; ++i)
        // Eight-byte writes overlap; mask union must not alter retirement order.
        result.push_back({Kind::kStore, 0x40000 + 4 * i});
    return result;
}

const ResponseFrontierAuditSample& frame(const SimulationStats& result, std::uint64_t seq,
                                         std::size_t core = 0) {
    for (const auto& sample : result.response_frontier_audit.at(core))
        if (sample.sequence == seq) return sample;
    throw std::runtime_error("store group runtime: missing committed feedback witness");
}

void population(const SimulationStats& off, const SimulationStats& on, unsigned stores) {
    check(off.total_core().retired_instructions == stores &&
          on.total_core().retired_instructions == stores &&
          off.total_core().retired_uops == on.total_core().retired_uops,
          "all source instructions must retire on both paths");
    check(off.total_core().memory_accesses == stores &&
          on.total_core().memory_accesses == stores,
          "merging must not erase source memory UOP population");
    check(off.total_core().perf_retired_stores == stores &&
          on.total_core().perf_retired_stores == stores &&
          off.total_core().perf_retirement.physical.perf_retired_stores ==
              on.total_core().perf_retirement.physical.perf_retired_stores,
          "store retirement populations must not be reduced with hierarchy traffic");
}

void same_window(unsigned n) {
    const auto steps = same_line(n);
    const auto off = run(steps, false);
    const auto on = run(steps, true);
    population(off, on, n);
    check(off.total_sequencer().requests == n && on.total_sequencer().requests == 1,
          "one admitted same-line group must replace n hierarchy store requests by one");
    check(off.total_core().l1d.accesses == n && on.total_core().l1d.accesses == 1,
          "one group must cause one hierarchy tag lookup, not n synthetic lookups");
    const auto& owner = frame(on, 0);
    std::uint64_t last_retire = 0;
    for (unsigned i = 0; i < n; ++i) {
        const auto& member = frame(on, i);
        check(member.has_store, "all group members remain stores in feedback");
        check(member.actual_retire_cycle >= last_retire,
              "group members must retire in source order");
        // Proposed audit-only fields, carried by the existing committed
        // frontier audit rather than adding per-UOP hot-path state.
        check(member.store_group_id == owner.store_group_id && owner.store_group_id != 0,
              "every suppressed follower must refer to its committed group");
        check(member.store_group_window_id == owner.store_group_window_id,
              "all grouped members must be accepted in the same model window");
        check(member.store_group_offload_cycle == owner.store_group_offload_cycle &&
              member.store_group_callback_cycle == owner.store_group_callback_cycle,
              "group members must share one offload and callback witness");
        last_retire = member.actual_retire_cycle;
    }
    check(owner.store_group_offload_cycle >= last_retire &&
          owner.store_send_cycle >= owner.store_group_offload_cycle &&
          owner.store_group_callback_cycle >= owner.store_send_cycle,
          "group offload/send/callback must follow all members' model retirements");
    for (unsigned i = 0; i < n; ++i)
        check(owner.store_group_offload_cycle >= frame(on,i).actual_completion_cycle,
              "group offload must not precede any member's model completion");
    check(owner.store_group_credit_release_cycle >= owner.store_group_callback_cycle,
          "owner shared credit must remain occupied through the group callback");
    for (unsigned i = 1; i < n; ++i)
        check(frame(on,i).store_group_credit_release_cycle >= owner.store_group_offload_cycle,
              "follower shared credit cannot be released before the group offloads");
}

void no_merge_through(Kind boundary) {
    const std::vector<Step> steps{{Kind::kStore,0x40000},
                                  {boundary,0x40004},
                                  {Kind::kStore,0x40008}};
    const auto off=run(steps,false), on=run(steps,true);
    check(on.total_sequencer().requests == off.total_sequencer().requests,
          "unknown, special or fence input must prevent hierarchy suppression");
    check(on.total_core().retired_uops == off.total_core().retired_uops,
          "fallback must retain all retired UOPs");
    check(on.total_core().memory_accesses == off.total_core().memory_accesses,
          "fallback must retain all source memory operations");
}
} // namespace

void test_spr_store_group_runtime() {
    same_window(2);
    no_merge_through(Kind::kUnknownStore);
    no_merge_through(Kind::kSecureStore);
    no_merge_through(Kind::kOtherContext);
    no_merge_through(Kind::kFence);

    auto multicore=group_config(true,16,16);
    multicore.cores=5;
    bool rejected=false;
    try { multicore.validate(); } catch (const std::invalid_argument&) { rejected=true; }
    check(rejected, "this slice must reject more than four cores");

    const auto four = same_line(4);
    const auto off_one = run(four, false, 1);
    const auto on_one = run(four, true, 1);
    population(off_one,on_one,4);
    check(on_one.total_sequencer().requests == off_one.total_sequencer().requests,
          "group members may not be retroactively merged across producer chunks");

    same_window(3);
    same_window(4);
    const auto five = same_line(5);
    const auto five_off=run(five,false), five_on=run(five,true);
    population(five_off,five_on,5);
    check(five_on.total_sequencer().requests == five_off.total_sequencer().requests,
          "a batch beyond the four-member bound must keep its request population");

    auto future = same_line(2);
    for (unsigned i=0;i<40;++i) future.push_back({Kind::kAlu,0,i != 0});
    future.push_back({Kind::kLoad,0x40000,true});
    const auto future_off=run(future,false,128,16,32);
    const auto future_on=run(future,true,128,16,32);
    check(future_on.spr_store_group_future_fallbacks > 0,
          "fixture must actually exercise the cross-Q future-callback fallback: candidates=" +
          std::to_string(future_on.spr_store_group_candidates) + " committed=" +
          std::to_string(future_on.spr_store_group_committed) + " permission=" +
          std::to_string(future_on.spr_store_group_permission_fallbacks) + " callback=" +
          std::to_string(frame(future_on,0).store_group_callback_cycle) + " next_issue=" +
          std::to_string(frame(future_on,42).actual_issue_cycle));
    check(future_on.total_sequencer().requests == future_off.total_sequencer().requests &&
          future_on.total_core().l1d.accesses == future_off.total_core().l1d.accesses &&
          future_on.total_core().retired_uops == future_off.total_core().retired_uops &&
          future_on.elapsed_reference_q16 == future_off.elapsed_reference_q16 &&
          future_on.total_core().perf_l1d_load_fb_hits == future_off.total_core().perf_l1d_load_fb_hits,
          "future fallback must restore hierarchy, retirement and FB-hit observer population");

    // Distinct producer chunks prevent grouping. Plenty of SQ/budget credits
    // keep this a low-pressure OFF/ON conservation check across windows.
    const std::vector<Step> mixed{
        {Kind::kUnknownStore,0x40000}, {Kind::kFence,0},
        {Kind::kSecureStore,0x40100}, {Kind::kAlu,0,true},
        {Kind::kStore,0x40200}, {Kind::kOtherContext,0x40300},
        {Kind::kLoad,0x40000,true}};
    const auto mixed_off=run(mixed,false,1,16,16);
    const auto mixed_on=run(mixed,true,1,16,16);
    check(mixed_on.spr_store_group_committed == 0 &&
          mixed_on.total_sequencer().requests == mixed_off.total_sequencer().requests &&
          mixed_on.total_core().retired_uops == mixed_off.total_core().retired_uops &&
          mixed_on.total_core().perf_retired_stores == mixed_off.total_core().perf_retired_stores &&
          mixed_on.total_core().perf_l1d_load_fb_hits == mixed_off.total_core().perf_l1d_load_fb_hits &&
          mixed_on.total_core().l1d.accesses == mixed_off.total_core().l1d.accesses &&
          mixed_on.total_core().l2.accesses == mixed_off.total_core().l2.accesses &&
          mixed_on.elapsed_reference_q16 == mixed_off.elapsed_reference_q16,
          "ungrouped mixed special/unknown stores must preserve low-pressure timing and PMU populations");

    // Two members cannot coexist in a single-entry shared budget. Whether
    // rejected before replay or rolled back, neither request may be erased.
    const auto two = same_line(2);
    const auto full_off = run(two,false,16,1);
    const auto full_on = run(two,true,16,1);
    population(full_off,full_on,2);
    check(full_on.total_sequencer().requests == full_off.total_sequencer().requests,
          "insufficient SQ/shared budget must not suppress a follower request");
}

#ifdef FASTSIM_SPR_STORE_GROUP_STANDALONE
int main() {
    try { test_spr_store_group_runtime(); }
    catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
    std::cout << "SPR store group runtime tests passed\n";
    return 0;
}
#endif
