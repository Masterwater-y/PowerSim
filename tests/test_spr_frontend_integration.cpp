#include "fastsim/config.hpp"
#include "fastsim/interval_core.hpp"
#include "fastsim/trace.hpp"
#include <filesystem>
#include <iostream>
#include <stdexcept>

using namespace fastsim;
namespace {
void require(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
SimulatorConfig settings() {
    SimulatorConfig c;
    c.cores = 1;
    c.core_model = "interval_weave";
    c.spr_retired_frontend = true;
    c.fetch_buffer_bytes = 64;
    c.fetch_queue_entries = 144;
    c.rename_width = 6;
    c.fetch_to_decode = c.decode_to_rename = 0;
    return c;
}
void rejects(const SimulatorConfig& c) {
    bool rejected = false;
    try { c.validate(); } catch (const std::invalid_argument&) { rejected = true; }
    require(rejected, "SPR frontend accepted incompatible configuration");
}
}
int main() {
    auto c = settings();
    c.validate();
    for (unsigned which = 0; which != 6; ++which) {
        auto bad = c;
        if (which == 0) bad.fetch_supply_speculative_shadow = true;
        if (which == 1) bad.l1i_speculative_path_state = true;
        if (which == 2) bad.l1i_speculative_entry_state = true;
        if (which == 3) bad.dtlb.speculative_path_state = true;
        if (which == 4) bad.branch.shadow_rob = true;
        if (which == 5) bad.decode_to_rename = 1;
        rejects(bad);
    }
    IntervalCoreModel missing(c);
    TraceRecord record; record.pc = 0x1000; record.flags = kRetires;
    bool absent = false;
    try { missing.schedule(record, false); }
    catch (const std::runtime_error& e) {
        absent = std::string(e.what()).find("frontend") != std::string::npos;
    }
    require(absent, "SPR mode did not reject missing functional frontend facts");

    const auto directory = std::filesystem::path(FASTSIM_PROJECT_ROOT) / "tmp/spr-frontend-integration";
    std::filesystem::create_directories(directory);
    const auto path = directory / "fused.fst";
    {
        BinaryTraceWriter writer(path.string(), 0);
        writer.enable_frontend_facts(); writer.set_address_space_id(1);
        for (unsigned macro = 0; macro != 2; ++macro) {
            fst::RetiredFrontendMacro m;
            m.begin_record = 2 * macro; m.end_record = m.begin_record + 2;
            m.pc = 0x1000; m.address_space_id = 1;
            m.code_identity = 123; m.semantic_identity = 456;
            m.instruction_size = 3; m.bytes[0] = 0x48; m.bytes[1] = 0x01; m.bytes[2] = 0x07;
            m.observed_uops = 2; m.known_flags = fst::kFrontendRequiredKnown;
            m.flags = fst::kFrontendComplete | fst::kFrontendSharedAllocation |
                      fst::kFrontendStartsMacro | fst::kFrontendDsbEligible;
            writer.register_frontend_macro(m);
            for (unsigned op = 0; op != 2; ++op) {
                auto r = record; r.flags |= kMicroOp;
                if (op == 1) r.flags |= kLastMicroOp;
                writer.append(r);
            }
        }
        writer.close();
    }
    BinaryTraceSource source(path.string()); IntervalCoreModel core(c);
    unsigned count = 0, slots = 0;
    while (source.next(record)) {
        if (count == 2) core.set_frontend_floor(100);
        const auto t = core.schedule(record, false, false, 0, false, &source,
                                     nullptr, source.current_address_space_id());
        require(t.spr_frontend_enabled && t.decode_cycle == t.fetch_cycle,
                "SPR mode retained the legacy Decode stage");
        require(t.rename_cycle >= t.fetch_cycle, "Rename precedes frontend delivery");
        slots += t.spr_frontend_slot_cost;
        ++count;
    }
    require(count == 4 && slots == 2, "fused functional UOPs lost their shared allocation accounting");
    const auto cross_path = directory / "cross.fst";
    {
        BinaryTraceWriter writer(cross_path.string(), 0);
        writer.enable_frontend_facts(); writer.set_address_space_id(1);
        for (unsigned macro = 0; macro != 2; ++macro) {
            fst::RetiredFrontendMacro m;
            m.begin_record = 2 * macro; m.end_record = m.begin_record + 2;
            m.pc = 0x103f; m.address_space_id = 1;
            m.code_identity = 789; m.semantic_identity = 987;
            m.instruction_size = 2; m.bytes[0] = 0x0f; m.bytes[1] = 0xa2;
            m.observed_uops = 2; m.known_flags = fst::kFrontendRequiredKnown;
            m.flags = fst::kFrontendComplete | fst::kFrontendMicrocode | fst::kFrontendStartsMacro;
            writer.register_frontend_macro(m);
            for (unsigned op = 0; op != 2; ++op) {
                auto r = record; r.pc = m.pc; r.flags = kRetires | kMicroOp;
                if (op == 1) r.flags |= kLastMicroOp;
                writer.append(r);
            }
        }
        writer.close();
    }
    auto cross_config = c;
    cross_config.l1i_enabled = true;
    cross_config.fetch_supply_model = true;
    cross_config.fetch_supply_static_instruction_span = false;
    cross_config.l1i.line_size = 64;
    BinaryTraceSource cross_source(cross_path.string());
    IntervalCoreModel cross_core(cross_config);
    unsigned accesses = 0;
    while (cross_source.next(record)) {
        const auto t = cross_core.schedule(record, false, false, 0, false, &cross_source,
                                          nullptr, cross_source.current_address_space_id());
        accesses += static_cast<unsigned>(t.l1i_access_count);
    }
    require(accesses == 4,
            "FFE-only cross-line repeated macro omitted an instruction-cache block");
    std::cout << "SPR retired frontend integration tests passed\n";
}
