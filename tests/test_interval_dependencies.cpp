#include "fastsim/interval_core.hpp"
#include "fastsim/spr_fill_calendar.hpp"
#include "fastsim/spr_store_window.hpp"
#include "fastsim/spr_port_calendar.hpp"
#include "fastsim/simulator.hpp"

#include <algorithm>
#include <filesystem>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
#include <unistd.h>

namespace {
using namespace fastsim;
void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error("interval dependencies: " + message);
}

TraceRecord alu() {
    TraceRecord record;
    record.pc = 0x1000;
    record.op_class = 1;
    record.flags = kRetires;
    return record;
}

std::filesystem::path write_trace(unsigned fanin, bool memory) {
    const auto directory = std::filesystem::path(FASTSIM_PROJECT_ROOT) /
        "tmp/fastsim-tests" / std::to_string(::getpid()) / "interval-dependencies";
    std::filesystem::create_directories(directory);
    const auto path = directory / (std::to_string(fanin) + (memory ? "-memory.fst" : "-fu.fst"));
    BinaryTraceWriter writer(path.string(), 0);
    writer.enable_complete_dependencies();
    // Repeated rows in different producer chunks exercise prefix retirement,
    // resident origin changes, and reuse of the cold extension buffers.
    for (unsigned block = 0; block < (memory ? 4u : 1u); ++block) {
        for (unsigned index = 0; index < (memory ? 128u : fanin + 2); ++index) {
            auto record = alu();
            record.pc += 4 * (128 * block + index);
            std::vector<std::uint32_t> extensions;
            if (index == 0) {
                if (memory) {
                    record.op_class = 56;
                    record.flags |= kLoad | kPhysicalAddress;
                    record.address = 0x8000 + 4096 * block;
                    record.size = 8;
                } else {
                    record.op_class = 3; // Slow divide is the oldest RAW producer.
                }
            } else if (index == fanin) {
                record.n_src = static_cast<std::uint8_t>(fanin);
                record.producer_dists = {1, 2, 3, 4};
                for (unsigned distance = 5; distance <= fanin; ++distance)
                    extensions.push_back(distance);
            } else if (index == fanin + 1) {
                // Operand count is not distinct producer count. Complete RAW
                // metadata must suppress the legacy static-map supplement.
                record.n_src = 7;
                record.producer_dists[0] = 1;
            }
            writer.append(record, nullptr, extensions);
        }
    }
    writer.close();
    return path;
}

std::filesystem::path write_spr_port_gap_trace() {
    const auto dir = std::filesystem::path(FASTSIM_PROJECT_ROOT) /
        "tmp/fastsim-tests" / std::to_string(::getpid()) / "spr-port-gap";
    std::filesystem::create_directories(dir);
    const auto path = dir / "gap.fst";
    BinaryTraceWriter writer(path.string(), 0);
    auto producer = alu(); producer.op_class = 3;
    writer.append(producer);
    auto late = producer; late.pc += 4; late.n_src = 1; late.producer_dists[0] = 1;
    writer.append(late);
    auto early = producer; early.pc += 8;
    writer.append(early);
    writer.close();
    return path;
}

std::filesystem::path write_iq_trace() {
    const auto dir = std::filesystem::path(FASTSIM_PROJECT_ROOT) /
        "tmp/fastsim-tests" / std::to_string(::getpid()) / "iq-partitions";
    std::filesystem::create_directories(dir);
    const auto path = dir / "loads.fst";
    BinaryTraceWriter writer(path.string(), 0);
    for (unsigned i = 0; i < 8; ++i) {
        auto record = alu();
        record.pc += 4 * i;
        record.op_class = 56;
        record.flags |= kLoad | kPhysicalAddress;
        record.address = 0x10000 + i * 4096;
        record.size = 8;
        writer.append(record);
    }
    writer.close();
    return path;
}

std::filesystem::path write_forwarded_load_trace() {
    const auto dir = std::filesystem::path(FASTSIM_PROJECT_ROOT) /
        "tmp/fastsim-tests" / std::to_string(::getpid()) / "fill-forwarding";
    std::filesystem::create_directories(dir);
    const auto path = dir / "forwarding.fst";
    BinaryTraceWriter writer(path.string(), 0);
    for (unsigned i = 0; i < 8; ++i) {
        auto store = alu();
        store.op_class = 57;
        store.flags |= kStore | kPhysicalAddress;
        store.address = 0x40000 + i * 4096;
        store.size = 8;
        writer.append(store);
        auto load = store;
        load.op_class = 56;
        load.flags = kRetires | kLoad | kPhysicalAddress;
        writer.append(load);
    }
    writer.close();
    return path;
}

class StoreFactsTrace final : public TraceSource {
  public:
    StoreFactsTrace(const std::filesystem::path& path, bool special, bool missing = false)
        : inner_(path.string()), special_(special), missing_(missing) {}
    bool next(TraceRecord& record) override {
        if (!inner_.next(record)) return false;
        active_ = record.is_write();
        facts_.attributes = fst::kStoreFactsComplete | fst::kStoreOrdinary;
        facts_.address = record.address;
        facts_.size = record.size;
        facts_.byte_enable = record.size == 64 ? ~std::uint64_t{0} :
            (std::uint64_t{1} << record.size) - 1;
        if (special_) facts_.attributes |= fst::kStoreStrictlyOrdered;
        return true;
    }
    std::string description() const override { return inner_.description(); }
    const fst::StoreRequestFacts* current_store_facts() const override {
        return active_ && !missing_ ? &facts_ : nullptr;
    }
  private:
    BinaryTraceSource inner_;
    bool special_, missing_, active_ = false;
    fst::StoreRequestFacts facts_{};
};

