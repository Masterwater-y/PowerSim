#ifndef FASTSIM_FST_MMU_CONTROLS_V2_CODEC
#define FASTSIM_FST_MMU_CONTROLS_V2_CODEC
#include "fastsim/fst_mmu_controls.hpp"
#include <type_traits>
namespace fastsim::fst::mmuctl_v2 {
using mmuctl::Bytes;
using mmuctl::check;
using mmuctl::Cursor;
using mmuctl::hash_bytes;
using mmuctl::put;
using mmuctl::row_hash;
using mmuctl::State;
inline constexpr auto HashSeed = mmuctl::HashSeed;
inline constexpr std::uint64_t Absent = UINT64_MAX;
inline constexpr std::uint64_t Finalized = 1, Complete = 2, Empty = 4;
inline constexpr std::uint32_t RequiredCoverage = 255;
inline Bytes prefix(unsigned kind, unsigned size) {
  Bytes b;
  put(b, kind, 2);
  put(b, 0, 2);
  put(b, size, 4);
  return b;
}
inline Cursor frame(const Bytes &b, unsigned kind, unsigned size) {
  check(b.size() == size, "bundle frame size");
  Cursor c{b};
  check(c.get(2) == kind && c.get(2) == 0 && c.get(4) == size,
        "bundle frame prefix");
  return c;
}
using mmuctl::Command;
using mmuctl::decode_command;
using mmuctl::decode_write;
using mmuctl::Operation;
using mmuctl::validate_operations;
using mmuctl::Write;
inline constexpr std::uint32_t Sealed = 1, Unsupported = 2, Partial = 4;
enum class OwnerKind : std::uint16_t {
  RetiredInstruction = 1,
  ArchitecturalBoundary = 2,
  Unbound = 3
};
enum class Frame : std::uint16_t {
  Baseline = 1,
  Owner,
  Write,
  Command,
  Footer = 6
};
inline std::uint64_t body_seed() {
  static constexpr char domain[] = "fst-mmuctl-body-v2";
  return hash_bytes(HashSeed, domain, sizeof(domain));
}
inline std::uint64_t operations_seed() {
  static constexpr char domain[] = "fst-mmuctl-ops-v2";
  return hash_bytes(HashSeed, domain, sizeof(domain));
}
inline std::uint64_t operation_hash(const std::vector<Operation> &ops) {
  validate_operations(ops, 256);
  auto h = operations_seed();
  for (const auto &op : ops) {
    auto b = mmuctl::encode(op);
    h = hash_bytes(h, b.data(), b.size());
  }
  return h;
}
inline const std::string &layout_descriptor() {
  static const std::string s = R"FSTLAYOUT(Frozen wire descriptor spr-mmuctl-v2

All integers below are explicit little-endian unsigned widths; offsets are bytes. No implicit C++ padding is on wire. Reserved bytes/unknown enum values/unsupported bits reject. `uN` means N-bit field, `zN` means N zero bytes. All rows use the existing 8-byte frame prefix `kind:u16,flags:u16=0,bytes:u32` unless explicitly stated otherwise. No native seq/pointer/tick/readiness/free-count/observed allocation outcome is serialized.

## 2. Hashes and identities

* `H32(hot)`: existing FNV-1a32 over exactly the original 64 hot bytes, seed 2166136261, prime 16777619. Every u64 hot-hash field is this value zero-extended; upper32 must be zero. `.core` keeps its existing u32 hot hash.
* `H64(bytes)`: existing FNV-1a64, seed 14695981039346656037, prime 1099511628211, modulo 2^64. `hot_hash` folds **all** original hot records, unmodified order, including warmup. Entry-member hot hash folds only the bound retained entry members in ordinal order, starting at the same seed.
* New body domains are ASCII including trailing NUL: `fst-archb-body-v1\0`, `fst-mmuctl-body-v2\0`, `fst-core-payload-v2\0`. Seed with H64(domain), then hash all nonfooter frames/tags and their exact encoded payload in actual wire order, excluding header and footer. Existing v1 domains/algorithms stay unchanged.
* Operation-list hash: H64 seeded with `fst-mmuctl-ops-v2\0`, followed by the exact encoded Write/Command frames in local index order. Empty operations have the domain seed, not zero. The same hash appears in controls Owner, archb InvocationSeal and archb Terminal when their operation list is known. No footer-to-footer hash dependency.
* Shared policy SHA256 is the maintained native source-policy digest. Each new layout has its own SHA256 of the final canonical UTF-8 layout descriptor. The curator must freeze the descriptor and its computed bytes before implementation release; do not reuse the v1 layout digest. Package manifests separately authenticate artifacts; FNV is an association/integrity check, not a cryptographic content certificate.
* Boundary identity is `(context:u32,generation:u32,event:u64)`. Generation nonzero after arming; event IDs begin at 1, monotonically increasing **within a context/generation**. Event numbering has no cross-core order meaning. UINT64_MAX is the absent ordinal sentinel; hashes accompanying absent ordinals are zero. Event 0 means no event. No “next ordinal” reconstruction.

## 4. `.mmuctl.gz` v2

Preserve v1 types/decoder/writer behavior behind version dispatch. New namespace/types should not overload the meaning of v1 `Member`, `Group`, `ready` or counters. Reuse existing Write96 and Command48 bytes/enums/indices/parent checks unchanged; application remains Deferred=0,Immediate=1,External=2. Invocation commands are boundary-owned with existing logical scope semantics; their values are not model timing.

Header160 retains v1 offsets: magic becomes `FSTMMU2\0`, version=2; policy/layout fields at16/48; required:u64 at80 (frozen v2 contract=1); context/coverage at88/92; limits owners/ops/diagnostics at96/100/104 remain4096/256/256; reserved108; size slots112 are `{96,64,96,48,0,320}`; trailing24 zero. Kinds remain Baseline1, Owner2, Write3, Command4, reserved/forbidden5, Footer6. Coverage255 = old63 + explicit boundary invocation64 + armed epoch128. A bound accepted-event marker is not a retained hot member.

| Frame | Exact fields after prefix |
|---|---|
| Baseline96 | `context:u32,generation:u32,cut_kind:u16,known:u16,proof_flags:u32,anchor_id:u64,anchor_hash:u64,state[4]:u64,epoch_reference:u64,z16` |
| Owner64 | `owner_kind:u16,flags:u16=0,reason:u32,context:u32,generation:u32,owner_id:u64,hot_hash:u64,count:u32,status:u32,op_hash:u64,causal_event:u64` |
| Footer320 | prefix + 23 u64 fields below + existing `reasons[16]:u64` |

Baseline cut_kind Unknown=0,ArmedEpoch=1,Empty=2. ArmedEpoch: known15, proof_flags copied from matching archb Epoch, anchor_id=UINT64_MAX,anchor_hash=0,epoch_reference=zero-extended generation; state exactly matches archb Epoch. Unknown cannot qualify. Empty has no events/hot and no fabricated baseline. This v2 baseline is pre-effect; v1 first-retained baseline remains unchanged.

Owner_kind RetiredInstruction=1,ArchitecturalBoundary=2,Unbound=3. Retired owner_id=original ordinal with original hot hash; boundary owner_id=event ID,hot_hash=0; unbound owner_id=monotonic diagnostic-group ID,hot_hash=0. Context/generation always explicit. `causal_event` is zero except an Unbound group's exact known accepted cause may reference an emitted Accepted without granting it ownership. reason=UINT32_MAX when absent, otherwise existing v1 Reason0..15. status bits `Sealed=1,Unsupported=2,Partial=4`; Complete qualification requires all retained owners and invocation-capable boundary owners sealed without unsupported/partial. One Owner followed immediately by count Write/Command frames, contiguous operation indexes0..count−1. No nested frame groups.

Exactly one Retired Owner per hot row, including zero effects; one Boundary Owner per emitted Accepted, including zero-effect IRQ and unsupported event markers. Unsupported event markers can have zero operations; their actual nonqualified effects remain separate Unbound groups. Proven invocation ownership with later missing entry may remain Boundary-owned but partial; it must not become ready. Partial invocation lists are explicitly partial and do not pretend InvocationSeal occurred. Controls Owner emission can wait for metadata, but retained owners are ascending ordinal, boundary owners ascending event ID, and a unified logical placement check enforces predecessor-before-boundary-before-entry; unbound diagnostics have no invented causal placement. Internal effect tickets detect inversions; no tickets go on wire.

Footer fields: `status,hot,sealed_retired,total_writes,retained_writes,boundary_writes,unbound_writes,total_commands,retained_commands,boundary_commands,unbound_commands,visits,retained_groups,boundary_groups,unbound_groups,unsupported_groups,suppressed_ops,pending_owners,hot_hash,body_hash,invocation_sealed_boundary,boundary_ops,event_rows`.

Conserve total writes/commands separately as retained+boundary+unbound; visits remain propagation visits, not commands. Unsupported/suppressed details retain scalar counts under existing fix1 rules, never count twice. Boundary groups=event_rows=archb.event_rows; invocation_sealed_boundary=archb.invocation_sealed; boundary_ops/writes/commands equal archb footer and matching event terminals. Every fully sealed invocation matches its Owner count/hash and InvocationSeal count/hash. All-source incomplete groups are visible; no promotion of six historical unbound PF commands.


Core framing exception: core-v2 uses existing u8 tags1/2/255; payload includes tags1/2 plus exact row/operand/macro bytes; excludes header/tag255/footer. All other frames use specified8-byte prefix.
Epoch proof bit1 is NoO3ExecutionInEpoch in the completed takeover generation, not historical cold memory/TLB state.
)FSTLAYOUT";
  return s;
}
inline constexpr std::array<std::uint8_t, 32> LayoutDigest = {
    0xdf, 0x78, 0xbc, 0xac, 0x5,  0x37, 0x1e, 0xd0, 0xea, 0x3b, 0x84,
    0x97, 0xf5, 0x62, 0x55, 0x32, 0x3f, 0x72, 0x21, 0xe5, 0x2b, 0x3e,
    0xf7, 0x84, 0x4f, 0x63, 0xd,  0x29, 0xac, 0xdc, 0x3d, 0x10};
