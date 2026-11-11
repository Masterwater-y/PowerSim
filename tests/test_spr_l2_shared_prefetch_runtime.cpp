#include "fastsim/simulator.hpp"

#include <iostream>
#include <stdexcept>
#include <vector>

namespace {
using namespace fastsim;

void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

// A functional stream: the first L2 miss predicts the adjacent physical line;
// an intervening independent load drives its request, and a later demand tests
// whether the response was actually published before demand lookup.
class L2PrefetchTrace final : public TraceSource {
  public:
    explicit L2PrefetchTrace(unsigned gap, bool write_driver = false, bool repeat = false,
                             bool settle = false, bool seed_llc = false, bool no_driver = false,
                             bool conflict = false, bool first_store = false) {
        if (seed_llc) {
            add_load(0x0ffc,0x8040);
            add_load(0x0ff8,0xc000);
            for (unsigned i=0;i<1000;++i) {
                TraceRecord record; record.pc=0x8000+4*i;
                record.flags=kRetires; record.n_dst=1;
                if (i) {record.n_src=1;record.producer_dists[0]=1;}
                records_.push_back(record);
            }
        }
        add_load(0x1000, 0x8000);
        if (first_store) records_.back().flags=kRetires|kStore|kPhysicalAddress;
        // Candidate enqueue takes a modeled cycle. Ensure the independent
        // driver load is accepted after that edge, rather than the same
        // OOO issue cycle as the training miss.
        for (unsigned i = 0; i < 8; ++i) {
            TraceRecord record;
            record.pc = 0x1800 + 4 * i;
            record.flags = kRetires;
            record.n_dst = 1;
            if (i) { record.n_src = 1; record.producer_dists[0] = 1; }
            records_.push_back(record);
        }
        if (!no_driver) {
            add_load(0x1004, conflict ? 0xa000 : 0xa080);
            if (write_driver) records_.back().flags = kRetires | kStore | kPhysicalAddress;
            records_.back().n_src = 1;
            records_.back().producer_dists[0] = 1;
        }
        for (unsigned i = 0; i < gap; ++i) {
            TraceRecord record;
            record.pc = 0x2000 + 4 * i;
            record.flags = kRetires;
            record.n_dst = 1;
            if (i != 0) {
                record.n_src = 1;
                record.producer_dists[0] = 1;
            }
            records_.push_back(record);
        }
        add_load(0x1008, 0x8040);
        if (gap) {
            records_.back().n_src = 1;
            records_.back().producer_dists[0] = 1;
        }
        if (repeat) add_load(0x100c, 0x8040);
        if (settle) {
            for (unsigned i = 0; i < 1000; ++i) {
                TraceRecord record;
                record.pc=0x4000+4*i; record.flags=kRetires; record.n_dst=1;
                if (i) {record.n_src=1; record.producer_dists[0]=1;}
                records_.push_back(record);
            }
            add_load(0x5000,0x8040);
            records_.back().n_src=1; records_.back().producer_dists[0]=1;
        }
    }
    bool next(TraceRecord& out) override {
        if (index_ == records_.size()) return false;
        out = records_[index_++];
        current_pc_ = out.pc;
        is_store_ = has_flag(out.flags,kStore);
        if (is_store_) {
            store_facts_.address=out.address; store_facts_.size=out.size;
            store_facts_.byte_enable=0xff;
            store_facts_.attributes=fst::kStoreFactsComplete|fst::kStoreOrdinary;
        }
        fragments_.clear();
        if (has_flag(out.flags, kLoad)) {
            fst::TranslationFragment f;
            f.virtual_address = out.address - 0x7000;
            f.physical_address = out.address;
            f.size = out.size;
            f.mapping.kind = fst::TranslationKind::kPaged;
            f.mapping.flags = fst::kTranslationLeafKnown;
            f.mapping.page_bits = 12;
            fragments_.push_back(f);
        }
        return true;
    }
    std::string description() const override { return "l2-shared-prefetch-fixture"; }
    std::optional<bool> current_ordinary_load_eligible() const override {
        return current_pc_ != 0x0ffc && current_pc_ != 0x0ff8;
    }
    const fst::StoreRequestFacts* current_memory_facts() const override { return &facts_; }
    const fst::StoreRequestFacts* current_store_facts() const override {
        return is_store_ ? &store_facts_ : nullptr;
    }
    const std::vector<fst::TranslationFragment>& current_translation_fragments() const override {
        return fragments_;
    }
  private:
    void add_load(std::uint64_t pc, std::uint64_t address) {
        TraceRecord record;
        record.pc = pc;
        record.address = address;
        record.size = 8;
        record.flags = kRetires | kLoad | kPhysicalAddress;
        record.op_class = 56;
        records_.push_back(record);
    }
    std::vector<TraceRecord> records_;
    std::vector<fst::TranslationFragment> fragments_;
    std::size_t index_ = 0;
    std::uint64_t current_pc_ = 0;
    bool is_store_ = false;
    fst::StoreRequestFacts store_facts_{};
    fst::StoreRequestFacts facts_{0, 0, 0, 0, 0, 0, 0,
        fst::kStoreFactsComplete | fst::kMemoryRead | fst::kOrdinaryLoad, 0, 0};
};

class CrossCoreWriteTrace final : public TraceSource {
  public:
    CrossCoreWriteTrace() {
        for (unsigned i=0;i<30;++i) {
            TraceRecord record;
            record.pc=0x6000+4*i; record.flags=kRetires; record.n_dst=1;
            if (i) {record.n_src=1;record.producer_dists[0]=1;}
            records_.push_back(record);
        }
        TraceRecord store;
        store.pc=0x7000;store.address=0x8040;store.size=8;
        store.flags=kRetires|kStore|kPhysicalAddress;
        store.n_src=1;store.producer_dists[0]=1;
        records_.push_back(store);
    }
    bool next(TraceRecord& out) override {
        if(index_==records_.size())return false;
        out=records_[index_++];return true;
    }
    std::string description() const override {return "l2-prefetch-cross-core-write";}
  private:
    std::vector<TraceRecord> records_;
    std::size_t index_=0;
};

SimulationStats run(bool enabled, unsigned gap, bool dirty_victim = false, bool cross_core = false,
                    bool follow = false, bool repeat = false, bool settle = false,
                    bool seed_llc = false, bool no_driver = false, bool conflict = false,
                    bool first_store = false, bool frfcfs = false, bool buffered = false) {
    SimulatorConfig config;
    config.cores = cross_core ? 2 : 1;
    config.cha_count = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.chunk_instructions = 16;
    config.interval_target_uops = 16;
    if (settle || seed_llc) config.interval_max_cycles = 128;
    config.response_queue_feedback = true;
    config.response_sparse_scoreboard = true;
    config.spr_l1_prefetch_l2_only = true;
    config.spr_l1_prefetch_suppress_issue = true;
    config.spr_l2_shared_prefetch = enabled;
    config.spr_l2_prefetch_follow = follow;
    if (repeat && !conflict) config.spr_prefetch_host_inflight_per_core=1;
    if (frfcfs) config.dram.scheduler="frfcfs";
    if (buffered) { config.tso_store=TsoStoreMode::kBuffer; config.needs_tso=true; }
    config.l1d.size_bytes = 64;
    config.l1d.associativity = 1;
    config.l2.size_bytes = 4096;
    config.l2.associativity = 1;
    if (seed_llc) config.l2.size_bytes = 64;
    if (dirty_victim) {
        config.llc.size_bytes=64;
        config.llc.associativity=1;
        config.dram.separate_write_queue=true;
    }
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::make_unique<L2PrefetchTrace>(gap,dirty_victim,repeat,settle,seed_llc,no_driver,conflict,first_store));
    if (cross_core) traces.push_back(std::make_unique<CrossCoreWriteTrace>());
    return Simulator(config, std::move(traces)).run();
}
} // namespace

