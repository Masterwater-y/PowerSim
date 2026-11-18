#include "fastsim/interval_core.hpp"
#include "fastsim/simulator.hpp"
#include <algorithm>
#include <memory>
#include <map>
#include <stdexcept>

namespace {
using namespace fastsim;
void require(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }
class RenameTrace final : public TraceSource {
  public:
    std::vector<TraceRecord> records;
    std::vector<unsigned> kinds;
    fst::StaticUopFactsRow facts;
    fst::RetiredFrontendMacro macro;
    std::vector<fst::StaticUopOperand> operands;
    fst::CoreFunctionalRow core;
    std::vector<fst::CoreFunctionalOperand> core_operands;
    bool core_available = false;
    bool available = true;
    mutable unsigned policy_reads = 0;
    unsigned semantics_version = 1;
    unsigned cursor = 0;
    bool next(TraceRecord& record) override {
        if (cursor == records.size()) return false;
        record = records[cursor];
        facts = {}; facts.record_ordinal = cursor;
        facts.flags = fst::kStaticExecutionKnown | fst::kStaticOperandRolesKnown;
        facts.destination_count = record.n_dst;
        macro = {}; macro.begin_record = cursor; macro.end_record = cursor + 1;
        macro.pc = record.pc; macro.observed_uops = 1;
        macro.known_flags = fst::kFrontendRequiredKnown;
        macro.flags = fst::kFrontendComplete;
        macro.instruction_size = 3;
        const auto kind = kinds[cursor++];
        if (kind == 1) macro.bytes = {0x48, 0x89, 0xc8};
        else if (kind == 2) macro.bytes = {0x0f, 0x28, 0xc1};
        else if (kind == 3) { macro.bytes = {0x31, 0xc0}; macro.instruction_size = 2; facts.zero_idiom = 1; }
        else macro.bytes = {0x48, 0x01, 0xc8};
        operands.clear();
        if (kind == 3) operands.push_back({0, 0, 5, 255});
        else if (kind == 1 || kind == 2) {
            operands.push_back({record.producer_dists[0], 0, 5, static_cast<std::uint8_t>(kind == 1 ? 0 : 1)});
            if (kind == 2) operands.push_back({record.producer_dists[1], 1, 5, 1});
        }
        facts.source_count = operands.size();
        return true;
    }
    std::string description() const override { return "rename-functional-fixture"; }
    std::string static_uop_policy_identity() const override { ++policy_reads; return std::string(64, 'a'); }
    unsigned static_uop_semantics_version() const override { return semantics_version; }
    const fst::StaticUopFactsRow* current_static_uop_facts() const override { return available ? &facts : nullptr; }
    const std::vector<fst::StaticUopOperand>& current_static_uop_operands() const override { return operands; }
    const fst::RetiredFrontendMacro* current_frontend_macro() const override { return &macro; }
    const fst::CoreFunctionalRow* current_core_functional_facts() const override { return core_available ? &core : nullptr; }
    const std::vector<fst::CoreFunctionalOperand>& current_core_functional_operands() const override { return core_operands; }
};
TraceRecord alu(unsigned seq, unsigned distance = 0, int op = 1) {
    TraceRecord r; r.flags = kRetires; r.pc = 0x1000 + 4 * seq;
    r.op_class = op; r.n_dst = 1; r.n_src = distance != 0;
    r.producer_dists[0] = distance; return r;
}
SimulatorConfig settings() {
    SimulatorConfig c; c.core_model = "interval_weave"; c.cores = 1;
    c.spr_shared_fu = true; c.spr_rename_elimination = true;
    c.spr_rename_policy_sha256 = std::string(64, 'a');
    c.interval_scheduler = "time_epoch"; c.interval_max_cycles = 1024;
    c.chunk_instructions = c.interval_target_uops = 64; c.lookahead_chunks = 2;
    c.fetch_width = c.decode_width = c.rename_width = c.dispatch_width = 8;
    c.issue_width = c.writeback_width = 1; c.commit_width = 8;
    c.iq_entries = c.spr_execution_iq_entries + c.spr_memory_iq_entries + c.spr_store_data_iq_entries;
    c.response_queue_feedback = c.response_sparse_scoreboard = true;
    c.response_block_summary = c.response_memory_descriptor = true;
    c.response_monotone_iq_calendar = c.response_activity_certificate = true;
    c.l1d.size_bytes = 4096; c.l2.size_bytes = 16384; c.llc.size_bytes = 65536;
    c.cha_count = c.dram.channels = c.dram.banks_per_channel = 1;
    return c;
}
RenameTrace fixture() {
    RenameTrace trace;
    auto load = alu(0); load.flags |= kLoad | kPhysicalAddress;
    load.op_class = 56; load.address = 0x8000; load.size = 8;
    trace.records.push_back(load); trace.kinds.push_back(0);
    // A load -> MOV chain crosses both a chunk and the live ROB export.
    for (unsigned seq = 1; seq < 140; ++seq) {
        trace.records.push_back(alu(seq, 1)); trace.kinds.push_back(1);
    }
    trace.records.push_back(alu(140, 1)); trace.kinds.push_back(0);
    // Zero breaks an old-value dependency; its static input has no producer.
    trace.records.push_back(alu(141, 1)); trace.kinds.push_back(3);
    trace.records.push_back(alu(142, 1)); trace.kinds.push_back(0);
    for (unsigned seq = 143; seq < 340; ++seq) {
        trace.records.push_back(alu(seq)); trace.kinds.push_back(seq % 3 == 0 ? 1 : 0);
    }
    return trace;
}
SimulationStats simulate(SimulatorConfig c, bool fast, bool facts = true, bool pure = false) {
    c.cpi_attribution = !fast; c.response_frontier_audit_stride_uops = fast ? 0 : 1;
    c.response_materialized_uop_fast_kernel = fast && !c.response_causal_block_transfer;
    c.validate();
    auto trace = std::make_unique<RenameTrace>(fixture()); trace->available = facts;
    if (pure) trace->records[0] = alu(0);
    std::vector<std::unique_ptr<TraceSource>> sources; sources.push_back(std::move(trace));
    return Simulator(c, std::move(sources)).run();
}
const ResponseFrontierAuditSample& sample(const SimulationStats& s, unsigned sequence) {
    const auto& rows = s.response_frontier_audit.at(0);
    auto it = std::find_if(rows.begin(), rows.end(), [sequence](const auto& row) { return row.sequence == sequence; });
    require(it != rows.end(), "missing rename audit sample"); return *it;
}
}
void test_spr_rename_elimination() {
    auto c = settings();
    // A producer source update must not disable compatible input semantics.
    c.spr_rename_policy_sha256 = std::string(64, 'b');
    const auto decision = [](SimulatorConfig config, unsigned version) {
        RenameTrace input; input.records = {alu(0)}; input.kinds = {1};
        input.semantics_version = version;
        TraceRecord record; input.next(record);
        IntervalCoreModel model(config);
        return model.schedule(record, false, false, 0, false, &input).rename_decision;
    };
    require(decision(c, 1).eliminated(), "source revision disabled compatible semantics");
    require(!decision(c, 2).eliminated(), "unknown semantics version was accepted");
    auto legacy_policy = c; legacy_policy.spr_rename_require_source_policy = true;
    require(!decision(legacy_policy, 1).eliminated(), "source policy rollback ignored mismatch");
    auto unpinned = c; unpinned.spr_rename_policy_sha256.clear(); unpinned.validate();
    require(decision(unpinned, 1).eliminated(), "source hash still mandatory");
    RenameTrace source; source.records = {alu(0)}; source.kinds = {1};
    TraceRecord r; source.next(r);
    require(spr_rename_decision(r, &source, true, false).kind == SprRenameKind::GprAlias, "GPR MOV eligibility");
    require(!spr_rename_decision(r, &source, false, false).eliminated(), "unsupported semantics did not fall back");
    require(!spr_rename_decision(r, &source, true, true).eliminated(), "PRF mode did not fall back");
    source.macro.bytes = {0x89, 0xc8}; source.macro.instruction_size = 2;
    require(!spr_rename_decision(r, &source, true, false).eliminated(), "32-bit MOV was eliminated");
    source.macro.bytes = {0x48, 0x89, 0xc8}; source.macro.instruction_size = 3;
    source.macro.flags |= fst::kFrontendSharedAllocation;
    require(!spr_rename_decision(r, &source, true, false).eliminated(), "allocation group was eliminated");
    source.macro.flags &= ~fst::kFrontendSharedAllocation;
    source.core_operands.resize(1);
    source.core_operands[0].flags = fst::kCorePinKnown;
    source.core_operands[0].static_num_pinned_writes = 1;
    require(!spr_rename_decision(r, &source, true, false).eliminated(), "pinned destination was eliminated");
    source.core_operands.clear();
    source.available = false;
    require(!spr_rename_decision(r, &source, true, false).eliminated(), "missing input was eliminated");

    RenameTrace xmm; r = alu(0, 0, 10); r.n_dst = 2;
    xmm.records = {r}; xmm.kinds = {2}; xmm.next(r);
    require(spr_rename_decision(r, &xmm, true, false).kind == SprRenameKind::XmmAlias, "XMM MOV eligibility");
    xmm.macro.bytes = {0xf3, 0x0f, 0x7e, 0xc1}; xmm.macro.instruction_size = 4;
    require(!spr_rename_decision(r, &xmm, true, false).eliminated(), "MOVQ high-zero was eliminated");
    xmm.macro.bytes = {0xc5, 0xf8, 0x28, 0xc1};
    require(!spr_rename_decision(r, &xmm, true, false).eliminated(), "VEX MOV was eliminated");
    xmm.macro.bytes = {0x0f, 0x28, 0xc1}; xmm.macro.instruction_size = 3;
    xmm.operands[0].producer_distance = 1; xmm.operands[1].producer_distance = 2;
    // Interval retains both lanes, without a physical-map or chain walk.
    IntervalCoreModel interval(c);
    const auto lane0 = interval.schedule(alu(0, 0, 9), false);
    const auto lane1 = interval.schedule(alu(1, 0, 9), false);
    xmm.facts.record_ordinal = xmm.macro.begin_record = 2; xmm.macro.end_record = 3;
    const auto timing = interval.schedule(r, false, false, 0, false, &xmm);
    require(timing.rename_decision.eliminated() && timing.completion_cycle == timing.issue_cycle &&
            timing.fu_occupancy_cycles == 0, "eliminated MOV consumed execution");
    require(timing.completion_cycle >= std::max(lane0.completion_cycle, lane1.completion_cycle),
            "XMM alias lost one lane producer");
    interval.reset_measurement_audit();
    require(interval.committed_pipeline_audit().spr_rename_xmm_alias == 0, "warmup audit reset");

    for (const bool partitioned : {false, true}) {
        c.spr_partitioned_iq = partitioned;
        auto generic = simulate(c, false), fast = simulate(c, true);
        require(generic.total_core().cycles == fast.total_core().cycles, "rename fast/generic timing differs");
        require(generic.total_core().retired_uops == 340 && fast.total_core().retired_uops == 340,
                "rename changed population");
        require(fast.response_materialized_fast_kernel_uops > 0, "rename disabled fast kernel");
        for (unsigned seq = 1; seq < 140; ++seq) {
            require(sample(generic, seq).actual_issue_cycle >= sample(generic, seq - 1).actual_completion_cycle,
                    "MOV lost corrected producer dependency");
            require(sample(generic, seq).actual_completion_cycle == sample(generic, seq).actual_issue_cycle,
                    "MOV added execution or writeback delay");
        }
        require(sample(generic, 140).actual_issue_cycle >= sample(generic, 139).actual_completion_cycle,
                "consumer read alias before its source");
        require(sample(generic, 141).actual_completion_cycle == sample(generic, 141).actual_issue_cycle,
                "zero added execution");
        require(sample(generic, 141).actual_issue_cycle < sample(generic, 140).actual_completion_cycle,
                "zero retained its old-value dependency");
        auto off = c; off.spr_rename_elimination = false;
        const auto legacy = simulate(off, false), fallback = simulate(c, false, false);
        require(legacy.total_core().cycles == fallback.total_core().cycles, "missing input changed legacy timing");
        require(legacy.llc.misses == generic.llc.misses, "rename changed memory population");
        std::map<std::uint64_t, unsigned> ordinary_writebacks;
        const auto members = fixture();
        for (unsigned seq = 0; seq < members.records.size(); ++seq)
            if (members.kinds[seq] == 0)
                require(++ordinary_writebacks[sample(generic, seq).actual_completion_cycle] <= c.writeback_width,
                        "ordinary WB capacity was lost while bypassing aliases");
    }
    c.spr_partitioned_iq = false;
    c.chunk_instructions = c.interval_target_uops = 512;
    c.response_activity_certificate = false;
    const auto scalar = simulate(c, false, true, true);
    c.response_causal_block_transfer = true;
    const auto blocks = simulate(c, true, true, true);
    require(scalar.total_core().cycles == blocks.total_core().cycles,
            "causal block transfer changed alias timing");
    require(blocks.response_causal_block_transferred_uops > 0,
            "causal block fixture did not exercise batched transitions");
}
#ifdef FASTSIM_RENAME_STANDALONE
int main() { test_spr_rename_elimination(); }
#endif
