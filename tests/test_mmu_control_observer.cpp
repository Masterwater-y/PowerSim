#include "fastsim/fst_mmu_control_reader.hpp"
#include "fastsim/trace.hpp"
#include "mem/functional_mmu_controls.hh"
#include <cassert>
#include <exception>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <unistd.h>
using namespace gem5::functional_mmu;
using namespace fastsim::fst::mmuctl;
// These fixtures use the actual hot writer, observer, MMU codec and reader.
struct ReviewFixture {
    State raw{1, 2, 3, 4};
    unsigned reads = 0;
    int tc = 0, isa = 0, mmu = 0;
    std::filesystem::path directory;
    std::string path;
    fastsim::TraceRecord hot;
    Observer observer;
    explicit ReviewFixture(const std::string &label)
        : directory(std::filesystem::path("tmp") /
                    ("mmuctl-fix1-" + label + "-" + std::to_string(getpid()))),
          path((directory / "source.fst").string()), observer(0, [this] {
              ++reads;
              return raw;
          }) {
        std::filesystem::create_directories(directory);
        hot.pc = 0x1000;
        hot.op_class = 1;
        fastsim::BinaryTraceWriter writer(path, 0);
        writer.append(hot);
        writer.close();
        observer.registerEndpoint(&tc, Domain::Unknown);
        observer.registerEndpoint(&isa, Domain::Unknown);
        observer.registerEndpoint(&mmu, Domain::ContextAllTranslations);
        Header h;
        h.policy[0] = 1;
        h.coverage = RequiredCoverage;
        observer.attach(path + ".mmuctl.gz", h);
        observer.resume();
    }
    ~ReviewFixture() { std::filesystem::remove_all(directory); }
    void bind() { observer.bind(1, 0, &hot); }
    void check_reader(bool ready, std::uint64_t commands, std::uint64_t writes) {
        Header h;
        h.policy[0] = 1;
        fastsim::fst::MmuControlReader reader(path, 0, 1, hex(h.policy));
        assert(reader.ready() == ready);
        assert(reader.coverage().total_commands == commands);
        assert(reader.coverage().total_writes == writes);
        reader.observe(0, &hot);
    }
    std::string bytes() const {
        std::ifstream in(path + ".mmuctl.gz", std::ios::binary);
        return {std::istreambuf_iterator<char>(in), {}};
    }
};
void review_first_cut(bool completed_owner) {
    ReviewFixture f(completed_owner ? "completed-owner" : "external-cut");
    auto &o = f.observer;
    {
        OwnerScope owner(&f.tc, 1, Application::Immediate);
        CommandScope c(&f.mmu, CommandKind::DemapPage, 0x1000, 7);
    }
    if (completed_owner) {
        {
            OwnerScope excluded(&f.tc, 2, Application::Immediate);
            CommandScope c(&f.mmu, CommandKind::DemapPage, 0x2000, 9);
        }
        o.seal(2);
    } else {
        CauseScope fault(Reason::UnboundPageFault);
        CommandScope c(&f.mmu, CommandKind::DemapPage, 0x2000, 9);
    }
    f.bind();
    o.seal(1);
    o.close(1);
    std::cerr << "first-cut ready=" << o.ready() << " total=" << o.footer().total_commands
              << " unbound=" << o.footer().unbound_commands << std::endl;
    assert(!o.ready());
    assert(o.baseline().cut == 0);
    assert(o.footer().total_commands == 2);
    assert(o.footer().retained_commands == 1 && o.footer().unbound_commands == 1);
    assert(o.footer().reasons[unsigned(Reason::InitialCutUnknown)]);
    assert(o.footer().reasons[unsigned(completed_owner ? Reason::FilteredInterior
                                                       : Reason::UnboundPageFault)]);
    f.check_reader(false, 2, 0);
}
void review_nested_first_cut() {
    ReviewFixture f("nested-cut");
    auto &o = f.observer;
    {
        CommandScope external(&f.mmu, CommandKind::DemapPage, 0x2000, 9);
        {
            OwnerScope owner(&f.tc, 1, Application::Immediate);
            CommandScope retained(&f.mmu, CommandKind::DemapPage, 0x1000, 7);
        }
    }
    f.bind();
    o.seal(1);
    o.close(1);
    assert(!o.ready());
    assert(o.footer().total_commands == 2);
    assert(o.footer().unbound_commands == 1 &&
           o.footer().reasons[unsigned(Reason::InitialCutUnknown)]);
    f.check_reader(false, 2, 0);
}
void review_stale_propagation_after_cut() {
    ReviewFixture f("stale-edge");
    auto &o = f.observer;
    int child, unknown;
    o.registerEndpoint(&child, Domain::Data);
    o.edge(&f.mmu, &child);
    f.bind();
    {
        CommandScope external(&f.mmu, CommandKind::FlushAll);
        EdgeScope edge(external, &f.mmu, &child);
        o.close(1);
        const auto footer = encode(o.footer());
        {
            CommandScope late(&unknown, CommandKind::FlushAll, 0, 0, true);
        }
        {
            EdgeScope late_edge(external, &f.mmu, &unknown);
        }
        assert(footer == encode(o.footer()));
    }
    o.seal(1);
    assert(!o.ready());
    f.check_reader(false, 1, 0);
}
void review_prefix_before_cut() {
    ReviewFixture f("proven-prefix");
    auto &o = f.observer;
    {
        OwnerScope owner(&f.tc, 2, Application::Immediate);
        CommandScope c(&f.mmu, CommandKind::DemapPage, 0x2000, 9);
    }
    {
        OwnerScope owner(&f.tc, 1, Application::Immediate);
        CommandScope c(&f.mmu, CommandKind::DemapPage, 0x1000, 7);
    }
    // Completion is later, but the excluded effect itself is provably earlier.
    o.seal(2);
    f.bind();
    o.seal(1);
    o.close(1);
    assert(o.ready());
    f.check_reader(true, 1, 0);
}
void review_terminal() {
    ReviewFixture f("terminal");
    auto &o = f.observer;
    f.bind();
    o.seal(1);
    o.close(1);
    const auto before = f.bytes();
    const auto footer = encode(o.footer());
    const auto reads = f.reads;
    {
        CauseScope fault(Reason::UnboundPageFault);
        CommandScope c(&f.mmu, CommandKind::DemapPage, 0x3000, 11);
    }
    {
        WriteScope w(&f.isa, Register::Cr3, 5, Mode::NoEffect, [&] {
            ++f.reads;
            return f.raw;
        });
        f.raw[1] = 5;
    }
    {
        OwnerScope owner(&f.tc, 1, Application::Deferred);
        WriteScope w(&f.isa, Register::Cr0, 8, Mode::WithEffect, [&] {
            ++f.reads;
            return f.raw;
        });
        f.raw[0] = 8;
    }
    o.destroy(1);
    o.seal(1);
    o.bind(2, 1, &f.hot);
    o.lifecycle();
    o.resume();
    o.forceClose();
    assert(!endpoint(&f.tc).observer && !endpoint(&f.isa).observer && !endpoint(&f.mmu).observer);
    assert(!o.payload());
    o.snapshot();
    assert(f.reads == reads);
    assert(encode(o.footer()) == footer);
    assert(f.bytes() == before);
    f.check_reader(true, 0, 0);
}
void review_drain() {
    ReviewFixture trigger("trigger"), other("non-trigger");
    trigger.bind();
    other.bind();
    other.observer.seal(1);
    // Same common boundary closes every participant; only trigger has a pending seal.
    for (auto *f : {&trigger, &other})
        f->observer.close(1);
    const auto reads = trigger.reads;
    {
        OwnerScope later(&trigger.tc, 2, Application::Immediate);
        WriteScope w(&trigger.isa, Register::Cr3, 2, Mode::NoEffect, [&] {
            ++trigger.reads;
            return trigger.raw;
        });
    }
    {
        CauseScope fault(Reason::UnboundPageFault);
        CommandScope c(&trigger.mmu, CommandKind::DemapPage, 0x4000, 0);
    }
    assert(trigger.reads == reads);
    assert(trigger.observer.footer().total_commands == 0);
    {
        OwnerScope pending(&trigger.tc, 1, Application::Deferred);
        WriteScope w(&trigger.isa, Register::Cr3, 9, Mode::WithEffect, [&] {
            ++trigger.reads;
            return trigger.raw;
        });
        trigger.raw[1] = 9;
        CommandScope flush(&trigger.mmu, CommandKind::FlushNonGlobal);
    }
    trigger.observer.seal(1);
    for (auto *f : {&trigger, &other}) {
        const auto footer = encode(f->observer.footer());
        const auto before = f->bytes();
        const auto count = f->reads;
        {
            OwnerScope drain(&f->tc, 2, Application::Immediate);
            WriteScope w(&f->isa, Register::Cr3, 17, Mode::NoEffect, [&] {
                ++f->reads;
                return f->raw;
            });
            f->raw[1] = 17;
        }
        {
            CommandScope c(&f->mmu, CommandKind::FlushAll);
        }
        f->observer.seal(2);
        f->observer.destroy(2);
        assert(count == f->reads && before == f->bytes() && footer == encode(f->observer.footer()));
        assert(f->observer.ready());
    }
    trigger.check_reader(true, 1, 1);
    other.check_reader(true, 0, 0);
}
void review_live_crossing() {
    ReviewFixture f("live-crossing");
    auto &o = f.observer;
    f.bind();
    o.seal(1);
    unsigned after_close_reads = 0;
    {
        CauseScope fault(Reason::UnboundPageFault);
        WriteScope w(&f.isa, Register::Cr3, 19, Mode::NoEffect, [&] {
            ++f.reads;
            return f.raw;
        });
        o.close(1);
        after_close_reads = f.reads;
        f.raw[1] = 19;
    }
    assert(!o.ready());
    assert(o.footer().unbound_writes == 1);
    assert(f.reads == after_close_reads);
    assert(o.footer().reasons[unsigned(Reason::EffectOrderUnsupported)]);
    assert(o.footer().reasons[unsigned(Reason::UnboundPageFault)]);
    f.check_reader(false, 0, 1);
}
void review_forced_live_seal() {
    ReviewFixture f("forced-live-seal");
    auto &o = f.observer;
    f.bind();
    unsigned reads = 0;
    {
        OwnerScope owner(&f.tc, 1, Application::Deferred);
        WriteScope write(&f.isa, Register::Cr3, 31, Mode::WithEffect, [&] {
            ++f.reads;
            return f.raw;
        });
        o.close(1);
        o.forceClose();
        reads = f.reads;
        f.raw[1] = 31;
    }
    const auto footer = encode(o.footer());
    const auto bytes = f.bytes();
    o.seal(1);
    o.destroy(1);
    assert(!o.ready() && o.footer().pending == 1 && o.footer().sealed == 0);
    assert(reads == f.reads);
    assert(footer == encode(o.footer()) && bytes == f.bytes());
    f.check_reader(false, 0, 1);
}
int main(int argc, char **argv) {
    if (argc == 2) {
        std::set_terminate([] {
            std::cerr << "unexpected native observer destructor termination\n";
            std::_Exit(42);
        });
        const std::string test = argv[1];
        if (test == "first-cut")
            review_first_cut(false);
        else if (test == "completed-owner")
            review_first_cut(true);
        else if (test == "terminal")
            review_terminal();
        else if (test == "nested-cut")
            review_nested_first_cut();
        else if (test == "stale-edge")
            review_stale_propagation_after_cut();
        else if (test == "drain")
            review_drain();
        else if (test == "live-crossing")
            review_live_crossing();
        else if (test == "forced-live-seal")
            review_forced_live_seal();
        else
            assert(false);
        return 0;
    }

    State raw{1, 2, 3, 4};
    int tc, isa, mmu, itb, dtb, stlb;
    Observer o(0, [&] { return raw; });
    o.registerEndpoint(&tc, Domain::Unknown);
    o.registerEndpoint(&isa, Domain::Unknown);
    o.registerEndpoint(&mmu, Domain::ContextAllTranslations);
    o.registerEndpoint(&itb, Domain::Instruction);
    o.registerEndpoint(&dtb, Domain::Data);
    o.registerEndpoint(&stlb, Domain::Unified);
    o.edge(&mmu, &itb);
    o.edge(&mmu, &dtb);
    o.edge(&itb, &stlb);
    o.edge(&dtb, &stlb);
    o.resume();
    std::array<unsigned char, 64> hot{};
    {
        OwnerScope owner(&tc, 11, Application::Immediate);
        WriteScope w(&isa, Register::Cr3, 2, Mode::WithEffect, [&] { return raw; });
        CommandScope root(&mmu, CommandKind::FlushNonGlobal);
        {
            EdgeScope e(root, &mmu, &itb);
            CommandScope visit(&itb, CommandKind::FlushNonGlobal, 0, 0, true);
            EdgeScope e2(visit, &itb, &stlb);
            CommandScope v2(&stlb, CommandKind::FlushNonGlobal, 0, 0, true);
        }
        {
            EdgeScope e(root, &mmu, &dtb);
            CommandScope visit(&dtb, CommandKind::FlushNonGlobal, 0, 0, true);
            EdgeScope e2(visit, &dtb, &stlb);
            CommandScope v2(&stlb, CommandKind::FlushNonGlobal, 0, 0, true);
        }
    }
    o.bind(11, 0, hot.data());
    assert(o.baseline().state == raw);
    o.seal(11);
    o.close(1);
    assert(o.footer().retained_writes == 1 && o.footer().retained_commands == 1 &&
           o.footer().visits == 4);
    assert(o.ready());
    Observer q(1, [&] { return raw; });
    int tc2, isa2;
    q.registerEndpoint(&tc2, Domain::Unknown);
    q.registerEndpoint(&isa2, Domain::Unknown);
    q.resume();
    {
        OwnerScope owner(&tc2, 2, Application::Immediate);
        WriteScope w(&isa2, Register::Cr0, 8, Mode::NoEffect, [&] { return raw; });
        raw[0] = 8;
    }
    q.bind(2, 0, hot.data());
    assert(q.baseline().state[0] == 1);
    q.seal(2);
    {
        CauseScope fault(Reason::UnboundPageFault);
        WriteScope w(&isa2, Register::Cr3, 9, Mode::NoEffect, [&] { return raw; });
        raw[1] = 9;
    }
    q.close(1);
    assert(!q.ready());
    assert(q.footer().unbound_writes == 1);
    assert(q.footer().reasons[unsigned(Reason::UnboundPageFault)]);
    // A live effectful younger owner at the closing cut cannot disappear.
    {
        Observer x(2, [&] { return raw; });
        int ctx, endpoint_;
        x.registerEndpoint(&ctx, Domain::Unknown);
        x.registerEndpoint(&endpoint_, Domain::Unknown);
        x.resume();
        x.bind(10, 0, hot.data());
        x.seal(10);
        {
            OwnerScope owner(&ctx, 11, Application::Immediate);
            WriteScope w(&endpoint_, Register::Cr3, 9, Mode::NoEffect, [&] { return raw; });
        }
        x.close(1);
        assert(!x.ready());
        assert(x.footer().unbound_writes == 1);
    }
    // Explicit terminal storage permit suppresses only one matched storage child.
    {
        Observer x(3, [&] { return raw; });
        int ctx, endpoint_;
        x.registerEndpoint(&ctx, Domain::Unknown);
        x.registerEndpoint(&endpoint_, Domain::Unknown);
        x.resume();
        {
            OwnerScope owner(&ctx, 10, Application::Deferred);
            WriteScope outer(&endpoint_, Register::Cr3, 9, Mode::WithEffect, [&] { return raw; });
            StoragePermit permit(&endpoint_, Register::Cr3);
            WriteScope storage(&endpoint_, Register::Cr3, 9, Mode::NoEffect, [&] { return raw; });
        }
        x.bind(10, 0, hot.data());
        x.seal(10);
        x.close(1);
        assert(x.ready());
        assert(x.footer().retained_writes == 1);
    }
    // Every independent identical root command survives nesting and repeated calls.
    {
        Observer x(4, [&] { return raw; });
        int ctx, root;
        x.registerEndpoint(&ctx, Domain::Unknown);
        x.registerEndpoint(&root, Domain::ContextAllTranslations);
        x.resume();
        {
            OwnerScope owner(&ctx, 10, Application::Immediate);
            CommandScope a(&root, CommandKind::DemapPage, 13, 17);
            CommandScope b(&root, CommandKind::DemapPage, 13, 17);
        }
        x.bind(10, 0, hot.data());
        x.seal(10);
        x.close(1);
        assert(x.ready());
        assert(x.footer().retained_commands == 2);
    }
    // Bounds affect observation only. All actual operations remain counted.
    {
        Observer x(5, [&] { return raw; });
        int ctx, endpoint_;
        x.registerEndpoint(&ctx, Domain::Unknown);
        x.registerEndpoint(&endpoint_, Domain::Unknown);
        x.limits(2, 1, 1);
        x.resume();
        x.bind(1, 0, hot.data());
        {
            OwnerScope owner(&ctx, 1, Application::Deferred);
            {
                WriteScope a(&endpoint_, Register::Cr3, 9, Mode::NoEffect, [&] { return raw; });
            }
            {
                WriteScope b(&endpoint_, Register::Cr3, 9, Mode::NoEffect, [&] { return raw; });
            }
        }
        x.seal(1);
        x.close(1);
        assert(!x.ready());
        assert(x.footer().total_writes == 2 && x.footer().unbound_writes == 2 &&
               x.footer().suppressed == 2);
    }
    // Lifecycle replacement in-window remains sticky across repeated resume calls.
    {
        Observer x(6, [&] { return raw; });
        x.resume();
        x.bind(1, 0, hot.data());
        x.seal(1);
        x.lifecycle();
        x.resume();
        x.resume();
        x.close(1);
        assert(!x.ready());
        assert(x.footer().reasons[unsigned(Reason::LifecycleInWindow)]);
    }
    // Filtered and squashed effectful owners are separate explicit diagnostics.
    {
        Observer x(7, [&] { return raw; });
        int ctx, endpoint_;
        x.registerEndpoint(&ctx, Domain::Unknown);
        x.registerEndpoint(&endpoint_, Domain::Unknown);
        x.resume();
        x.bind(1, 0, hot.data());
        x.seal(1);
        for (unsigned id : {2, 3}) {
            OwnerScope owner(&ctx, id, Application::Immediate);
            WriteScope w(&endpoint_, Register::Cr3, 9, Mode::NoEffect, [&] { return raw; });
        }
        x.seal(2);
        x.destroy(3);
        x.close(1);
        assert(x.footer().unbound_writes == 2);
        assert(x.footer().reasons[unsigned(Reason::FilteredInterior)] &&
               x.footer().reasons[unsigned(Reason::EffectfulSquash)]);
    }
    // Metadata close waits for a seal; forced teardown never manufactures one.
    {
        Observer x(8, [&] { return raw; });
        x.resume();
        x.bind(1, 0, hot.data());
        x.close(1);
        assert(!x.ready());
        x.forceClose();
        assert(!x.ready());
        assert(x.footer().pending == 1 && x.footer().sealed == 0);
    }
    // Disabled observation performs no register reads.
    {
        int unknown;
        unsigned reads = 0;
        OwnerScope owner(&unknown, 1, Application::Immediate);
        WriteScope write(&unknown, Register::Cr3, 0, Mode::WithEffect, [&] {
            ++reads;
            return raw;
        });
        assert(reads == 0);
    }
    // A consumed edge cannot absorb an independent reentrant low-level call.
    {
        Observer x(9, [&] { return raw; });
        int ctx, root, tlb;
        x.registerEndpoint(&ctx, Domain::Unknown);
        x.registerEndpoint(&root, Domain::ContextAllTranslations);
        x.registerEndpoint(&tlb, Domain::Instruction);
        x.edge(&root, &tlb);
        x.resume();
        x.bind(1, 0, hot.data());
        {
            OwnerScope owner(&ctx, 1, Application::Deferred);
            CommandScope command(&root, CommandKind::FlushAll);
            EdgeScope edge(command, &root, &tlb);
            CommandScope visit(&tlb, CommandKind::FlushAll, 0, 0, true);
            CommandScope reentrant(&tlb, CommandKind::FlushAll, 0, 0, true);
        }
        x.seal(1);
        x.close(1);
        assert(!x.ready());
        assert(x.footer().unbound_commands == 2 && x.footer().visits == 1);
    }
    // Younger effects before an older member's own deferred effects fail order.
    {
        Observer x(10, [&] { return raw; });
        int ctx, isa;
        x.registerEndpoint(&ctx, Domain::Unknown);
        x.registerEndpoint(&isa, Domain::Unknown);
        x.resume();
        x.bind(1, 0, hot.data());
        x.seal(1);
        x.bind(2, 1, hot.data());
        {
            OwnerScope owner(&ctx, 3, Application::Immediate);
            WriteScope write(&isa, Register::Cr3, 0, Mode::NoEffect, [&] { return raw; });
        }
        {
            OwnerScope owner(&ctx, 2, Application::Deferred);
            WriteScope write(&isa, Register::Cr3, 0, Mode::NoEffect, [&] { return raw; });
        }
        x.seal(2);
        x.bind(3, 2, hot.data());
        x.seal(3);
        x.close(3);
        assert(!x.ready());
        assert(x.footer().reasons[unsigned(Reason::EffectOrderUnsupported)]);
    }
    // Pending-member overflow abandons metadata, without inventing native seals.
    {
        Observer x(11, [&] { return raw; });
        x.limits(1, 1, 1);
        x.resume();
        x.bind(1, 0, hot.data());
        x.bind(2, 1, hot.data());
        x.seal(2);
        x.close(2);
        assert(!x.ready());
        assert(x.footer().hot == 2 && x.footer().sealed == 1 && x.footer().pending == 1);
    }
    // Endpoint teardown prevents pointer reuse from retaining another observer.
    {
        int endpoint_;
        {
            Observer x(12, [&] { return raw; });
            x.registerEndpoint(&endpoint_, Domain::Unknown);
            assert(endpoint(&endpoint_).observer == &x);
        }
        assert(!endpoint(&endpoint_).observer);
    }
    // Destruction from an older lifecycle cannot erase a reused native sequence.
    {
        Observer x(13, [&] { return raw; });
        int ctx, isa;
        x.registerEndpoint(&ctx, Domain::Unknown);
        x.registerEndpoint(&isa, Domain::Unknown);
        x.resume();
        auto old = x.generation();
        {
            OwnerScope owner(&ctx, 1, Application::Immediate, old);
            WriteScope write(&isa, Register::Cr3, 0, Mode::NoEffect, [&] { return raw; });
        }
        x.lifecycle();
        x.resume();
        auto current = x.generation();
        assert(current != old);
        {
            OwnerScope owner(&ctx, 1, Application::Immediate, current);
            WriteScope write(&isa, Register::Cr3, 0, Mode::NoEffect, [&] { return raw; });
        }
        x.destroy(1, old);
        x.bind(1, 0, hot.data(), current);
        x.seal(1, current);
        x.close(1);
        assert(x.ready());
        assert(x.footer().retained_writes == 1);
    }
    review_first_cut(false);
    review_first_cut(true);
    review_nested_first_cut();
    review_stale_propagation_after_cut();
    review_prefix_before_cut();
    review_terminal();
    review_drain();
    review_live_crossing();
    review_forced_live_seal();
    std::cout << "MMU production observer tests passed\n";
}