SimulationStats run_store_facts(const std::filesystem::path& path, SimulatorConfig c,
                                bool special, bool fast, bool missing = false) {
    c.response_materialized_uop_fast_kernel = fast;
    c.validate();
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::make_unique<StoreFactsTrace>(path, special, missing));
    Simulator simulator(c, std::move(traces));
    return simulator.run();
}

std::filesystem::path write_same_line_store_trace() {
    const auto dir = std::filesystem::path(FASTSIM_PROJECT_ROOT) /
        "tmp/fastsim-tests" / std::to_string(::getpid()) / "store-credit-proxy";
    std::filesystem::create_directories(dir);
    const auto path = dir / "same-line.fst";
    BinaryTraceWriter writer(path.string(), 0);
    for (unsigned i = 0; i < 128; ++i) {
        auto store = alu();
        store.pc += 4 * i;
        store.op_class = 57;
        store.flags |= kStore | kPhysicalAddress;
        store.address = 0x40000 + 8 * (i % 8);
        store.size = 8;
        writer.append(store);
    }
    writer.close();
    return path;
}

std::filesystem::path write_spr_load_bank_trace(bool same_bank, bool physical=true) {
    const auto dir=std::filesystem::path(FASTSIM_PROJECT_ROOT)/"tmp/fastsim-tests"/
        std::to_string(::getpid())/"spr-load-bank";
    std::filesystem::create_directories(dir);
    const auto path=dir/(physical?(same_bank?"same.fst":"distributed.fst"):"no-physical.fst");
    BinaryTraceWriter writer(path.string(),0);
    for(unsigned i=0;i<16;++i) {
        auto record=alu();record.pc+=4*i;record.op_class=56;
        record.flags|=kLoad; if(physical) record.flags|=kPhysicalAddress;
        record.address=0x20000+64*i+(same_bank?0:(i%4)*4);record.size=4;
        writer.append(record);
    }
    writer.close();return path;
}

std::filesystem::path write_split_load_bank_trace() {
    const auto dir=std::filesystem::path(FASTSIM_PROJECT_ROOT)/"tmp/fastsim-tests"/
        std::to_string(::getpid())/"spr-split-load-port";
    std::filesystem::create_directories(dir);
    const auto path=dir/"split.fst";
    BinaryTraceWriter writer(path.string(),0);
    auto record=alu();record.op_class=56;record.flags|=kLoad|kPhysicalAddress;
    record.address=0x2003c;record.size=8;writer.append(record);writer.close();
    return path;
}

std::filesystem::path write_out_of_order_load_trace() {
    const auto dir = std::filesystem::path(FASTSIM_PROJECT_ROOT) /
        "tmp/fastsim-tests" / std::to_string(::getpid()) / "fill-order";
    std::filesystem::create_directories(dir);
    const auto path = dir / "independent.fst";
    BinaryTraceWriter writer(path.string(), 0);
    auto slow = alu();
    slow.op_class = 3;
    writer.append(slow);
    auto older = alu();
    older.op_class = 56;
    older.flags |= kLoad | kPhysicalAddress;
    older.address = 0x80000;
    older.size = 8;
    older.n_src = 1;
    older.producer_dists[0] = 1;
    writer.append(older);
    auto younger = older;
    younger.address = 0x90000;
    younger.n_src = 0;
    younger.producer_dists[0] = 0;
    writer.append(younger);
    writer.close();
    return path;
}

class OrdinaryLoadTrace final : public TraceSource {
  public:
    OrdinaryLoadTrace(const std::filesystem::path& path, bool eligible, bool split=false, bool secure=false)
        : inner_(path.string()), eligible_(eligible) {
        facts_.attributes=fst::kStoreFactsComplete | fst::kMemoryRead | fst::kOrdinaryLoad;
        if (split) facts_.attributes|=fst::kStoreSplit;
        if (secure) facts_.attributes|=fst::kStoreSecure;
    }
    bool next(TraceRecord& record) override { return inner_.next(record); }
    std::string description() const override { return inner_.description(); }
    std::optional<bool> current_ordinary_load_eligible() const override { return eligible_; }
    const fst::StoreRequestFacts* current_memory_facts() const override { return &facts_; }
  private:
    BinaryTraceSource inner_;
    bool eligible_;
    fst::StoreRequestFacts facts_{};
};

// Real Simulator input with certified and absent functional request facts.
class MixedLoadFactsTrace final : public TraceSource {
  public:
    explicit MixedLoadFactsTrace(const std::filesystem::path& path) : inner_(path.string()) {
        facts_.attributes = fst::kStoreFactsComplete | fst::kMemoryRead | fst::kOrdinaryLoad;
    }
    bool next(TraceRecord& record) override {
        if (!inner_.next(record)) return false;
        known_ = (ordinal_++ % 16) < 8;
        return true;
    }
    std::string description() const override { return inner_.description(); }
    std::optional<bool> current_ordinary_load_eligible() const override {
        return known_ ? std::optional<bool>(true) : std::nullopt;
    }
    const fst::StoreRequestFacts* current_memory_facts() const override {
        return known_ ? &facts_ : nullptr;
    }
  private:
    BinaryTraceSource inner_;
    std::size_t ordinal_ = 0;
    bool known_ = false;
    fst::StoreRequestFacts facts_{};
};

SimulationStats run_mixed_loads(const std::filesystem::path& path, SimulatorConfig c) {
    c.validate();
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::make_unique<MixedLoadFactsTrace>(path));
    Simulator simulator(c, std::move(traces));
    return simulator.run();
}