int main() {
    SimulatorConfig unsupported;
    unsupported.spr_l1_prefetch_l2_only = true;
    unsupported.spr_l1_prefetch_suppress_issue = true;
    unsupported.spr_l2_shared_prefetch = true;
    unsupported.core_model = "interval_weave";
    unsupported.interval_scheduler = "time_epoch";
    unsupported.response_queue_feedback = true;
    unsupported.tso_store = TsoStoreMode::kBuffer;
    bool rejected = false;
    try { unsupported.validate(); } catch (const std::invalid_argument&) { rejected = true; }
    require(rejected, "L2 prefetch must reject buffered TSO until its feedback is supported");
    const auto off = run(false, 1000);
    const auto on = run(true, 1000);
    std::cerr << "L2 fixture: issued=" << on.spr_l2_shared_prefetch_issued
              << " completed=" << on.spr_l2_shared_prefetch_completed
              << " cancelled_demand=" << on.spr_l2_shared_prefetch_cancelled_demand
              << " cancelled_state=" << on.spr_l2_shared_prefetch_cancelled_state
              << " pending=" << on.spr_l2_shared_prefetch_pending
              << " dropped=" << on.spr_l2_shared_prefetch_dropped
              << " queued=" << on.spr_l2_shared_prefetch_queued_pending
              << " L2 misses off/on=" << off.cores[0].l2.misses << '/' << on.cores[0].l2.misses
              << "\n";
    require(on.spr_l2_shared_prefetch_issued > 0 &&
            on.spr_l2_shared_prefetch_dram_reads > 0,
            "L2 candidate must issue a typed shared DRAM request");
    require(on.spr_l2_shared_prefetch_completed > 0,
            "L2 response must complete before a later demand");
    require(on.spr_l2_shared_prefetch_initial_pending + on.spr_l2_shared_prefetch_issued ==
            on.spr_l2_shared_prefetch_completed + on.spr_l2_shared_prefetch_cancelled_demand +
            on.spr_l2_shared_prefetch_cancelled_state + on.spr_l2_shared_prefetch_pending,
            "prefetch issued population must be conserved");
    require(on.cores[0].l2.misses < off.cores[0].l2.misses,
            "completed prefetch must change the actual L2 demand lookup");
    require(on.cores[0].perf_retirement.physical.perf_retired_loads ==
            off.cores[0].perf_retirement.physical.perf_retired_loads,
            "prefetch must not create retired demand loads");
    const auto early = run(true, 0);
    require(early.spr_l2_shared_prefetch_cancelled_demand > 0,
            "demand before response must cancel future publication");
    require(early.spr_l2_shared_prefetch_dram_reads > 0,
            "cancelled publication must retain already issued DRAM traffic");
    const auto followed = run(true, 0, false, false, true);
    require(followed.spr_l2_prefetch_followed > 0,
            "same-line demand must follow an issued L2 prefetch");
    require(followed.spr_l2_shared_prefetch_cancelled_demand == 0,
            "a read follower must not cancel the prefetch");
    require(followed.cha.at(0).dram_reads < early.cha.at(0).dram_reads,
            "a follower must not issue a duplicate DRAM read");
    require(followed.cores[0].perf_retirement.physical.perf_retired_loads ==
            early.cores[0].perf_retirement.physical.perf_retired_loads,
            "following must preserve retired demand population");
    const auto repeated = run(true, 0, false, false, true, true);
    require(repeated.spr_l2_prefetch_followed >= 2,
            "multiple same-line demands must retain the shared prefetch service");
    require(repeated.spr_l2_prefetch_demand_fill_dropped == 0,
            "same-line followers must share one bounded demand fill");
    require(repeated.cores[0].l1d.misses == 4,
            "an in-flight follower must not expose an L1 line before completion");
    const auto settled = run(true, 0, false, false, true, true, true);
    std::cerr << "settled l1 misses=" << settled.cores[0].l1d.misses
              << " followed=" << settled.spr_l2_prefetch_followed
              << " completed=" << settled.spr_l2_shared_prefetch_completed
              << " fill_dropped=" << settled.spr_l2_prefetch_demand_fill_dropped << "\n";
    require(settled.cores[0].l1d.misses == 4,
            "the demand fill must be visible after the accepted callback");
    const auto llc = run(true,1000,false,false,true,false,false,true);
    std::cerr << "llc issued=" << llc.spr_l2_prefetch_llc_issued
              << " total=" << llc.spr_l2_shared_prefetch_issued
              << " trained=" << llc.spr_l2_shared_prefetch_trained
              << " dropped=" << llc.spr_l2_shared_prefetch_dropped << "\n";
    require(llc.spr_l2_prefetch_llc_issued > 0,
            "an L2 prefetch must use an available LLC line");
    const auto no_driver = run(true,1000,false,false,true,false,false,false,true);
    require(no_driver.spr_l2_shared_prefetch_completed > 0 &&
            no_driver.cores[0].l2.misses == 1,
            "queue readiness must launch the prefetch without another demand event");
    const auto hit_training = run(true,1000,false,false,true);
    require(hit_training.spr_l2_shared_prefetch_trained >= 3,
            "an eligible demand hitting a prefetched L2 line must train the predictor");
    const auto store_training=run(true,1000,false,false,true,false,false,false,true,false,true);
    require(store_training.spr_l2_shared_prefetch_completed > 0,
            "an eligible L2 GETX miss must train the predictor");
    const auto fr = run(true,0,false,false,true,false,false,false,false,false,false,true);
    require(fr.spr_l2_prefetch_followed > 0 && fr.spr_l2_shared_prefetch_dram_reads > 0,
            "FR-FCFS configuration must retain prefetch service and follower");
    require(fr.spr_prefetch_dram_repair_skipped_epochs > 0,
            "demand-only FR repair must not erase prefetch controller reservations");
    const auto buffer=run(true,1000,true,false,true,false,false,false,false,false,false,true,true);
    require(buffer.spr_l2_shared_prefetch_issued > 0 &&
            buffer.o3.at(0).spr_prefetch_store_frontier_clamps == 0 &&
            buffer.spr_l2_shared_prefetch_clamped_events == 0,
            "a buffered store must not advance the global prefetch frontier");
    const auto conflict = run(true,0,false,false,true,true,true,false,false,true);
    require(conflict.spr_l2_prefetch_demand_fill_dropped > 0 &&
            conflict.cores[0].l1d.misses == 5,
            "an L2 eviction must revoke the old demand fill");
    const auto cross_off = run(false,1000,false,true);
    const auto cross = run(true,1000,false,true);
    require(cross.spr_l2_shared_prefetch_cancelled_cross_core > 0 &&
            cross.spr_l2_shared_prefetch_cancelled_write > 0,
            "cross-core write must cancel the same-line prefetch");
    require(cross.spr_l2_shared_prefetch_dram_reads > 0,
            "cross-core cancellation must retain issued DRAM traffic");
    require(cross.cores[0].l2.misses == cross_off.cores[0].l2.misses &&
            cross.cores[0].perf_retirement.physical.perf_retired_loads ==
            cross_off.cores[0].perf_retirement.physical.perf_retired_loads,
            "cancelled cross-core fill must not create an L2 hit or retired load");
    const auto followed_write = run(true,0,false,true,true,true,true);
    require(followed_write.spr_l2_shared_prefetch_cancelled_write > 0 &&
            followed_write.spr_l2_prefetch_demand_fill_dropped > 0,
            "a same-line cross-core write must revoke an outstanding demand fill");
    const auto dirty = run(true,1000,true);
    require(dirty.spr_l2_shared_prefetch_dirty_writebacks > 0 &&
            dirty.dram_write_queue_enqueues > 0,
            "prefetch dirty victim must enter the real DRAM write queue");
}
