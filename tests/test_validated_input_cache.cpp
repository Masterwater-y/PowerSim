#include "fastsim/trace.hpp"
#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <unistd.h>

using namespace fastsim;
namespace {
void check(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
template<class T> void same(const T* a, const T* b, const char* message) {
    check(bool(a) == bool(b), message);
    if (a) check(std::memcmp(a, b, sizeof(T)) == 0, message);
}
void compare(TraceSource& original, TraceSource& cached, bool raw_eof = true) {
    TraceRecord a{}, b{};
    std::uint64_t count = 0;
    while (original.next(a)) {
        check(cached.next(b), "cached early EOF");
        same(&a, &b, "record changed");
        same(original.current_frontend_macro(), cached.current_frontend_macro(), "macro changed");
        same(original.current_static_uop_facts(), cached.current_static_uop_facts(), "UOP facts changed");
        same(original.current_memory_facts(), cached.current_memory_facts(), "memory facts changed");
        check(original.current_source_record_ordinal() == cached.current_source_record_ordinal(), "ordinal changed");
        check(original.current_address_space_id() == cached.current_address_space_id(), "ASID changed");
        check(original.current_dependency_extensions() == cached.current_dependency_extensions(), "RAW extensions changed");
        const auto& x = original.current_static_uop_operands();
        const auto& y = cached.current_static_uop_operands();
        check(x.size() == y.size(), "operand count changed");
        for (std::size_t i = 0; i < x.size(); ++i) same(&x[i], &y[i], "dynamic producer distance changed");
        // Repeated consumers must see the same view without consuming data.
        check(&y == &cached.current_static_uop_operands(), "operand view is unstable");
        check(original.current_memory_request_role() == cached.current_memory_request_role(), "memory role changed");
        check(original.current_memory_request_micro_pc() == cached.current_memory_request_micro_pc(), "micro PC changed");
        ++count;
    }
    check(count != 0 && !cached.next(b), "population/EOF changed");
    if (raw_eof) check(!cached.current_frontend_macro() && !cached.current_static_uop_facts() &&
                      !cached.current_memory_facts() && cached.current_static_uop_operands().empty() &&
                      cached.current_dependency_extensions().empty(), "stale facts after EOF");
}
}

int main() {
    const auto root = std::filesystem::absolute(std::filesystem::path("tmp") /
        ("validated-cache-tests-" + std::to_string(getpid())));
    std::filesystem::create_directories(root);
    try {
        const auto path = (root/"trace.fst").string();
        std::array<std::uint8_t,32> policy{};
        policy.fill(0x42);
        {
            BinaryTraceWriter writer(path, 0);
            writer.enable_complete_dependencies();
            writer.enable_static_uop_facts(policy);
            writer.enable_frontend_facts();
            for (unsigned macro = 0; macro < 40; ++macro) {
                fst::RetiredFrontendMacro front;
                front.begin_record = writer.record_count();
                front.observed_uops = macro % 3 + 1;
                front.end_record = front.begin_record + front.observed_uops;
                front.pc = 0x1000 + macro % 5;  // Same PC, different dynamic macro facts.
                front.address_space_id = macro < 20 ? 0x1000 : 0x2000;
                front.code_identity = front.semantic_identity = macro + 1;
                front.known_flags = fst::kFrontendRequiredKnown;
                front.flags = fst::kFrontendComplete | fst::kFrontendStartsMacro;
                front.instruction_size = 1; front.bytes[0] = 0x90;
                writer.set_address_space_id(front.address_space_id);
                writer.register_frontend_macro(front);
                for (unsigned uop = 0; uop < front.observed_uops; ++uop) {
                    TraceRecord record{};
                    record.pc = front.pc;
                    record.flags = kRetires | kMicroOp;
                    if (uop+1 == front.observed_uops) record.flags |= kLastMicroOp;
                    record.op_class = 1;
                    record.n_src = macro ? 1 : 0;
                    record.n_dst = 1;
                    if (macro) {
                        record.producer_dists[0] = uop+1;
                        record.producer_classes[0] = 0;
                    }
                    fst::StaticUopFactsRow facts;
                    facts.flags = fst::kStaticExecutionKnown | fst::kStaticOperandRolesKnown;
                    facts.integer_family = 1; facts.candidate_ports = 0x863;
                    facts.source_count = record.n_src; facts.destination_count = 1;
                    std::vector<fst::StaticUopOperand> operands;
                    if (macro) operands.push_back({uop+1,0,1,0});
                    writer.append(record, nullptr, {}, fst::MemoryRequestRole::Data,
                                  uop, false, false, nullptr, &facts, &operands);
                }
            }
            writer.close();
        }
        BinaryTraceSource original(path), cached(path);
        cached.preload_validated();
        compare(original, cached);
        auto warm_cache = std::make_unique<BinaryTraceSource>(path);
        warm_cache->preload_validated();
        WarmupInstructionTraceSource warm_original(std::make_unique<BinaryTraceSource>(path),3,8);
        WarmupInstructionTraceSource warm_cached(std::move(warm_cache),3,8);
        TraceRecord a{},b{};
        for (unsigned i=0;i<6;++i) {
            check(warm_original.next(a) && warm_cached.next(b), "warmup truncated");
            same(&a,&b,"warmup changed");
        }
        check(!warm_original.next(a) && !warm_cached.next(b), "warmup marker changed");
        warm_original.start_measurement(); warm_cached.start_measurement();
        compare(warm_original, warm_cached, false);
        bool rejected = false;
        try { cached.preload_validated(); } catch (const std::logic_error&) { rejected = true; }
        check(rejected, "consumed input preloaded twice");
        const auto manifest = (root/"manifest.txt").string();
        { std::ofstream out(manifest); out << "0 binary " << path << '\n'; }
        for (const char* setting : {"0", "1"}) {
            check(setenv("FASTSIM_PRELOAD_VALIDATED", setting, 1) == 0, "setenv failed");
            auto sources = open_trace_manifest(manifest, 1);
            BinaryTraceSource reference(path);
            compare(reference, *sources.front(), std::string(setting) == "1");
        }
        check(setenv("FASTSIM_PRELOAD_VALIDATED", "invalid", 1) == 0, "setenv failed");
        rejected = false;
        try { auto sources = open_trace_manifest(manifest, 1); }
        catch (const std::invalid_argument&) { rejected = true; }
        check(rejected, "invalid preload policy accepted");
        // A late bad dependency must fail during preloading, before the
        // simulator can start its measurement phase.
        const auto late_path = (root/"late.fst").string();
        {
            BinaryTraceWriter writer(late_path, 0);
            writer.enable_complete_dependencies();
            for (unsigned i = 0; i < 64; ++i) {
                TraceRecord record;
                record.pc = 0x2000 + i * 4;
                writer.append(record);
            }
            writer.close();
        }
        { std::ofstream out(manifest); out << "0 binary " << late_path << '\n'; }
        {
            std::fstream out(late_path, std::ios::in | std::ios::out | std::ios::binary);
            out.seekp(-static_cast<std::streamoff>(sizeof(TraceRecord)) +
                      static_cast<std::streamoff>(offsetof(TraceRecord, producer_dists)),
                      std::ios::end);
            const std::uint32_t invalid_distance = UINT32_MAX;
            out.write(reinterpret_cast<const char*>(&invalid_distance),
                      sizeof(invalid_distance));
            check(bool(out), "could not corrupt final dependency");
        }
        check(setenv("FASTSIM_PRELOAD_VALIDATED", "0", 1) == 0, "setenv failed");
        { auto sources = open_trace_manifest(manifest, 1); }
        check(setenv("FASTSIM_PRELOAD_VALIDATED", "1", 1) == 0, "setenv failed");
        rejected = false;
        try { auto sources = open_trace_manifest(manifest, 1); }
        catch (const std::runtime_error&) { rejected = true; }
        check(rejected, "late invalid input escaped preload validation");
        unsetenv("FASTSIM_PRELOAD_VALIDATED");
        std::filesystem::remove_all(root);
        std::cout << "validated input cache tests passed\n";
    } catch (...) {
        unsetenv("FASTSIM_PRELOAD_VALIDATED");
        std::filesystem::remove_all(root);
        throw;
    }
}
