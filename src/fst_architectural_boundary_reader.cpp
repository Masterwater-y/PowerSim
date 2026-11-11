#include "fastsim/fst_architectural_boundary_reader.hpp"
#include "fastsim/fst_retirement.hpp"
#include "fst_bundle_input.hpp"
#include "fst_boundary_macro_match.hpp"
#include <fstream>
#include <map>
namespace fastsim::fst {
namespace {
using namespace archb;
using bundle_detail::BundleInput;
constexpr std::array<unsigned, 7> Sizes{0, 96, 192, 128, 64, 160, 320};
void fold(std::uint64_t &h, const Bytes &b) {
  h = hash_bytes(h, b.data(), b.size());
}
template <class T> T native_read(std::ifstream &in) {
  T t{};
  in.read(reinterpret_cast<char *>(&t), sizeof(t));
  check(bool(in), "bundle truncated original");
  return t;
}
struct SourceEvent {
  std::uint64_t count = 0, hot_hash = HashSeed, begin = Absent, end = Absent;
  std::optional<RetiredFrontendMacro> macro;
  archb::Accepted accepted;
  bool prefix_verified = false;
};
class MetadataCursor {
  BundleInput in_;
  std::map<std::uint64_t, ArchitecturalBoundaryEvent> events_;
  std::uint64_t last_ = 0;

public:
  explicit MetadataCursor(const std::string &path) : in_(path + ".archb.gz") {
    decode_header(in_.read(160));
    decode_epoch(in_.frame(Sizes));
  }
  ArchitecturalBoundaryEvent next(std::uint64_t id) {
    check(id > last_, "boundary metadata replay order");
    for (;;) {
      auto found = events_.find(id);
      if (found != events_.end() && found->second.terminal.event) {
        auto result = found->second;
        events_.erase(found);
        last_ = id;
        return result;
      }
      auto b = in_.frame(Sizes);
      const auto k = bundle_detail::kind(b);
      if (k == 2) {
        auto a = decode_accepted(b);
        check(events_.size() < 32, "boundary metadata lookahead bound");
        events_.emplace(a.event, ArchitecturalBoundaryEvent{a, {}, {}, {}});
      } else if (k == 3) {
        auto s = decode_invocation_seal(b);
        auto it = events_.find(s.event);
        check(it != events_.end(), "metadata invocation origin");
        it->second.invocation = s;
      } else if (k == 5) {
        auto t = decode_terminal(b);
        auto it = events_.find(t.event);
        check(it != events_.end(), "metadata terminal origin");
        it->second.terminal = t;
      } else if (k == 6)
        mmuctl::bad("boundary owner has no event");
    }
  }
};
} // namespace
struct ArchitecturalBoundaryReader::Impl {
  archb::Header header;
  archb::Epoch epoch;
  archb::Footer footer;
  std::unique_ptr<bundle_detail::BundleInput> replay;
  std::unique_ptr<MetadataCursor> metadata, control_metadata;
  std::optional<archb::EntryMember> next_member, current;
  std::map<std::uint64_t, ArchitecturalBoundaryEvent> replay_events;
  std::vector<ArchitecturalBoundaryEvent> before;
  std::uint64_t next = 0, body = archb::body_seed();
  bool done = false;
  void advance() {
    next_member.reset();
    while (!done) {
      auto b = replay->frame(Sizes);
      auto k = bundle_detail::kind(b);
      if (k == 6) {
        check(b == archb::encode(footer) && body == footer.body_hash,
              "archb replay source changed");
        replay->eof();
        done = true;
        break;
      }
      fold(body, b);
      if (k == 2) {
        auto a = archb::decode_accepted(b);
        check(replay_events.size() < 32, "archb replay live bound");
        replay_events.emplace(a.event,
                              ArchitecturalBoundaryEvent{a, {}, {}, {}});
      } else if (k == 3) {
        auto s = archb::decode_invocation_seal(b);
        auto it = replay_events.find(s.event);
        check(it != replay_events.end(), "archb replay invocation");
        it->second.invocation = s;
      } else if (k == 4) {
        next_member = archb::decode_entry_member(b);
        return;
      } else if (k == 5) {
        auto t = archb::decode_terminal(b);
        auto it = replay_events.find(t.event);
        check(it != replay_events.end(), "archb replay terminal");
        replay_events.erase(it);
      } else
        mmuctl::bad("archb replay frame");
    }
  }
};
ArchitecturalBoundaryReader::~ArchitecturalBoundaryReader() = default;
ArchitecturalBoundaryReader::ArchitecturalBoundaryReader(
    const std::string &path, std::uint32_t core, std::uint64_t records,
    const std::string &policy)
    : impl_(std::make_unique<Impl>()) {
  using namespace archb;
  auto &p = *impl_;
  BundleInput input(path + ".archb.gz"), core_input(path + ".core.gz"),
      ffe(path + ".ffe.gz");
  p.header = decode_header(input.read(160));
  check(p.header.core == core && mmuctl::hex(p.header.policy) == policy,
        "archb core/policy");
  StreamValidator validator(p.header);
  auto eb = input.frame(Sizes);
  p.epoch = decode_epoch(eb);
  validator.accept(p.epoch);
  std::uint64_t body = body_seed();
  fold(body, eb);
  auto ch = core_v2::decode_header(core_input.read(128));
  check(ch.core_id == core && ch.policy_sha256 == p.header.policy,
        "archb/core identity");
  auto fh = ffe.read(32);
  Cursor fc{fh};
  for (auto v :
       std::array<unsigned char, 8>{'F', 'S', 'T', 'F', 'F', 'E', '1', 0})
    check(fc.get(1) == v, "bundle frontend magic");
  check(fc.get(4) == 1 && fc.get(4) == 32 && fc.get(4) == core &&
            fc.get(4) == 80,
        "bundle frontend header");
  fc.zero(8);
  std::ifstream hot(path, std::ios::binary), pred(path, std::ios::binary),
      sem(path + ".fsem", std::ios::binary);
  check(bool(hot) && bool(pred) && bool(sem), "bundle missing original");
  Bytes hb(72);
  hot.read(reinterpret_cast<char *>(hb.data()), 72);
  check(bool(hot), "bundle hot header");
  Cursor hc{hb};
  for (auto byte :
       std::array<unsigned char, 8>{'F', 'S', 'T', 'R', 'C', '0', '1', 0})
    check(hc.get(1) == byte, "bundle hot magic");
  check(hc.get(4) == 7, "bundle hot version");
  check(hc.get(4) == 72 && hc.get(4) == 64 && hc.get(4) == core &&
            hc.get(8) == records,
        "bundle hot identity");
  const auto features = hc.get(8);
  check(!(features & ~((1ull << 19) - 1)) &&
            (features & RequiredFeatures) == RequiredFeatures,
        "bundle missing required features");
  auto sh = native_read<RetirementHeader>(sem);
  RetirementHeader expected;
  check(sh.magic == expected.magic && sh.version == 2 && sh.header_size == 56 &&
            sh.core_id == core && sh.record_count == records && sh.flags == 1 &&
            sh.row_size == 16 && !sh.reserved && sh.row_count <= records,
        "bundle fsem header");
  std::uint64_t sem_count = 0;
  std::optional<RetirementRow> sem_next;
  auto advance_sem = [&] {
    if (sem_count < sh.row_count) {
      auto r = native_read<RetirementRow>(sem);
      check(r.record_ordinal < records &&
                (r.hardware_entry == 1 || r.hardware_entry == 3) &&
                (!sem_next || r.record_ordinal > sem_next->record_ordinal),
            "bundle fsem row");
      sem_next = r;
      ++sem_count;
    } else
      sem_next.reset();
  };
  advance_sem();
  std::optional<EntryMember> member;
  std::map<std::uint64_t, SourceEvent> events;
  std::map<std::uint64_t, archb::Terminal> pending_macro_checks;
  // The archb stream can be read ahead to its next member, so a terminal for
  // an event without members may precede the FFE row that proves its prefix.
  std::multimap<std::uint64_t, Accepted> pending_prefix_checks;
  std::optional<RetiredFrontendMacro> last_macro;
  const auto prefix_matches = [](const Accepted &a,
                                 const RetiredFrontendMacro &m) {
    return a.prefix_flags && a.prefix_begin == m.begin_record &&
           a.prefix_end == m.end_record && !(m.flags & kFrontendComplete);
  };
  std::uint64_t count = 0, hot_hash = HashSeed,
                payload = core_v2::payload_seed(), macros = 0, macro_begin = 0,
                na = 0, boundary = 0, unknown = 0;
  bool arch_done = false;
  auto read_arch = [&] {
    member.reset();
    while (!arch_done) {
      auto b = input.frame(Sizes);
      auto k = bundle_detail::kind(b);
      if (k == 6) {
        p.footer = decode_footer(b);
        validator.finish(p.footer);
        check(p.footer.body_hash == body, "archb body hash");
        input.eof();
        arch_done = true;
        break;
      }
      fold(body, b);
      if (k == 2) {
        auto a = decode_accepted(b);
        validator.accept(a);
        check(events.size() < 32, "bundle live event bound");
        SourceEvent se;
        se.accepted = a;
        se.prefix_verified =
            !a.prefix_flags || (last_macro && prefix_matches(a, *last_macro));
        events.emplace(a.event, se);
        if (a.predecessor_kind == 1) {
          check(a.predecessor_ordinal < records &&
                    a.predecessor_ordinal <= (UINT64_MAX - 72) / 64,
                "bundle predecessor ordinal");
          pred.seekg(72 + a.predecessor_ordinal * 64);
          std::array<unsigned char, 64> raw{};
          pred.read(reinterpret_cast<char *>(raw.data()), 64);
          check(bool(pred) && row_hash(raw.data()) == a.predecessor_hash,
                "bundle original predecessor hash");
        }
      } else if (k == 3) {
        validator.accept(decode_invocation_seal(b));
      } else if (k == 4) {
        auto r = decode_entry_member(b);
        validator.accept(r);
        member = r;
        return;
      } else if (k == 5) {
        auto t = decode_terminal(b);
        auto it = events.find(t.event);
        check(it != events.end(), "bundle terminal event");
        auto &e = it->second;
        if ((t.seal_flags & 64) && !e.prefix_verified) {
          check(e.accepted.prefix_begin >= macro_begin,
                "boundary partial prefix ffe mismatch");
          pending_prefix_checks.emplace(e.accepted.prefix_begin, e.accepted);
        }
        check(t.members == e.count && t.member_hot_hash == e.hot_hash,
              "bundle terminal member hash");
        if (t.members) {
          if (e.macro) {
            check(bundle_detail::terminal_matches_frontend(t, *e.macro),
                  "bundle terminal frontend span");
          } else {
            // An incomplete event can close before the FFE macro carrying
            // its final member. Validate its exact observed extent now and
            // its frontend identities when that macro is read.
            check(t.status == 2 && e.begin == macro_begin &&
                      t.entry_begin == e.begin && t.entry_end == e.end &&
                      t.entry_end <= count && pending_macro_checks.size() < 32 &&
                      pending_macro_checks.emplace(e.begin, t).second,
                  "bundle terminal frontend span");
          }
          if (t.status == 1)
            check(
                (e.macro->flags & (kFrontendComplete | kFrontendStartsMacro)) ==
                    (kFrontendComplete | kFrontendStartsMacro),
                "complete boundary has partial frontend");
        }
        validator.accept(t);
        events.erase(it);
      } else
        mmuctl::bad("bundle unexpected archb frame");
    }
  };
  for (;;) {
    const auto tag = core_input.read(1)[0];
    if (tag == 255) {
      auto f = core_v2::decode_footer(core_input.read(64));
      check(f.records == records && count == records && macro_begin == count &&
                f.macros == macros && f.hot_hash == hot_hash &&
                f.payload_hash == payload && f.na_rom_rows == na &&
                f.boundary_rows == boundary &&
                f.unknown_applicable_rows == unknown,
            "bundle core footer");
      core_input.eof();
      break;
    }
    payload = hash_bytes(payload, &tag, 1);
    if (tag == 2) {
      auto b = core_input.read(80);
      fold(payload, b);
      auto m = core_v2::decode_macro(b);
      check(m.begin_record == macro_begin && m.end_record == count &&
                m.begin_record < m.end_record &&
                m.observed_uops == count - macro_begin && !m.reserved &&
                m.instruction_size <= 15 && !(m.flags & ~kFrontendValueFlags) &&
                !(m.known_flags & ~kFrontendRequiredKnown) &&
                !(m.flags & ~m.known_flags),
            "bundle core macro");
      check(ffe.read(1)[0] == 1 && ffe.read(80) == b,
            "bundle independent ffe mismatch");
      const auto pending_prefix = pending_prefix_checks.equal_range(m.begin_record);
      for (auto it = pending_prefix.first; it != pending_prefix.second; ++it)
        check(prefix_matches(it->second, m),
              "boundary partial prefix ffe mismatch");
      pending_prefix_checks.erase(pending_prefix.first, pending_prefix.second);
      auto pending = pending_macro_checks.find(m.begin_record);
      if (pending != pending_macro_checks.end()) {
        check(bundle_detail::terminal_matches_frontend(pending->second, m),
              "bundle deferred terminal frontend span");
        pending_macro_checks.erase(pending);
      }
      for (auto &[id, e] : events) {
        (void)id;
        e.prefix_verified |= prefix_matches(e.accepted, m);
        if (e.count && e.begin == m.begin_record) {
          check(e.end == m.end_record, "boundary macro member coverage");
          e.macro = m;
        }
      }
      last_macro = m;
      macro_begin = count;
      ++macros;
      continue;
    }
    check(tag == 1 && count < records, "bundle core tag/population");
    auto b = core_input.read(128);
    fold(payload, b);
    auto row = core_v2::decode_row(b);
    check(row.record_ordinal == count && row.macro_begin == macro_begin &&
              row.source_count <= 256 && row.destination_count <= 256 &&
              row.operand_count ==
                  unsigned(row.source_count) + row.destination_count,
          "bundle core row identity");
    std::vector<CoreFunctionalOperand> ops;
    for (unsigned i = 0; i < row.operand_count; ++i) {
      auto ob = core_input.read(24);
      fold(payload, ob);
      ops.push_back(core_v2::decode_operand(ob));
    }
    core_v2::validate_row(ch, row, ops);
    std::array<unsigned char, 64> raw{};
    hot.read(reinterpret_cast<char *>(raw.data()), 64);
    check(bool(hot) && row.hot_record_hash == row_hash(raw.data()),
          "bundle hot core hash");
    hot_hash = hash_bytes(hot_hash, raw.data(), 64);
    if (!member && !arch_done)
      read_arch();
    auto ext = core_v2::extension(row);
    na += bool(ext.not_applicable_flags);
    unknown += core_v2::unknown_applicable(row);
    if (member && member->ordinal == count) {
      auto r = *member;
      check(ext.extension_known & 4 && ext.boundary_event == r.event &&
                ext.boundary_context == r.context &&
                ext.boundary_generation == r.generation &&
                r.hot_hash == row.hot_record_hash &&
                r.micro_pc == row.micro_pc &&
                r.macro_begin == row.macro_begin &&
                r.rom_kind == unsigned(ext.rom_kind),
            "bundle core/entry member mismatch");
      check(sem_next && sem_next->record_ordinal == count &&
                sem_next->hot_record_hash == row.hot_record_hash,
            "bundle fsem membership");
      auto &e = events.at(r.event);
      if (ext.not_applicable_flags ||
          ext.semantic_origin == core_v2::Origin::HardwareEntryRom) {
        const auto &accepted = e.accepted;
        const auto unsupported = reason(Reason::UnsupportedCause) |
                                 reason(Reason::UnsupportedMode) |
                                 reason(Reason::Nested);
        check((accepted.known & 4) &&
                  (accepted.cause == Cause::PageFault ||
                   accepted.cause == Cause::ExternalInterrupt) &&
                  accepted.mode == 1 && !(accepted.fact_flags & 16) &&
                  !accepted.parent_event &&
                  !(accepted.reason_mask & unsupported),
              "bundle ROM N/A has unsupported accepted ancestry");
      }
      if (r.member_flags & 1)
        check(bool(sem_next->hardware_entry & 2) ==
                  bool(e.accepted.prefix_flags &&
                       e.accepted.cause == Cause::PageFault),
              "boundary precise-fault abandonment mismatch");
      if (!e.count)
        e.begin = r.macro_begin;
      e.end = count + 1;
      ++e.count;
      e.hot_hash = hash_bytes(e.hot_hash, raw.data(), 64);
      ++boundary;
      member.reset();
    } else
      check(!(ext.extension_known & 4), "core token has no exact entry member");
    if (member)
      check(member->ordinal > count, "archb member skipped");
    if (sem_next && sem_next->record_ordinal == count) {
      check(sem_next->hot_record_hash == row.hot_record_hash,
            "bundle fsem hot hash");
      advance_sem();
    }
    ++count;
  }
  if (!arch_done) {
    read_arch();
  }
  check(!member && arch_done && !sem_next && pending_macro_checks.empty() &&
            pending_prefix_checks.empty() &&
            sh.stream_hash == hot_hash &&
            sem.peek() == std::char_traits<char>::eof(),
        "bundle unconsumed member/semantics");
  check(ffe.read(1)[0] == 255, "bundle frontend footer");
  auto ff = ffe.read(24);
  Cursor cff{ff};
  check(cff.get(8) == records && cff.get(8) == macros && cff.get(8) == hot_hash,
        "bundle frontend footer identity");
  ffe.eof();
  check(p.footer.hot == records && p.footer.hot_hash == hot_hash &&
            p.footer.na_rom_rows == na && p.footer.member_rows == boundary,
        "bundle cross-file footer populations");
  check(!(p.footer.status & Complete) || !unknown,
        "complete bundle has applicable unknown core facts");
  p.replay = std::make_unique<BundleInput>(path + ".archb.gz");
  check(p.replay->read(160) == encode(p.header) &&
            p.replay->frame(Sizes) == encode(p.epoch),
        "archb changed on reopen");
  fold(p.body, encode(p.epoch));
  p.metadata = std::make_unique<MetadataCursor>(path);
  p.control_metadata = std::make_unique<MetadataCursor>(path);
}
void ArchitecturalBoundaryReader::observe(std::uint64_t ordinal,
                                          const void *hot,
                                          const CoreFunctionalRow &core) {
  auto &p = *impl_;
  clear_current();
  check(ordinal == p.next && ordinal < p.footer.hot, "archb replay ordinal");
  if (!p.next_member && !p.done)
    p.advance();
  auto e = core_v2::extension(core);
  if (p.next_member && p.next_member->ordinal == ordinal) {
    auto m = *p.next_member;
    check(m.hot_hash == row_hash(hot) && e.boundary_event == m.event &&
              e.boundary_context == m.context &&
              e.boundary_generation == m.generation &&
              core.micro_pc == m.micro_pc && core.macro_begin == m.macro_begin,
          "archb replay exact binding");
    auto it = p.replay_events.find(m.event);
    check(it != p.replay_events.end(), "archb replay accepted missing");
    if (m.member_flags & 1)
      p.before.push_back(it->second);
    p.current = m;
    p.next_member.reset();
  } else
    check(!e.boundary_event, "archb replay missing token member");
  ++p.next;
  if (p.next == p.footer.hot) {
    p.advance();
    check(p.done && !p.next_member, "archb replay trailing members");
  }
}
void ArchitecturalBoundaryReader::clear_current() {
  impl_->current.reset();
  impl_->before.clear();
}
const archb::EntryMember *ArchitecturalBoundaryReader::current() const {
  return impl_->current ? &*impl_->current : nullptr;
}
const archb::Header &ArchitecturalBoundaryReader::header() const {
  return impl_->header;
}
const archb::Epoch &ArchitecturalBoundaryReader::epoch() const {
  return impl_->epoch;
}
const archb::Footer &ArchitecturalBoundaryReader::coverage() const {
  return impl_->footer;
}
bool ArchitecturalBoundaryReader::ready() const {
  return impl_->footer.status & archb::Complete;
}
ArchitecturalBoundaryEvent
ArchitecturalBoundaryReader::replay_control_event(std::uint64_t event) {
  return impl_->control_metadata->next(event);
}
ArchitecturalBoundaryEvent
ArchitecturalBoundaryReader::next_event(std::uint64_t event) {
  return impl_->metadata->next(event);
}
const std::vector<ArchitecturalBoundaryEvent> &
ArchitecturalBoundaryReader::boundaries_before(std::uint64_t ordinal) const {
  static const std::vector<ArchitecturalBoundaryEvent> empty;
  return impl_->next && ordinal == impl_->next - 1 ? impl_->before : empty;
}
} // namespace fastsim::fst