SimulatorConfig config() {
    SimulatorConfig c;
    c.cores = 1;
    c.core_model = "interval_weave";
    c.interval_scheduler = "time_epoch";
    c.interval_max_cycles = 8;
    c.interval_target_uops = 7;
    c.chunk_instructions = 7;
    c.lookahead_chunks = 2;
    c.response_queue_feedback = true;
    c.response_sparse_scoreboard = true;
    c.response_block_summary = true;
    c.response_memory_descriptor = true;
    c.response_monotone_iq_calendar = true;
    c.response_activity_certificate = true;
    c.committed_static_dependency_feedback = true;
    c.l1d.size_bytes = 4ull << 10;
    c.l2.size_bytes = 16ull << 10;
    c.llc.size_bytes = 64ull << 10;
    c.llc_fill_response_latency = 100;
    c.cha_count = c.dram.channels = c.dram.banks_per_channel = 1;
    return c;
}

SimulationStats run(const std::filesystem::path& path, SimulatorConfig c,
                    bool fast, bool audit, bool warmup = false) {
    c.response_materialized_uop_fast_kernel = fast;
    c.response_frontier_audit_stride_uops = audit ? 1 : 0;
    c.cpi_attribution = audit;
    c.validate();
    std::unique_ptr<TraceSource> trace = (c.spr_load_fill_feedback || c.spr_load_bank_feedback)
        ? std::unique_ptr<TraceSource>(std::make_unique<OrdinaryLoadTrace>(path, true))
        : std::unique_ptr<TraceSource>(std::make_unique<BinaryTraceSource>(path.string()));
    if (warmup) trace = std::make_unique<WarmupInstructionTraceSource>(std::move(trace), 1, 511);
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::move(trace));
    Simulator simulator(c, std::move(traces));
    return simulator.run();
}

SimulationStats run_uncertified_loads(const std::filesystem::path& path, SimulatorConfig c) {
    c.validate();
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::make_unique<OrdinaryLoadTrace>(path, false));
    Simulator simulator(c, std::move(traces));
    return simulator.run();
}

const ResponseFrontierAuditSample& sample(const SimulationStats& stats, unsigned sequence) {
    const auto& samples = stats.response_frontier_audit.at(0);
    const auto it = std::find_if(samples.begin(), samples.end(), [&](const auto& row) {
        return row.sequence == sequence;
    });
    require(it != samples.end(), "missing sample " + std::to_string(sequence));
    return *it;
}

void check_feedback(const SimulationStats& stats, unsigned fanin, unsigned first_block = 0) {
    for (unsigned block = first_block; block < 4; ++block) {
        const auto& producer = sample(stats, 128 * block);
        const auto& consumer = sample(stats, 128 * block + fanin);
        require(consumer.actual_issue_cycle >= producer.actual_completion_cycle,
                "extended RAW must wait for actual memory writeback");
        require(consumer.producer_dists[4] == 0 &&
                    consumer.producer_dist_extensions.size() == fanin - 4 &&
                    consumer.producer_dist_extensions.back() == fanin,
                "sparse RAW identity must survive chunks separately from StoreSet");
        require(sample(stats, 128 * block + fanin + 1).actual_issue_cycle >=
                    consumer.actual_completion_cycle,
                "corrected timing must reach the transitive consumer");
    }
}

void compare(const SimulationStats& generic, const SimulationStats& fast) {
    require(generic.total_core().cycles == fast.total_core().cycles &&
                generic.total_core().retired_uops == fast.total_core().retired_uops &&
                generic.total_core().memory_accesses == fast.total_core().memory_accesses &&
                generic.llc.accesses == fast.llc.accesses && generic.llc.misses == fast.llc.misses,
            "generic and optimized kernels must preserve timing and event populations");
    require(fast.response_materialized_fast_kernel_uops > 0,
            "comparison must exercise the production fast kernel");
}
} // namespace