struct Header {
  std::array<std::uint8_t, 32> policy{};
  std::uint32_t context = 0, coverage = 0, max_owners = 4096, max_ops = 256,
                max_diagnostics = 256;
};
inline Bytes encode(const Header &h) {
  check(h.policy != std::array<std::uint8_t, 32>{} && !(h.coverage & ~255u) &&
            h.max_owners == 4096 && h.max_ops == 256 &&
            h.max_diagnostics == 256,
        "v2 controls header");
  Bytes b{'F', 'S', 'T', 'M', 'M', 'U', '2', 0};
  put(b, 2, 2);
  put(b, 160, 2);
  put(b, 0x01020304, 4);
  b.insert(b.end(), h.policy.begin(), h.policy.end());
  b.insert(b.end(), LayoutDigest.begin(), LayoutDigest.end());
  put(b, 1, 8);
  put(b, h.context, 4);
  put(b, h.coverage, 4);
  put(b, h.max_owners, 4);
  put(b, h.max_ops, 4);
  put(b, h.max_diagnostics, 4);
  put(b, 0, 4);
  for (auto n : {96, 64, 96, 48, 0, 320})
    put(b, n, 4);
  b.resize(160);
  return b;
}
inline Header decode_header(const Bytes &b) {
  check(b.size() == 160, "v2 controls header size");
  Cursor c{b};
  for (auto v :
       std::array<unsigned char, 8>{'F', 'S', 'T', 'M', 'M', 'U', '2', 0})
    check(c.get(1) == v, "v2 controls magic");
  check(c.get(2) == 2 && c.get(2) == 160 && c.get(4) == 0x01020304,
        "v2 controls version/endian");
  Header h;
  for (auto &v : h.policy)
    v = static_cast<std::remove_reference_t<decltype(v)>>(c.get(1));
  for (auto v : LayoutDigest)
    check(c.get(1) == v, "v2 controls layout");
  check(c.get(8) == 1, "v2 controls flags");
  h.context =
      static_cast<std::remove_reference_t<decltype(h.context)>>(c.get(4));
  h.coverage =
      static_cast<std::remove_reference_t<decltype(h.coverage)>>(c.get(4));
  h.max_owners =
      static_cast<std::remove_reference_t<decltype(h.max_owners)>>(c.get(4));
  h.max_ops =
      static_cast<std::remove_reference_t<decltype(h.max_ops)>>(c.get(4));
  h.max_diagnostics =
      static_cast<std::remove_reference_t<decltype(h.max_diagnostics)>>(
          c.get(4));
  c.zero(4);
  for (auto n : {96, 64, 96, 48, 0, 320})
    check(c.get(4) == unsigned(n), "v2 controls sizes");
  c.zero(24);
  encode(h);
  return h;
}
struct Baseline {
  std::uint32_t context = 0;
  std::uint32_t generation = 0;
  std::uint16_t cut_kind = 0;
  std::uint16_t known = 0;
  std::uint32_t proof_flags = 0;
  std::uint64_t anchor_id = Absent;
  std::uint64_t anchor_hash = 0;
  State state{};
  std::uint64_t epoch_reference = 0;
};
inline void validate(const Baseline &r) {
  check(r.cut_kind <= 2 && !(r.known & ~15u) && !(r.proof_flags & ~15u) &&
            r.anchor_id == Absent && !r.anchor_hash,
        "v2 baseline fields");
  check(r.cut_kind != 1 ||
            (r.generation && r.known == 15 && r.proof_flags == 15 &&
             r.epoch_reference == r.generation),
        "armed epoch baseline");
  check(r.cut_kind != 2 || (!r.known && !r.proof_flags && !r.epoch_reference &&
                            r.state == State{}),
        "empty baseline");
  for (unsigned i = 0; i < 4; ++i)
    check((r.known & (1u << i)) || !r.state[i], "unknown baseline state");
}
inline Bytes encode(const Baseline &r) {
  validate(r);
  auto b = prefix(1, 96);
  put(b, std::uint64_t(r.context), 4);
  put(b, std::uint64_t(r.generation), 4);
  put(b, std::uint64_t(r.cut_kind), 2);
  put(b, std::uint64_t(r.known), 2);
  put(b, std::uint64_t(r.proof_flags), 4);
  put(b, std::uint64_t(r.anchor_id), 8);
  put(b, std::uint64_t(r.anchor_hash), 8);
  for (auto v : r.state)
    put(b, v, 8);
  put(b, std::uint64_t(r.epoch_reference), 8);
  b.resize(b.size() + 16, 0);
  check(b.size() == 96, "internal encoded frame size");
  return b;
}
inline Baseline decode_baseline(const Bytes &b) {
  auto c = frame(b, 1, 96);
  Baseline r;
  r.context = static_cast<decltype(r.context)>(c.get(4));
  r.generation = static_cast<decltype(r.generation)>(c.get(4));
  r.cut_kind = static_cast<decltype(r.cut_kind)>(c.get(2));
  r.known = static_cast<decltype(r.known)>(c.get(2));
  r.proof_flags = static_cast<decltype(r.proof_flags)>(c.get(4));
  r.anchor_id = static_cast<decltype(r.anchor_id)>(c.get(8));
  r.anchor_hash = static_cast<decltype(r.anchor_hash)>(c.get(8));
  for (auto &v : r.state)
    v = static_cast<std::remove_reference_t<decltype(v)>>(c.get(8));
  r.epoch_reference = static_cast<decltype(r.epoch_reference)>(c.get(8));
  c.zero(16);
  validate(r);
  return r;
}
struct Owner {
  OwnerKind owner_kind = OwnerKind::Unbound;
  std::uint16_t flags = 0;
  std::uint32_t reason = UINT32_MAX;
  std::uint32_t context = 0;
  std::uint32_t generation = 0;
  std::uint64_t owner_id = 0;
  std::uint64_t hot_hash = 0;
  std::uint32_t count = 0;
  std::uint32_t status = 0;
  std::uint64_t op_hash = 0;
  std::uint64_t causal_event = 0;
};
inline void validate(const Owner &r) {
  check(unsigned(r.owner_kind) >= 1 && unsigned(r.owner_kind) <= 3 &&
            !r.flags && (r.reason == UINT32_MAX || r.reason < 16) &&
            r.generation && r.count <= 256 && !(r.status & ~7u) &&
            r.hot_hash <= UINT32_MAX,
        "v2 owner fields");
  check(r.owner_kind == OwnerKind::RetiredInstruction ||
            (!r.hot_hash && r.owner_id),
        "v2 owner identity");
  check(r.owner_kind == OwnerKind::Unbound || !r.causal_event,
        "v2 causal ownership");
}
inline Bytes encode(const Owner &r) {
  validate(r);
  auto b = prefix(2, 64);
  put(b, std::uint64_t(r.owner_kind), 2);
  put(b, std::uint64_t(r.flags), 2);
  put(b, std::uint64_t(r.reason), 4);
  put(b, std::uint64_t(r.context), 4);
  put(b, std::uint64_t(r.generation), 4);
  put(b, std::uint64_t(r.owner_id), 8);
  put(b, std::uint64_t(r.hot_hash), 8);
  put(b, std::uint64_t(r.count), 4);
  put(b, std::uint64_t(r.status), 4);
  put(b, std::uint64_t(r.op_hash), 8);
  put(b, std::uint64_t(r.causal_event), 8);
  check(b.size() == 64, "internal encoded frame size");
  return b;
}
inline Owner decode_owner(const Bytes &b) {
  auto c = frame(b, 2, 64);
  Owner r;
  r.owner_kind = static_cast<decltype(r.owner_kind)>(c.get(2));
  r.flags = static_cast<decltype(r.flags)>(c.get(2));
  r.reason = static_cast<decltype(r.reason)>(c.get(4));
  r.context = static_cast<decltype(r.context)>(c.get(4));
  r.generation = static_cast<decltype(r.generation)>(c.get(4));
  r.owner_id = static_cast<decltype(r.owner_id)>(c.get(8));
  r.hot_hash = static_cast<decltype(r.hot_hash)>(c.get(8));
  r.count = static_cast<decltype(r.count)>(c.get(4));
  r.status = static_cast<decltype(r.status)>(c.get(4));
  r.op_hash = static_cast<decltype(r.op_hash)>(c.get(8));
  r.causal_event = static_cast<decltype(r.causal_event)>(c.get(8));
  validate(r);
  return r;
}
struct Footer {
  std::uint64_t status = Finalized;
  std::uint64_t hot = 0;
  std::uint64_t sealed_retired = 0;
  std::uint64_t total_writes = 0;
  std::uint64_t retained_writes = 0;
  std::uint64_t boundary_writes = 0;
  std::uint64_t unbound_writes = 0;
  std::uint64_t total_commands = 0;
  std::uint64_t retained_commands = 0;
  std::uint64_t boundary_commands = 0;
  std::uint64_t unbound_commands = 0;
  std::uint64_t visits = 0;
  std::uint64_t retained_groups = 0;
  std::uint64_t boundary_groups = 0;
  std::uint64_t unbound_groups = 0;
  std::uint64_t unsupported_groups = 0;
  std::uint64_t suppressed_ops = 0;
  std::uint64_t pending_owners = 0;
  std::uint64_t hot_hash = HashSeed;
  std::uint64_t body_hash = body_seed();
  std::uint64_t invocation_sealed_boundary = 0;
  std::uint64_t boundary_ops = 0;
  std::uint64_t event_rows = 0;
  std::array<std::uint64_t, 16> reasons{};
};
inline void validate(const Footer &r) {
  check((r.status & Finalized) && !(r.status & ~7ull),
        "v2 controls footer status");
}
inline Bytes encode(const Footer &r) {
  validate(r);
  auto b = prefix(6, 320);
  put(b, std::uint64_t(r.status), 8);
  put(b, std::uint64_t(r.hot), 8);
  put(b, std::uint64_t(r.sealed_retired), 8);
  put(b, std::uint64_t(r.total_writes), 8);
  put(b, std::uint64_t(r.retained_writes), 8);
  put(b, std::uint64_t(r.boundary_writes), 8);
  put(b, std::uint64_t(r.unbound_writes), 8);
  put(b, std::uint64_t(r.total_commands), 8);
  put(b, std::uint64_t(r.retained_commands), 8);
  put(b, std::uint64_t(r.boundary_commands), 8);
  put(b, std::uint64_t(r.unbound_commands), 8);
  put(b, std::uint64_t(r.visits), 8);
  put(b, std::uint64_t(r.retained_groups), 8);
  put(b, std::uint64_t(r.boundary_groups), 8);
  put(b, std::uint64_t(r.unbound_groups), 8);
  put(b, std::uint64_t(r.unsupported_groups), 8);
  put(b, std::uint64_t(r.suppressed_ops), 8);
  put(b, std::uint64_t(r.pending_owners), 8);
  put(b, std::uint64_t(r.hot_hash), 8);
  put(b, std::uint64_t(r.body_hash), 8);
  put(b, std::uint64_t(r.invocation_sealed_boundary), 8);
  put(b, std::uint64_t(r.boundary_ops), 8);
  put(b, std::uint64_t(r.event_rows), 8);
  for (auto v : r.reasons)
    put(b, v, 8);
  check(b.size() == 320, "internal encoded frame size");
  return b;
}
inline Footer decode_footer(const Bytes &b) {
  auto c = frame(b, 6, 320);
  Footer r;
  r.status = static_cast<decltype(r.status)>(c.get(8));
  r.hot = static_cast<decltype(r.hot)>(c.get(8));
  r.sealed_retired = static_cast<decltype(r.sealed_retired)>(c.get(8));
  r.total_writes = static_cast<decltype(r.total_writes)>(c.get(8));
  r.retained_writes = static_cast<decltype(r.retained_writes)>(c.get(8));
  r.boundary_writes = static_cast<decltype(r.boundary_writes)>(c.get(8));
  r.unbound_writes = static_cast<decltype(r.unbound_writes)>(c.get(8));
  r.total_commands = static_cast<decltype(r.total_commands)>(c.get(8));
  r.retained_commands = static_cast<decltype(r.retained_commands)>(c.get(8));
  r.boundary_commands = static_cast<decltype(r.boundary_commands)>(c.get(8));
  r.unbound_commands = static_cast<decltype(r.unbound_commands)>(c.get(8));
  r.visits = static_cast<decltype(r.visits)>(c.get(8));
  r.retained_groups = static_cast<decltype(r.retained_groups)>(c.get(8));
  r.boundary_groups = static_cast<decltype(r.boundary_groups)>(c.get(8));
  r.unbound_groups = static_cast<decltype(r.unbound_groups)>(c.get(8));
  r.unsupported_groups = static_cast<decltype(r.unsupported_groups)>(c.get(8));
  r.suppressed_ops = static_cast<decltype(r.suppressed_ops)>(c.get(8));
  r.pending_owners = static_cast<decltype(r.pending_owners)>(c.get(8));
  r.hot_hash = static_cast<decltype(r.hot_hash)>(c.get(8));
  r.body_hash = static_cast<decltype(r.body_hash)>(c.get(8));
  r.invocation_sealed_boundary =
      static_cast<decltype(r.invocation_sealed_boundary)>(c.get(8));
  r.boundary_ops = static_cast<decltype(r.boundary_ops)>(c.get(8));
  r.event_rows = static_cast<decltype(r.event_rows)>(c.get(8));
  for (auto &v : r.reasons)
    v = static_cast<std::remove_reference_t<decltype(v)>>(c.get(8));
  validate(r);
  return r;
}
struct Group {
  Owner owner;
  std::vector<Operation> operations;
};
class Writer {
  gzFile out_ = nullptr;
  std::uint64_t body_ = body_seed();
  bool baseline_ = false, finished_ = false;
  std::uint64_t next_retired_ = 0, last_boundary_ = 0, last_unbound_ = 0;
  void write(const Bytes &b, bool body = true) {
    check(out_ && !finished_, "v2 controls closed writer");
    check(gzwrite(out_, b.data(), unsigned(b.size())) == int(b.size()),
          "v2 controls write");
    if (body)
      body_ = hash_bytes(body_, b.data(), b.size());
  }

public:
  Writer(const std::string &path, const Header &h) {
    auto b = encode(h);
    out_ = gzopen(path.c_str(), "wb1");
    check(out_, "v2 controls open");
    try {
      write(b, false);
    } catch (...) {
      gzclose(out_);
      out_ = nullptr;
      throw;
    }
  }
  ~Writer() {
    if (out_)
      gzclose(out_);
  }
  Writer(const Writer &) = delete;
  Writer &operator=(const Writer &) = delete;
  void baseline(const Baseline &b) {
    check(!baseline_ && !finished_, "v2 baseline order");
    write(encode(b));
    baseline_ = true;
  }
  void owner(const Owner &o, const std::vector<Operation> &ops) {
    check(baseline_ && !finished_ && o.count == ops.size(),
          "v2 owner count/order");
    validate_operations(ops, 256);
    check(o.op_hash == operation_hash(ops), "v2 owner operations hash");
    if (o.owner_kind == OwnerKind::RetiredInstruction)
      check(o.owner_id == next_retired_++, "v2 retained order");
    else if (o.owner_kind == OwnerKind::ArchitecturalBoundary) {
      check(o.owner_id > last_boundary_, "v2 boundary order");
      last_boundary_ = o.owner_id;
    } else {
      check(o.owner_id > last_unbound_, "v2 diagnostic order");
      last_unbound_ = o.owner_id;
    }
    write(encode(o));
    for (const auto &op : ops)
      write(mmuctl::encode(op));
  }
  void finish(Footer f) {
    check(baseline_ && !finished_, "v2 footer order");
    f.body_hash = body_;
    write(encode(f), false);
    finished_ = true;
    auto output = out_;
    out_ = nullptr;
    check(gzclose(output) == Z_OK, "v2 controls close");
  }
};

} // namespace fastsim::fst::mmuctl_v2

#endif // FASTSIM_FST_MMU_CONTROLS_V2_CODEC
