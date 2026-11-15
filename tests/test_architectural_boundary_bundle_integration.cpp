#include "fastsim/trace.hpp"
#include "mem/functional_architectural_boundaries.hh"
#include <cassert>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
using namespace fastsim;
namespace a = gem5::functional_arch;
namespace m = gem5::functional_mmu;
void create_bundle(const std::string &path, unsigned prefix = 0,
                   bool partial = false, bool missing_entry = false,
                   bool cut_short = false, bool entry_prefix_only = false,
                   bool control_write = false) {
  std::array<std::uint8_t, 32> policy{};
  policy[0] = 1;
  BinaryTraceWriter hot(path, 0);
  hot.enable_complete_dependencies();
  hot.enable_memory_request_roles();
  hot.enable_raw_syscall_microops();
  hot.enable_functional_retirement();
  hot.enable_frontend_facts();
  hot.enable_static_uop_facts(policy);
  fst::CoreFunctionalHeader ch;
  ch.policy_sha256 = policy;
  ch.architectural_registers = {39, 48, 1, 2, 1, 0, 6, 4096};
  ch.version = 2;
  ch.header_size = 128;
  ch.magic[6] = '2';
  fst::CoreFunctionalWriter core(path + ".core.gz", ch);
  int tc = 0, irq = 0, isa = 0;
  std::uint64_t cr2 = 0;
  a::MacroOrigin prefix_origin;
  fst::mmuctl::State state{};
  m::Observer mmu(0, [&] { return state; });
  mmu.registerEndpoint(&tc, fst::mmuctl::Domain::ContextAllTranslations);
  mmu.registerEndpoint(&isa, fst::mmuctl::Domain::Unknown);
  mmu.resume();
  a::Observer observer(7, &tc, mmu, [&] { return state; }, [&] { return cr2; });
  fst::archb::Header ah;
  ah.core = 0;
  ah.policy = policy;
  ah.coverage = 255;
  observer.attach(path, ah);
  observer.completedTakeover();
  observer.arm(4096, 0);
  for (unsigned n = 0; n < prefix; ++n) {
    const bool partial_member = partial && n + 1 == prefix;
    TraceRecord rec;
    rec.pc = 4096 + 4 * n;
    rec.op_class = 1;
    if (partial_member)
      rec.flags |= kMicroOp;
    fst::RetiredFrontendMacro macro;
    macro.begin_record = n;
    macro.end_record = n + 1;
    macro.pc = rec.pc;
    macro.bytes[0] = 0x90;
    macro.instruction_size = 1;
    macro.observed_uops = 1;
    macro.known_flags = fst::kFrontendRequiredKnown;
    macro.flags = (partial_member ? 0u : unsigned(fst::kFrontendComplete)) |
                  fst::kFrontendStartsMacro | fst::kFrontendDsbEligible;
    const std::uint64_t context = 0x31;
    macro.code_identity = fst::frontend_hash(
        fst::frontend_hash(fst::kFrontendHashSeed, &context, 8),
        macro.bytes.data(), 1);
    macro.semantic_identity = 10;
    fst::StaticUopFactsRow stat;
    stat.flags = fst::kStaticOperandRolesKnown;
    std::vector<fst::StaticUopOperand> ops;
    hot.append(rec, nullptr, {}, fst::MemoryRequestRole::Data, 0, false, false,
               nullptr, &stat, &ops);
    if (control_write && n == 0) {
      m::OwnerScope owner(&tc, 10 + n, fst::mmuctl::Application::Deferred);
      m::WriteScope write(&isa, fst::mmuctl::Register::Cr3, 0x1003,
                          fst::mmuctl::Mode::WithEffect, [&] { return state; });
      state[1] = 0x1003;
    }
    mmu.bind(10 + n, n, &rec);
    prefix_origin = observer.newMacroOrigin();
    observer.successfulHot(10 + n, &rec, n, n, prefix_origin, !partial_member,
                           {}, 0, 0, true);
    fst::CoreFunctionalRow row;
    row.record_ordinal = n;
    row.macro_begin = n;
    row.pc = rec.pc;
    row.code_identity = macro.code_identity;
    row.semantic_prefix_identity = 10;
    if (partial_member)
      row.static_flags = fst::kCoreMicroop | fst::kCoreFirstMicroop;
    row.known_flags = 255;
    row.decoder_context = context;
    row.decoder_origin = fst::CoreDecoderOrigin::ExactByteWitness;
    row.frontend_origin = fst::CoreFrontendOrigin::ObserverRecord;
    row.normalization_status = fst::CoreNormalizationStatus::Complete;
    fst::core_v2::Extension ext;
    ext.semantic_origin = fst::core_v2::Origin::DecodedRoot;
    ext.allocation_policy = fst::core_v2::AllocationPolicy::DecodedRootRules;
    ext.extension_known = 1;
    fst::core_v2::set_extension(row, ext);
    core.observe(&rec, row, {});
    hot.register_frontend_macro(macro);
    core.append_macro(macro);
    observer.macroSpan(macro, !partial_member);
    mmu.seal(10 + n);
    observer.postUpdateSeal(10 + n);
    observer.postAdvance(10 + n, 0, {});
  }
  a::AcceptedFacts facts;
  facts.cause = partial ? fst::archb::Cause::PageFault
                        : fst::archb::Cause::ExternalInterrupt;
  facts.exact_cause = true;
  facts.pc_known = true;
  facts.carrier = partial ? 0 : 2;
  facts.mode = 1;
  facts.vector = partial ? 14 : 32;
  facts.fault_va = partial ? 0x1234 : 0;
  facts.pc = 4096;
  auto scope = observer.beginAccepted(
      &irq, facts, partial ? prefix_origin : a::MacroOrigin{});
  if (partial) {
    m::CauseScope cause(fst::mmuctl::Reason::UnboundPageFault, &irq, &tc);
    m::CommandScope command(&tc, fst::mmuctl::CommandKind::DemapPage,
                            facts.fault_va, 0);
  }
  auto token =
      scope.redirect(&irq, &tc, 0x8001, partial ? 2 : 1, facts.vector, 4096, 0);
  if (partial)
    cr2 = facts.fault_va;
  scope.seal();
  fst::RetiredFrontendMacro macro;
  macro.begin_record = prefix;
  macro.end_record = 2 + prefix;
  macro.pc = 4096;
  macro.code_identity = 5;
  macro.semantic_identity = 9;
  macro.observed_uops = 2;
  macro.flags = fst::kFrontendPseudo | fst::kFrontendMicrocode |
                fst::kFrontendStartsMacro |
                (cut_short ? 0u : unsigned(fst::kFrontendComplete));
  macro.known_flags = fst::kFrontendRequiredKnown;
  for (unsigned i = 0; i < (missing_entry ? 0u : 2u); ++i) {
    const bool bound_entry = !entry_prefix_only || i == 0;
    TraceRecord rec;
    rec.pc = 4096;
    rec.op_class = -3;
    rec.flags |= kMicroOp;
    if (i && !cut_short)
      rec.flags |= kLastMicroOp;
    fst::StaticUopFactsRow stat;
    stat.flags = fst::kStaticOperandRolesKnown;
    std::vector<fst::StaticUopOperand> ops;
    hot.append(rec, nullptr, {}, fst::MemoryRequestRole::Data, 0x8001 + i, bound_entry,
               partial && i == 0, nullptr, &stat, &ops);
    mmu.bind(100 + i, i + prefix, &rec);
    observer.successfulHot(100 + i, &rec, i + prefix, prefix, {},
                           i == 1 && !cut_short, bound_entry ? token : a::BoundaryToken{}, 0x8001 + i,
                           i && !cut_short ? 4 : 2, i == 0);
    fst::CoreFunctionalRow row;
    row.record_ordinal = i + prefix;
    row.macro_begin = prefix;
    row.pc = 4096;
    row.code_identity = 5;
    row.semantic_prefix_identity = i ? 9 : 6;
    row.micro_pc = 0x8001 + i;
    row.known_flags = 234;
    row.frontend_origin = fst::CoreFrontendOrigin::ObserverRecord;
    row.normalization_status = fst::CoreNormalizationStatus::Complete;
    row.static_flags = fst::kCoreMicroop |
                       (i ? (cut_short ? 0u : unsigned(fst::kCoreLastMicroop))
                          : unsigned(fst::kCoreFirstMicroop));
    fst::core_v2::Extension extension;
    extension.not_applicable_flags = 21;
    extension.semantic_origin = fst::core_v2::Origin::HardwareEntryRom;
    extension.allocation_policy =
        fst::core_v2::AllocationPolicy::IndependentRomMember;
    extension.rom_kind = i && !cut_short
                             ? fst::core_v2::RomKind::UnconditionalEret
                             : fst::core_v2::RomKind::InternalBranch;
    extension.extension_known = 7;
    extension.boundary_event = token.event();
    extension.boundary_context = token.context();
    extension.boundary_generation = token.generation();
    if (!bound_entry) {
      extension = {};
      extension.semantic_origin = fst::core_v2::Origin::UnsupportedRom;
      observer.coreFactsUnknown(true);
    }
    fst::core_v2::set_extension(row, extension);
    core.observe(&rec, row, {});
    if (i) {
      hot.register_frontend_macro(macro);
      core.append_macro(macro);
      observer.macroSpan(macro, !cut_short);
      observer.closeAdmission();
    }
    mmu.seal(100 + i);
    observer.postUpdateSeal(100 + i);
    observer.postAdvance(100 + i, i ? 0 : 0x8002,
                         i ? a::BoundaryToken{} : token);
  }
  if (missing_entry || entry_prefix_only) {
    observer.closeAdmission();
    observer.forceClose();
  }
  assert(observer.finished());
  assert(observer.coverage().terminal_complete ==
         (missing_entry || cut_short || entry_prefix_only ? 0u : 1u));
  hot.close();
  core.finish((missing_entry ? 0 : 2) + prefix);
  // Test fixture authoring declares the complete newly written bundle. The core
  // file was emitted as v2 by its writer; no v1 payload is relabeled.
  std::fstream file(path, std::ios::in | std::ios::out | std::ios::binary);
  std::uint64_t flags = 0;
  file.seekg(32);
  file.read(reinterpret_cast<char *>(&flags), 8);
  flags |= fst::kArchitecturalBoundaryBundle | fst::kLogicalMmuControls |
           fst::kCoreFunctionalFacts;
  file.seekp(32);
  file.write(reinterpret_cast<const char *>(&flags), 8);
}
std::vector<unsigned char> unpack(const std::string &path) {
  auto f = gzopen(path.c_str(), "rb");
  assert(f);
  std::vector<unsigned char> v;
  unsigned char b[4096];
  int n;
  while ((n = gzread(f, b, sizeof(b))) > 0)
    v.insert(v.end(), b, b + n);
  assert(n == 0);
  assert(gzclose(f) == Z_OK);
  return v;
}
void pack(const std::string &path, const std::vector<unsigned char> &v) {
  auto f = gzopen(path.c_str(), "wb1");
  assert(f);
  assert(gzwrite(f, v.data(), static_cast<unsigned>(v.size())) ==
         static_cast<int>(v.size()));
  assert(gzclose(f) == Z_OK);
}
void put64(std::vector<unsigned char> &v, std::size_t offset, std::uint64_t n) {
  for (unsigned i = 0; i < 8; ++i)
    v[offset + i] = static_cast<unsigned char>(n >> (i * 8));
}
void corruption_tests(const std::string &path) {
  auto core = unpack(path + ".core.gz"), arch = unpack(path + ".archb.gz"),
       mmu = unpack(path + ".mmuctl.gz"), ffe = unpack(path + ".ffe.gz");
  unsigned mutation = 0;
  auto reject = [&] {
    ++mutation;
    bool caught = false;
    try {
      InstructionSliceTraceSource slice(
          std::make_unique<BinaryTraceSource>(path), 0, 1);
      TraceRecord r;
      slice.next(r);
    } catch (const std::runtime_error &) {
      caught = true;
    }
    if (!caught)
      std::cerr << "mutation accepted " << mutation << "\n";
    assert(caught);
  };
  // The selected macro is the ordinary prefix at ordinal zero. Every changed
  // member below is outside that slice; repair FNV to exercise cross-file
  // joins.
  const std::size_t entry = 128 + 129 + 81 + 1;
  for (auto offset : {entry + 112, entry + 96, entry + 24, entry + 80}) {
    auto changed = core;
    changed[offset] ^= 1;
    put64(changed, changed.size() - 32,
          fst::mmuctl::hash_bytes(fst::core_v2::payload_seed(),
                                  changed.data() + 128,
                                  changed.size() - 128 - 65));
    pack(path + ".core.gz", changed);
    reject();
    pack(path + ".core.gz", core);
  }
  for (auto offset :
       {std::size_t(160 + 96 + 88), std::size_t(160 + 96 + 192 + 128 + 24),
        std::size_t(160 + 96 + 192 + 128 + 48)}) {
    auto changed = arch;
    changed[offset] ^= 1;
    put64(changed, changed.size() - 320 + 32,
          fst::archb::hash_bytes(fst::archb::body_seed(), changed.data() + 160,
                                 changed.size() - 160 - 320));
    pack(path + ".archb.gz", changed);
    reject();
    pack(path + ".archb.gz", arch);
  }
  // A concatenated gzip member and trailing decoded byte are both forbidden.
  auto changed = arch;
  changed.push_back(0);
  pack(path + ".archb.gz", changed);
  reject();
  pack(path + ".archb.gz", arch);
  auto f = gzopen((path + ".archb.gz").c_str(), "ab1");
  assert(f);
  assert(gzwrite(f, "x", 1) == 1);
  assert(gzclose(f) == Z_OK);
  reject();
  pack(path + ".archb.gz", arch);
  changed = ffe;
  changed[32 + 81 + 1 + 32] ^= 1;
  pack(path + ".ffe.gz", changed);
  reject();
  pack(path + ".ffe.gz", ffe);
  changed = mmu;
  changed[160 + 96 + 64 + 48] ^= 1;
  put64(changed, changed.size() - 320 + 160,
        fst::mmuctl_v2::hash_bytes(fst::mmuctl_v2::body_seed(),
                                   changed.data() + 160,
                                   changed.size() - 160 - 320));
  pack(path + ".mmuctl.gz", changed);
  reject();
  pack(path + ".mmuctl.gz", mmu);
  {
    std::fstream hot(path, std::ios::in | std::ios::out | std::ios::binary);
    std::uint64_t flags = 0;
    hot.seekg(32);
    hot.read(reinterpret_cast<char *>(&flags), 8);
    for (auto bad :
         {flags | (1ull << 19), flags & ~(1ull << 18), flags & ~(1ull << 17)}) {
      hot.seekp(32);
      hot.write(reinterpret_cast<const char *>(&bad), 8);
      hot.flush();
      reject();
    }
    hot.seekp(32);
    hot.write(reinterpret_cast<const char *>(&flags), 8);
    hot.flush();
  }
  {
    std::fstream sem(path + ".fsem",
                     std::ios::in | std::ios::out | std::ios::binary);
    char original = 0;
    sem.seekg(56 + 12);
    sem.read(&original, 1);
    const char bad = original ^ 2;
    sem.seekp(56 + 12);
    sem.write(&bad, 1);
    sem.flush();
    reject();
    sem.seekp(56 + 12);
    sem.write(&original, 1);
    sem.flush();
  }
  changed = core;
  changed[6] = '1';
  changed[8] = 1;
  pack(path + ".core.gz", changed);
  reject();
  pack(path + ".core.gz", core);
  BinaryTraceSource valid(path);
  assert(valid.architectural_boundary_bundle_ready());
}
// Repair the complete production body hash after a semantically targeted edit.
void rewrite_frames(
    const std::string &path, bool controls,
    const std::function<void(unsigned, fst::mmuctl::Bytes &)> &change) {
  auto bytes = unpack(path);
  std::size_t footer = 0;
  for (std::size_t at = 160; at < bytes.size();) {
    fst::mmuctl::Bytes prefix(bytes.begin() + at, bytes.begin() + at + 8);
    fst::mmuctl::Cursor c{prefix};
    auto kind = c.get(2);
    c.get(2);
    auto size = c.get(4);
    assert(size >= 8 && size <= bytes.size() - at);
    fst::mmuctl::Bytes frame(bytes.begin() + at, bytes.begin() + at + size);
    change(static_cast<unsigned>(kind), frame);
    assert(frame.size() == size);
    std::copy(frame.begin(), frame.end(), bytes.begin() + at);
    if (kind == 6)
      footer = at;
    at += size;
  }
  assert(footer && footer + 320 == bytes.size());
  put64(bytes, footer + (controls ? 160 : 32),
        fst::mmuctl::hash_bytes(controls ? fst::mmuctl_v2::body_seed()
                                         : fst::archb::body_seed(),
                                bytes.data() + 160, footer - 160));
  pack(path, bytes);
}
unsigned expect_lineage_rejection(const std::string &path, const char *label) {
  try {
    InstructionSliceTraceSource slice(std::make_unique<BinaryTraceSource>(path),
                                      0, 1);
    TraceRecord r;
    slice.next(r);
  } catch (const std::runtime_error &) {
    return 0;
  }
  std::cerr << "FIX1 RED: " << label
            << " accepted outside the selected prefix slice\n";
  return 1;
}
unsigned predecessor_lineage_tests(const std::string &path) {
  create_bundle(path, 2);
  BinaryTraceSource valid(path);
  assert(valid.architectural_boundary_bundle_ready());
  auto original = unpack(path + ".archb.gz");
  std::array<unsigned char, 64> older{};
  std::ifstream hot(path, std::ios::binary);
  hot.seekg(72);
  hot.read(reinterpret_cast<char *>(older.data()), 64);
  assert(hot);
  const auto older_hash = fst::archb::row_hash(older.data());
  unsigned failures = 0;
  for (bool before_first : {false, true}) {
    rewrite_frames(path + ".archb.gz", false, [&](unsigned kind, auto &frame) {
      if (kind == 2) {
        auto a = fst::archb::decode_accepted(frame);
        assert(a.predecessor_ordinal == 1);
        a.predecessor_kind = before_first ? 2 : 1;
        a.predecessor_ordinal = before_first ? fst::archb::Absent : 0;
        a.predecessor_hash = before_first ? 0 : older_hash;
        frame = fst::archb::encode(a);
      }
      if (kind == 5) {
        auto t = fst::archb::decode_terminal(frame);
        t.predecessor_ordinal = before_first ? fst::archb::Absent : 0;
        t.predecessor_hash = before_first ? 0 : older_hash;
        frame = fst::archb::encode(t);
      }
    });
    failures += expect_lineage_rejection(
        path, before_first ? "false BeforeFirst after two owners"
                           : "older real retained predecessor");
    pack(path + ".archb.gz", original);
  }
  // An explicitly unknown predecessor remains an incomplete source, never a
  // fabricated BeforeFirst certificate or an arbitrary earlier owner.
  rewrite_frames(path + ".archb.gz", false, [&](unsigned kind, auto &frame) {
    using namespace fst::archb;
    if (kind == 2) {
      auto a = fst::archb::decode_accepted(frame);
      a.predecessor_kind = 0;
      a.predecessor_ordinal = Absent;
      a.predecessor_hash = 0;
      a.reason_mask |= reason(Reason::PredecessorUnknown);
      frame = fst::archb::encode(a);
    }
    if (kind == 5) {
      auto t = fst::archb::decode_terminal(frame);
      t.predecessor_ordinal = Absent;
      t.predecessor_hash = 0;
      t.status = 2;
      t.seal_flags &= ~1u;
      t.reason_mask |= reason(Reason::PredecessorUnknown);
      frame = fst::archb::encode(t);
    }
    if (kind == 6) {
      auto f = fst::archb::decode_footer(frame);
      f.status = Finalized;
      f.terminal_complete = 0;
      f.terminal_partial = 1;
      f.reasons[unsigned(Reason::PredecessorUnknown)] = 1;
      frame = fst::archb::encode(f);
    }
  });
  auto controls = unpack(path + ".mmuctl.gz");
  rewrite_frames(path + ".mmuctl.gz", true, [](unsigned kind, auto &frame) {
    if (kind == 6) {
      auto f = fst::mmuctl_v2::decode_footer(frame);
      f.status = fst::mmuctl_v2::Finalized;
      frame = fst::mmuctl_v2::encode(f);
    }
  });
  BinaryTraceSource unknown(path);
  assert(!unknown.architectural_boundary_bundle_ready());
  assert(unknown.core_functional_coverage()->na_rom_rows == 2);
  pack(path + ".archb.gz", original);
  pack(path + ".mmuctl.gz", controls);
  return failures;
}
unsigned rom_ancestry_tests(const std::string &path) {
  create_bundle(path, 0, false, false, true);
  BinaryTraceSource valid_cut(path);
  assert(!valid_cut.architectural_boundary_bundle_ready());
  assert(valid_cut.core_functional_coverage()->na_rom_rows == 2);
  const auto original = unpack(path + ".archb.gz"),
             controls = unpack(path + ".mmuctl.gz");
  unsigned failures = 0;
  for (unsigned mutation = 0; mutation < 4; ++mutation) {
    using namespace fst::archb;
    const auto reason_index =
        unsigned(mutation == 1   ? Reason::UnsupportedMode
                 : mutation == 2 ? Reason::Nested
                                 : Reason::UnsupportedCause);
    rewrite_frames(path + ".archb.gz", false, [&](unsigned kind, auto &frame) {
      if (kind == 2) {
        auto a = fst::archb::decode_accepted(frame);
        if (mutation == 0)
          a.cause = Cause::NMI;
        if (mutation == 1)
          a.mode = 2;
        if (mutation == 3)
          a.known &= ~4u;
        a.reason_mask |= 1u << reason_index;
        frame = fst::archb::encode(a);
      }
      if (kind == 3) {
        auto s = fst::archb::decode_invocation_seal(frame);
        s.status = 2;
        if (mutation == 1)
          s.mode = 2;
        frame = fst::archb::encode(s);
      }
      if (kind == 5) {
        auto t = fst::archb::decode_terminal(frame);
        t.status = 3;
        t.reason_mask |= 1u << reason_index;
        frame = fst::archb::encode(t);
      }
      if (kind == 6) {
        auto f = fst::archb::decode_footer(frame);
        f.terminal_partial = 0;
        f.terminal_unsupported = 1;
        if (mutation == 0) {
          f.accepted_irq = 0;
          f.accepted_other = 1;
        }
        f.reasons[reason_index] = 1;
        frame = fst::archb::encode(f);
      }
    });
    rewrite_frames(path + ".mmuctl.gz", true, [](unsigned kind, auto &frame) {
      using namespace fst::mmuctl_v2;
      if (kind == 2) {
        auto o = decode_owner(frame);
        if (o.owner_kind == OwnerKind::ArchitecturalBoundary)
          o.status = Unsupported;
        frame = fst::mmuctl_v2::encode(o);
      }
      if (kind == 6) {
        auto f = fst::mmuctl_v2::decode_footer(frame);
        f.unsupported_groups = 1;
        frame = fst::mmuctl_v2::encode(f);
      }
    });
    const char *labels[] = {"unsupported NMI ROM N/A ancestry",
                            "unsupported-mode ROM N/A ancestry",
                            "Nested-reason ROM N/A ancestry",
                            "unknown exact-cause ROM N/A ancestry"};
    failures += expect_lineage_rejection(path, labels[mutation]);
    pack(path + ".archb.gz", original);
    pack(path + ".mmuctl.gz", controls);
  }
  return failures;
}
// A source claiming readiness must not make an unreplayable initial event
// disappear from the wrapper's readiness decision after the first record.
struct InitialBoundaryFixture final : TraceSource {
  std::vector<fst::ArchitecturalBoundaryEvent> initial;
  unsigned emitted = 0;
  bool next(TraceRecord &record) override {
    initial.clear();
    record = {};
    return emitted++ < 2;
  }
  bool architectural_boundary_bundle_ready() const override { return true; }
  const std::vector<fst::ArchitecturalBoundaryEvent> &
  initial_architectural_boundaries() const override { return initial; }
  std::string description() const override { return "initial boundary fixture"; }
};
int main() {
  for (bool no_operations : {false, true}) {
    auto fixture = std::make_unique<InitialBoundaryFixture>();
    fst::ArchitecturalBoundaryEvent event;
    event.terminal.status = 1;
    event.terminal.entry_begin = no_operations ? 0 : fst::archb::Absent;
    if (!no_operations)
      event.operations.emplace();
    fixture->initial.push_back(event);
    ScoreRangeTraceSource invalid_initial(std::move(fixture), 1, 2, 2);
    assert(!invalid_initial.architectural_boundary_bundle_ready());
    TraceRecord first;
    assert(invalid_initial.next(first));
    assert(invalid_initial.initial_architectural_boundaries().empty());
    assert(!invalid_initial.architectural_boundary_bundle_ready());
  }
  const std::string path = "tmp/p4-architectural-bundle/integration.fst";
  std::filesystem::create_directories("tmp/p4-architectural-bundle");
  create_bundle(path);
  BinaryTraceSource source(path);
  assert(source.core_functional_version() == 2);
  assert(source.mmu_control_version() == 2);
  assert(!source.mmu_control_coverage());
  assert(source.architectural_boundary_bundle_ready());
  assert(!source.current_mmu_control_state_v2());
  assert(source.initial_architectural_boundaries().size() == 1);
  assert(source.initial_architectural_boundaries()[0].operations->empty());
  TraceRecord rec;
  for (unsigned i = 0; i < 2; ++i) {
    assert(source.next(rec));
    assert(source.initial_architectural_boundaries().empty());
    assert(source.current_architectural_boundary_member());
    assert(source.current_mmu_controls_v2());
    assert(source.current_mmu_control_state_v2() &&
           (*source.current_mmu_control_state_v2())[1] == 0);
    if (i == 0) {
      const auto &before = source.boundaries_before(i);
      assert(before.size() == 1);
      assert(before[0].terminal.status == 1);
      assert(before[0].operations && before[0].operations->empty());
    }
    assert(source.current_architectural_boundary_member()->ordinal == i);
    assert(source.current_core_functional_facts()->record_ordinal == i);
  }
  assert(!source.next(rec));
  assert(!source.current_architectural_boundary_member());
  assert(!source.current_mmu_controls_v2());
  assert(!source.current_mmu_control_state_v2());
  create_bundle(path, 1, false, false, false, false, true);
  {
    BinaryTraceSource control_transition(path);
    assert(control_transition.architectural_boundary_bundle_ready());
    assert(control_transition.next(rec));
    assert(control_transition.current_mmu_controls_v2()->operations.size() == 1);
    assert(!control_transition.current_mmu_control_state_v2());
    assert(control_transition.next(rec));
    assert(control_transition.current_mmu_control_state_v2() &&
           (*control_transition.current_mmu_control_state_v2())[1] == 0x1003);
    while (control_transition.next(rec)) {}
  }
  create_bundle(path);
  InstructionSliceTraceSource slice(std::make_unique<BinaryTraceSource>(path),
                                    0, 1);
  assert(slice.next(rec));
  assert(slice.current_architectural_boundary_member()->ordinal == 0);
  assert(slice.next(rec));
  assert(slice.current_architectural_boundary_member()->ordinal == 1);
  assert(!slice.architectural_boundary_bundle_ready());
  assert(!slice.next(rec));
  assert(!slice.current_architectural_boundary_member());
  // The initial event is a two-uop macro. A valid BEGIN can only follow it;
  // inspect this prefix without asking the deliberately short fixture for its
  // later score endpoint.
  ScoreRangeTraceSource full_prefix(std::make_unique<BinaryTraceSource>(path),
                                    2, 3, 3);
  assert(full_prefix.architectural_boundary_bundle_ready());
  assert(full_prefix.initial_architectural_boundaries().size() == 1);
  assert(full_prefix.next(rec));
  assert(full_prefix.boundaries_before(0).size() == 1);
  assert(full_prefix.architectural_boundary_bundle_ready());
  assert(full_prefix.current_mmu_control_state_v2() &&
         (*full_prefix.current_mmu_control_state_v2())[1] == 0);
  assert(full_prefix.next(rec));
  create_bundle(path, true);
  BinaryTraceSource prefixed(path);
  assert(prefixed.architectural_boundary_bundle_ready());
  assert(prefixed.next(rec));
  assert(!prefixed.current_architectural_boundary_member());
  assert(prefixed.next(rec));
  assert(prefixed.boundaries_before(1).size() == 1);
  assert(prefixed.boundaries_before(1)[0].accepted.predecessor_ordinal == 0);
  assert(prefixed.next(rec));
  assert(!prefixed.next(rec));
  ScoreRangeTraceSource later_boundary(std::make_unique<BinaryTraceSource>(path),
                                       1, 3, 3);
  assert(later_boundary.architectural_boundary_bundle_ready());
  assert(later_boundary.initial_architectural_boundaries().empty());
  assert(later_boundary.next(rec));
  assert(later_boundary.next(rec));
  assert(later_boundary.boundaries_before(1).size() == 1);
  assert(later_boundary.next(rec));
  assert(!later_boundary.next(rec));
  WarmupInstructionTraceSource warm(std::make_unique<BinaryTraceSource>(path),
                                    1, 1, 1, 2, true);
  assert(warm.next(rec));
  assert(!warm.current_architectural_boundary_member());
  assert(!warm.next(rec));
  assert(!warm.current_mmu_controls_v2());
  warm.start_measurement();
  assert(warm.next(rec));
  assert(warm.current_architectural_boundary_member()->ordinal == 1);
  assert(warm.boundaries_before(1).size() == 1);
  assert(!warm.architectural_boundary_bundle_ready());
  assert(warm.next(rec));
  assert(!warm.next(rec));
  assert(!warm.current_architectural_boundary_member());
  bool refused = false;
  try {
    upgrade_binary_trace_to_v7(path, path + ".copy", SyscallAbi::kUnknown);
  } catch (const std::exception &) {
    refused = true;
  }
  assert(refused);
  corruption_tests(path);
  create_bundle(path, true, true);
  BinaryTraceSource pf(path);
  assert(pf.architectural_boundary_bundle_ready());
  assert(pf.next(rec));
  assert(pf.next(rec));
  const auto &event = pf.boundaries_before(1).at(0);
  assert(event.accepted.prefix_flags == 3);
  assert(event.invocation->cr2_after == 0x1234);
  assert(event.operations && event.operations->size() == 1);
  assert(event.terminal.seal_flags & 64);
  create_bundle(path, false, false, true);
  BinaryTraceSource no_head(path);
  assert(!no_head.architectural_boundary_bundle_ready());
  assert(no_head.initial_architectural_boundaries().size() == 1);
  assert(no_head.initial_architectural_boundaries()[0].terminal.entry_begin ==
         fst::archb::Absent);
  ScoreRangeTraceSource unready(std::make_unique<BinaryTraceSource>(path),
                                1, 2, 2);
  assert(!unready.architectural_boundary_bundle_ready());
  assert(!no_head.next(rec));
  assert(no_head.initial_architectural_boundaries().empty());
  // A partial boundary without entry members can be read ahead of the hot
  // prefix whose FFE row proves it. The full audit must wait for that row.
  create_bundle(path, 2, true, true);
  BinaryTraceSource partial_no_head(path);
  assert(!partial_no_head.architectural_boundary_bundle_ready());
  assert(partial_no_head.next(rec));
  assert(partial_no_head.next(rec));
  assert(!partial_no_head.next(rec));
  {
    // Alter both copies so the independent-copy check still passes. The
    // resulting complete FFE span must disagree with the retired hot record,
    // even with a valid core payload hash.
    const auto core = unpack(path + ".core.gz"), ffe = unpack(path + ".ffe.gz");
    auto changed_core = core, changed_ffe = ffe;
    changed_core[128 + 129 + 81 + 129 + 1 + 48] |= fst::kFrontendComplete;
    changed_ffe[32 + 81 + 1 + 48] |= fst::kFrontendComplete;
    put64(changed_core, changed_core.size() - 32,
          fst::mmuctl::hash_bytes(fst::core_v2::payload_seed(),
                                  changed_core.data() + 128,
                                  changed_core.size() - 128 - 65));
    pack(path + ".core.gz", changed_core);
    pack(path + ".ffe.gz", changed_ffe);
    bool rejected = false;
    try {
      BinaryTraceSource invalid(path);
    } catch (const std::runtime_error &e) {
      rejected = std::string(e.what()).find("macro completion disagrees with retired records") !=
                 std::string::npos;
    }
    assert(rejected);
    pack(path + ".core.gz", core);
    pack(path + ".ffe.gz", ffe);
  }
  {
    // Keep the predecessor, end, and all hashes valid, but widen the claimed
    // partial prefix across two actual FFE spans. The deferred join itself
    // must reject this, rather than accepting a pending event at EOF.
    const auto arch = unpack(path + ".archb.gz");
    rewrite_frames(path + ".archb.gz", false, [](unsigned kind, auto &frame) {
      if (kind == 2) {
        auto accepted = fst::archb::decode_accepted(frame);
        assert(accepted.prefix_begin == 1 && accepted.prefix_end == 2);
        accepted.prefix_begin = 0;
        frame = fst::archb::encode(accepted);
      }
    });
    bool rejected = false;
    try {
      BinaryTraceSource invalid(path);
    } catch (const std::runtime_error &e) {
      rejected = std::string(e.what()).find("boundary partial prefix ffe mismatch") !=
                 std::string::npos;
    }
    assert(rejected);
    pack(path + ".archb.gz", arch);
  }
  create_bundle(path, false, false, false, true);
  BinaryTraceSource cut(path);
  assert(!cut.architectural_boundary_bundle_ready());
  assert(cut.core_functional_coverage()->na_rom_rows == 2);
  assert(cut.core_functional_coverage()->unknown_applicable_rows == 0);
  assert(cut.architectural_boundary_coverage()->terminal_partial == 1);
  create_bundle(path, false, false, false, false, true);
  BinaryTraceSource prefix_entry(path);
  assert(!prefix_entry.architectural_boundary_bundle_ready());
  assert(prefix_entry.architectural_boundary_coverage()->terminal_partial == 1);
  assert(prefix_entry.next(rec));
  assert(prefix_entry.current_architectural_boundary_member());
  assert(prefix_entry.next(rec));
  assert(!prefix_entry.current_architectural_boundary_member());
  assert(!prefix_entry.next(rec));
  const auto fix_failures = predecessor_lineage_tests(path + ".fix1") +
                            rom_ancestry_tests(path + ".fix1");
  assert(!fix_failures);
  // Leave a complete source for the private maintained-auditor regression.
  create_bundle(path, true, true);
  std::cout << "attached architectural bundle full-source integration passed\n";
}
