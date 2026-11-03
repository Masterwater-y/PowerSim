#include "fastsim/interval_core.hpp"
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
    std::unique_ptr<TraceSource> trace = std::make_unique<BinaryTraceSource>(path.string());
    if (warmup) trace = std::make_unique<WarmupInstructionTraceSource>(std::move(trace), 1, 511);
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.push_back(std::move(trace));
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
