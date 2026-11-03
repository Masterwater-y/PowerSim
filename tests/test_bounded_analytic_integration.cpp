#include "fastsim/simulator.hpp"
#include "fastsim/trace.hpp"
#include <iostream>
#include <stdexcept>
#include <filesystem>
#include <fstream>
using namespace fastsim;
namespace {
void check(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
class Source final : public TraceSource {
    std::vector<TraceRecord> records_;
    std::size_t at_ = 0;
    bool local_;
public:
    explicit Source(bool local = false, bool split = false, std::uint64_t base = 0x10000) : local_(local) {
        for (unsigned i = 0; i < 256; ++i) {
            TraceRecord r;
            r.flags = kRetires | kStore | kPhysicalAddress;
            r.address = base + i * 64;
            r.size = split && i == 0 ? 128 : 8;
            records_.push_back(r);
        }
        TraceRecord f;
        f.flags = kRetires | kSerialize;
        records_.push_back(f);
    }
    bool next(TraceRecord& r) override {
        if (at_ == records_.size()) return false;
        r = records_[at_++];
        return true;
    }
    std::optional<fst::MemoryRequestRole> current_memory_request_role() const override {
        return local_ && (at_ - 1) % 4 == 1 ? fst::MemoryRequestRole::NoAccess : fst::MemoryRequestRole::Data;
    }
    std::string description() const override { return "bounded analytical integration"; }
};
class AtomicSource final : public TraceSource {
    unsigned at_ = 0;
    bool qualified_;
    StaticInstructionInfo locked_;
public:
    explicit AtomicSource(bool qualified) : qualified_(qualified) {
        locked_.pc = 0x2000; locked_.size = 4; locked_.fallthrough_pc = 0x2004;
        locked_.flags = kStaticMemory | kStaticReadBarrier | kStaticWriteBarrier | kStaticLockedRmw;
    }
    bool next(TraceRecord& r) override {
        if (at_ == 3) return false;
        r = {}; r.pc = at_ == 1 ? locked_.pc : 0x1000; r.address = 0x10000; r.size = 8;
        r.flags = kRetires | kPhysicalAddress | (at_++ == 1 ? kAtomic : kStore);
        return true;
    }
    const StaticInstructionInfo* static_instruction(std::uint64_t pc) const override {
        return qualified_ && pc == locked_.pc ? &locked_ : nullptr;
    }
    std::string description() const override { return "bounded atomic qualification"; }
};

SimulatorConfig config() {
    SimulatorConfig c;
    c.cores = 1;
    c.core_model = "interval_weave";
    c.interval_scheduler = "time_epoch";
    c.response_queue_feedback = c.response_sparse_scoreboard = true;
    c.needs_tso = true;
    c.tso_store = TsoStoreMode::kBuffer;
    c.tso_store_buffer_size = c.sq_entries = 114;
    c.store_timing_model = "bounded_analytic";
    c.store_commit_to_iew_delay = 1;
    c.l1d.store_hit_latency = 1;
    c.l2.store_hit_latency = 5;
    c.interval_target_uops = c.chunk_instructions = 512;
    c.interval_max_cycles = 100000;
    c.dram.channels = c.cha_count = 1;
    c.l1d.size_bytes = 512; c.l1d.associativity = 2;
    c.l2.size_bytes = 2048; c.l2.associativity = 2;
    c.cpi_attribution = true;
    c.response_frontier_audit_stride_uops = 1;
    return c;
}
SimulationStats run(bool certificate, bool local = false, unsigned chunk = 512, unsigned sq = 114, unsigned cores = 1, bool split = false) {
    auto c = config();
    c.buffer_tso_residence_certificate = certificate;
    c.chunk_instructions = c.interval_target_uops = chunk;
    c.sq_entries = c.tso_store_buffer_size = sq;
    c.cores = cores;
    c.validate();
    std::vector<std::unique_ptr<TraceSource>> sources;
    for (unsigned core = 0; core < cores; ++core) sources.push_back(std::make_unique<Source>(local, split));
    Simulator sim(c, std::move(sources));
    return sim.run();
}
void verify(const SimulationStats& s, bool local, unsigned cores, unsigned sq) {
    check(s.total_core().retired_instructions == cores * 257, "input population must be conserved");
    check(s.bounded_store_estimates == cores * (local ? 192 : 256), "analytical store output actually consumed");
    const auto& observation=s.bounded_store_demand_observation;
    check(observation.demand.samples==s.bounded_store_estimates,"accepted store population matches demand observation");
    std::uint64_t observed_wait=0;
    for(const auto& frames:s.response_frontier_audit) for(const auto& frame:frames)
        if(frame.regular_store) observed_wait+=frame.store_sq_release_cycle-frame.store_send_cycle;
    check(observation.demand.sum==observed_wait,"observer equals actual audited send to SQ release across chunks");
    check(s.bounded_store_classification_events == s.total_core().memory_accesses, "cache intents classified once");
    check(s.timing_feedback_calls == s.bounded_store_epochs, "one complete response pass per epoch");
    check(s.total_o3().sq_max_occupancy <= sq, "SQ occupancy remains bounded");
    for (const auto& core : s.response_frontier_audit) {
        std::uint64_t prior = 0;
        bool prior_local = true;
        for (const auto& frame : core) {
            if (frame.regular_store) {
                check(frame.store_send_cycle >= frame.actual_retire_cycle + 2, "canWB before store admission");
                check(frame.store_send_cycle >= prior + (prior_local ? 0 : 1), "cross-window FIFO response edge");
                check(frame.store_sq_release_cycle > frame.store_send_cycle, "mandatory response releases SQ");
            }
            const bool local_frame = local && frame.sequence < 256 && frame.sequence % 4 == 1;
            if (frame.regular_store || local_frame) {
                prior = frame.store_sq_release_cycle;
                prior_local = local_frame;
            }
        }
    }
}
}
void test_bounded_analytic_integration() {
    {
        const std::string path="tmp/store-lifecycle-integration.bin";
        std::filesystem::create_directories("tmp");
        auto c=config();c.store_lifecycle_audit_path=path;c.validate();
        SimulationStats observed;
        {
            std::vector<std::unique_ptr<TraceSource>> sources;
            sources.push_back(std::make_unique<Source>());
            Simulator sim(c,std::move(sources));observed=sim.run();
        }
        const auto plain=run(false);
        check(observed.total_core().cycles==plain.total_core().cycles,
              "store observation cannot affect target cycles");
        std::ifstream input(path,std::ios::binary);char magic[8];std::uint32_t version=0,bytes=0;
        input.read(magic,8);input.read(reinterpret_cast<char*>(&version),4);input.read(reinterpret_cast<char*>(&bytes),4);
        check(std::string(magic,8)=="FSSTLIF1" && version==1 && bytes==160,"versioned lifecycle format");
        std::array<std::uint64_t,20> row{};std::uint64_t population=0,service=0;
        while(input.read(reinterpret_cast<char*>(row.data()),sizeof(row))) {
            ++population;service+=row[8]-row[7];
            check(row[19]==1 && row[17]==row[1],"only accepted measured source members");
        }
        check(population==observed.response_residuals[0].store_uops &&
              service==observed.response_residuals[0].store_send_to_response_cycles,
              "lifecycle stream conserves final accepted store time and population");
        input.close();std::filesystem::remove(path);
        c.cpi_attribution=false;
        bool rejected=false;try {c.validate();}catch(const std::invalid_argument&){rejected=true;}
        check(rejected,"unsupported observation must reject before simulation");
    }
    for (const auto local : {false, true}) {
        for (const auto sq : {2u, 114u}) {
            auto x = run(false, local, 32, sq, 2), y = run(true, local, 32, sq, 2);
            verify(x, local, 2, sq);
            check(x.total_core().cycles == y.total_core().cycles && x.total_core().l1d.misses == y.total_core().l1d.misses,
                  "certificate must not gate bounded prepare");
        }
    }
    verify(run(false), false, 1, 114);
    const auto split = run(false, false, 32, 114, 1, true);
    const auto& audit = split.response_frontier_audit.at(0);
    std::uint64_t boundary = 0, stores = 0;
    for (const auto& frame : audit) {
        if (!frame.regular_store) continue;
        stores += frame.memory_events.size();
        if (frame.sequence == 0) boundary = frame.store_sq_release_cycle;
        else check(frame.buffer_tso_permission_ready_cycle >= boundary + frame.buffer_tso_prepare_service_cycles,
                   "split store must stop younger prepare scanning until completion");
    }
    check(stores == 257, "all store fragments retain final-timing audit evidence");
    for (const bool qualified : {false, true}) {
        auto c = config(); c.committed_static_memory_ordering = true; c.validate();
        std::vector<std::unique_ptr<TraceSource>> sources;
        sources.push_back(std::make_unique<AtomicSource>(qualified));
        bool rejected = false;
        try {
            Simulator sim(c, std::move(sources)); const auto atomic = sim.run();
            const auto& frames = atomic.response_frontier_audit.at(0);
            check(frames.size() == 3, "qualified atomic witness complete");
            check(frames[1].actual_issue_cycle >= frames[0].store_sq_release_cycle,
                  "atomic admission must wait for prior FIFO response");
            check(frames[2].store_send_cycle >= frames[1].actual_completion_cycle,
                  "younger store follows atomic completion");
        } catch (const std::invalid_argument&) { rejected = true; }
        check(rejected == !qualified, "unqualified atomic must reject, qualified barriers must model");
    }
    auto threshold = config(); threshold.store_request_tbes = 179; threshold.validate();
    for (unsigned kind = 0; kind < 5; ++kind) {
        auto c = config();
        if (kind == 0) c.store_request_tbes = 1;
        if (kind == 1) c.cache_store_ports = 115;
        if (kind == 2) c.store_timing_model = "invalid";
        if (kind == 3) c.store_request_optional_entries = 1;
        if (kind == 4) c.store_request_tbes = 178;
        bool rejected = false;
        try { c.validate(); } catch (const std::invalid_argument&) { rejected = true; }
        check(rejected, "unsupported resource/profile must reject the analytical subdomain");
    }
}
#ifdef FASTSIM_BOUNDED_INTEGRATION_STANDALONE
int main() {
    try { test_bounded_analytic_integration(); std::cout << "bounded analytical production integration passed\n"; }
    catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
#endif
