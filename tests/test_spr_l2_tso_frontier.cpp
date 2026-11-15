#include "fastsim/simulator.hpp"

#include <algorithm>
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
    explicit L2PrefetchTrace(unsigned gap, bool write_driver = false) {
        add_load(0x1000, 0x8000);
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
        add_load(0x1004, 0xa080);
        if (write_driver) records_.back().flags = kRetires | kStore | kPhysicalAddress;
        records_.back().n_src = 1;
        records_.back().producer_dists[0] = 1;
        if (write_driver) { add_load(0x1010,0xa0c0); records_.back().flags=kRetires|kStore|kPhysicalAddress; }
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
    }
    bool next(TraceRecord& out) override {
        if (index_ == records_.size()) return false;
        out = records_[index_++];
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
    std::optional<bool> current_ordinary_load_eligible() const override { return true; }
    const fst::StoreRequestFacts* current_memory_facts() const override { return &facts_; }
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

SimulationStats run(bool enabled, unsigned gap, bool dirty_victim = false, bool cross_core = false) {
    SimulatorConfig config;
    config.cores = cross_core ? 2 : 1;
    config.cha_count = 1;
    config.core_model = "interval_weave";
    config.interval_scheduler = "time_epoch";
    config.chunk_instructions = 2048;
    config.interval_target_uops = 2048;
    config.response_queue_feedback = true;
    config.cpi_attribution=true; config.response_frontier_audit_stride_uops=1;
    config.response_sparse_scoreboard = true;
    config.spr_l1_prefetch_l2_only = true;
    config.spr_l1_prefetch_suppress_issue = true;
    config.spr_l2_shared_prefetch = enabled;
    config.tso_store = TsoStoreMode::kBuffer;
    config.needs_tso = true;
    config.l1d.size_bytes = 4096;
    config.l1d.associativity = 1;
    config.l2.size_bytes = 4096;
    config.l2.associativity = 1;
    if (dirty_victim) {
        config.llc.size_bytes=64;
        config.llc.associativity=1;
        config.dram.separate_write_queue=true;
    }
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::make_unique<L2PrefetchTrace>(gap,dirty_victim));
    if (cross_core) traces.push_back(std::make_unique<CrossCoreWriteTrace>());
    return Simulator(config, std::move(traces)).run();
}
} // namespace

int main() {
    const auto result = run(true, 300, true, false);
    std::uint64_t latest_issue = 0, prior_release = 0;
    unsigned stores = 0;
    for (const auto& frame : result.response_frontier_audit.at(0)) {
        for (const auto& event : frame.memory_events)
            latest_issue = std::max(latest_issue, event.corrected_issue_cycle);
        if (frame.regular_store) {
            require(frame.store_sq_release_cycle >= prior_release, "FIFO store release moved backwards");
            prior_release = frame.store_sq_release_cycle;
            ++stores;
        }
    }
    require(stores == 2, "both stores must appear in the audit");
    for (const auto& frame : result.response_frontier_audit.at(0))
        if (frame.regular_store)
            require(frame.store_sq_release_cycle >= latest_issue, "SQ release precedes accepted batch frontier");
    require(result.spr_l2_shared_prefetch_issued > 0, "fixture must exercise prefetch");
    require(result.o3.at(0).spr_prefetch_store_frontier_clamps > 0 &&
            result.o3.at(0).spr_prefetch_store_frontier_cycles > 0,
            "conservative SQ delay must be observable");
}
