#include "fastsim/simulator.hpp"
#include "fastsim/causal_read.hpp"

#include <algorithm>
#include <array>
#include <filesystem>
#include <fstream>
#include <limits>
#include <map>
#include <memory>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include <unistd.h>

namespace {
using namespace fastsim;

void check(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error("causal_read test: " + message);
}

class Records final : public TraceSource {
  public:
    explicit Records(std::vector<TraceRecord> records, bool boundary = false,
                     std::map<std::uint64_t, StaticInstructionInfo> operands = {})
        : records_(std::move(records)), boundary_(boundary), operands_(std::move(operands)) {}
    bool next(TraceRecord& record) override {
        if (cursor_ == records_.size()) return false;
        record = records_[cursor_++];
        return true;
    }
    std::string description() const override { return "causal functional records"; }
    bool has_measurement_boundary() const override { return boundary_; }
    bool static_instruction_operands_complete() const override { return !operands_.empty(); }
    const StaticInstructionInfo* static_instruction(std::uint64_t pc) const override {
        const auto found = operands_.find(pc);
        return found == operands_.end() ? nullptr : &found->second;
    }
  private:
    std::vector<TraceRecord> records_;
    std::size_t cursor_ = 0;
    bool boundary_;
    std::map<std::uint64_t, StaticInstructionInfo> operands_;
};

SimulatorConfig config() {
    SimulatorConfig c;
    c.core_model = "causal_read";
    c.measurement_scope = MeasurementScope::kUser;
    c.syscall_restart_latency = 0;
    c.cores = 1;
    c.coherence = false;
    c.fetch_buffer_bytes = 0;
    c.fetch_width = c.decode_width = c.rename_width = c.dispatch_width = 4;
    c.issue_width = c.writeback_width = c.commit_width = 4;
    c.fetch_to_decode = c.decode_to_rename = c.rename_to_dispatch = 1;
    c.dispatch_to_issue = c.issue_to_execute = 1;
    c.l1d = {512, 2, 64, 2, ReplacementPolicy::kLru};
    c.l2 = {1024, 2, 64, 3, ReplacementPolicy::kLru};
    c.llc = {4096, 4, 64, 5, ReplacementPolicy::kLru};
    c.cha_count = 1;
    c.noc_one_way_latency = 2;
    c.dram.channels = 1;
    c.dram.banks_per_channel = 2;
    c.dram.ranks_per_channel = 1;
    c.dram.row_bytes = 256;
    c.dram.t_cl = 13;
    c.dram.t_rcd = 11;
    c.dram.t_rp = 7;
    c.dram.burst_cycles = 2;
    c.dram.frontend_latency = 3;
    c.dram.backend_latency = 5;
    c.chunk_instructions = 7;
    return c;
}

TraceRecord alu(std::uint32_t dependency = 0, std::int16_t op = 1) {
    TraceRecord record;
    record.pc = 0x1000;
    record.op_class = op;
    record.n_dst = 1;
    if (dependency != 0) {
        record.n_src = 1;
        record.producer_dists[0] = dependency;
    }
    return record;
}

TraceRecord load(std::uint64_t line, std::uint32_t dependency = 0) {
    auto record = alu(dependency);
    record.flags = kRetires | kLoad | kPhysicalAddress;
    record.address = line * 64;
    record.size = 8;
    record.op_class = 56;
    return record;
}

TraceRecord store(std::uint64_t line, std::uint32_t dependency = 0) {
    auto record = load(line, dependency);
    record.flags = kRetires | kStore | kPhysicalAddress;
    record.op_class = 57;
    record.n_dst = 0;
    return record;
}

struct Event {
    std::string kind;
    std::uint64_t cycle, sequence, fragment, line;
    int level;
    std::uint64_t generation, leader;
};
struct Result {
    SimulationStats stats;
    std::string audit;
    std::vector<Event> events;
    const Event& event(const std::string& kind, std::uint64_t seq,
                       int level = -2, std::uint64_t fragment = 0) const {
        for (const auto& e : events)
            if (e.kind == kind && e.sequence == seq && e.fragment == fragment &&
                (level == -2 || level == e.level)) return e;
        throw std::runtime_error("missing event " + kind + " for " + std::to_string(seq));
    }
};

Result run(SimulatorConfig c, const std::vector<TraceRecord>& records,
           bool audit_enabled = true,
           const std::map<std::uint64_t, StaticInstructionInfo>& operands = {},
           std::uint64_t warmup_macros = 0) {
    static std::uint64_t serial = 0;
    const auto directory = std::filesystem::path(FASTSIM_PROJECT_ROOT) /
        "tmp" / "fastsim-tests" / std::to_string(::getpid()) / "causal-read";
    std::filesystem::create_directories(directory);
    const auto path = directory / (std::to_string(serial++) + ".csv");
    c.causal_read_audit_path = audit_enabled ? path.string() : "";
    std::vector<std::unique_ptr<TraceSource>> sources;
    std::unique_ptr<TraceSource> source = std::make_unique<Records>(records, false, operands);
    if (warmup_macros) source = std::make_unique<WarmupInstructionTraceSource>(
        std::move(source), warmup_macros, records.size() - warmup_macros);
    sources.push_back(std::move(source));
    Simulator simulator(c, std::move(sources));
    Result result;
    result.stats = simulator.run();
    check(simulator.finished(), "Simulator must expose successful completion");
    bool repeated = false;
    try { simulator.run(); } catch (const std::logic_error&) { repeated = true; }
    check(repeated, "a consumed source cannot run a second time");
    const auto& stats = result.stats;
    check(stats.causal_read.enabled && stats.interval_steps == 0 &&
              stats.interval_accepted_uops == 0 && stats.replayed_shared_events == 0 &&
              stats.trace_worker_threads == 0,
          "Simulator must execute only the new event path");
    check(stats.cores[0].retired_uops + stats.functional_warmup_uops == records.size(),
          "retired population, including retained warmup");
    check(stats.causal_read.retire_active_cycles + stats.causal_read.retire_idle_cycles ==
              stats.cores[0].cycles + stats.causal_read.measurement_begin_cycles[0],
          "elapsed retirement accounting");
    for (const auto& cha : stats.cha)
        check(cha.llc_outcomes_conserved(), "shared-cache outcome conservation");
    if (audit_enabled) {
        std::ifstream input(path);
        result.audit.assign(std::istreambuf_iterator<char>(input), {});
        std::istringstream rows(result.audit);
        std::string line;
        std::getline(rows, line);
        std::uint64_t previous = 0;
        while (std::getline(rows, line)) {
            std::replace(line.begin(), line.end(), ',', ' ');
            std::istringstream fields(line);
            Event e;
            check(static_cast<bool>(fields >> e.kind >> e.cycle >> e.sequence >>
                  e.fragment >> e.line >> e.level >> e.generation >> e.leader),
                  "complete audit row");
            check(e.cycle >= previous, "audit must be chronological");
            previous = e.cycle;
            result.events.push_back(e);
        }
        std::map<std::string, std::map<std::uint64_t, std::uint32_t>> widths;
        std::map<std::pair<std::uint64_t, std::uint64_t>, std::uint64_t> data;
        for (const auto& e : result.events) {
            if (e.kind == "fetch" || e.kind == "decode" || e.kind == "rename" ||
                e.kind == "dispatch" || e.kind == "issue" ||
                e.kind == "writeback" || e.kind == "retire")
                ++widths[e.kind][e.cycle];
            if (e.level == 0 && (e.kind == "hit" || e.kind == "miss" || e.kind == "merge"))
                ++widths[records[e.sequence].is_write() ? "store_port" : "load_port"][e.cycle];
            if (e.kind == "data")
                check(data.emplace(std::make_pair(e.sequence, e.fragment), e.cycle).second,
                      "each source fragment consumes its data response exactly once");
        }
        const std::map<std::string, std::uint32_t> limits{
            {"fetch", c.fetch_width}, {"decode", c.decode_width},
            {"rename", c.rename_width}, {"dispatch", c.dispatch_width},
            {"issue", c.issue_width}, {"writeback", c.writeback_width},
            {"retire", c.commit_width}, {"load_port", c.cache_load_ports},
            {"store_port", c.cache_store_ports}};
        for (const auto& stage : widths)
            for (const auto& cycle : stage.second)
                check(cycle.second <= limits.at(stage.first),
                      "same-cycle pumps must preserve " + stage.first + " bandwidth");
        for (std::size_t i = 0; i != records.size(); ++i) {
            const auto issue = result.event("issue", i).cycle;
            const auto writeback = result.event("writeback", i).cycle;
            check(writeback >= result.event("completion_ready", i).cycle,
                  "WB bandwidth follows functional completion readiness");
            check(issue >= result.event("dispatch", i).cycle + c.dispatch_to_issue,
                  "dispatch-to-issue edge");
            check(result.event("retire", i).cycle >= writeback + c.execute_to_commit,
                  "completion-to-retirement edge");
            for (const auto distance : records[i].producer_dists)
                if (distance != 0)
                    check(issue >= result.event("writeback", i - distance).cycle,
                          "every functional producer must be ready before issue");
            if (records[i].is_memory() && !records[i].is_write()) {
                check(result.event("execute", i).cycle == issue + c.issue_to_execute + 1,
                      "load address generation includes the memory FU execution stage");
                const auto fragments =
                    (records[i].address + records[i].size - 1) / c.l1d.line_size -
                    records[i].address / c.l1d.line_size + 1;
                for (std::uint64_t fragment = 0; fragment != fragments; ++fragment)
                    check(writeback >= data.at({i, fragment}),
                          "load cannot write back before any required fragment");
            }
            if (records[i].is_write()) {
                check(result.event("store_send", i).cycle >= result.event("retire", i).cycle,
                      "store hierarchy requests begin only after commit");
                const auto fragments =
                    (records[i].address + records[i].size - 1) / c.l1d.line_size -
                    records[i].address / c.l1d.line_size + 1;
                for (std::uint64_t f = 0; f != fragments; ++f) {
                    const auto line = records[i].address / c.l1d.line_size + f;
                    const auto level = line < c.dram.size_bytes / c.l1d.line_size ? 0 : -1;
                    check(result.event("sq_release", i).cycle >=
                              result.event("store_response", i, level, f).cycle,
                          "SQ keeps every fragment until response");
                }
            }
        }
    }
    return result;
}

void load_execution_and_writeback_boundaries() {
    auto c = config();
    c.fetch_to_decode = c.decode_to_rename = c.rename_to_dispatch = 0;
    c.dispatch_to_issue = c.issue_to_execute = 0;
    c.minimum_load_latency = 1;
    c.l1d.hit_latency = 1;
    c.writeback_width = 1;
    // The initial three independent loads share one miss. Data is available
    // simultaneously, but each producer must win its own writeback slot.
    const auto shared = run(c, {load(0), load(0), load(0), alu(1)});
    const auto callback = shared.event("data", 0).cycle;
    for (unsigned i = 0; i != 3; ++i) {
        check(shared.event("issue", i).cycle == 0 &&
                  shared.event("execute", i).cycle == 1 &&
                  shared.event(i == 0 ? "miss" : "merge", i, 0).cycle == 1,
              "ordinary loads cross the FU stage before hit/miss admission");
        check(shared.event("data", i).cycle == callback &&
                  shared.event("completion_ready", i).cycle == callback &&
                  shared.event("writeback", i).cycle == callback + i,
              "one response serves followers, then finite WB serializes wakeups");
    }
    check(shared.event("ready", 3).cycle == callback + 2 &&
              shared.event("issue", 3).cycle >= callback + 2,
          "a consumer cannot use a returned value before its producer writes back");
    // A dependent second access is resident: issue -> execute/admit -> data,
    // one target cycle on each edge. The cold load is never latency-clamped.
    const auto hit = run(c, {load(0), load(0, 1), alu(1)});
    const auto issued = hit.event("issue", 1).cycle;
    check(hit.event("execute", 1).cycle == issued + 1 &&
              hit.event("hit", 1, 0).cycle == issued + 1 &&
              hit.event("data", 1).cycle == issued + 2 &&
              hit.event("writeback", 1).cycle == issued + 2,
          "resident load keeps execution and cache response as separate edges");
    c.issue_to_execute = 3;
    const auto delayed = run(c, {load(0), alu()});
    check(delayed.event("execute", 0).cycle == 4 &&
              delayed.event("miss", 0, 0).cycle == 4 &&
              delayed.event("completion_ready", 1).cycle == 4,
          "configured extra execution delay applies to both load and ALU paths");
}

void response_identity_and_issue_order() {
    auto c = config();
    const auto result = run(c, {load(0), load(0), alu(2), load(0, 1)});
    const auto& a = result.event("miss", 0, 0);
    const auto& b = result.event("merge", 1, 0);
    check(a.generation == b.generation && b.leader == 0, "same-line owner identity");
    check(result.event("data", 0).cycle == result.event("data", 1).cycle,
          "same generation uses one data response");
    check(result.event("issue", 2).cycle >= result.event("writeback", 0).cycle,
          "dependent ALU waits for load writeback");
    check(result.event("hit", 3, 0).cycle >= result.event("data", 0).cycle,
          "post-response access observes installed data");
    check(result.stats.cha[0].dram_reads == 1 &&
              result.stats.causal_read.miss_generations[0] == 1 &&
              result.stats.causal_read.merged_misses[0] == 1,
          "one hierarchy service, one follower, one subsequent hit");

    c.integer_divide_latency = 40;
    c.integer_alu_units = 1;
    const auto reordered = run(c, {alu(0, 3), alu(1), alu(), load(0, 3), load(1), alu(1)});
    check(reordered.event("issue", 2).cycle < reordered.event("issue", 1).cycle,
          "ready independent ALU must use the gap before its older blocked peer");
    check(reordered.event("issue", 4).cycle < reordered.event("issue", 3).cycle &&
              reordered.event("miss", 4, 0).cycle < reordered.event("miss", 3, 0).cycle,
          "memory order must follow actual issue, not the program-order clamp");
    check(reordered.event("issue", 5).cycle >= reordered.event("data", 4).cycle,
          "reordered load still owns its consumer response");
}

void capacities_and_resource_intervals() {
    auto c = config();
    c.rob_entries = 3;
    c.iq_entries = 2;
    c.lq_entries = 2;
    c.l1d_mshrs = c.l2_mshrs = c.llc_mshrs = 1;
    const std::vector<TraceRecord> records{load(0), load(1), alu(), load(2), alu(4), load(3)};
    const auto r = run(c, records);
    check(r.stats.causal_read.dispatch_blocked_cycles[0] > 0 ||
              r.stats.causal_read.dispatch_blocked_cycles[1] > 0,
          "finite core queues apply real backpressure");
    std::array<std::uint64_t, 3> areas{};
    for (std::size_t i = 0; i != records.size(); ++i) {
        const auto dispatch = r.event("dispatch", i).cycle;
        const auto retire = r.event("retire", i).cycle;
        areas[0] += retire - dispatch;
        areas[1] += r.event(records[i].is_memory() ? "writeback" : "issue", i).cycle - dispatch;
        if (records[i].is_memory()) areas[2] += retire - dispatch;
    }
    check(areas == r.stats.causal_read.queue_occupancy_cycles,
          "ROB/IQ/LQ integrals equal their independently reconstructed lifetimes");
    check(r.stats.o3[0].rob_max_occupancy <= c.rob_entries &&
              r.stats.o3[0].iq_max_occupancy <= c.iq_entries &&
              r.stats.o3[0].lq_max_occupancy <= c.lq_entries,
          "queues never over-allocate");
    std::array<std::uint64_t, 3> miss_areas{};
    std::map<std::uint64_t, Event> active;
    for (const auto& e : r.events) {
        if (e.kind == "miss") active.emplace(e.generation, e);
        if (e.kind == "fill") {
            const auto start = active.at(e.generation);
            miss_areas[static_cast<std::size_t>(e.level)] += e.cycle - start.cycle;
            active.erase(e.generation);
        }
    }
    check(active.empty() && miss_areas == r.stats.causal_read.mshr_occupancy_cycles,
          "MSHR occupancy covers exactly admission through fill");

    c = config();
    c.l1d_mshrs = 1;
    const auto bypass = run(c, {load(0), load(1, 1), load(0, 2), load(2, 3)});
    check(bypass.event("data", 2).cycle < bypass.event("data", 1).cycle,
          "resident hit bypasses the full miss table");
    check(bypass.event("capacity_wait", 3, 0).cycle < bypass.event("miss", 3, 0).cycle &&
              bypass.event("miss", 3, 0).cycle >= bypass.event("data", 1).cycle,
          "blocked miss retries at capacity release");
    check(bypass.stats.cores[0].l1d.accesses == 4,
          "capacity retries do not duplicate demand counters");
}

void completion_bandwidth_and_multiple_producers() {
    auto c = config();
    c.fetch_to_decode = c.decode_to_rename = c.rename_to_dispatch = 0;
    c.dispatch_to_issue = c.issue_to_execute = 0;
    c.fetch_width = c.decode_width = c.rename_width = c.dispatch_width = 8;
    c.issue_width = c.integer_alu_units = 8;
    c.writeback_width = c.commit_width = 1;
    c.execute_to_commit = 2;
    const auto simultaneous = run(c, std::vector<TraceRecord>(8, alu()));
    for (std::size_t i = 0; i != 8; ++i) {
        check(simultaneous.event("issue", i).cycle == 0 &&
                  simultaneous.event("writeback", i).cycle == i + 1 &&
                  simultaneous.event("retire", i).cycle == i + 3,
              "finite writeback/commit stages serialize simultaneous completed ALUs");
    }
    auto consumer = alu();
    consumer.n_src = 3;
    consumer.producer_dists = {1, 2, 2, 0};
    const auto dependencies = run(config(), {load(0), load(8), consumer});
    check(dependencies.event("issue", 2).cycle >=
              std::max(dependencies.event("writeback", 0).cycle,
                       dependencies.event("writeback", 1).cycle),
          "all distinct producers wake once even when multiple operands share a producer");
}

void exact_callback_and_fragments() {
    auto c = config();
    c.fetch_to_decode = c.decode_to_rename = c.rename_to_dispatch = 0;
    c.dispatch_to_issue = c.issue_to_execute = 0;
    const auto first = run(c, {load(0)});
    const auto callback = first.event("data", 0).cycle;
    // The dependent load issues one cycle before its FU stage/admission.
    c.integer_alu_latency = static_cast<std::uint32_t>(callback - 1);
    const auto tied = run(c, {load(0), alu(), load(0, 1)});
    check(tied.event("hit", 2, 0).cycle == callback,
          "callback precedes an admission at the same cycle");
    check(tied.stats.causal_read.merged_misses[0] == 0 &&
              tied.stats.causal_read.miss_generations[0] == 1,
          "expired generations cannot acquire same-cycle followers");

    c = config();
    c.cache_load_ports = 1;
    auto split = load(0);
    split.address = 60;
    split.size = 16;
    const auto fragmented = run(c, {split, alu(1)});
    const auto first_data = fragmented.event("data", 0, 0, 0).cycle;
    const auto second_data = fragmented.event("data", 0, 0, 1).cycle;
    check(fragmented.event("issue", 1).cycle >= std::max(first_data, second_data),
          "consumer waits for all cross-line fragments");
    check(fragmented.stats.causal_read.load_fragments == 2 &&
              fragmented.stats.cores[0].memory_uops == 1 &&
              fragmented.stats.cores[0].memory_accesses == 2,
          "UOP and fragment populations remain distinct");
    check(fragmented.event("miss", 0, 0, 1).cycle >
              fragmented.event("miss", 0, 0, 0).cycle,
          "a split request obeys per-fragment cache port bandwidth");
}

void service_changes_recompute_successors() {
    auto fast = config();
    fast.integer_alu_latency = 90;
    fast.dram.t_cl = 1;
    auto slow = fast;
    slow.dram.t_cl = 150;
    const std::vector<TraceRecord> records{
        load(0), alu(), load(0, 1), alu(3), load(1, 1)};
    const auto a = run(fast, records);
    const auto b = run(slow, records);
    check(a.event("hit", 2, 0).cycle > a.event("data", 0).cycle &&
              b.event("merge", 2, 0).generation == b.event("miss", 0, 0).generation,
          "service lifetime changes must recompute hit/follower identity");
    check(a.event("issue", 3).cycle < b.event("issue", 3).cycle &&
              a.event("issue", 4).cycle < b.event("issue", 4).cycle,
          "real response changes propagate both earlier and later through the producer chain");
}

void controller_release_and_replacement() {
    auto c = config();
    c.dram.read_buffer_size = 1;
    c.dram.min_reads_per_switch = 1;
    c.dram.frontend_latency = 17;
    c.dram.backend_latency = 19;
    const auto queued = run(c, {load(0), load(8), load(16)});
    check(queued.stats.causal_read.dram_capacity_blocks > 0 &&
              queued.stats.causal_read.max_dram_outstanding == 1,
          "controller capacity includes already scheduled, not yet data-ready reads");
    check(queued.event("dram_admit", 1).cycle == queued.event("dram_release", 0).cycle &&
              queued.event("dram_admit", 1).cycle < queued.event("data", 0).cycle,
          "controller release precedes the configured return pipeline, not CPU retire");

    c = config();
    c.l1d = {64, 1, 64, 2, ReplacementPolicy::kLru};
    c.l2 = {128, 1, 64, 3, ReplacementPolicy::kLru};
    c.llc = {256, 1, 64, 5, ReplacementPolicy::kLru};
    const auto replaced = run(c, {load(0), load(2, 1), load(0, 1)});
    check(replaced.stats.cores[0].l1d.misses == 3 &&
              replaced.stats.cores[0].l2.misses == 3 &&
              replaced.stats.llc.hits == 1 && replaced.stats.cha[0].dram_reads == 2,
          "replacement and inclusion change the next causal path");

    // Delay an L2 hit's return across a conflicting L2 fill. The data may
    // complete its load, but it cannot resurrect a line invalidated by L2.
    c.l2.hit_latency = 40;
    const auto timing = run(c, {load(0), load(1, 1), load(2, 1)});
    const auto b_ready = timing.event("writeback", 1).cycle;
    const auto c_fill = timing.event("fill", 2, 1).cycle;
    c.integer_alu_latency = static_cast<std::uint32_t>(c_fill - b_ready - 25);
    const auto invalidated = run(c, {load(0), load(1, 1), load(2, 1), alu(2),
                                     load(0, 1), load(0, 1)});
    check(invalidated.event("hit", 4, 1).cycle < invalidated.event("fill", 2, 1).cycle &&
              invalidated.event("fill_discard", 4, 0).cycle >
                  invalidated.event("fill", 2, 1).cycle,
          "test crosses an actual L2 eviction while the hit response is in flight");
    check(invalidated.stats.causal_read.discarded_l1_fills == 1 &&
              invalidated.event("miss", 5, 0).cycle >= invalidated.event("data", 4).cycle,
          "invalidated L2 return completes the load without reinstalling stale L1 data");
}

// A separate cycle-stepping oracle for all-dispatched integer DAGs. It does
// not use Solver's event queue, wake lists or core frontier computations.
void randomized_ready_queue_oracle() {
    std::mt19937 random(918731);
    for (unsigned trial = 0; trial != 40; ++trial) {
        auto c = config();
        c.fetch_to_decode = c.decode_to_rename = c.rename_to_dispatch = 0;
        c.dispatch_to_issue = c.issue_to_execute = 0;
        c.fetch_width = c.decode_width = c.rename_width = c.dispatch_width = 32;
        c.writeback_width = c.commit_width = 32;
        c.issue_width = 1 + random() % 4;
        c.integer_alu_units = 1 + random() % 3;
        c.integer_multiply_units = 1 + random() % 2;
        c.integer_alu_latency = 1 + random() % 5;
        c.integer_multiply_latency = 2 + random() % 7;
        c.integer_divide_latency = 3 + random() % 13;
        c.integer_alu_pipelined = random() % 2 != 0;
        c.integer_multiply_pipelined = random() % 2 != 0;
        std::vector<TraceRecord> records;
        for (std::uint32_t i = 0; i != 24; ++i)
            records.push_back(alu(i == 0 || random() % 3 == 0 ? 0 : 1 + random() % i,
                                  static_cast<std::int16_t>(1 + random() % 3)));
        const auto r = run(c, records);
        const auto missing = std::numeric_limits<std::uint64_t>::max();
        std::vector<std::uint64_t> issued(records.size(), missing), done(records.size(), missing);
        std::array<std::vector<std::uint64_t>, 2> lanes;
        lanes[0].resize(c.integer_alu_units);
        lanes[1].resize(c.integer_multiply_units);
        std::size_t issued_count = 0;
        for (std::uint64_t t = 0; issued_count != records.size() && t < 10000; ++t) {
            std::uint32_t budget = c.issue_width;
            for (std::size_t i = 0; i != records.size() && budget != 0; ++i) {
                if (issued[i] != missing) continue;
                const auto dependency = records[i].producer_dists[0];
                if (dependency != 0 && done[i - dependency] > t) continue;
                const auto op = records[i].op_class;
                const auto pool = op == 1 ? 0u : 1u;
                auto lane = std::find_if(lanes[pool].begin(), lanes[pool].end(),
                                         [t](auto ready) { return ready <= t; });
                if (lane == lanes[pool].end()) continue;
                const auto latency = op == 1 ? c.integer_alu_latency :
                    op == 2 ? c.integer_multiply_latency : c.integer_divide_latency;
                const auto pipelined = op == 1 ? c.integer_alu_pipelined :
                    op == 2 ? c.integer_multiply_pipelined : c.integer_divide_pipelined;
                issued[i] = t;
                done[i] = t + latency;
                *lane = t + (pipelined ? 1u : latency);
                --budget;
                ++issued_count;
            }
        }
        check(issued_count == records.size(), "oracle completes");
        for (std::size_t i = 0; i != records.size(); ++i) {
            check(r.event("issue", i).cycle == issued[i],
                  "random dependency/FU schedule differs from independent cycle oracle");
            check(r.event("writeback", i).cycle == done[i], "random completion timing");
        }
    }
}

void chunk_and_identity_invariance() {
    auto c = config();
    c.dram.channels = 2;
    c.cha_count = 2;
    std::mt19937 random(428713);
    std::vector<TraceRecord> records;
    for (std::uint32_t i = 0; i != 1600; ++i) {
        const auto dependency = i == 0 || random() % 2 == 0 ? 0u : 1 + random() % i;
        records.push_back(random() % 3 == 0 ? load(random() % 64, dependency) :
                          alu(dependency, static_cast<std::int16_t>(1 + random() % 3)));
    }
    c.chunk_instructions = 1;
    const auto baseline = run(c, records);
    c.chunk_instructions = 127;
    const auto chunked = run(c, records);
    check(baseline.audit == chunked.audit, "host chunk boundaries cannot change target events");
    check(chunked.stats.causal_read.max_live_uops <= c.rob_entries + c.fetch_queue_entries &&
              chunked.stats.causal_read.max_decode_buffer <= c.chunk_instructions,
          "active core/functional state is bounded independently of trace length");
    const auto no_audit = run(c, records, false);
    check(no_audit.stats.cores[0].cycles == baseline.stats.cores[0].cycles &&
              no_audit.stats.cores[0].l1d.misses == baseline.stats.cores[0].l1d.misses &&
              no_audit.stats.causal_read.data_callbacks == baseline.stats.causal_read.data_callbacks,
          "audit output does not change timing or cache state");
    for (auto& record : records) {
        record.pc += 0x876540;
        if (record.is_memory()) record.address += 64ull * 4096;
    }
    const auto renamed = run(c, records);
    check(renamed.stats.cores[0].cycles == baseline.stats.cores[0].cycles &&
              renamed.stats.causal_read.miss_generations == baseline.stats.causal_read.miss_generations,
          "PC and geometry-preserving address renaming cannot select timing policy");
    for (std::size_t i = 0; i != records.size(); ++i)
        check(renamed.event("issue", i).cycle == baseline.event("issue", i).cycle,
              "renamed functional identities preserve every issue time");
}

void unsupported_inputs_fail_closed() {
    const auto fails = [](SimulatorConfig c, std::vector<TraceRecord> records,
                          bool boundary = false) {
        std::vector<std::unique_ptr<TraceSource>> sources;
        sources.push_back(std::make_unique<Records>(std::move(records), boundary));
        bool rejected = false;
        try { Simulator(c, std::move(sources)).run(); }
        catch (const std::invalid_argument&) { rejected = true; }
        check(rejected, "unsupported semantics must fail the entire run");
    };
    auto c = config();
    c.inclusive_llc = true;
    fails(c, {load(0)});
    c = config(); c.dram.scheduler = "frfcfs";
    fails(c, {load(0)});
    c = config(); c.dtlb.enabled = true;
    fails(c, {load(0)});
    c = config(); c.memory_exposure = 0.5;
    fails(c, {load(0)});
    auto bad = load(0); bad.flags = bad.flags | kStore;
    fails(config(), {alu(), bad});
    bad = alu(); bad.flags = bad.flags | kBranch;
    fails(config(), {bad});
    bad = alu(); bad.n_src = 5;
    fails(config(), {bad});
    fails(config(), {alu(1)});
    bad = load(0); bad.address = std::numeric_limits<std::uint64_t>::max() - 3;
    fails(config(), {bad});
    fails(config(), {load(0)}, true);
    check(run(config(), {}).stats.cores[0].cycles == 0, "empty input has no target cycles");
}

void mixed_fu_and_branch_dependencies() {
    auto c = config();
    c.float_complex_units = 1;
    c.float_divide_latency = 23;
    c.float_multiply_latency = 7;
    c.simd_latency = 3;
    const auto r = run(c, {alu(0, 9), alu(0, 7), alu(0, 12), load(0),
                           alu(1, 8), alu(0, 51), alu(0, 87)});
    check(r.event("issue", 1).cycle >= r.event("issue", 0).cycle + 23,
          "FP divide holds the shared complex FU for its nonpipelined occupancy");
    check(r.event("issue", 2).cycle < r.event("issue", 1).cycle,
          "independent SIMD bypasses a busy floating point pool");
    check(r.event("writeback", 1).cycle == r.event("issue", 1).cycle +
              c.issue_to_execute + 7, "FP multiply uses configured latency");
    check(r.event("issue", 4).cycle >= r.event("writeback", 3).cycle,
          "load-to-FMA dependency is woken by the actual response");

    c.branch.speculative_history = true;
    auto branch = alu(1);
    branch.flags = kRetires | kBranch | kConditional | kTaken | kBranchOutcomeValid;
    branch.target = branch.next_pc = 0x2000;
    auto not_taken = branch;
    not_taken.flags &= ~static_cast<std::uint16_t>(kTaken);
    not_taken.n_src = 0;
    not_taken.producer_dists.fill(0);
    not_taken.next_pc = not_taken.pc + 4;
    auto second_branch = not_taken;
    second_branch.pc += 4;
    second_branch.next_pc += 4;
    const auto concurrent = run(c, {load(0), not_taken, second_branch, alu()});
    check(concurrent.stats.cores[0].branch.misses == 0 &&
              concurrent.event("fetch", 2).cycle < concurrent.event("retire", 1).cycle,
          "multiple live branch checkpoints train by identity at their own ordered retirement");
    const auto slow = run(c, {load(0), branch, alu()});
    check(slow.stats.cores[0].branch.misses == 1, "cold taken branch exercises recovery");
    check(slow.event("fetch", 2).cycle == slow.event("writeback", 1).cycle +
              c.branch.mispredict_penalty,
          "correct-path fetch resumes from the dependent branch completion");
    c.dram.t_cl += 30;
    const auto slower = run(c, {load(0), branch, alu()});
    check(slower.event("fetch", 2).cycle - slow.event("fetch", 2).cycle ==
              slower.event("data", 0).cycle - slow.event("data", 0).cycle,
          "load response shifts branch recovery without an added CPI correction");

    c.integer_divide_latency = 40;
    auto first = alu(0, 3);
    auto last = alu();
    first.flags = kRetires | kMicroOp;
    last.flags = kRetires | kMicroOp | kLastMicroOp;
    auto truncated = branch;
    truncated.pc = 0x1010;
    truncated.n_src = 6;
    truncated.producer_dists.fill(0);
    StaticInstructionInfo writer, reader;
    writer.pc = first.pc;
    writer.operand_semantics_valid = true;
    writer.write_register_mask[0] = 1;
    reader.pc = truncated.pc;
    reader.operand_semantics_valid = true;
    reader.read_register_mask[0] = 1;
    const auto repaired = run(c, {first, last, truncated}, true,
                              {{writer.pc, writer}, {reader.pc, reader}});
    check(repaired.stats.causal_read.operand_completed_uops == 1 &&
              repaired.event("issue", 2).cycle >= repaired.event("writeback", 0).cycle &&
              repaired.event("writeback", 0).cycle > repaired.event("writeback", 1).cycle,
          "truncated operands wait for all live producing macro members, not just its last Uop");
}

void store_lifecycle_forwarding_and_dirty_eviction() {
    auto c = config();
    c.sq_entries = 1;
    const auto r = run(c, {store(0), alu(), store(1), load(1)});
    check(r.event("retire", 0).cycle < r.event("store_response", 0).cycle,
          "store ROB retirement is independent of its cache response");
    check(r.event("dispatch", 2).cycle >= r.event("sq_release", 0).cycle,
          "full SQ blocks dispatch until the owning response");
    check(r.event("store_send", 2).cycle >= r.event("store_response", 0).cycle,
          "TSO prevents a second store in flight");
    std::uint64_t area = 0;
    for (auto seq : {0u, 2u})
        area += r.event("sq_release", seq).cycle - r.event("dispatch", seq).cycle;
    check(area == r.stats.causal_read.sq_occupancy_cycles &&
              r.stats.causal_read.sq_dispatch_blocked_cycles > 0,
          "SQ occupancy integrates dispatch through response, including after commit");

    c = config();
    c.integer_divide_latency = 60;
    auto full = store(0); full.size = 16;
    const auto f = run(c, {alu(0, 3), full, load(0), load(1)});
    check(f.stats.causal_read.forwarded_loads == 1 &&
              f.event("store_forward", 2).leader == 1 &&
              f.event("data", 2).cycle < f.event("retire", 1).cycle,
          "executed unsent store forwards before its ROB commit");
    check(std::none_of(f.events.begin(), f.events.end(), [](const auto& e) {
              return e.sequence == 2 && e.kind == "miss";
          }), "store-forwarded load generates no cache request");
    auto partial = store(0); partial.size = 4;
    const auto p = run(c, {alu(0, 3), partial, load(0)});
    check(p.stats.causal_read.forwarded_loads == 0 &&
              p.event("load_order_wait", 2).cycle < p.event("store_send", 1).cycle &&
              p.event("merge", 2, 0).cycle >= p.event("store_send", 1).cycle,
          "partial coverage waits, then rechecks the hierarchy after store send");
    const auto unknown = run(c, {alu(0, 3), store(0, 1), load(1)});
    check(unknown.event("miss", 2, 0).cycle >= unknown.event("execute", 1).cycle,
          "future functional store addresses cannot bypass unresolved older stores");

    c = config();
    c.sq_entries = 2;
    c.cache_store_ports = 1;
    auto split = store(0); split.address = 60; split.size = 16;
    const auto s = run(c, {split, store(2)});
    check(s.event("miss", 0, 0, 1).cycle > s.event("miss", 0, 0, 0).cycle &&
              s.event("store_send", 1).cycle >= s.event("sq_release", 0).cycle,
          "split stores respect per-fragment ports and the complete TSO transaction");

    c = config();
    c.l1d = {64, 1, 64, 2, ReplacementPolicy::kLru};
    c.l2 = {128, 2, 64, 3, ReplacementPolicy::kLru};
    c.llc = {128, 2, 64, 5, ReplacementPolicy::kLru};
    c.sq_entries = 1;
    std::vector<TraceRecord> stores;
    for (unsigned line = 0; line != 8; ++line) stores.push_back(store(line));
    const auto d = run(c, stores);
    check(d.stats.causal_read.dirty_writebacks[0] > 0 &&
              d.stats.causal_read.dirty_writebacks[1] > 0 &&
              d.stats.causal_read.dirty_writebacks[2] > 0 &&
              d.stats.dram_write_queue_enqueues == d.stats.cha[0].dram_writes,
          "dirty data survives private evictions and reaches the DRAM write queue");
    check(d.stats.causal_read.store_callbacks == 8 &&
              d.stats.causal_read.drained_cycle > d.stats.cores[0].cycles,
          "trailing store responses drain after the last architectural retirement");
}

void multicore_sharing_coherence_and_boundary() {
    struct Case {
        SimulationStats stats;
        std::vector<CausalReadEvent> events;
        std::vector<std::pair<std::uint32_t, std::uint64_t>> services;
        const CausalReadEvent& at(const std::string& kind, unsigned core,
                                  std::uint64_t seq, int level = -2) const {
            for (const auto& e : events)
                if (kind == e.kind && e.core == core && e.sequence == seq &&
                    (level == -2 || e.level == level)) return e;
            throw std::runtime_error("missing multicore event " + kind);
        }
    };
    const auto execute = [](SimulatorConfig c,
                            const std::vector<std::vector<TraceRecord>>& records,
                            bool warmup = false) {
        c.cores = static_cast<std::uint32_t>(records.size());
        std::vector<std::unique_ptr<TraceSource>> owned;
        std::vector<TraceSource*> sources;
        for (const auto& stream : records) {
            std::unique_ptr<TraceSource> source = std::make_unique<Records>(stream);
            if (warmup) source = std::make_unique<WarmupInstructionTraceSource>(
                std::move(source), 1, stream.size() - 1);
            sources.push_back(source.get());
            owned.push_back(std::move(source));
        }
        Case result;
        result.stats.cores.resize(c.cores);
        result.stats.o3.resize(c.cores);
        result.stats.cha.resize(c.cha_count);
        run_causal_multicore(c, sources, result.stats,
            [&](auto arrival, auto, auto core, auto sequence, auto) {
                result.services.emplace_back(core, sequence);
                return CausalReadDramResponse{arrival, arrival + 40, arrival + 45, 0};
            }, [&](const auto& e) { result.events.push_back(e); }, [](auto, auto) {});
        return result;
    };

    auto c = config();
    c.integer_divide_latency = 300;
    const auto shared = execute(c, {{load(0), alu(1), alu(0, 3), load(1, 1)},
                                    {load(0), alu(1), load(2)}});
    check(shared.services.size() == 3 && shared.stats.causal_read.merged_misses[2] == 1 &&
              shared.stats.causal_read.max_dram_outstanding != 0,
          "two private caches share one actual LLC generation and DRAM request");
    check(shared.at("dram_admit", 1, 2).cycle < shared.at("dram_admit", 0, 3).cycle,
          "another core's ready request precedes a dependency-blocked core");
    for (unsigned core = 0; core != 2; ++core)
        check(shared.at("issue", core, 1).cycle >= shared.at("writeback", core, 0).cycle,
              "shared response wakes the correct core-local producer");

    c.coherence = true;
    const auto coherent = execute(c, {{store(0), alu(0, 3), alu(1, 3), load(0, 1)},
                                      {alu(0, 3), load(0, 1), store(0, 1)}});
    check(coherent.services.size() == 1 &&
              coherent.stats.causal_read.coherence_data_transfers >= 2 &&
              coherent.stats.causal_read.coherence_invalidations >= 1,
          "dirty peer data supplies readers; a writer invalidates shared copies");
    check(coherent.at("coherence_invalidate", 1, 2).leader_core == 0 &&
              coherent.at("miss", 0, 3, 0).cycle >
                  coherent.at("coherence_invalidate", 1, 2).cycle,
          "a later peer load cannot hit its invalidated private copy");
    std::map<std::uint64_t, std::pair<unsigned, bool>> leases;
    for (const auto& e : coherent.events) {
        if (std::string(e.kind) == "coherence_acquire") {
            const bool write = (e.core == 0 && e.sequence == 0) || (e.core == 1 && e.sequence == 2);
            auto& state = leases[e.line];
            check(!state.second && (!write || state.first == 0), "exclusive coherence permission");
            ++state.first; state.second = write;
        } else if (std::string(e.kind) == "coherence_release") {
            auto& state = leases.at(e.line);
            check(state.first != 0, "unique coherence release");
            if (--state.first == 0) state.second = false;
        }
    }
    for (const auto& entry : leases) check(entry.second.first == 0, "coherence leases drain");

    // Source-derived MESI_Three_Level/SimpleNetwork edges. The independent
    // service above returns 45 cycles after admission; no gem5 label times
    // or scheduler implementation helpers supply these expected boundaries.
    auto stages = c;
    stages.fetch_to_decode = stages.decode_to_rename = stages.rename_to_dispatch = 0;
    stages.dispatch_to_issue = stages.issue_to_execute = 0;
    stages.l1d.hit_latency = 1;
    stages.l1d.miss_request_latency = 3;
    stages.l2.hit_latency = 2;
    stages.l2.miss_request_latency = 2;
    stages.l2.fill_response_latency = 2;
    stages.llc.hit_latency = 2;
    stages.llc.miss_request_latency = 2;
    stages.llc.fill_response_latency = 1;
    stages.noc_one_way_latency = 4;
    stages.directory_memory_latency = 5;
    stages.llc_fill_response_latency = 6;
    stages.coherence_peer_response_latency = 2;
    const auto cold = execute(stages, {{load(0), alu(1)}});
    check(cold.at("miss", 0, 0, 0).cycle == cold.at("execute", 0, 0).cycle &&
              cold.at("dram_admit", 0, 0).cycle - cold.at("execute", 0, 0).cycle == 16 &&
              cold.at("data", 0, 0).cycle - cold.at("execute", 0, 0).cycle == 29 + 45,
          "cold read has stage-owned messages, without a pre-L1 coherence round trip");
    check(cold.at("fill", 0, 0, 1).cycle - cold.at("fill", 0, 0, 2).cycle == 5 &&
              cold.at("fill", 0, 0, 0).cycle - cold.at("fill", 0, 0, 1).cycle == 2,
          "each upper generation waits for its response message arrival");
    auto delayed = stages;
    delayed.l2.fill_response_latency = 9;
    const auto late = execute(delayed, {{load(0), alu(1)}});
    check(late.at("dram_admit", 0, 0).cycle == cold.at("dram_admit", 0, 0).cycle &&
              late.at("fill", 0, 0, 1).cycle == cold.at("fill", 0, 0, 1).cycle &&
              late.at("data", 0, 0).cycle == cold.at("data", 0, 0).cycle + 7 &&
              late.at("issue", 0, 1).cycle == cold.at("issue", 0, 1).cycle + 7,
          "changing only return service moves upper visibility and consumers, not lower admission");
    auto follower = stages;
    follower.integer_alu_latency = 73;
    const auto merged = execute(follower, {{load(0), alu(), load(0, 1)}});
    check(merged.at("merge", 0, 2, 0).cycle > merged.at("fill", 0, 0, 1).cycle &&
              merged.at("merge", 0, 2, 0).cycle < merged.at("fill", 0, 0, 0).cycle &&
              merged.services.size() == 1,
          "a load arriving during private return flight merges in the still-live L1 MSHR");
    std::array<std::uint64_t, 3> staged_areas{};
    std::map<std::uint64_t, CausalReadEvent> staged_pending;
    for (const auto& e : merged.events) {
        if (std::string(e.kind) == "miss") staged_pending.emplace(e.generation, e);
        if (std::string(e.kind) == "fill") {
            staged_areas.at(e.level) += e.cycle - staged_pending.at(e.generation).cycle;
            staged_pending.erase(e.generation);
        }
    }
    check(staged_pending.empty() && staged_areas == merged.stats.causal_read.mshr_occupancy_cycles,
          "independent generation lifetimes cover the separated fill-return events");

    const auto exclusive = execute(stages, {{load(0), store(0, 1)}});
    check(exclusive.services.size() == 1 && exclusive.stats.causal_read.directory_requests == 1 &&
              exclusive.stats.causal_read.permission_upgrades == 0 &&
              exclusive.stats.causal_read.exclusive_store_hits == 1 &&
              exclusive.at("store_response", 0, 1).cycle - exclusive.at("hit", 0, 1, 0).cycle == 1,
          "unshared read fills E and the following local E-to-M store needs no directory request");
    exclusive.at("permission_exclusive", 0, 0, 0);

    const auto upgrade = execute(stages,
        {{load(0), alu(1, 3), store(0, 1)},
         {load(0), alu(1, 3), alu(1, 3), load(0, 1)}});
    check(upgrade.services.size() == 1 && upgrade.stats.causal_read.permission_upgrades == 1 &&
              upgrade.stats.causal_read.coherence_invalidations == 1 &&
              upgrade.at("permission_request", 0, 2, 0).cycle <
                  upgrade.at("coherence_invalidate", 0, 2).cycle &&
              upgrade.at("coherence_invalidate", 0, 2).cycle <
                  upgrade.at("store_response", 0, 2).cycle &&
              upgrade.at("miss", 1, 3, 0).cycle > upgrade.at("store_response", 0, 2).cycle,
          "shared-to-modified upgrade invalidates its peer and returns permission without a DRAM read");
    upgrade.at("permission_shared", 0, 0, 0);
    upgrade.at("permission_shared", 1, 0, 0);
    for (const auto& cha : upgrade.stats.cha)
        check(cha.llc_outcomes_conserved(), "permission upgrade is a distinct shared outcome");
    // Separate first arrival by one executed ALU: independent cross-core
    // requests in the same cycle do not have an inherent reader-first order.
    const auto inflight = execute(delayed, {{load(0)}, {alu(), store(0, 1)}});
    check(inflight.at("retire", 1, 1).cycle < inflight.at("data", 0, 0).cycle &&
              inflight.at("coherence_acquire", 1, 1).cycle >= inflight.at("data", 0, 0).cycle &&
              inflight.at("coherence_invalidate", 1, 1).cycle > inflight.at("fill", 0, 0, 0).cycle,
          "line ordering keeps a writer from invalidating a still-returning older read fill");

    auto owner_config = stages;
    owner_config.l1d.size_bytes = 64;
    owner_config.l1d.associativity = 1;
    const std::vector<TraceRecord> owner_stream{store(0), alu(1, 3), load(1, 1), load(0, 1)};
    const auto owner_only = execute(owner_config, {owner_stream});
    owner_config.integer_alu_latency = static_cast<std::uint32_t>(
        owner_only.at("hit", 0, 3, 1).cycle);
    const auto owner_overlap = execute(owner_config, {owner_stream, {alu(), load(0, 1)}});
    check(owner_overlap.at("coherence_acquire", 1, 1).cycle >
                  owner_overlap.at("hit", 0, 3, 1).cycle &&
              owner_overlap.at("coherence_acquire", 1, 1).cycle <
                  owner_overlap.at("fill", 0, 3, 0).cycle &&
              owner_overlap.stats.causal_read.coherence_data_transfers == 1,
          "a peer arriving during a dirty owner's local return cannot erase ownership before the real snoop");
    owner_overlap.at("coherence_downgrade", 1, 1);

    const std::vector<std::vector<TraceRecord>> records{
        {store(0), alu(), load(0), store(1), alu()},
        {load(2), alu(1), load(0), alu(1)}};
    const auto flat = execute(c, records);
    const auto phased = execute(c, records, true);
    check(flat.events.size() == phased.events.size(), "boundary creates no drain events");
    for (std::size_t i = 0; i != flat.events.size(); ++i) {
        const auto& a = flat.events[i]; const auto& b = phased.events[i];
        check(std::string(a.kind) == b.kind && a.cycle == b.cycle && a.core == b.core &&
                  a.sequence == b.sequence && a.line == b.line && a.generation == b.generation,
              "functional boundary must preserve the whole event timeline");
    }
    check(phased.stats.functional_warmup_enabled && phased.stats.functional_warmup_uops == 2 &&
              phased.at("store_response", 0, 0).cycle > phased.at("retire", 0, 0).cycle &&
              phased.at("fetch", 0, 1).cycle < phased.at("store_response", 0, 0).cycle,
          "warmup store/SQ remains live while measured instructions proceed");
    for (unsigned core = 0; core != 2; ++core)
        check(phased.stats.cores[core].cycles == flat.stats.cores[core].cycles -
                  phased.at("retire", core, 0).cycle &&
                  phased.stats.cores[core].retired_uops == records[core].size() - 1,
              "measurement cuts counts and elapsed cycles without resetting state");
}

void native_kernel_and_serialization() {
    auto c = config();
    auto serial = alu();
    serial.flags |= kSerialize;
    auto branch = alu(1);
    branch.flags |= kBranch | kConditional | kTaken | kBranchOutcomeValid;
    branch.target = 0x2000;
    std::vector<TraceRecord> records{store(0), load(1), serial, alu(1), branch};
    const auto user = run(c, records);
    check(user.event("issue", 2).cycle >= user.event("sq_release", 0).cycle &&
              user.event("issue", 2).cycle >= user.event("retire", 1).cycle &&
              user.event("fetch", 3).cycle > user.event("retire", 2).cycle,
          "serialization drains older ROB/SQ work and gates younger fetch");
    for (auto& record : records) record.set_kernel_mode(true);
    c.native_kernel_trace = true;
    c.measurement_scope = MeasurementScope::kUserPlusKernel;
    const auto kernel = run(c, records);
    const auto& counters = kernel.stats.cores[0];
    check(user.audit == kernel.audit,
          "privilege tags retain canonical operation and dependency timing");
    check(counters.native_kernel_records == records.size() &&
              counters.native_kernel_retired_uops == counters.retired_uops &&
              counters.native_kernel_retired_instructions == counters.retired_instructions &&
              counters.native_kernel_memory_uops == 2 &&
              counters.native_kernel_memory_accesses == 2 && counters.serializing_uops == 1 &&
              counters.native_kernel_branch.branches == counters.branch.branches &&
              counters.branch.branches == 1 &&
              kernel.stats.threads[0].native_kernel_retired_uops == records.size(),
          "kernel counts are a conserved subset of aggregate core/thread populations");
    c.native_kernel_trace = false;
    bool rejected = false;
    try { run(c, records); } catch (const std::invalid_argument&) { rejected = true; }
    check(rejected, "kernel data cannot silently enter a user-only configuration");
}

void simulator_multicore_bindings() {
    auto c = config();
    c.cores = 4;
    std::vector<ThreadTraceBinding> bindings;
    for (unsigned index = 0; index != c.cores; ++index) {
        const auto core = c.cores - 1 - index;
        ThreadTraceBinding binding;
        binding.thread_id = 10 + core;
        binding.initial_core = core;
        binding.trace = std::make_unique<Records>(std::vector<TraceRecord>{load(core)});
        bindings.push_back(std::move(binding));
    }
    Simulator simulator(c, std::move(bindings));
    const auto stats = simulator.run();
    for (unsigned core = 0; core != c.cores; ++core)
        check(stats.threads[core].thread_id == 10 + core &&
                  stats.threads[core].initial_core == core &&
                  stats.threads[core].final_core == core &&
                  stats.threads[core].cycles == stats.cores[core].cycles &&
                  stats.cores[core].retired_uops == 1,
              "Simulator preserves each core/thread binding independently of input order");
}

void existing_mmio_escape_lifecycle() {
    auto c = config();
    c.allow_mmio_escape = true;
    c.coherence = true;
    c.native_kernel_trace = true;
    c.measurement_scope = MeasurementScope::kUserPlusKernel;
    c.sq_entries = 2;
    c.integer_divide_latency = 60;
    auto escaped_store = store(0);
    escaped_store.address = c.dram.size_bytes + 188;
    escaped_store.size = 16; // Two non-RAM line fragments.
    auto escaped_load = load(0);
    escaped_load.address = c.dram.size_bytes + 192;
    std::vector<TraceRecord> records{
        alu(0, 3), store(0), escaped_store, escaped_load, alu(1), store(1)};
    for (auto& record : records) record.set_kernel_mode(true);
    const auto result = run(c, records);
    const auto& counters = result.stats.cores[0];
    check(counters.memory_uops == 4 && counters.native_kernel_memory_uops == 4 &&
              counters.memory_accesses == 2 && counters.native_kernel_memory_accesses == 2 &&
              counters.mmio_escape_accesses == 3,
          "escape retains native memory UOPs but separates non-RAM line counts");
    check(result.event("store_send", 2).cycle >= result.event("sq_release", 1).cycle &&
              result.event("store_send", 5).cycle >= result.event("sq_release", 2).cycle &&
              result.event("sq_release", 2).cycle >= result.event("retire", 2).cycle,
          "escape stores preserve committed SQ lifetime and both TSO boundaries");
    check(result.stats.causal_read.forwarded_loads == 0 &&
              result.event("data", 3, -1).cycle >= result.event("store_send", 2).cycle &&
              result.event("issue", 4).cycle >= result.event("writeback", 3).cycle &&
              result.event("writeback", 3).cycle >=
                  result.event("issue", 3).cycle + c.issue_to_execute + c.minimum_load_latency,
          "escape loads retain ordering, minimum core latency and dependency wakeup");
    for (const auto& event : result.events)
        if (event.sequence == 2 || event.sequence == 3)
            check(event.level == -1, "escape must never reach a cache, coherence or DRAM service");
    check(result.stats.causal_read.coherence_transactions == 2 &&
              result.stats.cha[0].dram_reads == 2 && result.stats.cha[0].dram_writes == 0,
          "only the two RAM stores create shared transactions");
    std::array<std::uint64_t, 3> areas{};
    std::uint64_t sq_area = 0;
    for (unsigned seq = 0; seq != records.size(); ++seq) {
        const auto dispatch = result.event("dispatch", seq).cycle;
        areas[0] += result.event("retire", seq).cycle - dispatch;
        areas[1] += result.event(records[seq].is_memory() ? "writeback" : "issue", seq).cycle - dispatch;
        if (records[seq].is_write()) sq_area += result.event("sq_release", seq).cycle - dispatch;
        else if (records[seq].is_memory()) areas[2] += result.event("retire", seq).cycle - dispatch;
    }
    check(areas == result.stats.causal_read.queue_occupancy_cycles &&
              sq_area == result.stats.causal_read.sq_occupancy_cycles,
          "all ROB/IQ/LQ/SQ lifetimes, including escape, are conserved");
    c.chunk_instructions = 1;
    const auto phased = run(c, records, true, {}, 3);
    check(phased.events.size() == result.events.size(), "escape warmup creates no extra events");
    for (unsigned i = 0; i != result.events.size(); ++i) {
        const auto& a = result.events[i]; const auto& b = phased.events[i];
        check(a.kind == b.kind && a.cycle == b.cycle && a.sequence == b.sequence &&
                  a.fragment == b.fragment && a.line == b.line && a.level == b.level,
              "warmup and chunk size preserve escape event identity and timing");
    }
    check(phased.stats.cores[0].mmio_escape_accesses == 1 &&
              phased.stats.cores[0].memory_accesses == 1 &&
              phased.stats.functional_warmup_uops == 3,
          "escape statistics respect the same measurement boundary as RAM accesses");

    c = config();
    const auto ram = run(c, {store(0), load(0), alu(1)});
    c.allow_mmio_escape = true;
    check(ram.audit == run(c, {store(0), load(0), alu(1)}).audit,
          "enabling escape leaves an all-RAM timeline unchanged");
    auto mixed = load(0); mixed.address = c.dram.size_bytes - 4;
    auto top_store = store(0); top_store.address = std::numeric_limits<std::uint64_t>::max();
    top_store.size = 1;
    auto top_load = top_store; top_load.flags = kRetires | kLoad | kPhysicalAddress;
    top_load.op_class = 56; top_load.n_dst = 1;
    const auto boundary = run(c, {mixed, alu(1), top_store, top_load});
    check(boundary.stats.cores[0].memory_accesses == 1 &&
              boundary.stats.cores[0].mmio_escape_accesses == 3 &&
              boundary.event("data", 0, -1, 1).cycle ==
                  boundary.event("issue", 0).cycle + c.issue_to_execute + c.minimum_load_latency,
          "RAM boundary splits per line and the final uint64 byte does not wrap");
    const auto rejects = [](SimulatorConfig cfg, const TraceRecord& record) {
        bool rejected = false;
        try { run(cfg, {record}); } catch (const std::invalid_argument&) { rejected = true; }
        check(rejected, "escape must not weaken physical-range validation");
    };
    rejects(config(), escaped_load);
    rejects(config(), mixed);
    auto invalid = escaped_load; invalid.size = 0; rejects(c, invalid);
    invalid = escaped_load; invalid.flags &= ~kPhysicalAddress; rejects(c, invalid);
    invalid = top_load; invalid.size = 2; rejects(c, invalid);
}

void complete_dynamic_dependency_companions() {
    const auto directory = std::filesystem::path(FASTSIM_PROJECT_ROOT) /
        "tmp" / "fastsim-tests" / std::to_string(::getpid()) / "dependencies";
    std::filesystem::create_directories(directory);
    for (std::uint32_t fanin : {8u, 16u, 20u}) {
        const auto path = directory / (std::to_string(fanin) + ".fst");
        {
            BinaryTraceWriter writer(path.string(), 0);
            writer.enable_complete_dependencies();
            writer.append(alu(0, 3)); // The oldest producer is deliberately slow.
            for (unsigned i = 1; i != fanin; ++i) writer.append(alu());
            auto consumer = alu();
            consumer.n_src = static_cast<std::uint8_t>(fanin);
            consumer.producer_dists = {1, 2, 3, 4};
            std::vector<std::uint32_t> extra;
            for (unsigned distance = 5; distance <= fanin; ++distance) extra.push_back(distance);
            writer.append(consumer, nullptr, extra);
            auto shared_writer = alu(1);
            shared_writer.n_src = 7; // Seven operands, one dynamic producer.
            writer.append(shared_writer);
            writer.close();
        }
        check(std::filesystem::file_size(path) == 72 + 64 * (fanin + 2) &&
                  std::filesystem::file_size(path.string() + ".deps") == 48 + 16 + 4 * (fanin - 4),
              "only overflow records pay for sparse dependency storage");
        std::vector<CausalReadEvent> baseline;
        for (unsigned chunk : {1u, 64u}) {
            auto c = config(); c.chunk_instructions = chunk; c.integer_divide_latency = 200;
            WarmupInstructionTraceSource source(std::make_unique<BinaryTraceSource>(path.string()),
                                                1, fanin + 1);
            check(source.complete_dependencies() && source.has_dependency_extensions(),
                  "warmup wrapper preserves the dependency capability");
            SimulationStats stats;
            stats.cores.resize(1); stats.o3.resize(1); stats.cha.resize(c.cha_count);
            std::vector<CausalReadEvent> events;
            run_causal_read(c, source, stats, [](auto at, auto, auto, auto) {
                return CausalReadDramResponse{at, at + 10, at + 10, 0};
            }, [&](const auto& event) { events.push_back(event); });
            const auto cycle = [&](const std::string& kind, unsigned seq) {
                const auto found = std::find_if(events.begin(), events.end(), [&](const auto& e) {
                    return kind == e.kind && e.sequence == seq;
                });
                check(found != events.end(), "dependency event exists");
                return found->cycle;
            };
            check(cycle("issue", fanin) >= cycle("writeback", 0) &&
                      cycle("issue", fanin + 1) >= cycle("writeback", fanin),
                  "consumer waits for the slow producer beyond the fourth/sixteenth slot");
            check(stats.causal_read.complete_dependency_uops == fanin + 2 &&
                      stats.causal_read.extended_dependency_uops == 1 &&
                      stats.causal_read.extended_dependency_edges == fanin - 4 &&
                      stats.causal_read.operand_completed_uops == 0 &&
                      stats.functional_warmup_uops == 1 && stats.cores[0].retired_uops == fanin + 1,
                  "complete edges cross the boundary without static-map reconstruction");
            if (baseline.empty()) baseline = events;
            else {
                check(baseline.size() == events.size(), "chunking preserves event population");
                for (std::size_t i = 0; i != events.size(); ++i)
                    check(std::string(events[i].kind) == baseline[i].kind &&
                              events[i].cycle == baseline[i].cycle &&
                              events[i].sequence == baseline[i].sequence &&
                              events[i].measured == baseline[i].measured,
                          "lookahead owns each record's extension independently");
            }
        }
        InstructionSliceTraceSource slice(std::make_unique<BinaryTraceSource>(path.string()), 0, fanin + 2);
        check(slice.complete_dependencies(), "slice propagates complete dependencies");
        TraceRecord record;
        for (unsigned i = 0; i <= fanin; ++i) check(slice.next(record), "slice record exists");
        check(slice.current_dependency_extensions().size() == fanin - 4,
              "slice retains extended edge ordinals");
        if (fanin == 8) {
            const auto rewritten = directory / "rewritten.fst";
            upgrade_binary_trace_to_v7(path.string(), rewritten.string(), SyscallAbi::kUnknown);
            BinaryTraceSource copy(rewritten.string());
            check(copy.complete_dependencies(), "rewrite retains completeness declaration");
            for (unsigned i = 0; i <= fanin; ++i) check(copy.next(record), "rewritten record exists");
            check(copy.current_dependency_extensions() == slice.current_dependency_extensions(),
                  "FST rewrite preserves extended dependency identities");
        }
        // A declared table must exist, and a row must match the exact hot record.
        const auto companion = path.string() + ".deps";
        std::filesystem::rename(companion, companion + ".saved");
        bool rejected = false;
        try { BinaryTraceSource missing(path.string()); } catch (const std::runtime_error&) { rejected = true; }
        check(rejected, "missing declared dependency table fails closed");
        std::filesystem::rename(companion + ".saved", companion);
        {
            std::fstream corrupt(companion, std::ios::in | std::ios::out | std::ios::binary);
            corrupt.seekp(48 + 12);
            const std::uint32_t hash = 0;
            corrupt.write(reinterpret_cast<const char*>(&hash), sizeof(hash));
        }
        rejected = false;
        try {
            BinaryTraceSource mismatched(path.string());
            while (mismatched.next(record)) {}
        } catch (const std::runtime_error&) { rejected = true; }
        check(rejected, "mismatched hot record and dependency extension fails closed");
    }
}

}  // namespace

void test_causal_read() {
    load_execution_and_writeback_boundaries();
    existing_mmio_escape_lifecycle();
    complete_dynamic_dependency_companions();
    simulator_multicore_bindings();
    native_kernel_and_serialization();
    multicore_sharing_coherence_and_boundary();
    mixed_fu_and_branch_dependencies();
    store_lifecycle_forwarding_and_dirty_eviction();
    response_identity_and_issue_order();
    capacities_and_resource_intervals();
    completion_bandwidth_and_multiple_producers();
    exact_callback_and_fragments();
    service_changes_recompute_successors();
    controller_release_and_replacement();
    randomized_ready_queue_oracle();
    chunk_and_identity_invariance();
    unsupported_inputs_fail_closed();
}
