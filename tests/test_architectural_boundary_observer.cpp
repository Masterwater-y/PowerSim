#include "mem/functional_architectural_boundaries.hh"
#include <cassert>
#include <iostream>
using namespace gem5::functional_arch;
namespace functional_mmu = gem5::functional_mmu;
void pageFault() {
  int tc = 0, fault = 0;
  fastsim::fst::mmuctl::State state{};
  gem5::functional_mmu::Observer mmu(0, [&] { return state; });
  mmu.registerEndpoint(&tc,
                       fastsim::fst::mmuctl::Domain::ContextAllTranslations);
  mmu.resume();
  Observer observer(
      0, &tc, mmu, [&] { return state; }, [] { return std::uint64_t(0); });
  observer.completedTakeover();
  observer.arm(0, 0);
  AcceptedFacts facts;
  facts.cause = fastsim::fst::archb::Cause::PageFault;
  facts.exact_cause = true;
  facts.pc_known = true;
  facts.mode = 1;
  facts.vector = 14;
  facts.fault_va = 0x1234;
  facts.error_code = 0;
  facts.carrier = 0;
  auto scope = observer.beginAccepted(&fault, facts, {});
  {
    gem5::functional_mmu::CauseScope cause(
        fastsim::fst::mmuctl::Reason::UnboundPageFault, &fault, &tc);
    gem5::functional_mmu::CommandScope command(
        &tc, fastsim::fst::mmuctl::CommandKind::DemapPage, 0x1234, 0);
  }
  auto token = scope.redirect(&fault, &tc, 0x8001, 2, 14, 0, 0);
  assert(token.valid());
  scope.seal();
  assert(observer.coverage().invocation_sealed == 1);
  assert(observer.coverage().boundary_commands == 1);
  observer.closeAdmission();
  observer.forceClose();
  assert(observer.finished());
  auto before = observer.coverage().accepted;
  auto ignored = observer.beginAccepted(&fault, facts, {});
  assert(!ignored.token().valid());
  assert(observer.coverage().accepted == before);
}

