#include "fastsim/fst_mmu_control_reader.hpp"
#include "fastsim/fst_mmu_controls.hpp"
#include "fastsim/trace.hpp"
#include <cassert>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <unistd.h>
using namespace fastsim::fst::mmuctl;
template <class F> void rejects(F f) {
    bool caught = false;
    try {
        f();
    } catch (const std::exception &) {
        caught = true;
    }
    assert(caught);
}
std::vector<unsigned char> unpack(const std::string &path) {
    auto file = gzopen(path.c_str(), "rb");
    assert(file);
    std::vector<unsigned char> result;
    unsigned char buffer[4096];
    int n;
    while ((n = gzread(file, buffer, sizeof(buffer))) > 0)
        result.insert(result.end(), buffer, buffer + n);
    assert(n == 0 && gzclose(file) == Z_OK);
    return result;
}
void pack(const std::string &path, const std::vector<unsigned char> &bytes) {
    auto file = gzopen(path.c_str(), "wb1");
    assert(file);
    assert(gzwrite(file, bytes.data(), bytes.size()) == int(bytes.size()));
    assert(gzclose(file) == Z_OK);
}
void full_fixture(const std::string &path) {
    Header header;
    header.policy[0] = 1;
    header.coverage = RequiredCoverage;
    fastsim::BinaryTraceWriter hot(path, 0);
    hot.enable_complete_dependencies();
    hot.enable_raw_syscall_microops();
    hot.enable_functional_retirement();
    hot.enable_frontend_facts();
    hot.enable_static_uop_facts(header.policy);
    Writer controls(path + ".mmuctl.gz", header);
    Footer footer;
    footer.status = Finalized | Complete;
    footer.hot = footer.sealed = 3;
    for (unsigned i = 0; i < 3; ++i) {
        fastsim::TraceRecord row;
        row.pc = 0x1000 + i;
        row.op_class = 1;
        fastsim::fst::StaticUopFactsRow stat;
        std::vector<fastsim::fst::StaticUopOperand> operands;
        hot.append(row, nullptr, {}, fastsim::fst::MemoryRequestRole::Data, 0, false, false,
                   nullptr, &stat, &operands);
        fastsim::fst::RetiredFrontendMacro macro;
        macro.begin_record = i;
        macro.end_record = i + 1;
        macro.pc = row.pc;
        macro.instruction_size = 1;
        macro.bytes[0] = 0x90;
        macro.observed_uops = 1;
        macro.code_identity = 11;
        macro.semantic_identity = 19;
        macro.flags = fastsim::fst::kFrontendComplete | fastsim::fst::kFrontendStartsMacro;
        macro.known_flags = fastsim::fst::kFrontendRequiredKnown;
        hot.register_frontend_macro(macro);
        if (!i) {
            Baseline baseline;
            baseline.hash = row_hash(&row);
            baseline.known = 15;
            baseline.generation = 1;
            baseline.cut = 1;
            controls.baseline(baseline);
        }
        Member member;
        member.ordinal = i;
        member.hash = row_hash(&row);
        member.generation = 1;
        member.status = Sealed;
        controls.member(member, {});
        footer.hot_hash = hash_bytes(footer.hot_hash, &row, 64);
    }
    hot.close();
    controls.finish(footer);
    // The real codec supplies the bound companion; BinaryTraceWriter deliberately
    // has no production API to declare a companion it cannot preserve itself.
    std::fstream file(path, std::ios::binary | std::ios::in | std::ios::out);
    file.seekg(32);
    std::uint64_t flags;
    file.read(reinterpret_cast<char *>(&flags), 8);
    flags |= fastsim::fst::kLogicalMmuControls;
    file.seekp(32);
    file.write(reinterpret_cast<char *>(&flags), 8);
}
int main(int argc,char** argv) {
    std::uint64_t counter = UINT64_MAX - 1;
    assert(!saturating_add(counter, 2));
    assert(counter == UINT64_MAX);
    assert(!saturating_add(counter));
    Header h;
    h.policy[0] = 1;
    h.context = 3;
    auto bytes = encode(h);
    assert(bytes.size() == 160);
    assert(decode_header(bytes).context == 3);
    bytes[12] = 0;
    rejects([&] { decode_header(bytes); });
    bytes = encode(h);
    bytes[159] = 1;
    rejects([&] { decode_header(bytes); });
    Baseline b;
    b.known = 15;
    b.generation = 1;
    b.cut = 1;
    b.hash = 0x12345678;
    assert(encode(b).size() == 80);
    assert(decode_baseline(encode(b)).hash == b.hash);
    b.hash = 1ull << 32;
    rejects([&] { encode(b); });
    b.hash = 0;
    Member m;
    m.context = 3;
    m.generation = 1;
    m.status = 1;
    assert(encode(m).size() == 48);
    Write w;
    w.reg = Register::Cr3;
    w.requested = 99;
    w.before[1] = 88;
    w.after[1] = 99;
    w.before_known = w.after_known = 15;
    assert(encode(w).size() == 96);
    assert(decode_write(encode(w)).after[1] == 99);
    Command c;
    c.kind = CommandKind::DemapPage;
    c.va = 0x123;
    c.asn = 55;
    assert(encode(c).size() == 48);
    assert(decode_command(encode(c)).asn == 55);
    c.kind = CommandKind::FlushAll;
    rejects([&] { encode(c); });
    Diagnostic d;
    assert(encode(d).size() == 48);
    Footer f;
    assert(encode(f).size() == 256);
    auto fb = encode(f);
    fb[7] = 1;
    rejects([&] { decode_footer(fb); });
    assert(layout_descriptor().find("retained") != std::string::npos);
    const auto dir = std::filesystem::path("tmp") / ("mmuctl-tests-" + std::to_string(getpid()));
    std::filesystem::create_directories(dir);
    const auto path = (dir / "stream.fst").string();
    std::array<unsigned char, 64> row{};
    row[0] = 0x55;
    {
        fastsim::TraceRecord record;
        record.pc = 0x55;
        record.op_class = 1;
        fastsim::BinaryTraceWriter writer(path, h.context);
        writer.append(record);
        writer.close();
        std::memcpy(row.data(), &record, 64);
    }
    h.coverage = RequiredCoverage;
    b.hash = row_hash(row.data());
    b.state = {1, 2, 3, 4};
    m.ordinal = 0;
    m.hash = b.hash;
    m.context = h.context;
    m.generation = b.generation;
    m.count = 0;
    m.status = Sealed;
    f.hot = f.sealed = 1;
    f.hot_hash = hash_bytes(HashSeed, row.data(), 64);
    f.status = Finalized | Complete;
    {
        Writer writer(path + ".mmuctl.gz", h);
        writer.baseline(b);
        writer.member(m, {});
        writer.finish(f);
    }
    {
        fastsim::fst::MmuControlReader reader(path, h.context, 1, hex(h.policy));
        assert(reader.ready());
        assert(!reader.current());
        reader.observe(0, row.data());
        assert(reader.current()->member.hash == m.hash);
        reader.clear_current();
        assert(!reader.current());
    }
    {
        Write inconsistent;
        inconsistent.before_known = inconsistent.after_known = 15;
        inconsistent.before = {10, 20, 30, 40};
        inconsistent.after = inconsistent.before;
        auto with_write = m;
        with_write.count = 1;
        auto footer = f;
        footer.total_writes = footer.retained_writes = 1;
        Writer writer(path + ".mmuctl.gz", h);
        writer.baseline(b);
        writer.member(with_write, {inconsistent});
        writer.finish(footer);
    }
    rejects([&] { fastsim::fst::MmuControlReader reader(path, h.context, 1, hex(h.policy)); });
    {
        Writer writer(path + ".mmuctl.gz", h);
        writer.baseline(b);
        writer.member(m, {});
        writer.finish(f);
    }
    {
        std::ofstream trailing(path + ".mmuctl.gz", std::ios::app);
        trailing.put('x');
    }
    rejects([&] { fastsim::fst::MmuControlReader reader(path, h.context, 1, hex(h.policy)); });
    {
        Writer writer(path + ".mmuctl.gz", h);
        writer.baseline(b);
        auto wrong = m;
        wrong.hash ^= 1;
        writer.member(wrong, {});
        writer.finish(f);
    }
    rejects([&] { fastsim::fst::MmuControlReader reader(path, h.context, 1, hex(h.policy)); });
    // A finalized incomplete file is usable for diagnosis, never control ready.
    {
        auto incomplete = f;
        incomplete.status = Finalized;
        incomplete.total_commands = incomplete.unbound_commands = 1;
        incomplete.groups = 1;
        incomplete.reasons[unsigned(Reason::UnboundPageFault)] = 1;
        Diagnostic diagnostic;
        diagnostic.context = h.context;
        diagnostic.generation = b.generation;
        diagnostic.reason = Reason::UnboundPageFault;
        diagnostic.count = 1;
        Command command;
        command.kind = CommandKind::DemapPage;
        command.va = 0x987;
        command.asn = 7;
        Writer writer(path + ".mmuctl.gz", h);
        writer.baseline(b);
        writer.member(m, {});
        writer.diagnostic(diagnostic, {command});
        writer.finish(incomplete);
    }
    {
        fastsim::fst::MmuControlReader reader(path, h.context, 1, hex(h.policy));
        assert(!reader.ready());
        reader.observe(0, row.data());
        assert(reader.coverage().unbound_commands == 1);
    }
    const auto legacy = (dir / "legacy.fst").string();
    {
        fastsim::BinaryTraceWriter writer(legacy, 0);
        fastsim::TraceRecord record;
        writer.append(record);
        writer.close();
    }
    {
        fastsim::BinaryTraceSource source(legacy);
        assert(source.mmu_control_version() == 0 && !source.mmu_controls_ready() &&
               !source.mmu_control_baseline());
    }
    const auto full = (dir / "full.fst").string();
    full_fixture(full);
    {
        fastsim::BinaryTraceSource source(full);
        fastsim::TraceRecord record;
        assert(source.mmu_controls_ready());
        unsigned count = 0;
        while (source.next(record)) {
            assert(source.current_mmu_controls()->member.ordinal == count++);
        }
        assert(count == 3 && !source.current_mmu_controls());
    }
    {
        fastsim::InstructionSliceTraceSource source(
            std::make_unique<fastsim::BinaryTraceSource>(full), 1, 1);
        fastsim::TraceRecord record;
        assert(source.mmu_controls_require_prefix() && !source.mmu_controls_ready());
        assert(source.mmu_control_baseline()->ordinal == 0);
        assert(source.next(record) && source.current_mmu_controls()->member.ordinal == 1);
        assert(!source.next(record) && !source.current_mmu_controls());
    }
    {
        fastsim::WarmupInstructionTraceSource source(
            std::make_unique<fastsim::BinaryTraceSource>(full), 1, 1, 1, 1, true);
        fastsim::TraceRecord record;
        assert(source.next(record));
        assert(!source.next(record) && !source.current_mmu_controls());
        source.start_measurement();
        assert(!source.current_mmu_controls());
        assert(source.next(record) && source.current_mmu_controls()->member.ordinal == 1);
        assert(!source.next(record) && !source.current_mmu_controls());
    }
    rejects([&] {
        fastsim::upgrade_binary_trace_to_v7(full, (dir / "copy.fst").string(),
                                            fastsim::SyscallAbi::kLinuxX86_64);
    });
    auto original = unpack(full + ".mmuctl.gz");
    for (auto offset :
         {std::size_t(159), std::size_t(160 + 80 + 2 * 48 + 16), original.size() - 1}) {
        auto corrupted = original;
        corrupted[offset] ^= 1;
        pack(full + ".mmuctl.gz", corrupted);
        rejects([&] {
            fastsim::InstructionSliceTraceSource source(
                std::make_unique<fastsim::BinaryTraceSource>(full), 0, 1);
        });
    }
    pack(full + ".mmuctl.gz", original);
    std::filesystem::remove(full + ".mmuctl.gz");
    rejects([&] { fastsim::BinaryTraceSource source(full); });
    std::filesystem::remove_all(dir);
    if(argc==2)full_fixture(argv[1]);
    std::cout << "MMU controls codec and full-source reader tests passed\n";
}
