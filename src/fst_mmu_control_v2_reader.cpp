#include "fastsim/fst_mmu_control_v2_reader.hpp"
#include "fst_bundle_input.hpp"
#include <optional>
namespace fastsim::fst {
namespace {
using namespace mmuctl_v2;
using bundle_detail::BundleInput;
constexpr std::array<unsigned, 7> Sizes{0, 96, 64, 96, 48, 0, 320};
void fold(std::uint64_t &h, const Bytes &b) {
  h = hash_bytes(h, b.data(), b.size());
}
std::vector<Operation> read_ops(BundleInput &input, unsigned count,
                                std::uint64_t &hash) {
  check(count <= 256, "v2 controls operation bound");
  std::vector<Operation> ops;
  while (count--) {
    auto b = input.frame(Sizes);
    fold(hash, b);
    auto kind = bundle_detail::kind(b);
    if (kind == 3)
      ops.emplace_back(decode_write(b));
    else if (kind == 4)
      ops.emplace_back(decode_command(b));
    else
      mmuctl::bad("v2 expected operation");
  }
  validate_operations(ops, 256);
  return ops;
}
void conserve(std::uint64_t total, std::uint64_t a, std::uint64_t b,
              std::uint64_t c) {
  check(a <= total && b <= total - a && c == total - a - b,
        "v2 controls conservation");
}
} // namespace
struct MmuControlV2Reader::Impl {
  mmuctl_v2::Header header;
  mmuctl_v2::Baseline baseline;
  mmuctl_v2::Footer footer;
  std::unique_ptr<BundleInput> replay;
  std::optional<mmuctl_v2::Group> current;
  ArchitecturalBoundaryReader *boundaries = nullptr;
  std::vector<ArchitecturalBoundaryEvent> before, initial;
  bool initial_visible = true;
  std::uint64_t next = 0, hash = mmuctl_v2::body_seed();
};
MmuControlV2Reader::~MmuControlV2Reader() = default;
MmuControlV2Reader::MmuControlV2Reader(const std::string &path,
                                       std::uint32_t context,
                                       std::uint64_t records,
                                       const std::string &policy,
                                       ArchitecturalBoundaryReader &boundaries)
    : impl_(std::make_unique<Impl>()) {
  using namespace mmuctl_v2;
  auto &p = *impl_;
  p.boundaries = &boundaries;
  BundleInput input(path + ".mmuctl.gz");
  p.header = decode_header(input.read(160));
  check(p.header.context == context && mmuctl::hex(p.header.policy) == policy &&
            p.header.policy == boundaries.header().policy,
        "v2 controls identity");
  auto bb = input.frame(Sizes);
  p.baseline = decode_baseline(bb);
  auto &b = p.baseline;
  auto &e = boundaries.epoch();
  check(b.context == context && b.generation == e.generation &&
            b.state == e.state && b.known == e.state_known &&
            b.proof_flags == e.proof_flags,
        "v2 pre-effect baseline/epoch mismatch");
  std::ifstream hot(path, std::ios::binary);
  check(bool(hot), "v2 missing hot original");
  hot.seekg(72);
  auto body = body_seed();
  fold(body, bb);
  auto hot_hash = HashSeed;
  std::uint64_t count = 0, sealed = 0, last_boundary = 0, last_unbound = 0,
                groups[3]{}, writes[3]{}, commands[3]{}, unsupported = 0,
                invocations = 0, diagnostics = 0;
  State state = b.state;
  bool unknown = false;
  std::array<std::uint64_t, 16> reasons{};
  for (;;) {
    auto bytes = input.frame(Sizes);
    if (bundle_detail::kind(bytes) == 6) {
      p.footer = decode_footer(bytes);
      input.eof();
      break;
    }
    fold(body, bytes);
    auto owner = decode_owner(bytes);
    check(owner.context == context && (owner.generation == b.generation ||
                                       (owner.status & Unsupported)),
          "v2 owner generation/context");
    auto ops = read_ops(input, owner.count, body);
    check(owner.op_hash == operation_hash(ops), "v2 operation-list hash");
    unsigned bucket = unsigned(owner.owner_kind) - 1;
    ++groups[bucket];
    unsupported += bool(owner.status & (Unsupported | Partial));
    if (owner.reason != UINT32_MAX)
      ++reasons[owner.reason];
    std::optional<archb::InvocationSeal> boundary_invocation;
    if (owner.owner_kind == OwnerKind::RetiredInstruction) {
      check(owner.owner_id == count && count < records,
            "v2 retained population/order");
      std::array<unsigned char, 64> row{};
      hot.read(reinterpret_cast<char *>(row.data()), 64);
      check(bool(hot) && owner.hot_hash == row_hash(row.data()),
            "v2 original hot binding");
      hot_hash = hash_bytes(hot_hash, row.data(), 64);
      ++count;
      sealed += bool(owner.status & Sealed);
      unknown |= owner.status != Sealed;
    } else if (owner.owner_kind == OwnerKind::ArchitecturalBoundary) {
      check(owner.owner_id > last_boundary, "v2 boundary event order");
      auto event = boundaries.next_event(owner.owner_id);
      last_boundary = owner.owner_id;
      unknown |=
          !(event.accepted.known & 1) || event.accepted.controls_pre != state;
      boundary_invocation = event.invocation;
      if (!count && event.accepted.predecessor_ordinal == archb::Absent) {
        check(p.initial.size() < 32, "initial boundary bound");
        if (event.invocation && (event.invocation->known & 64))
          event.operations = ops;
        p.initial.push_back(event);
      }
      check(event.accepted.context == owner.context &&
                event.accepted.generation == owner.generation,
            "v2 boundary owner token");
      if (event.invocation) {
        auto &inv = *event.invocation;
        check(inv.op_count == owner.count && inv.op_hash == owner.op_hash,
              "v2 invocation ownership/hash");
        ++invocations;
      } else
        check(owner.status & (Unsupported | Partial),
              "v2 fabricated invocation seal");
      auto &terminal = event.terminal;
      check(terminal.control_ops == owner.count &&
                terminal.control_op_hash == owner.op_hash,
            "v2 terminal ownership/hash");
      if (terminal.entry_begin != archb::Absent)
        check(count == terminal.entry_begin, "v2 effect order boundary/entry");
      if (event.accepted.predecessor_kind == 1)
        check(count && event.accepted.predecessor_ordinal == count - 1,
              "v2 boundary must name exact preceding retained owner");
      else if (event.accepted.predecessor_kind == 2)
        check(!count, "v2 BeforeFirst follows retained owners");
      if (terminal.status == 1)
        check(owner.status == Sealed, "complete boundary has incomplete owner");
      unknown |= owner.status != Sealed;
    } else {
      check(owner.owner_id > last_unbound, "v2 unbound group order");
      last_unbound = owner.owner_id;
      diagnostics += owner.count;
      check(diagnostics <= 256, "v2 diagnostic detail bound");
      unknown = true;
    }
    for (const auto &op : ops) {
      if (auto w = std::get_if<Write>(&op)) {
        ++writes[bucket];
        unknown |= w->before != state || w->before_known != 15 ||
                   w->after_known != 15 ||
                   w->application == mmuctl::Application::External;
        state = w->after;
      } else {
        ++commands[bucket];
        unknown |= std::get<Command>(op).domain !=
                   mmuctl::Domain::ContextAllTranslations;
      }
    }
    if (boundary_invocation)
      unknown |= !(boundary_invocation->known & 32) ||
                 boundary_invocation->controls_post != state;
  }
  const auto &f = p.footer;
  const auto &a = boundaries.coverage();
  check(f.hot == records && count == records && f.sealed_retired == sealed &&
            f.hot_hash == hot_hash && f.body_hash == body &&
            f.retained_groups == groups[0] && f.boundary_groups == groups[1] &&
            f.unbound_groups == groups[2] &&
            f.unsupported_groups == unsupported &&
            f.retained_writes == writes[0] && f.boundary_writes == writes[1] &&
            f.retained_commands == commands[0] &&
            f.boundary_commands == commands[1],
        "v2 controls footer population");
  conserve(f.total_writes, f.retained_writes, f.boundary_writes,
           f.unbound_writes);
  conserve(f.total_commands, f.retained_commands, f.boundary_commands,
           f.unbound_commands);
  check(writes[2] <= f.unbound_writes && commands[2] <= f.unbound_commands &&
            f.suppressed_ops ==
                f.unbound_writes - writes[2] + f.unbound_commands - commands[2],
        "v2 suppressed conservation");
  check(f.boundary_groups == a.event_rows && f.event_rows == a.event_rows &&
            f.invocation_sealed_boundary == a.invocation_sealed &&
            f.invocation_sealed_boundary == invocations &&
            f.boundary_ops == a.boundary_ops &&
            f.boundary_writes == a.boundary_writes &&
            f.boundary_commands == a.boundary_commands &&
            f.boundary_ops == f.boundary_writes + f.boundary_commands,
        "v2 architectural boundary conservation");
  for (unsigned i = 0; i < 16; ++i) {
    check(f.reasons[i] >= reasons[i], "v2 reason count");
  }
  check(bool(f.status & Empty) == (!records && !f.event_rows),
        "v2 empty bundle");
  if (f.status & Complete) {
    check(boundaries.ready() && p.header.coverage == 255 && b.cut_kind == 1 &&
              b.known == 15 && sealed == records && !unknown &&
              !f.pending_owners && !f.suppressed_ops && !f.unbound_writes &&
              !f.unbound_commands && !f.unsupported_groups,
          "v2 false completeness");
    for (auto n : f.reasons)
      check(!n, "v2 complete with reasons");
  }
  p.replay = std::make_unique<BundleInput>(path + ".mmuctl.gz");
  check(p.replay->read(160) == encode(p.header) &&
            p.replay->frame(Sizes) == encode(p.baseline),
        "v2 controls changed on reopen");
  fold(p.hash, encode(p.baseline));
}
void MmuControlV2Reader::observe(std::uint64_t ordinal, const void *hot) {
  using namespace mmuctl_v2;
  auto &p = *impl_;
  clear_current();
  check(ordinal == p.next && ordinal < p.footer.hot, "v2 replay ordinal");
  for (;;) {
    auto b = p.replay->frame(Sizes);
    fold(p.hash, b);
    auto owner = decode_owner(b);
    auto ops = read_ops(*p.replay, owner.count, p.hash);
    check(owner.op_hash == operation_hash(ops), "v2 replay operation hash");
    if (owner.owner_kind != OwnerKind::RetiredInstruction) {
      if (owner.owner_kind == OwnerKind::ArchitecturalBoundary) {
        auto event = p.boundaries->replay_control_event(owner.owner_id);
        if (event.invocation && (event.invocation->known & 64))
          event.operations = ops;
        if (event.terminal.entry_begin != archb::Absent) {
          check(event.terminal.entry_begin == ordinal,
                "boundary replay placement");
          p.before.push_back(std::move(event));
        }
      }
      continue;
    }
    check(owner.owner_id == ordinal && owner.hot_hash == row_hash(hot),
          "v2 replay hot binding");
    p.current = Group{owner, std::move(ops)};
    ++p.next;
    break;
  }
  if (p.next == p.footer.hot) {
    for (;;) {
      auto b = p.replay->frame(Sizes);
      if (bundle_detail::kind(b) == 6) {
        check(b == encode(p.footer) && p.hash == p.footer.body_hash,
              "v2 replay source changed");
        p.replay->eof();
        break;
      }
      fold(p.hash, b);
      auto o = decode_owner(b);
      check(o.owner_kind != OwnerKind::RetiredInstruction,
            "v2 trailing retained owner");
      read_ops(*p.replay, o.count, p.hash);
    }
  }
}
void MmuControlV2Reader::clear_current() {
  impl_->current.reset();
  impl_->before.clear();
  impl_->initial_visible = false;
}
const mmuctl_v2::Group *MmuControlV2Reader::current() const {
  return impl_->current ? &*impl_->current : nullptr;
}
const mmuctl_v2::Header &MmuControlV2Reader::header() const {
  return impl_->header;
}
const mmuctl_v2::Baseline &MmuControlV2Reader::baseline() const {
  return impl_->baseline;
}
const mmuctl_v2::Footer &MmuControlV2Reader::coverage() const {
  return impl_->footer;
}
bool MmuControlV2Reader::ready() const {
  return impl_->footer.status & mmuctl_v2::Complete;
}
} // namespace fastsim::fst

namespace fastsim::fst {
const std::vector<ArchitecturalBoundaryEvent> &
MmuControlV2Reader::boundaries_before(std::uint64_t ordinal) const {
  static const std::vector<ArchitecturalBoundaryEvent> empty;
  return impl_->next && ordinal == impl_->next - 1 ? impl_->before : empty;
}
} // namespace fastsim::fst

namespace fastsim::fst {
const std::vector<ArchitecturalBoundaryEvent> &
MmuControlV2Reader::initial_boundaries() const {
  static const std::vector<ArchitecturalBoundaryEvent> empty;
  return impl_->initial_visible ? impl_->initial : empty;
}
} // namespace fastsim::fst