void exactCapability() {
  int tc = 0, fault = 0, other = 0;
  State state{};
  functional_mmu::Observer mmu(0, [&] { return state; });
  mmu.registerEndpoint(&tc,
                       fastsim::fst::mmuctl::Domain::ContextAllTranslations);
  mmu.resume();
  Observer observer(0, &tc, mmu, [&] { return state; }, [] { return 0; });
  observer.completedTakeover();
  observer.arm(0, 0);
  AcceptedFacts facts;
  facts.cause = wire::Cause::PageFault;
  facts.exact_cause = true;
  facts.pc_known = true;
  facts.mode = 1;
  facts.vector = 14;
  auto scope = observer.beginAccepted(&fault, facts, {});
  {
    functional_mmu::CauseScope cause(
        fastsim::fst::mmuctl::Reason::UnboundPageFault, &other, &tc);
    functional_mmu::CommandScope command(
        &tc, fastsim::fst::mmuctl::CommandKind::DemapPage, 0, 0);
  }
  scope.redirect(&fault, &tc, 0x8001, 2, 14, 0, 0);
  scope.seal();
  assert(observer.coverage().boundary_commands == 0);
  assert(mmu.footer().unbound_commands == 1);
  observer.forceClose();
}
void completeEntry(bool advance_first) {
  int tc = 0, irq = 0;
  State state{};
  unsigned reads = 0;
  functional_mmu::Observer mmu(0, [&] {
    ++reads;
    return state;
  });
  mmu.registerEndpoint(&tc,
                       fastsim::fst::mmuctl::Domain::ContextAllTranslations);
  mmu.resume();
  Observer observer(
      0, &tc, mmu,
      [&] {
        ++reads;
        return state;
      },
      [] { return 0; });
  wire::Header header;
  header.policy[0] = 1;
  header.coverage = 255;
  observer.attach(advance_first ? "tmp/p4-architectural-bundle/advance-first"
                                : "tmp/p4-architectural-bundle/misc-first",
                  header);
  observer.completedTakeover();
  observer.arm(0, 0);
  AcceptedFacts facts;
  facts.cause = wire::Cause::ExternalInterrupt;
  facts.exact_cause = true;
  facts.pc_known = true;
  facts.carrier = 2;
  facts.mode = 1;
  facts.vector = 32;
  auto scope = observer.beginAccepted(&irq, facts, {});
  auto token = scope.redirect(&irq, &tc, 0x8001, 1, 32, 0, 0);
  scope.seal();
  unsigned char hot[64]{};
  mmu.bind(7, 0, hot);
  observer.successfulHot(7, hot, 0, 0, {}, true, token, 0x8001, 4, true);
  struct Macro {
    std::uint64_t begin_record = 0, end_record = 1, semantic_identity = 1,
                  code_identity = 2;
  } macro;
  observer.macroSpan(macro);
  observer.closeAdmission();
  assert(!observer.finished());
  if (advance_first) {
    observer.postAdvance(7, 0, {});
    assert(!observer.finished());
    mmu.seal(7);
    observer.postUpdateSeal(7);
  } else {
    mmu.seal(7);
    observer.postUpdateSeal(7);
    assert(!observer.finished());
    observer.postAdvance(7, 0, {});
  }
  std::cerr << "advance_first=" << advance_first
            << " complete=" << observer.coverage().terminal_complete
            << " finished=" << observer.finished() << "\n";
  assert(observer.finished());
  assert(observer.coverage().terminal_complete == 1);
  assert(observer.coverage().sealed_members == 1);
  auto before = reads;
  observer.postUpdateSeal(7);
  observer.postAdvance(7, 0, {});
  observer.arm(0, 0);
  observer.forceClose();
  assert(reads == before);
}
void nestedIsUnbound() {
  int tc = 0, outer = 0, inner = 0;
  State state{};
  functional_mmu::Observer mmu(0, [&] { return state; });
  mmu.registerEndpoint(&tc,
                       fastsim::fst::mmuctl::Domain::ContextAllTranslations);
  mmu.resume();
  Observer observer(0, &tc, mmu, [&] { return state; }, [] { return 0; });
  wire::Header h;
  h.policy[0] = 1;
  h.coverage = 255;
  observer.attach("tmp/p4-architectural-bundle/nested", h);
  observer.completedTakeover();
  observer.arm(0, 0);
  AcceptedFacts f;
  f.cause = wire::Cause::PageFault;
  f.exact_cause = true;
  f.pc_known = true;
  f.mode = 1;
  f.vector = 14;
  auto one = observer.beginAccepted(&outer, f, {});
  {
    auto two = observer.beginAccepted(&inner, f, {});
    functional_mmu::CauseScope cause(
        fastsim::fst::mmuctl::Reason::UnboundPageFault, &inner, &tc);
    {
      functional_mmu::CommandScope command(
          &tc, fastsim::fst::mmuctl::CommandKind::DemapPage, 0, 0);
    }
    two.seal();
  }
  one.redirect(&outer, &tc, 0x8001, 2, 14, 0, 0);
  one.seal();
  observer.forceClose();
  assert(observer.coverage().boundary_commands == 0);
  assert(mmu.footer().unbound_commands == 1);
  assert(!(observer.coverage().status & wire::Complete));
}
void invocationCutAndOverflow() {
  for (bool overflow : {false, true}) {
    int tc = 0, fault = 0;
    State state{};
    unsigned reads = 0;
    functional_mmu::Observer mmu(0, [&] {
      ++reads;
      return state;
    });
    mmu.registerEndpoint(&tc,
                         fastsim::fst::mmuctl::Domain::ContextAllTranslations);
    mmu.resume();
    Observer observer(
        0, &tc, mmu,
        [&] {
          ++reads;
          return state;
        },
        [&] {
          ++reads;
          return 0;
        });
    wire::Header h;
    h.policy[0] = 1;
    h.coverage = 255;
    observer.attach(overflow ? "tmp/p4-architectural-bundle/overflow"
                             : "tmp/p4-architectural-bundle/invocation-cut",
                    h);
    observer.completedTakeover();
    observer.arm(0, 0);
    AcceptedFacts f;
    f.cause = wire::Cause::PageFault;
    f.exact_cause = true;
    f.pc_known = true;
    f.mode = 1;
    f.vector = 14;
    auto scope = observer.beginAccepted(&fault, f, {});
    {
      functional_mmu::CauseScope cause(
          fastsim::fst::mmuctl::Reason::UnboundPageFault, &fault, &tc);
      for (unsigned i = 0; i < (overflow ? 257u : 1u); ++i) {
        functional_mmu::CommandScope command(
            &tc, fastsim::fst::mmuctl::CommandKind::DemapPage, 0, 0);
      }
    }
    scope.redirect(&fault, &tc, 0x8001, 2, 14, 0, 0);
    if (!overflow) {
      observer.closeAdmission();
      auto before = reads;
      scope.seal();
      assert(reads == before);
    } else
      scope.seal();
    observer.forceClose();
    assert(observer.finished());
    assert(mmu.footer().unbound_commands == (overflow ? 257 : 1));
    assert(observer.coverage().boundary_commands == 0);
  }
}