void test_interval_dependencies() {
    {
        // Two dependents become ready together. Rotation places the flexible
        // ALU on port 1, leaving port 0 free for the restricted operation.
        const auto run_ports = [](bool rotate) {
            auto c = config();
            c.committed_static_dependency_feedback = false;
            c.spr_shared_fu = true;
            c.spr_shared_fu_gap_aware = true;
            c.spr_shared_fu_round_robin = rotate;
            IntervalCoreModel core(c);
            auto first = alu();
            core.schedule(first, false, false, 0, false);
            auto flexible = alu(); flexible.n_src = 1; flexible.producer_dists[0] = 1;
            const auto second = core.schedule(flexible, false, false, 0, false);
            auto restricted = alu(); restricted.op_class = 9; restricted.n_src = 1;
            restricted.producer_dists[0] = 2;
            const auto third = core.schedule(restricted, false, false, 0, false);
            return std::make_pair(second.issue_cycle, third.issue_cycle);
        };
        const auto fixed = run_ports(false), rotated = run_ports(true);
        require(fixed.second > fixed.first && rotated.second == rotated.first,
                "SPR per-class port rotation must reach real interval scheduling");
    }
    {
        // The older dependent divide reserves a distant future time on SPR
        // port 1. A younger independent divide is allowed to issue in the
        // earlier gap; input remains the same retired functional stream.
        auto c = config();
        c.committed_static_dependency_feedback = false;
        c.spr_shared_fu = true;
        c.spr_shared_fu_gap_aware = true;
        c.integer_divide_latency = 50;
        c.integer_divide_pipelined = true;
        IntervalCoreModel core(c);
        auto first = alu(); first.op_class = 3;
        const auto producer = core.schedule(first, false, false, 0, false);
        auto older = first; older.pc += 4; older.n_src = 1; older.producer_dists[0] = 1;
        const auto late = core.schedule(older, false, false, 0, false);
        auto independent = first; independent.pc += 8;
        const auto early = core.schedule(independent, false, false, 0, false);
        require(late.issue_cycle >= producer.completion_cycle &&
                    early.issue_cycle < late.issue_cycle,
                "future shared-port reservation must not block an earlier free gap");
    }
    {
        auto c = config();
        c.committed_static_dependency_feedback = false;
        c.spr_shared_fu = true;
        c.spr_shared_fu_gap_aware = true;
        c.integer_divide_latency = 50;
        c.integer_divide_pipelined = true;
        c.cache_store_ports = 2;
        IntervalCoreModel core(c);
        auto producer = alu(); producer.op_class = 3;
        core.schedule(producer, false, false, 0, false);
        auto store = alu(); store.op_class = 57; store.flags |= kStore | kPhysicalAddress;
        store.address = 0x8000; store.size = 8; store.n_src = 1; store.producer_dists[0] = 1;
        const auto late1 = core.schedule(store, false, false, 0, false);
        store.address += 64; store.producer_dists[0] = 2;
        const auto late2 = core.schedule(store, false, false, 0, false);
        store.address += 64; store.n_src = 0; store.producer_dists[0] = 0;
        const auto early = core.schedule(store, false, false, 0, false);
        require(early.issue_cycle < std::min(late1.issue_cycle, late2.issue_cycle),
                "paired store data and address ports must both allow earlier gaps");
    }
    {
        auto c = config();
        c.committed_static_dependency_feedback = false;
        c.spr_shared_fu = true;
        c.spr_shared_fu_gap_aware = true;
        c.integer_divide_latency = 50;
        c.integer_divide_pipelined = true;
        IntervalCoreModel core(c);
        auto producer = alu(); producer.op_class = 3;
        core.schedule(producer, false, false, 0, false);
        auto late = alu(); late.op_class = 9; late.n_src = 1; late.producer_dists[0] = 1;
        const auto future = core.schedule(late, false, false, 0, false);
        auto early = alu(); early.op_class = 9;
        const auto first = core.schedule(early, false, false, 0, false);
        const auto second = core.schedule(early, false, false, 0, false);
        require(first.issue_cycle + 4 <= future.issue_cycle &&
                    second.issue_cycle >= first.issue_cycle + 4,
                "per-op issue intervals must remain enforced across a future reservation");
    }
    {
        auto c = config();
        c.committed_static_dependency_feedback = false;
        c.spr_shared_fu = true;
        c.spr_shared_fu_gap_aware = true;
        c.integer_divide_latency = 50;
        c.integer_divide_pipelined = true;
        c.interval_max_cycles = 1000;
        c.chunk_instructions = 1;
        c.interval_target_uops = 1;
        const auto path = write_spr_port_gap_trace();
        const auto split = run(path, c, false, true);
        require(sample(split, 2).actual_issue_cycle < sample(split, 1).actual_issue_cycle,
                "earlier gap must persist across actual chunk boundaries");
        c.chunk_instructions = 8;
        c.interval_target_uops = 8;
        const auto one_chunk = run(path, c, false, true);
        require(sample(one_chunk, 2).actual_issue_cycle < sample(one_chunk, 1).actual_issue_cycle &&
                    split.total_core().cycles == one_chunk.total_core().cycles,
                "gap scheduling must preserve timing across chunk sizes");
        compare(one_chunk, run(path, c, true, false));
    }
    {
        SprPortCalendar calendar;
        calendar.reserve(100, 20);
        calendar.reserve(10, 20);
        require(calendar.available(30, 70) && !calendar.available(80, 25) &&
                    !calendar.available(25, 10),
                "port reservations must respect earlier and later interval boundaries");
        calendar.reserve(30, 70);
        calendar.prune(100);
        require(calendar.live() == 1 && !calendar.available(110, 1) &&
                    calendar.available(120, 1),
                "port reservation pruning must retain active future intervals");
    }
    {
        // Independent occupancy oracle exercises arbitrary insertion order,
        // multiple-length reservations, and pruning before a monotone bound.
        SprPortCalendar calendar;
        std::array<bool, 1024> occupied{};
        std::uint32_t seed = 42;
        for (unsigned i=0; i<2500; ++i) {
            seed = seed * 1664525u + 1013904223u;
            const auto begin = static_cast<unsigned>((seed >> 8) % 1000);
            const auto duration = static_cast<unsigned>((seed % 7) + 1);
            bool expected = true;
            for (unsigned j=begin; j<begin+duration; ++j) expected &= !occupied[j];
            require(calendar.available(begin, duration) == expected,
                    "unordered port interval availability must match occupancy oracle");
            if (expected) {
                calendar.reserve(begin, duration);
                for (unsigned j=begin; j<begin+duration; ++j) occupied[j] = true;
            }
        }
        calendar.prune(512);
        for (unsigned i=512; i<1024; ++i)
            require(calendar.available(i, 1) == !occupied[i],
                    "pruning must retain every occupied interval after the bound");
    }
    {
        // Diagnostic switch changes only the latency table; shared port
        // arbitration and normal default target behavior remain intact.
        auto target = config();
        target.committed_static_dependency_feedback = false;
        target.spr_shared_fu = true;
        target.simd_latency = 1;
        auto generic_latency = target;
        generic_latency.spr_shared_fu_generic_latency = true;
        auto invalid = generic_latency;
        invalid.spr_shared_fu = false;
        bool rejected = false;
        try { invalid.validate(); }
        catch (const std::invalid_argument&) { rejected = true; }
        require(rejected, "generic-latency diagnostic requires shared FU");
        IntervalCoreModel target_core(target), generic_core(generic_latency);
        for (unsigned i=0; i<3; ++i) {
            auto op = alu();
            op.op_class = 18; // legacy identity for SPR SimdMult
            op.pc += 4 * i;
            const auto t = target_core.schedule(op, false, false, 0, false);
            const auto g = generic_core.schedule(op, false, false, 0, false);
            require(t.issue_cycle == g.issue_cycle,
                    "latency diagnostic must preserve shared-port admissions");
            require(t.completion_cycle - t.execute_cycle == 3 &&
                    g.completion_cycle - g.execute_cycle == 1,
                    "shared-port SimdMult target/generic latencies must differ");
        }
    }
    {
        auto c=config();c.committed_static_dependency_feedback=false;
        c.spr_load_bank_feedback=true;c.cache_load_ports=8;c.issue_width=8;
        c.dispatch_width=8;c.rename_width=8;c.decode_width=8;c.fetch_width=8;
        c.interval_target_uops=16;c.chunk_instructions=16;c.interval_max_cycles=32;
        const auto same=write_spr_load_bank_trace(true);
        const auto spread=write_spr_load_bank_trace(false);
        const auto congested=run(same,c,false,false);
        const auto dispersed=run(spread,c,false,false);
        require(congested.o3.at(0).spr_bank_packets==16 && dispersed.o3.at(0).spr_bank_packets==16,
                "certified single-line loads must consume one bank packet each");
        require(congested.o3.at(0).spr_bank_waits>dispersed.o3.at(0).spr_bank_waits,
                "same-bank packets must contend more than spread offsets");
        const auto unproved=run_uncertified_loads(same,c);
        require(unproved.o3.at(0).spr_bank_packets==0 && unproved.o3.at(0).spr_bank_fallbacks==16 &&
                unproved.o3.at(0).spr_load_port_packets==16,
                "known single packets must consume a port but not guessed bank credits");
        c.cache_load_ports=3;
        const auto target_same=run(same,c,false,false);
        const auto target_spread=run(spread,c,false,false);
        require(target_same.o3.at(0).spr_bank_waits>target_spread.o3.at(0).spr_bank_waits,
                "SPR three-port same-bank pressure must exceed spread offsets");
    }
    {
        auto c=config();c.committed_static_dependency_feedback=false;
        c.spr_load_bank_feedback=true;c.strict_physical_address=false;
        auto path=write_spr_load_bank_trace(true,false);
        const auto nonphysical=run(path,c,false,false);
        require(nonphysical.o3.at(0).spr_bank_packets==0 && nonphysical.o3.at(0).spr_bank_fallbacks==16 &&
                nonphysical.o3.at(0).spr_load_port_fallbacks==16 && nonphysical.o3.at(0).spr_load_port_packets==0,
                "nonphysical records must not receive packet reservations");
        path=write_spr_load_bank_trace(true);
        std::vector<std::unique_ptr<TraceSource>> traces;
        traces.push_back(std::make_unique<OrdinaryLoadTrace>(path,true,true));
        c.validate();Simulator simulator(c,std::move(traces));
        const auto split=simulator.run();
        require(split.o3.at(0).spr_bank_packets==0 && split.o3.at(0).spr_bank_fallbacks==16 &&
                split.o3.at(0).spr_load_port_fallbacks==16 && split.o3.at(0).spr_load_port_packets==0,
                "split packets must stay on fallback until individually tracked");
    }
    {
        auto c=config();c.committed_static_dependency_feedback=false;
        c.spr_load_bank_feedback=true;c.cache_load_ports=3;c.issue_width=8;
        c.dispatch_width=8;c.rename_width=8;c.decode_width=8;c.fetch_width=8;
        c.interval_target_uops=16;c.chunk_instructions=16;c.interval_max_cycles=32;
        auto orphan=c;orphan.spr_load_bank_feedback=false;orphan.spr_load_port_fallback_proxy=true;
        bool rejected=false;try {orphan.validate();}catch(const std::invalid_argument&){rejected=true;}
        require(rejected,"fallback port proxy must require the joint bank/port calendar");
        const auto path=write_spr_load_bank_trace(true);
        const auto off=run_mixed_loads(path,c);
        c.spr_load_port_fallback_proxy=true;
        const auto on=run_mixed_loads(path,c);
        require(on.o3.at(0).spr_load_port_proxy_fragments==8 &&
                on.o3.at(0).spr_load_port_fallbacks==8 &&
                on.o3.at(0).spr_bank_fallbacks==8 &&
                on.o3.at(0).spr_bank_packets==8,
                "unknown load fragments reserve ports without guessing bank or erasing fallback population");
        require(on.o3.at(0).spr_load_port_proxy_waits>0,
                "mixed accepted fragments must contend for the shared port width");
        require(on.total_core().memory_accesses==off.total_core().memory_accesses &&
                on.total_core().retired_instructions==off.total_core().retired_instructions,
                "port proxy must preserve demand and retired populations");
        c.chunk_instructions=4;c.interval_target_uops=4;
        const auto cross_chunk=run_mixed_loads(path,c);
        require(cross_chunk.o3.at(0).spr_load_port_proxy_fragments==8 &&
                cross_chunk.o3.at(0).spr_bank_packets==8,
                "mixed fragment reservations must survive chunk boundaries");
        const auto split_path=write_split_load_bank_trace();
        std::vector<std::unique_ptr<TraceSource>> split_traces;
        split_traces.push_back(std::make_unique<OrdinaryLoadTrace>(split_path,true,true));
        Simulator split_simulator(c,std::move(split_traces));
        const auto split=split_simulator.run();
        require(split.o3.at(0).spr_load_port_proxy_fragments==2 &&
                split.o3.at(0).spr_load_port_fallbacks==2 &&
                split.o3.at(0).spr_bank_packets==0 &&
                split.total_core().retired_instructions==1,
                "a split load must retain two model fragments without invented bank facts");
        std::vector<std::unique_ptr<TraceSource>> secure_traces;
        secure_traces.push_back(std::make_unique<OrdinaryLoadTrace>(split_path,true,true,true));
        Simulator secure_simulator(c,std::move(secure_traces));
        const auto secure=secure_simulator.run();
        require(secure.o3.at(0).spr_load_port_proxy_fragments==0 &&
                secure.o3.at(0).spr_load_port_fallbacks==2,
                "known secure split requests must remain uncovered fallback");
        const auto forwarded=run_mixed_loads(write_forwarded_load_trace(),c);
        require(forwarded.total_core().forwarded_loads>0 &&
                forwarded.o3.at(0).spr_load_port_proxy_fragments==0,
                "forwarded loads must not consume a hierarchy load-port proxy token");
    }
    {
        SprStoreWindow stores(4, 2);
        for (std::uint64_t line = 1; line <= 4; ++line) {
            const auto ticket = stores.reserve(line, 0);
            require(ticket.admission == (line - 1) / 2,
                    "four store lines must overlap within send width");
            stores.complete(ticket, 100);
        }
        const auto same_line = stores.reserve(1, 0);
        require(same_line.admission == 2,
                "an in-flight line must reuse its existing line-window slot");
        stores.complete(same_line, 101);
        const auto fifth = stores.reserve(5, 0);
        require(fifth.admission == 100,
                "fifth distinct store line must wait for a window slot");
        stores.complete(fifth, 120);
        stores.barrier_until(130);
        require(stores.reserve(6, 100).admission >= 130,
                "a special-store barrier must block later ordinary sends");
        SprStoreWindow credits(1, 2);
        const SprStoreWindow::CreditKey key{0, 1, 2};
        const auto first = credits.reserve(7, 0, key);
        require(!first.credit_join, "first store has no live line to join");
        credits.complete(first, 20);
        const auto joined = credits.reserve(7, 1, key);
        require(joined.credit_join, "matching live line must expose credit join");
        credits.complete(joined, 25);
        const auto other = credits.reserve(7, 2, SprStoreWindow::CreditKey{0, 2, 2});
        require(!other.credit_join, "different context cannot join");
        credits.complete(other, 30);
        require(!credits.reserve(7, 3, key).credit_join,
                "mixed identity remains uncertified until line expiry");
    }
    {
        auto c = config();
        c.committed_static_dependency_feedback = false;
        c.needs_tso = true;
        c.tso_store = TsoStoreMode::kBuffer;
        c.store_wait_relief = false;
        c.cache_store_ports = 2;
        c.spr_store_window_feedback = true;
        c.spr_store_window_lines = 4;
        const auto path = write_forwarded_load_trace();
        const auto ordinary = run_store_facts(path, c, false, false);
        const auto fast = run_store_facts(path, c, false, true);
        compare(ordinary, fast);
        require(ordinary.o3.at(0).spr_store_window_admitted > 0,
                "certified ordinary stores must enter the store window");
        const auto special = run_store_facts(path, c, true, false);
        require(special.o3.at(0).spr_store_window_admitted == 0 &&
                    special.o3.at(0).spr_store_window_fallbacks > 0,
                "strictly ordered stores must use the conservative path");
        c.spr_store_window_feedback = false;
        const auto special_baseline = run_store_facts(path, c, true, false);
        require(special.total_core().cycles == special_baseline.total_core().cycles,
                "special-store fallback must preserve baseline timing");
    }
    {
        auto c = config();
        c.committed_static_dependency_feedback = false;
        c.needs_tso = true;
        c.tso_store = TsoStoreMode::kBuffer;
        c.store_wait_relief = false;
        c.cache_store_ports = 2;
        c.sq_entries = 2;
        c.tso_store_buffer_size = 2;
        c.spr_store_window_feedback = true;
        c.spr_store_window_lines = 4;
        const auto path = write_same_line_store_trace();
        const auto off = run_store_facts(path, c, false, false);
        c.spr_store_sq_join_credit_proxy = true;
        const auto on = run_store_facts(path, c, false, false);
        require(on.o3.at(0).spr_store_sq_joined > 0,
                "same-line stores must exercise the SQ credit proxy");
        require(on.o3.at(0).spr_store_sq_credit_saved_cycles > 0,
                "joined stores must release modeled SQ credits earlier");
        require(on.o3.at(0).sq_stall_cycles < off.o3.at(0).sq_stall_cycles,
                "credit proxy must affect SQ pressure in a saturated fixture");
        require(on.total_core().l1d.accesses == off.total_core().l1d.accesses &&
                on.total_core().perf_retirement.physical.perf_retired_stores ==
                    off.total_core().perf_retirement.physical.perf_retired_stores,
                "SQ proxy must preserve hierarchy and retired-store populations");
        const auto special = run_store_facts(path, c, true, false);
        require(special.o3.at(0).spr_store_sq_joined == 0,
                "special stores must never join the SQ credit proxy");
        const auto unknown = run_store_facts(path, c, false, false, true);
        require(unknown.o3.at(0).spr_store_sq_joined == 0,
                "missing store facts must never join the SQ credit proxy");
        c.chunk_instructions = 1;
        c.interval_target_uops = 1;
        const auto cross_chunk = run_store_facts(path, c, false, false);
        require(cross_chunk.o3.at(0).spr_store_sq_joined > 0,
                "credit proxy must retain matching lines across producer chunks");
    }
    {
        SprFillCalendar calendar(1, 8);
        const auto future = calendar.reserve(100, 20);
        const auto early = calendar.reserve(10, 20);
        const auto overlap = calendar.reserve(15, 10);
        require(future.admission == 100 && early.admission == 10 &&
                    overlap.admission == 30,
                "fill reservations must reuse earlier non-overlapping gaps");
        calendar.finish(early, 30);
        calendar.finish(overlap, 40);
        calendar.finish(future, 120);
        SprFillCalendar conflict(1, 2);
        const auto later = conflict.reserve(100, 20);
        const auto before = conflict.reserve(10, 20);
        require(conflict.finish(before, 110),
                "a late extension across an already accepted future fill must be reported");
        require(!conflict.reserve(200, 1).valid,
                "calendar host bound must fall back instead of inventing a hardware stall");
        conflict.finish(later, 120);
        conflict.prune(120);
        require(conflict.reserve(120, 1).valid,
                "completed intervals must release host bookkeeping");
    }
    {
        // Distinct L1 misses contend for one target fill credit.  Increasing
        // the credit to two permits overlap without changing trace contents.
        auto c = config();
        c.committed_static_dependency_feedback = false;
        c.spr_load_fill_feedback = true;
        c.spr_l1d_fill_entries = 1;
        {
            auto incompatible = c;
            incompatible.response_pending_fill = true;
            bool rejected = false;
            try { incompatible.validate(); }
            catch (const std::invalid_argument& e) {
                rejected = std::string(e.what()).find("spr_load_fill_feedback") != std::string::npos;
            }
            require(rejected, "experimental fill credits must reject competing fill solvers");
        }
        c.chunk_instructions = 8;
        c.interval_target_uops = 8;
        c.interval_max_cycles = 1024;
        const auto path = write_iq_trace();
        const auto uncertified = run_uncertified_loads(path, c);
        auto without_fill = c;
        without_fill.spr_load_fill_feedback = false;
        const auto uncertified_baseline = run(path, without_fill, false, false);
        require(uncertified.o3.at(0).spr_fill_uncertified_fallbacks > 0 &&
                    uncertified.o3.at(0).spr_fill_candidates == 0 &&
                    uncertified.total_core().cycles == uncertified_baseline.total_core().cycles,
                "uncertified loads must preserve the original timing path");
        const auto limited = run(path, c, false, true);
        compare(limited, run(path, c, true, false));
        c.spr_l1d_fill_entries = 2;
        const auto wider = run(path, c, false, true);
        require(sample(limited, 1).memory_response_cycle >
                    sample(wider, 1).memory_response_cycle,
                "independent misses must respect L1 fill credit capacity");
        c.spr_l1d_fill_entries = 16;
        c.spr_l2_fill_entries = 1;
        const auto l2_limited = run(path, c, false, true);
        c.spr_l2_fill_entries = 2;
        const auto l2_wider = run(path, c, false, true);
        require(sample(l2_limited, 1).memory_response_cycle >
                    sample(l2_wider, 1).memory_response_cycle,
                "independent private-L2 misses must respect L2 fill credit capacity");
        c.chunk_instructions = 2;
        c.interval_target_uops = 2;
        c.spr_l1d_fill_entries = 1;
        c.spr_l2_fill_entries = 48;
        const auto across_chunks = run(path, c, false, true);
        c.spr_l1d_fill_entries = 2;
        const auto across_chunks_wider = run(path, c, false, true);
        require(sample(across_chunks, 2).memory_response_cycle >
                    sample(across_chunks_wider, 2).memory_response_cycle,
                "outstanding fill pressure must survive a chunk boundary: limited=" +
                    std::to_string(sample(across_chunks, 2).memory_response_cycle) +
                    " wider=" + std::to_string(sample(across_chunks_wider, 2).memory_response_cycle) +
                    " fallback=" + std::to_string(across_chunks.o3.at(0).spr_fill_calendar_fallbacks));
        c.chunk_instructions = 8;
        c.interval_target_uops = 8;
        const auto forwarded_path = write_forwarded_load_trace();
        c.store_forwarding = true;
        c.spr_l1d_fill_entries = 1;
        const auto forwarded_on = run(forwarded_path, c, false, false);
        c.spr_load_fill_feedback = false;
        const auto forwarded_off = run(forwarded_path, c, false, false);
        require(forwarded_on.total_core().forwarded_loads > 0 &&
                    forwarded_on.total_core().cycles == forwarded_off.total_core().cycles,
                "forwarded loads must not allocate L1 fill credits");
        c.store_forwarding = false;
        c.integer_divide_latency = 5000;
        c.spr_l1d_fill_entries = 1;
        c.interval_max_cycles = 20000;
        const auto reordered_path = write_out_of_order_load_trace();
        const auto unordered_baseline = run(reordered_path, c, false, true);
        c.spr_load_fill_feedback = true;
        const auto unordered_fill = run(reordered_path, c, false, true);
        // The core may issue the younger UOP first, while the existing shared
        // request envelope can still serialize its memory admission. Gap
        // behavior is tested directly above, independent of that envelope.
        require(sample(unordered_baseline, 2).actual_issue_cycle <
                    sample(unordered_baseline, 1).actual_issue_cycle,
                "fixture must exercise an out-of-order core issue");
        require(unordered_fill.o3.at(0).spr_fill_calendar_fallbacks == 0,
                "ordinary out-of-order fills must use bounded calendar gaps");
    }
    for (int op = 61; op <= 76; ++op)
        require(spr_iq_partition(op) == 1,
                "SPR vector memory OpClasses must use the memory IQ");
    require(spr_iq_partition(60) == 0 && spr_iq_partition(77) == 0 &&
                spr_iq_partition(kSprStoreAddress) == 1 &&
                spr_iq_partition(kSprStoreData) == 2,
            "SPR IQ class boundaries must follow target routing");
    {
        // An unresolved integer UOP fills only the execution partition.  A
        // following memory UOP may still enter the independent memory IQ.
        auto c = config();
        c.spr_partitioned_iq = true;
        c.spr_execution_iq_entries = 1;
        c.spr_memory_iq_entries = 1;
        c.spr_store_data_iq_entries = 1;
        c.integer_divide_latency = 200;
        c.committed_static_dependency_feedback = false;
        c.validate();
        IntervalCoreModel core(c);
        auto divide = alu();
        divide.op_class = 3;
        const auto first = core.schedule(divide, false, false, 0, false);
        auto dependent = alu();
        dependent.n_src = 1;
        dependent.producer_dists[0] = 1;
        core.schedule(dependent, false, false, 0, false);
        auto load = alu();
        load.op_class = 56;
        load.flags |= kLoad | kPhysicalAddress;
        load.address = 0x8000;
        load.size = 8;
        const auto mem = core.schedule(load, false, false, 0, false);
        const auto blocked = core.schedule(alu(), false, false, 0, false);
        require(mem.dispatch_cycle < first.completion_cycle,
                "memory IQ must not share execution IQ capacity");
        require(blocked.dispatch_cycle >= first.completion_cycle,
                "execution IQ must stall when its own partition is full");

        // A cache miss extends the first load beyond the producer's local
        // estimate. The response stage must carry that occupancy to the next
        // load in the same interval.
        c.response_frontier_audit_stride_uops = 1;
        c.chunk_instructions = 8;
        c.interval_target_uops = 8;
        c.interval_max_cycles = 1024;
        const auto path = write_iq_trace();
        const auto feedback = run(path, c, false, true);
        require(feedback.o3.at(0).iq_max_occupancy <= 3,
                "IQ occupancy must not report the shared proxy capacity when a partition fills");
        require(sample(feedback, 1).actual_dispatch_cycle >=
                    sample(feedback, 0).actual_completion_cycle,
                "memory partition must retain response-extended occupancy");
        compare(feedback, run(path, c, true, false));
        auto wider = c;
        wider.spr_memory_iq_entries = 8;
        const auto parallel = run(path, wider, false, true);
        require(sample(parallel, 1).actual_dispatch_cycle <
                    sample(parallel, 0).actual_completion_cycle,
                "available memory IQ slots must permit overlapping loads");
        c.chunk_instructions = 2;
        c.interval_target_uops = 2;
        compare(run(path, c, false, true), run(path, c, true, false));
    }
    {
        auto c = config();
        c.spr_partitioned_iq = true;
        c.spr_execution_iq_entries = c.spr_memory_iq_entries =
            c.spr_store_data_iq_entries = 1;
        c.integer_divide_latency = 200;
        c.committed_static_dependency_feedback = false;
        c.validate();
        IntervalCoreModel core(c);
        auto divide = alu();
        divide.op_class = 3;
        const auto first = core.schedule(divide, false, false, 0, false);
        auto dependent_data = alu();
        dependent_data.op_class = kSprStoreData;
        dependent_data.n_src = 1;
        dependent_data.producer_dists[0] = 1;
        core.schedule(dependent_data, false, false, 0, false);
        const auto independent_alu = core.schedule(alu(), false, false, 0, false);
        auto data = alu();
        data.op_class = kSprStoreData;
        const auto blocked_data = core.schedule(data, false, false, 0, false);
        require(independent_alu.dispatch_cycle < first.completion_cycle &&
                    blocked_data.dispatch_cycle >= first.completion_cycle,
                "store-data IQ must be independent of execution IQ");
    }
    for (unsigned fanin : {5u, 8u, 16u, 20u}) {
        const auto path = write_trace(fanin, false);
        auto c = config();
        c.integer_divide_latency = 200;
        c.committed_pipeline_audit = true;
        c.validate();
        BinaryTraceSource source(path.string());
        IntervalCoreModel core(c);
        TraceRecord record;
        std::vector<IntervalTiming> timing;
        while (source.next(record)) {
            timing.push_back(core.schedule(record, false, false, 0, false, &source));
        }
        require(timing[fanin].issue_cycle >= timing[0].completion_cycle &&
                    timing[fanin + 1].issue_cycle >= timing[fanin].completion_cycle,
                "base scheduling must include the oldest extended producer");
        require(core.committed_pipeline_audit().dependency_edges == fanin + 1,
                "dependency audit must count inline and sparse edges exactly once");

        const auto memory_path = write_trace(fanin, true);
        auto feedback_config = config();
        if (fanin == 16) {
            feedback_config.chunk_instructions = 64;
            feedback_config.interval_max_cycles = 1024;
            feedback_config.interval_target_uops = 64;
        }
        const auto generic = run(memory_path, feedback_config, false, true);
        check_feedback(generic, fanin);
        compare(generic, run(memory_path, feedback_config, true, false));
        if (fanin == 16) {
            require(sample(generic, fanin).issue_gate_dependency_slot >= 5 &&
                        sample(generic, fanin).issue_gate_kind ==
                            static_cast<unsigned>(ResponseIssueGateKind::kRegisterProducer),
                    "extra RAW must retain register writeback semantics in gate attribution");
            feedback_config.chunk_instructions = 64;
            feedback_config.interval_max_cycles = 1024;
            feedback_config.interval_target_uops = 64;
            const auto batched = run(memory_path, feedback_config, false, true, true);
            check_feedback(batched, fanin, 1);
            require(batched.functional_warmup_uops == 1 &&
                        batched.total_core().retired_uops == 511,
                    "warmup wrapper must preserve extension and retirement identities");
            compare(batched, run(memory_path, feedback_config, true, false, true));
            feedback_config.response_causal_block_transfer = true;
            const auto transferred = run(memory_path, feedback_config, false, false, true);
            require(transferred.total_core().cycles == batched.total_core().cycles,
                    "block transfer must preserve extended dependencies");
        }
    }
}