void scenarioMatrix() {
  for (unsigned scenario = 0; scenario < 22; ++scenario) {
    int tc = 0, fault = 0;
    State state{};
    std::uint64_t cr2 = 0;
    functional_mmu::Observer mmu(0, [&] { return state; });
    mmu.resume();
    Observer observer(0, &tc, mmu, [&] { return state; }, [&] { return cr2; });
    wire::Header h;
    h.policy[0] = 1;
    h.coverage = 255;
    observer.attach(
        "tmp/p4-architectural-bundle/scenario-" + std::to_string(scenario), h);
    if (scenario != 1)
      observer.completedTakeover();
    if (scenario == 2)
      observer.executed();
    observer.arm(4096, 0);
    if (scenario == 3)
      observer.omitted();
    if (scenario == 18)
      observer.lifecycle();
    if (scenario == 19)
      observer.arm(4096, 0);
    const bool prefix = scenario == 4 || scenario == 5 || scenario == 10 ||
                        (scenario >= 14 && scenario <= 17);
    mmu.registerEndpoint(&tc,
                         fastsim::fst::mmuctl::Domain::ContextAllTranslations);
    unsigned char hot[64]{};
    hot[0] = 1;
    auto origin = observer.newMacroOrigin();
    struct Macro {
      std::uint64_t begin_record, end_record, semantic_identity = 1,
                                              code_identity = 2;
    };
    Macro before{0, 1};
    if (scenario == 14 || scenario == 15) {
      functional_mmu::OwnerScope owner(
          &tc, 7, fastsim::fst::mmuctl::Application::Immediate);
      functional_mmu::CommandScope command(
          &tc, fastsim::fst::mmuctl::CommandKind::DemapPage, 0, 0);
    }
    if (prefix) {
      mmu.bind(7, 0, hot);
      observer.successfulHot(7, hot, 0, 0, origin, false, {}, 0, 0, true);
    }
    AcceptedFacts facts;
    facts.exact_cause = true;
    facts.pc_known = true;
    facts.mode = 1;
    facts.cause = wire::Cause::PageFault;
    facts.vector = 14;
    facts.fault_va = 0x1234;
    facts.carrier = scenario == 6 || scenario == 21 ? 1 : 0;
    facts.pc = 4096;
    if (scenario == 11)
      facts.cause = wire::Cause::NMI;
    if (scenario == 12)
      facts.mode = 2;
    if (scenario == 16)
      facts.pc_known = false;
    if (scenario == 20)
      facts.exact_cause = false;
    auto scope = observer.beginAccepted(
        &fault, facts, scenario == 5 ? observer.newMacroOrigin() : origin);
    if (scenario == 14 || scenario == 15) {
      functional_mmu::CauseScope cause(
          fastsim::fst::mmuctl::Reason::UnboundPageFault, &fault, &tc);
      functional_mmu::CommandScope command(
          &tc, fastsim::fst::mmuctl::CommandKind::DemapPage, 0x1234, 0);
    }
    auto token = scope.redirect(&fault, &tc, 0x8001, 2, 14, 4096, 0);
    cr2 = 0x1234;
    if (scenario == 13)
      state[1] = 5;
    scope.seal();
    if (scenario == 15) {
      functional_mmu::OwnerScope owner(
          &tc, 7, fastsim::fst::mmuctl::Application::Immediate);
      functional_mmu::CommandScope command(
          &tc, fastsim::fst::mmuctl::CommandKind::DemapPage, 1, 0);
    }
    if (prefix && scenario != 10) {
      mmu.seal(7);
      observer.postUpdateSeal(7);
      observer.macroSpan(before, false);
    }
    hot[0] = 2;
    const auto ordinal = unsigned(prefix);
    mmu.bind(8, ordinal, hot);
    const auto kind = scenario == 7 ? 5 : scenario == 9 ? 2 : 4;
    observer.successfulHot(8, hot, ordinal, ordinal, {}, true, token, 0x8001,
                           kind, scenario != 6, scenario != 17);
    Macro entry{ordinal, ordinal + 1};
    if (scenario != 10)
      observer.macroSpan(entry);
    observer.closeAdmission();
    if (scenario == 10) {
      assert(!observer.finished());
      mmu.seal(7);
      observer.postUpdateSeal(7);
      observer.macroSpan(before, false);
      observer.macroSpan(entry);
    }
    mmu.seal(8);
    observer.postUpdateSeal(8);
    observer.postAdvance(8, scenario == 8 ? 0x8002 : 0,
                         scenario == 8 ? token : BoundaryToken{});
    observer.forceClose();
    assert(observer.finished());
    const bool complete = scenario == 0 || scenario == 4 || scenario == 10 ||
                          scenario == 14 || scenario == 21;
    assert(bool(observer.coverage().status & wire::Complete) == complete);
    assert(!observer.qualified(token));
  }
}
void eventOverflow() {
  int tc = 0, fault = 0;
  State state{};
  functional_mmu::Observer mmu(0, [&] { return state; });
  mmu.resume();
  Observer observer(0, &tc, mmu, [&] { return state; }, [] { return 0; });
  wire::Header h;
  h.policy[0] = 1;
  h.coverage = 255;
  observer.attach("tmp/p4-architectural-bundle/events-overflow", h);
  observer.completedTakeover();
  observer.arm(0, 0);
  AcceptedFacts f;
  f.exact_cause = true;
  f.pc_known = true;
  f.mode = 1;
  f.cause = wire::Cause::ExternalInterrupt;
  f.carrier = 2;
  f.vector = 32;
  for (unsigned i = 0; i < 33; ++i) {
    auto scope = observer.beginAccepted(&fault, f, {});
    scope.redirect(&fault, &tc, 0x8001, 1, 32, 0, 0);
    scope.seal();
  }
  observer.forceClose();
  assert(observer.finished());
  assert(observer.coverage().accepted == 33);
  assert(observer.coverage().event_rows == 32);
  assert(observer.coverage().suppressed_events == 1);
  assert(!(observer.coverage().status & wire::Complete));
}
void unknownCoreCheck() {
  int tc = 0;
  State state{};
  functional_mmu::Observer mmu(0, [&] { return state; });
  mmu.resume();
  Observer observer(0, &tc, mmu, [&] { return state; }, [] { return 0; });
  observer.completedTakeover();
  observer.arm(0, 0);
  observer.coreFactsUnknown(true);
  observer.closeAdmission();
  assert(observer.finished());
  assert(!(observer.coverage().status & wire::Complete));
}
int main() {
  pageFault();
  exactCapability();
  completeEntry(false);
  completeEntry(true);
  nestedIsUnbound();
  invocationCutAndOverflow();
  scenarioMatrix();
  eventOverflow();
  unknownCoreCheck();
  std::cout << "architectural boundary production observer matrix passed\n";
}
