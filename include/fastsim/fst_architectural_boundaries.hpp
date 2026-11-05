#ifndef FASTSIM_FST_ARCHITECTURAL_BOUNDARIES_CODEC
#define FASTSIM_FST_ARCHITECTURAL_BOUNDARIES_CODEC
#include "fastsim/fst_mmu_controls.hpp"
#include <map>
#include <type_traits>
namespace fastsim::fst {
inline constexpr std::uint64_t kArchitecturalBoundaryBundle = 1ull << 18;
namespace archb {
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
inline constexpr std::uint64_t RequiredFeatures = 0x749e0;
inline std::uint64_t body_seed() {
  static constexpr char domain[] = "fst-archb-body-v1";
  return hash_bytes(HashSeed, domain, sizeof(domain));
}
enum class Cause : std::uint16_t {
  PageFault = 1,
  ExternalInterrupt,
  NMI,
  SMI,
  INIT,
  SIPI,
  Trap,
  OtherFault,
  HTM,
  Retry,
  OtherExternal
};
enum class Frame : std::uint16_t {
  Epoch = 1,
  Accepted,
  InvocationSeal,
  EntryMember,
  Terminal,
  Footer
};
enum class Reason : unsigned {
  InitialEpochUnknown,
  UnsupportedCause,
  UnsupportedMode,
  Nested,
  NoRedirect,
  PredecessorUnknown,
  PrefixUnknown,
  MissingEntryHead,
  EntryDiscontinuity,
  MissingExit,
  ControlMismatch,
  EffectOrderUnsupported,
  Overflow,
  LifecycleMismatch,
  PostCut,
  CounterOverflow
};
inline constexpr std::uint32_t reason(Reason r) { return 1u << unsigned(r); }
inline const std::string &layout_descriptor() {
  static const std::string s = R"FSTLAYOUT(Frozen wire descriptor spr-archb-v1

All integers below are explicit little-endian unsigned widths; offsets are bytes. No implicit C++ padding is on wire. Reserved bytes/unknown enum values/unsupported bits reject. `uN` means N-bit field, `zN` means N zero bytes. All rows use the existing 8-byte frame prefix `kind:u16,flags:u16=0,bytes:u32` unless explicitly stated otherwise. No native seq/pointer/tick/readiness/free-count/observed allocation outcome is serialized.

## 2. Hashes and identities

* `H32(hot)`: existing FNV-1a32 over exactly the original 64 hot bytes, seed 2166136261, prime 16777619. Every u64 hot-hash field is this value zero-extended; upper32 must be zero. `.core` keeps its existing u32 hot hash.
* `H64(bytes)`: existing FNV-1a64, seed 14695981039346656037, prime 1099511628211, modulo 2^64. `hot_hash` folds **all** original hot records, unmodified order, including warmup. Entry-member hot hash folds only the bound retained entry members in ordinal order, starting at the same seed.
* New body domains are ASCII including trailing NUL: `fst-archb-body-v1\0`, `fst-mmuctl-body-v2\0`, `fst-core-payload-v2\0`. Seed with H64(domain), then hash all nonfooter frames/tags and their exact encoded payload in actual wire order, excluding header and footer. Existing v1 domains/algorithms stay unchanged.
* Operation-list hash: H64 seeded with `fst-mmuctl-ops-v2\0`, followed by the exact encoded Write/Command frames in local index order. Empty operations have the domain seed, not zero. The same hash appears in controls Owner, archb InvocationSeal and archb Terminal when their operation list is known. No footer-to-footer hash dependency.
* Shared policy SHA256 is the maintained native source-policy digest. Each new layout has its own SHA256 of the final canonical UTF-8 layout descriptor. The curator must freeze the descriptor and its computed bytes before implementation release; do not reuse the v1 layout digest. Package manifests separately authenticate artifacts; FNV is an association/integrity check, not a cryptographic content certificate.
* Boundary identity is `(context:u32,generation:u32,event:u64)`. Generation nonzero after arming; event IDs begin at 1, monotonically increasing **within a context/generation**. Event numbering has no cross-core order meaning. UINT64_MAX is the absent ordinal sentinel; hashes accompanying absent ordinals are zero. Event 0 means no event. No “next ordinal” reconstruction.

## 3. `.archb.gz` v1

Header160 offsets: `0 magic[8]="FSTARB1\0"; 8 version:u16=1;10 size:u16=160;12 endian:u32=0x01020304;16 policy[32];48 layout[32];80 required_features:u64;88 context:u32;92 core:u32;96 max_active:u32=1;100 max_pending:u32=32;104 max_ops:u32=256;108 coverage:u32;112 frame_sizes[6]:u32={96,192,128,64,160,320};136 z24`.

`required_features=0x749e0` (bits5 complete dependencies,6 request roles,7 functional retirement,8 raw syscall microops,11 frontend,14 static UOPs,16 core,17 controls,18 boundary bundle). These existing bit positions were checked in their actual headers; additional declared source features remain validated by their own maintained readers. Coverage bits 0–7 respectively certify accepted-call hook, exact invocation/cause, redirected-PC token transport, all successful-commit/omission classification, hot+post-update binding, source ROM-kind accessor, common cutoff, initial epoch. Required coverage=255; unsupported actual events still invalidate readiness despite complete hook coverage.

| Kind / size | Fields following prefix, in exact order |
|---|---|
| Epoch=1 /96 | `context:u32,generation:u32,proof_flags:u32,state_known:u32,state[4]:u64,pc:u64,micro_pc:u64,hot_at_arm:u64,z16` |
| Accepted=2 /192 | `event:u64,context:u32,generation:u32,cause:u16,vector:u16,fact_flags:u32,reason_mask:u64,pc:u64,micro_pc:u64,fault_va:u64,error_code:u64,predecessor_kind:u8,carrier:u8,mode:u8,z1,prefix_flags:u32,predecessor_ordinal:u64,predecessor_hash:u64,prefix_begin:u64,prefix_end:u64,parent_event:u64,controls_pre[4]:u64,cr2_before:u64,epoch_generation:u32,known:u32,z24` |
| InvocationSeal=3 /128 | `event:u64,context:u32,generation:u32,status:u32,known:u32,redirect_micro_pc:u64,vector_arg:u64,ip_arg:u64,error_arg:u64,cr2_after:u64,controls_post[4]:u64,op_count:u32,writes:u32,commands:u32,entry_kind:u16,mode:u8,z1,op_hash:u64` |
| EntryMember=4 /64 | `event:u64,context:u32,generation:u32,ordinal:u64,hot_hash:u64,macro_begin:u64,micro_pc:u64,member_flags:u32,rom_kind:u8,z3` |
| Terminal=5 /160 | `event:u64,context:u32,generation:u32,status:u32,reason_mask:u32,seal_flags:u32,z4,predecessor_ordinal:u64,predecessor_hash:u64,entry_begin:u64,entry_end:u64,first_hash:u64,last_hash:u64,members:u64,sealed_members:u64,member_hot_hash:u64,ffe_semantic_hash:u64,ffe_code_hash:u64,control_ops:u64,control_op_hash:u64,z16` |
| Footer=6 /320 | prefix + the 23 u64 fields listed below + `reasons[16]:u64` |

Footer fields in order: `status,hot,hot_hash,body_hash,accepted,accepted_pf,accepted_irq,accepted_other,terminal_complete,terminal_partial,terminal_unsupported,suppressed_events,pending,invocation_sealed,entry_bound,member_rows,sealed_members,boundary_ops,boundary_writes,boundary_commands,event_rows,epoch_rows,na_rom_rows`.

Enums and canonical values:

* Cause: PageFault=1, ExternalInterrupt=2, NMI=3, SMI=4, INIT=5, SIPI=6, Trap=7, OtherFault=8, HTM=9, Retry=10, OtherExternal=11. Only exact PF/ExternalInterrupt and supported LongMode invocation qualify; unsupported kinds are recorded separately, never promoted by vector/name.
* Mode: Unknown=0, LongMode=1, Other=2. Vector is u16 so 0..255 are genuine and 65535 is unknown. Carrier: ordinary faulting member=0, fetch carrier=1, asynchronous no-head=2. IRQ facts must not fabricate a head.
* predecessor_kind: Unknown=0, Retained=1, BeforeFirst=2. Retained requires original successful hot anchor. BeforeFirst has absent ordinal/hash and matching armed Epoch; Unknown never qualifies. Prefix flags: `HasPartial=1,SameDynamicMacroProved=2`; no prefix means absent begin/end. Partial prefix has `[begin,end)` ending immediately after the exact predecessor, same dynamic macro origin proven internally, and matching partial ffe span.
* Accepted fact_flags: `VA=1,Error=2,CR2=4,PredecessorSealPending=8,ParentEvent=16`; known bits: `ControlsPre=1,AcceptedPC=2,ActualCause=4,Epoch=8`. Unavailable fields are canonical zero except absent ordinals/vector. Error/VA absent for ordinary IRQ. CR2 meaningful only for PF. The independent PF error has real presence, not zero-as-absent.
* entry_kind: Unknown=0, LongInterrupt=1, LongInterruptWithError=2, OtherUnsupported=3. Store the actual selected redirect micro-PC; policy binds labels to the source. Invocation status: Sealed=1, Unsupported=2. known bits: `Redirect=1,VectorArg=2,IPArg=4,ErrorArg=8,CR2After=16,ControlsPost=32,Operations=64`. IRQ ErrorArg/CR2After stay absent, not fake known zeros.
* Member flags: `ActualHead=1,GeneratedUnconditionalEret=2`. rom_kind uses core-v2 enum below. EntryMember is emitted only after successful hot write; it does not claim post-update seal.
* Terminal status: Complete=1, Partial=2, Unsupported=3. seal_flags: `Predecessor=1,Invocation=2,Head=4,ExactExit=8,AllMembersPostUpdate=16,SpanAgreement=32,PartialPrefixProven=64`. Complete requires first six and bit64 iff partial prefix. BeforeFirst's Predecessor bit means the Epoch/prefix certificate, never a fabricated row. Missing entry uses absent begin/end, zero first/last hashes and zero members; empty member hash is H64 seed.
* Reasons bit/index 0–15: InitialEpochUnknown, UnsupportedCause, UnsupportedMode, Nested, NoRedirect, PredecessorUnknown, PrefixUnknown, MissingEntryHead, EntryDiscontinuity, MissingExit, ControlMismatch, EffectOrderUnsupported, Overflow, LifecycleMismatch, PostCut, CounterOverflow. reason_mask has no unknown bits. Reason counters are diagnostics and may overlap; they do not partition events.
* Epoch proof_flags: `NoO3ExecutionInEpoch=1,BeforeResume=2,PrefixObserverArmed=4,NoLivePriorControlScope=8`; state_known=15 for CR0/CR3/CR4/EFER. Qualified epoch requires flags15, hot_at_arm=0, non-ROM initial micro-PC and the source handoff checks. Unknown epoch may be structurally emitted but cannot qualify BeforeFirst/full boundary capability.
* Footer status bits Finalized=1,Complete=2,Empty=4. Empty means zero hot AND zero accepted; zero-effect IRQ is never empty. Terminal footer required; EOF is not a completion signal.

Lifecycle wire rules: Epoch precedes accepted events/retained bundle work. Emit Accepted at actual acceptance before effects; exactly one InvocationSeal only after invocation returns with sampled immediate facts; stream EntryMember rows as they actually retire; emit exactly one Terminal when sufficient metadata resolves or original cutoff/teardown forces Partial/Unsupported. Metadata rows may interleave across events; all reference a prior Accepted. One event's EntryMembers strictly increase ordinals and cannot duplicate any hot member. At footer `accepted=accepted_pf+accepted_irq+accepted_other=terminal_complete+terminal_partial+terminal_unsupported+suppressed_events+pending`; `event_rows=accepted-suppressed_events`. Finalized normally forces pending=0 with explicit partial terminals; nonzero pending is never Complete. Overflow may suppress new event details with saturating scalar counts, never stall native work; suppressed IDs cannot appear as valid member/control owners.


Core framing exception: core-v2 uses existing u8 tags1/2/255; payload includes tags1/2 plus exact row/operand/macro bytes; excludes header/tag255/footer. All other frames use specified8-byte prefix.
Epoch proof bit1 is NoO3ExecutionInEpoch in the completed takeover generation, not historical cold memory/TLB state.
)FSTLAYOUT";
  return s;
}
inline constexpr std::array<std::uint8_t, 32> LayoutDigest = {
    0x36, 0x22, 0xb4, 0x7c, 0xd,  0x37, 0xb0, 0x8c, 0x19, 0x40, 0x3e,
    0x8c, 0xbc, 0xf5, 0xb8, 0xa9, 0xaf, 0x29, 0x45, 0x9c, 0xa0, 0x7c,
    0x1d, 0x6d, 0x7a, 0x37, 0x50, 0x7b, 0x2a, 0xb0, 0x48, 0xa0};
struct Header {
  std::array<std::uint8_t, 32> policy{};
  std::uint32_t context = 0, core = 0, coverage = 0;
};
inline Bytes encode(const Header &h) {
  check(h.policy != std::array<std::uint8_t, 32>{} && !(h.coverage & ~255u),
        "archb header fields");
  Bytes b{'F', 'S', 'T', 'A', 'R', 'B', '1', 0};
  put(b, 1, 2);
  put(b, 160, 2);
  put(b, 0x01020304, 4);
  b.insert(b.end(), h.policy.begin(), h.policy.end());
  b.insert(b.end(), LayoutDigest.begin(), LayoutDigest.end());
  put(b, RequiredFeatures, 8);
  put(b, h.context, 4);
  put(b, h.core, 4);
  put(b, 1, 4);
  put(b, 32, 4);
  put(b, 256, 4);
  put(b, h.coverage, 4);
  for (auto n : {96, 192, 128, 64, 160, 320})
    put(b, n, 4);
  b.resize(160);
  return b;
}
inline Header decode_header(const Bytes &b) {
  check(b.size() == 160, "archb header size");
  Cursor c{b};
  for (auto v :
       std::array<unsigned char, 8>{'F', 'S', 'T', 'A', 'R', 'B', '1', 0})
    check(c.get(1) == v, "archb magic");
  check(c.get(2) == 1 && c.get(2) == 160 && c.get(4) == 0x01020304,
        "archb version/endian");
  Header h;
  for (auto &v : h.policy)
    v = static_cast<std::remove_reference_t<decltype(v)>>(c.get(1));
  for (auto v : LayoutDigest)
    check(c.get(1) == v, "archb layout");
  check(c.get(8) == RequiredFeatures, "archb features");
  h.context =
      static_cast<std::remove_reference_t<decltype(h.context)>>(c.get(4));
  h.core = static_cast<std::remove_reference_t<decltype(h.core)>>(c.get(4));
  check(c.get(4) == 1 && c.get(4) == 32 && c.get(4) == 256, "archb limits");
  h.coverage =
      static_cast<std::remove_reference_t<decltype(h.coverage)>>(c.get(4));
  for (auto n : {96, 192, 128, 64, 160, 320})
    check(c.get(4) == unsigned(n), "archb frame sizes");
  c.zero(24);
  encode(h);
  return h;
}
struct Epoch {
  std::uint32_t context = 0;
  std::uint32_t generation = 0;
  std::uint32_t proof_flags = 0;
  std::uint32_t state_known = 0;
  State state{};
  std::uint64_t pc = 0;
  std::uint64_t micro_pc = 0;
  std::uint64_t hot_at_arm = 0;
};
inline void validate(const Epoch &r) {
  check(!(r.proof_flags & ~15u) && !(r.state_known & ~15u), "epoch masks");
  for (unsigned i = 0; i < 4; ++i)
    check((r.state_known & (1u << i)) || !r.state[i], "unknown epoch state");
  check(r.proof_flags != 15 || (r.generation && r.state_known == 15 &&
                                !r.hot_at_arm && !(r.micro_pc & 0x8000)),
        "epoch proof");
}
inline Bytes encode(const Epoch &r) {
  validate(r);
  auto b = prefix(1, 96);
  put(b, std::uint64_t(r.context), 4);
  put(b, std::uint64_t(r.generation), 4);
  put(b, std::uint64_t(r.proof_flags), 4);
  put(b, std::uint64_t(r.state_known), 4);
  for (auto v : r.state)
    put(b, v, 8);
  put(b, std::uint64_t(r.pc), 8);
  put(b, std::uint64_t(r.micro_pc), 8);
  put(b, std::uint64_t(r.hot_at_arm), 8);
  b.resize(b.size() + 16, 0);
  check(b.size() == 96, "internal encoded frame size");
  return b;
}
inline Epoch decode_epoch(const Bytes &b) {
  auto c = frame(b, 1, 96);
  Epoch r;
  r.context = static_cast<decltype(r.context)>(c.get(4));
  r.generation = static_cast<decltype(r.generation)>(c.get(4));
  r.proof_flags = static_cast<decltype(r.proof_flags)>(c.get(4));
  r.state_known = static_cast<decltype(r.state_known)>(c.get(4));
  for (auto &v : r.state)
    v = static_cast<std::remove_reference_t<decltype(v)>>(c.get(8));
  r.pc = static_cast<decltype(r.pc)>(c.get(8));
  r.micro_pc = static_cast<decltype(r.micro_pc)>(c.get(8));
  r.hot_at_arm = static_cast<decltype(r.hot_at_arm)>(c.get(8));
  c.zero(16);
  validate(r);
  return r;
}
struct Accepted {
  std::uint64_t event = 0;
  std::uint32_t context = 0;
  std::uint32_t generation = 0;
  Cause cause = Cause::OtherFault;
  std::uint16_t vector = 65535;
  std::uint32_t fact_flags = 0;
  std::uint64_t reason_mask = 0;
  std::uint64_t pc = 0;
  std::uint64_t micro_pc = 0;
  std::uint64_t fault_va = 0;
  std::uint64_t error_code = 0;
  std::uint8_t predecessor_kind = 0;
  std::uint8_t carrier = 0;
  std::uint8_t mode = 0;
  std::uint32_t prefix_flags = 0;
  std::uint64_t predecessor_ordinal = Absent;
  std::uint64_t predecessor_hash = 0;
  std::uint64_t prefix_begin = Absent;
  std::uint64_t prefix_end = Absent;
  std::uint64_t parent_event = 0;
  State controls_pre{};
  std::uint64_t cr2_before = 0;
  std::uint32_t epoch_generation = 0;
  std::uint32_t known = 0;
};
inline void validate(const Accepted &r) {
  check(r.event && r.generation, "boundary identity");
  check(unsigned(r.cause) >= 1 && unsigned(r.cause) <= 11 &&
            (r.vector <= 255 || r.vector == 65535) && !(r.fact_flags & ~31u) &&
            !(r.reason_mask & ~65535ull) && r.predecessor_kind <= 2 &&
            r.carrier <= 2 && r.mode <= 2 && !(r.prefix_flags & ~3u) &&
            !(r.known & ~15u),
        "accepted enums/masks");
  check(r.predecessor_hash <= UINT32_MAX &&
            (r.predecessor_kind == 1
                 ? r.predecessor_ordinal != Absent
                 : r.predecessor_ordinal == Absent && !r.predecessor_hash),
        "accepted predecessor");
  check((r.prefix_flags & 1)
            ? r.prefix_begin < r.prefix_end && r.predecessor_kind == 1 &&
                  r.prefix_end == r.predecessor_ordinal + 1
            : r.prefix_begin == Absent && r.prefix_end == Absent &&
                  !r.prefix_flags,
        "accepted prefix");
  check((r.fact_flags & 1) || !r.fault_va, "unknown VA");
  check((r.fact_flags & 2) || !r.error_code, "unknown error");
  check((r.fact_flags & 4) || !r.cr2_before, "unknown CR2");
  check((r.fact_flags & 16) ? r.parent_event != 0 : !r.parent_event,
        "parent presence");
  check((r.known & 1) || r.controls_pre == State{}, "unknown pre-controls");
  check((r.known & 2) || (!r.pc && !r.micro_pc), "unknown accepted PC");
  check((r.known & 8) ? r.epoch_generation == r.generation
                      : !r.epoch_generation,
        "epoch reference");
  check(r.cause != Cause::ExternalInterrupt ||
            (r.carrier == 2 && !(r.fact_flags & 7u)),
        "IRQ has fabricated head/fault operands");
  check(r.cause == Cause::PageFault || !(r.fact_flags & 7u),
        "non-PF fault operands");
}
inline Bytes encode(const Accepted &r) {
  validate(r);
  auto b = prefix(2, 192);
  put(b, std::uint64_t(r.event), 8);
  put(b, std::uint64_t(r.context), 4);
  put(b, std::uint64_t(r.generation), 4);
  put(b, std::uint64_t(r.cause), 2);
  put(b, std::uint64_t(r.vector), 2);
  put(b, std::uint64_t(r.fact_flags), 4);
  put(b, std::uint64_t(r.reason_mask), 8);
  put(b, std::uint64_t(r.pc), 8);
  put(b, std::uint64_t(r.micro_pc), 8);
  put(b, std::uint64_t(r.fault_va), 8);
  put(b, std::uint64_t(r.error_code), 8);
  put(b, std::uint64_t(r.predecessor_kind), 1);
  put(b, std::uint64_t(r.carrier), 1);
  put(b, std::uint64_t(r.mode), 1);
  b.resize(b.size() + 1, 0);
  put(b, std::uint64_t(r.prefix_flags), 4);
  put(b, std::uint64_t(r.predecessor_ordinal), 8);
  put(b, std::uint64_t(r.predecessor_hash), 8);
  put(b, std::uint64_t(r.prefix_begin), 8);
  put(b, std::uint64_t(r.prefix_end), 8);
  put(b, std::uint64_t(r.parent_event), 8);
  for (auto v : r.controls_pre)
    put(b, v, 8);
  put(b, std::uint64_t(r.cr2_before), 8);
  put(b, std::uint64_t(r.epoch_generation), 4);
  put(b, std::uint64_t(r.known), 4);
  b.resize(b.size() + 24, 0);
  check(b.size() == 192, "internal encoded frame size");
  return b;
}
inline Accepted decode_accepted(const Bytes &b) {
  auto c = frame(b, 2, 192);
  Accepted r;
  r.event = static_cast<decltype(r.event)>(c.get(8));
  r.context = static_cast<decltype(r.context)>(c.get(4));
  r.generation = static_cast<decltype(r.generation)>(c.get(4));
  r.cause = static_cast<decltype(r.cause)>(c.get(2));
  r.vector = static_cast<decltype(r.vector)>(c.get(2));
  r.fact_flags = static_cast<decltype(r.fact_flags)>(c.get(4));
  r.reason_mask = static_cast<decltype(r.reason_mask)>(c.get(8));
  r.pc = static_cast<decltype(r.pc)>(c.get(8));
  r.micro_pc = static_cast<decltype(r.micro_pc)>(c.get(8));
  r.fault_va = static_cast<decltype(r.fault_va)>(c.get(8));
  r.error_code = static_cast<decltype(r.error_code)>(c.get(8));
  r.predecessor_kind = static_cast<decltype(r.predecessor_kind)>(c.get(1));
  r.carrier = static_cast<decltype(r.carrier)>(c.get(1));
  r.mode = static_cast<decltype(r.mode)>(c.get(1));
  c.zero(1);
  r.prefix_flags = static_cast<decltype(r.prefix_flags)>(c.get(4));
  r.predecessor_ordinal =
      static_cast<decltype(r.predecessor_ordinal)>(c.get(8));
  r.predecessor_hash = static_cast<decltype(r.predecessor_hash)>(c.get(8));
  r.prefix_begin = static_cast<decltype(r.prefix_begin)>(c.get(8));
  r.prefix_end = static_cast<decltype(r.prefix_end)>(c.get(8));
  r.parent_event = static_cast<decltype(r.parent_event)>(c.get(8));
  for (auto &v : r.controls_pre)
    v = static_cast<std::remove_reference_t<decltype(v)>>(c.get(8));
  r.cr2_before = static_cast<decltype(r.cr2_before)>(c.get(8));
  r.epoch_generation = static_cast<decltype(r.epoch_generation)>(c.get(4));
  r.known = static_cast<decltype(r.known)>(c.get(4));
  c.zero(24);
  validate(r);
  return r;
}
struct InvocationSeal {
  std::uint64_t event = 0;
  std::uint32_t context = 0;
  std::uint32_t generation = 0;
  std::uint32_t status = 0;
  std::uint32_t known = 0;
  std::uint64_t redirect_micro_pc = 0;
  std::uint64_t vector_arg = 0;
  std::uint64_t ip_arg = 0;
  std::uint64_t error_arg = 0;
  std::uint64_t cr2_after = 0;
  State controls_post{};
  std::uint32_t op_count = 0;
  std::uint32_t writes = 0;
  std::uint32_t commands = 0;
  std::uint16_t entry_kind = 0;
  std::uint8_t mode = 0;
  std::uint64_t op_hash = 0;
};
inline void validate(const InvocationSeal &r) {
  check(r.event && r.generation, "boundary identity");
  check(r.status >= 1 && r.status <= 2 && !(r.known & ~127u) &&
            r.entry_kind <= 3 && r.mode <= 2 && r.op_count <= 256 &&
            r.writes <= r.op_count && r.commands == r.op_count - r.writes,
        "invocation fields");
  check((r.known & 1) || (!r.redirect_micro_pc && !r.entry_kind),
        "unknown redirect");
  check((r.known & 2) || !r.vector_arg, "unknown vector argument");
  check((r.known & 4) || !r.ip_arg, "unknown IP argument");
  check((r.known & 8) || !r.error_arg, "unknown error argument");
  check((r.known & 16) || !r.cr2_after, "unknown CR2 after");
  check((r.known & 32) || r.controls_post == State{}, "unknown controls after");
  check((r.known & 64) || (!r.op_count && !r.op_hash), "unknown operations");
}
inline Bytes encode(const InvocationSeal &r) {
  validate(r);
  auto b = prefix(3, 128);
  put(b, std::uint64_t(r.event), 8);
  put(b, std::uint64_t(r.context), 4);
  put(b, std::uint64_t(r.generation), 4);
  put(b, std::uint64_t(r.status), 4);
  put(b, std::uint64_t(r.known), 4);
  put(b, std::uint64_t(r.redirect_micro_pc), 8);
  put(b, std::uint64_t(r.vector_arg), 8);
  put(b, std::uint64_t(r.ip_arg), 8);
  put(b, std::uint64_t(r.error_arg), 8);
  put(b, std::uint64_t(r.cr2_after), 8);
  for (auto v : r.controls_post)
    put(b, v, 8);
  put(b, std::uint64_t(r.op_count), 4);
  put(b, std::uint64_t(r.writes), 4);
  put(b, std::uint64_t(r.commands), 4);
  put(b, std::uint64_t(r.entry_kind), 2);
  put(b, std::uint64_t(r.mode), 1);
  b.resize(b.size() + 1, 0);
  put(b, std::uint64_t(r.op_hash), 8);
  check(b.size() == 128, "internal encoded frame size");
  return b;
}
inline InvocationSeal decode_invocation_seal(const Bytes &b) {
  auto c = frame(b, 3, 128);
  InvocationSeal r;
  r.event = static_cast<decltype(r.event)>(c.get(8));
  r.context = static_cast<decltype(r.context)>(c.get(4));
  r.generation = static_cast<decltype(r.generation)>(c.get(4));
  r.status = static_cast<decltype(r.status)>(c.get(4));
  r.known = static_cast<decltype(r.known)>(c.get(4));
  r.redirect_micro_pc = static_cast<decltype(r.redirect_micro_pc)>(c.get(8));
  r.vector_arg = static_cast<decltype(r.vector_arg)>(c.get(8));
  r.ip_arg = static_cast<decltype(r.ip_arg)>(c.get(8));
  r.error_arg = static_cast<decltype(r.error_arg)>(c.get(8));
  r.cr2_after = static_cast<decltype(r.cr2_after)>(c.get(8));
  for (auto &v : r.controls_post)
    v = static_cast<std::remove_reference_t<decltype(v)>>(c.get(8));
  r.op_count = static_cast<decltype(r.op_count)>(c.get(4));
  r.writes = static_cast<decltype(r.writes)>(c.get(4));
  r.commands = static_cast<decltype(r.commands)>(c.get(4));
  r.entry_kind = static_cast<decltype(r.entry_kind)>(c.get(2));
  r.mode = static_cast<decltype(r.mode)>(c.get(1));
  c.zero(1);
  r.op_hash = static_cast<decltype(r.op_hash)>(c.get(8));
  validate(r);
  return r;
}
struct EntryMember {
  std::uint64_t event = 0;
  std::uint32_t context = 0;
  std::uint32_t generation = 0;
  std::uint64_t ordinal = 0;
  std::uint64_t hot_hash = 0;
  std::uint64_t macro_begin = 0;
  std::uint64_t micro_pc = 0;
  std::uint32_t member_flags = 0;
  std::uint8_t rom_kind = 0;
};
inline void validate(const EntryMember &r) {
  check(r.event && r.generation, "boundary identity");
  check(r.ordinal != Absent && r.hot_hash <= UINT32_MAX &&
            r.macro_begin <= r.ordinal && !(r.member_flags & ~3u) &&
            r.rom_kind <= 5,
        "entry member fields");
  check(bool(r.member_flags & 2) == (r.rom_kind == 4),
        "generated Eret marker mismatch");
  check(!(r.member_flags & 1) || r.macro_begin == r.ordinal,
        "entry head macro");
}
inline Bytes encode(const EntryMember &r) {
  validate(r);
  auto b = prefix(4, 64);
  put(b, std::uint64_t(r.event), 8);
  put(b, std::uint64_t(r.context), 4);
  put(b, std::uint64_t(r.generation), 4);
  put(b, std::uint64_t(r.ordinal), 8);
  put(b, std::uint64_t(r.hot_hash), 8);
  put(b, std::uint64_t(r.macro_begin), 8);
  put(b, std::uint64_t(r.micro_pc), 8);
  put(b, std::uint64_t(r.member_flags), 4);
  put(b, std::uint64_t(r.rom_kind), 1);
  b.resize(b.size() + 3, 0);
  check(b.size() == 64, "internal encoded frame size");
  return b;
}
inline EntryMember decode_entry_member(const Bytes &b) {
  auto c = frame(b, 4, 64);
  EntryMember r;
  r.event = static_cast<decltype(r.event)>(c.get(8));
  r.context = static_cast<decltype(r.context)>(c.get(4));
  r.generation = static_cast<decltype(r.generation)>(c.get(4));
  r.ordinal = static_cast<decltype(r.ordinal)>(c.get(8));
  r.hot_hash = static_cast<decltype(r.hot_hash)>(c.get(8));
  r.macro_begin = static_cast<decltype(r.macro_begin)>(c.get(8));
  r.micro_pc = static_cast<decltype(r.micro_pc)>(c.get(8));
  r.member_flags = static_cast<decltype(r.member_flags)>(c.get(4));
  r.rom_kind = static_cast<decltype(r.rom_kind)>(c.get(1));
  c.zero(3);
  validate(r);
  return r;
}
struct Terminal {
  std::uint64_t event = 0;
  std::uint32_t context = 0;
  std::uint32_t generation = 0;
  std::uint32_t status = 0;
  std::uint32_t reason_mask = 0;
  std::uint32_t seal_flags = 0;
  std::uint64_t predecessor_ordinal = Absent;
  std::uint64_t predecessor_hash = 0;
  std::uint64_t entry_begin = Absent;
  std::uint64_t entry_end = Absent;
  std::uint64_t first_hash = 0;
  std::uint64_t last_hash = 0;
  std::uint64_t members = 0;
  std::uint64_t sealed_members = 0;
  std::uint64_t member_hot_hash = HashSeed;
  std::uint64_t ffe_semantic_hash = 0;
  std::uint64_t ffe_code_hash = 0;
  std::uint64_t control_ops = 0;
  std::uint64_t control_op_hash = 0;
};
inline void validate(const Terminal &r) {
  check(r.event && r.generation, "boundary identity");
  check(r.status >= 1 && r.status <= 3 && !(r.reason_mask & ~65535u) &&
            !(r.seal_flags & ~127u) && r.predecessor_hash <= UINT32_MAX &&
            r.first_hash <= UINT32_MAX && r.last_hash <= UINT32_MAX &&
            r.sealed_members <= r.members && r.control_ops <= 256,
        "terminal fields");
  check(r.predecessor_ordinal != Absent || !r.predecessor_hash,
        "absent predecessor hash");
  check(r.members ? r.entry_begin < r.entry_end &&
                        r.members == r.entry_end - r.entry_begin
                  : r.entry_begin == Absent && r.entry_end == Absent &&
                        !r.first_hash && !r.last_hash &&
                        r.member_hot_hash == HashSeed,
        "terminal member span");
  check(r.status != 1 || (!r.reason_mask && (r.seal_flags & 63) == 63 &&
                          r.members && r.members == r.sealed_members),
        "false complete terminal");
}
inline Bytes encode(const Terminal &r) {
  validate(r);
  auto b = prefix(5, 160);
  put(b, std::uint64_t(r.event), 8);
  put(b, std::uint64_t(r.context), 4);
  put(b, std::uint64_t(r.generation), 4);
  put(b, std::uint64_t(r.status), 4);
  put(b, std::uint64_t(r.reason_mask), 4);
  put(b, std::uint64_t(r.seal_flags), 4);
  b.resize(b.size() + 4, 0);
  put(b, std::uint64_t(r.predecessor_ordinal), 8);
  put(b, std::uint64_t(r.predecessor_hash), 8);
  put(b, std::uint64_t(r.entry_begin), 8);
  put(b, std::uint64_t(r.entry_end), 8);
  put(b, std::uint64_t(r.first_hash), 8);
  put(b, std::uint64_t(r.last_hash), 8);
  put(b, std::uint64_t(r.members), 8);
  put(b, std::uint64_t(r.sealed_members), 8);
  put(b, std::uint64_t(r.member_hot_hash), 8);
  put(b, std::uint64_t(r.ffe_semantic_hash), 8);
  put(b, std::uint64_t(r.ffe_code_hash), 8);
  put(b, std::uint64_t(r.control_ops), 8);
  put(b, std::uint64_t(r.control_op_hash), 8);
  b.resize(b.size() + 16, 0);
  check(b.size() == 160, "internal encoded frame size");
  return b;
}
inline Terminal decode_terminal(const Bytes &b) {
  auto c = frame(b, 5, 160);
  Terminal r;
  r.event = static_cast<decltype(r.event)>(c.get(8));
  r.context = static_cast<decltype(r.context)>(c.get(4));
  r.generation = static_cast<decltype(r.generation)>(c.get(4));
  r.status = static_cast<decltype(r.status)>(c.get(4));
  r.reason_mask = static_cast<decltype(r.reason_mask)>(c.get(4));
  r.seal_flags = static_cast<decltype(r.seal_flags)>(c.get(4));
  c.zero(4);
  r.predecessor_ordinal =
      static_cast<decltype(r.predecessor_ordinal)>(c.get(8));
  r.predecessor_hash = static_cast<decltype(r.predecessor_hash)>(c.get(8));
  r.entry_begin = static_cast<decltype(r.entry_begin)>(c.get(8));
  r.entry_end = static_cast<decltype(r.entry_end)>(c.get(8));
  r.first_hash = static_cast<decltype(r.first_hash)>(c.get(8));
  r.last_hash = static_cast<decltype(r.last_hash)>(c.get(8));
  r.members = static_cast<decltype(r.members)>(c.get(8));
  r.sealed_members = static_cast<decltype(r.sealed_members)>(c.get(8));
  r.member_hot_hash = static_cast<decltype(r.member_hot_hash)>(c.get(8));
  r.ffe_semantic_hash = static_cast<decltype(r.ffe_semantic_hash)>(c.get(8));
  r.ffe_code_hash = static_cast<decltype(r.ffe_code_hash)>(c.get(8));
  r.control_ops = static_cast<decltype(r.control_ops)>(c.get(8));
  r.control_op_hash = static_cast<decltype(r.control_op_hash)>(c.get(8));
  c.zero(16);
  validate(r);
  return r;
}
struct Footer {
  std::uint64_t status = Finalized;
  std::uint64_t hot = 0;
  std::uint64_t hot_hash = HashSeed;
  std::uint64_t body_hash = body_seed();
  std::uint64_t accepted = 0;
  std::uint64_t accepted_pf = 0;
  std::uint64_t accepted_irq = 0;
  std::uint64_t accepted_other = 0;
  std::uint64_t terminal_complete = 0;
  std::uint64_t terminal_partial = 0;
  std::uint64_t terminal_unsupported = 0;
  std::uint64_t suppressed_events = 0;
  std::uint64_t pending = 0;
  std::uint64_t invocation_sealed = 0;
  std::uint64_t entry_bound = 0;
  std::uint64_t member_rows = 0;
  std::uint64_t sealed_members = 0;
  std::uint64_t boundary_ops = 0;
  std::uint64_t boundary_writes = 0;
  std::uint64_t boundary_commands = 0;
  std::uint64_t event_rows = 0;
  std::uint64_t epoch_rows = 0;
  std::uint64_t na_rom_rows = 0;
  std::array<std::uint64_t, 16> reasons{};
};
inline void validate(const Footer &r) {
  check((r.status & Finalized) && !(r.status & ~7ull), "archb footer status");
}
inline Bytes encode(const Footer &r) {
  validate(r);
  auto b = prefix(6, 320);
  put(b, std::uint64_t(r.status), 8);
  put(b, std::uint64_t(r.hot), 8);
  put(b, std::uint64_t(r.hot_hash), 8);
  put(b, std::uint64_t(r.body_hash), 8);
  put(b, std::uint64_t(r.accepted), 8);
  put(b, std::uint64_t(r.accepted_pf), 8);
  put(b, std::uint64_t(r.accepted_irq), 8);
  put(b, std::uint64_t(r.accepted_other), 8);
  put(b, std::uint64_t(r.terminal_complete), 8);
  put(b, std::uint64_t(r.terminal_partial), 8);
  put(b, std::uint64_t(r.terminal_unsupported), 8);
  put(b, std::uint64_t(r.suppressed_events), 8);
  put(b, std::uint64_t(r.pending), 8);
  put(b, std::uint64_t(r.invocation_sealed), 8);
  put(b, std::uint64_t(r.entry_bound), 8);
  put(b, std::uint64_t(r.member_rows), 8);
  put(b, std::uint64_t(r.sealed_members), 8);
  put(b, std::uint64_t(r.boundary_ops), 8);
  put(b, std::uint64_t(r.boundary_writes), 8);
  put(b, std::uint64_t(r.boundary_commands), 8);
  put(b, std::uint64_t(r.event_rows), 8);
  put(b, std::uint64_t(r.epoch_rows), 8);
  put(b, std::uint64_t(r.na_rom_rows), 8);
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
  r.hot_hash = static_cast<decltype(r.hot_hash)>(c.get(8));
  r.body_hash = static_cast<decltype(r.body_hash)>(c.get(8));
  r.accepted = static_cast<decltype(r.accepted)>(c.get(8));
  r.accepted_pf = static_cast<decltype(r.accepted_pf)>(c.get(8));
  r.accepted_irq = static_cast<decltype(r.accepted_irq)>(c.get(8));
  r.accepted_other = static_cast<decltype(r.accepted_other)>(c.get(8));
  r.terminal_complete = static_cast<decltype(r.terminal_complete)>(c.get(8));
  r.terminal_partial = static_cast<decltype(r.terminal_partial)>(c.get(8));
  r.terminal_unsupported =
      static_cast<decltype(r.terminal_unsupported)>(c.get(8));
  r.suppressed_events = static_cast<decltype(r.suppressed_events)>(c.get(8));
  r.pending = static_cast<decltype(r.pending)>(c.get(8));
  r.invocation_sealed = static_cast<decltype(r.invocation_sealed)>(c.get(8));
  r.entry_bound = static_cast<decltype(r.entry_bound)>(c.get(8));
  r.member_rows = static_cast<decltype(r.member_rows)>(c.get(8));
  r.sealed_members = static_cast<decltype(r.sealed_members)>(c.get(8));
  r.boundary_ops = static_cast<decltype(r.boundary_ops)>(c.get(8));
  r.boundary_writes = static_cast<decltype(r.boundary_writes)>(c.get(8));
  r.boundary_commands = static_cast<decltype(r.boundary_commands)>(c.get(8));
  r.event_rows = static_cast<decltype(r.event_rows)>(c.get(8));
  r.epoch_rows = static_cast<decltype(r.epoch_rows)>(c.get(8));
  r.na_rom_rows = static_cast<decltype(r.na_rom_rows)>(c.get(8));
  for (auto &v : r.reasons)
    v = static_cast<std::remove_reference_t<decltype(v)>>(c.get(8));
  validate(r);
  return r;
}
// Streaming metadata state, bounded by the frozen 32 live tokens. It retains
// no entry-member history. Both writer and reader use this wire validator.
class StreamValidator {
public:
  struct Pending {
    Accepted accepted;
    InvocationSeal invocation;
    bool invoked = false;
    std::uint64_t members = 0, begin = Absent, end = Absent, first = 0,
                  last = 0, last_upc = 0, first_upc = 0;
    std::uint8_t last_kind = 0;
    bool head = false;
  };

private:
  Header header_;
  Epoch epoch_;
  bool armed_ = false, finished_ = false;
  std::map<std::uint64_t, Pending> pending_;
  std::uint64_t last_event_ = 0, last_member_ = Absent;
  Footer observed_;
  Pending &event(std::uint64_t id, std::uint32_t context,
                 std::uint32_t generation) {
    check(armed_ && !finished_ && context == header_.context &&
              generation == epoch_.generation,
          "archb event epoch");
    auto it = pending_.find(id);
    check(it != pending_.end(), "archb reference has no live accepted event");
    return it->second;
  }

public:
  explicit StreamValidator(const Header &h) : header_(h) { encode(h); }
  const Epoch &epoch() const { return epoch_; }
  const auto &pending() const { return pending_; }
  const Footer &observed() const { return observed_; }
  void accept(const Epoch &r) {
    validate(r);
    check(!armed_ && !finished_ && r.context == header_.context,
          "archb epoch order/context");
    epoch_ = r;
    armed_ = true;
    ++observed_.epoch_rows;
  }
  void accept(const Accepted &r) {
    validate(r);
    check(armed_ && !finished_ && r.context == header_.context &&
              r.generation == epoch_.generation && r.event > last_event_ &&
              pending_.size() < 32,
          "archb accepted order/liveness");
    check(r.predecessor_kind != 2 ||
              (epoch_.proof_flags == 15 && epoch_.state_known == 15 &&
               r.epoch_generation == epoch_.generation),
          "unproven BeforeFirst");
    if (r.predecessor_kind == 1)
      check(r.predecessor_ordinal != Absent, "absent retained predecessor");
    check(last_event_ || r.event == 1, "first boundary ID is not one");
    if (!pending_.empty())
      check((r.fact_flags & 16) && pending_.count(r.parent_event) &&
                (r.reason_mask & reason(Reason::Nested)),
            "unmarked nested event");
    else
      check(!(r.fact_flags & 16), "parent event is not live");
    last_event_ = r.event;
    Pending p;
    p.accepted = r;
    pending_.emplace(r.event, p);
    ++observed_.event_rows;
    if (r.cause == Cause::PageFault)
      ++observed_.accepted_pf;
    else if (r.cause == Cause::ExternalInterrupt)
      ++observed_.accepted_irq;
    else
      ++observed_.accepted_other;
  }
  void accept(const InvocationSeal &r) {
    validate(r);
    auto &p = event(r.event, r.context, r.generation);
    check(!p.invoked, "duplicate invocation seal");
    const auto &a = p.accepted;
    if (r.status == 1) {
      check((r.known & 103) == 103, "sealed invocation missing facts");
      check(r.mode == 1 && a.mode == 1, "unsupported sealed mode");
      check((a.cause == Cause::PageFault && r.entry_kind == 2 &&
             (r.known & 24) == 24) ||
                (a.cause == Cause::ExternalInterrupt && r.entry_kind == 1 &&
                 !(r.known & 24)),
            "invocation/cause contradiction");
      check(r.vector_arg == a.vector, "invocation vector mismatch");
      if (a.cause == Cause::PageFault)
        check((a.fact_flags & 7) == 7 && r.error_arg == a.error_code &&
                  r.cr2_after == a.fault_va,
              "PF invocation operand mismatch");
    }
    p.invocation = r;
    p.invoked = true;
    ++observed_.invocation_sealed;
    observed_.boundary_ops += r.op_count;
    observed_.boundary_writes += r.writes;
    observed_.boundary_commands += r.commands;
  }
  void accept(const EntryMember &r) {
    validate(r);
    auto &p = event(r.event, r.context, r.generation);
    check(last_member_ == Absent || r.ordinal > last_member_,
          "duplicate/out-of-order entry member");
    check(p.invoked, "entry precedes invocation seal");
    check(!p.members || (r.ordinal == p.end && r.macro_begin == p.begin),
          "entry member discontinuity");
    if (!p.members) {
      p.begin = r.macro_begin;
      p.first = r.hot_hash;
      p.first_upc = r.micro_pc;
      p.head = r.member_flags & 1;
      ++observed_.entry_bound;
    } else
      check(!(r.member_flags & 1), "duplicate entry head");
    p.end = r.ordinal + 1;
    p.last = r.hot_hash;
    p.last_upc = r.micro_pc;
    p.last_kind = r.rom_kind;
    ++p.members;
    last_member_ = r.ordinal;
    ++observed_.member_rows;
  }
  void accept(const Terminal &r) {
    validate(r);
    auto &p = event(r.event, r.context, r.generation);
    const auto &a = p.accepted;
    check(r.predecessor_ordinal == a.predecessor_ordinal &&
              r.predecessor_hash == a.predecessor_hash &&
              r.members == p.members && r.entry_begin == p.begin &&
              r.entry_end == p.end && r.first_hash == p.first &&
              r.last_hash == p.last,
          "terminal exact member/predecessor mismatch");
    check(!(r.seal_flags & 2) || p.invoked, "invented invocation seal");
    check(!(r.seal_flags & 4) ||
              (p.head && p.invoked &&
               p.first_upc == p.invocation.redirect_micro_pc),
          "invented entry-head seal");
    check(!(r.seal_flags & 8) || p.last_kind == 4, "invented exact-exit seal");
    check(!(r.seal_flags & 16) || r.sealed_members == r.members,
          "invented all-member seal");
    if (p.invoked)
      check(r.control_ops == p.invocation.op_count &&
                r.control_op_hash == p.invocation.op_hash,
            "terminal invocation operations");
    if (r.status == 1) {
      check(
          p.invoked && p.invocation.status == 1 && p.head && p.last_kind == 4 &&
              p.first_upc == p.invocation.redirect_micro_pc && !a.reason_mask &&
              (a.cause == Cause::PageFault ||
               a.cause == Cause::ExternalInterrupt) &&
              a.mode == 1 && a.predecessor_kind && a.known == 15 &&
              epoch_.proof_flags == 15,
          "false complete lineage");
      check(bool(r.seal_flags & 64) == bool(a.prefix_flags & 1) &&
                (!(a.prefix_flags & 1) || a.prefix_flags == 3),
            "partial-prefix proof");
      ++observed_.terminal_complete;
    } else if (r.status == 2)
      ++observed_.terminal_partial;
    else
      ++observed_.terminal_unsupported;
    observed_.sealed_members += r.sealed_members;
    for (unsigned i = 0; i < 16; ++i)
      if (r.reason_mask & (1u << i))
        ++observed_.reasons[i];
    pending_.erase(r.event);
  }
  void finish(const Footer &f) {
    validate(f);
    check(armed_ && !finished_, "archb footer order");
    check(f.epoch_rows == 1 && f.event_rows == observed_.event_rows &&
              f.member_rows == observed_.member_rows &&
              f.entry_bound == observed_.entry_bound &&
              f.invocation_sealed == observed_.invocation_sealed &&
              f.sealed_members == observed_.sealed_members &&
              f.terminal_complete == observed_.terminal_complete &&
              f.terminal_partial == observed_.terminal_partial &&
              f.terminal_unsupported == observed_.terminal_unsupported &&
              f.pending == pending_.size(),
          "archb footer population");
    check(f.boundary_ops == observed_.boundary_ops &&
              f.boundary_writes == observed_.boundary_writes &&
              f.boundary_commands == observed_.boundary_commands,
          "archb operation conservation");
    check(f.accepted >= f.suppressed_events &&
              f.event_rows == f.accepted - f.suppressed_events &&
              f.accepted_pf >= observed_.accepted_pf &&
              f.accepted_irq >= observed_.accepted_irq &&
              f.accepted_other >= observed_.accepted_other &&
              f.accepted_pf <= f.accepted &&
              f.accepted_irq <= f.accepted - f.accepted_pf &&
              f.accepted_other == f.accepted - f.accepted_pf - f.accepted_irq,
          "archb cause conservation");
    check(f.terminal_complete <= f.event_rows &&
              f.terminal_partial <= f.event_rows - f.terminal_complete &&
              f.terminal_unsupported <=
                  f.event_rows - f.terminal_complete - f.terminal_partial &&
              f.pending == f.event_rows - f.terminal_complete -
                               f.terminal_partial - f.terminal_unsupported,
          "archb terminal conservation");
    check(f.na_rom_rows <= f.member_rows && f.member_rows <= f.hot &&
              bool(f.status & Empty) == (!f.hot && !f.accepted),
          "archb empty/member counters");
    for (unsigned i = 0; i < 16; ++i)
      check(f.reasons[i] >= observed_.reasons[i], "archb reason conservation");
    if (f.status & Complete) {
      check(header_.coverage == 255 && epoch_.proof_flags == 15 &&
                epoch_.state_known == 15 && !f.pending &&
                !f.suppressed_events && !f.terminal_partial &&
                !f.terminal_unsupported && f.sealed_members == f.member_rows,
            "archb false completeness");
      for (auto n : f.reasons)
        check(!n, "archb complete with reasons");
    }
    finished_ = true;
  }
};
class Writer {
  gzFile out_ = nullptr;
  std::uint64_t body_ = body_seed();
  StreamValidator validator_;
  bool finished_ = false;
  void write(const Bytes &b, bool body = true) {
    check(out_ && !finished_, "archb closed writer");
    check(gzwrite(out_, b.data(), unsigned(b.size())) == int(b.size()),
          "archb write");
    if (body)
      body_ = hash_bytes(body_, b.data(), b.size());
  }
  template <class T> void append(const T &r) {
    check(!finished_, "archb after footer");
    validator_.accept(r);
    write(encode(r));
  }

public:
  Writer(const std::string &path, const Header &h) : validator_(h) {
    auto b = encode(h);
    out_ = gzopen(path.c_str(), "wb1");
    check(out_, "archb open");
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
  void epoch(const Epoch &r) { append(r); }
  void accepted(const Accepted &r) { append(r); }
  void invocation_seal(const InvocationSeal &r) { append(r); }
  void entry_member(const EntryMember &r) { append(r); }
  void terminal(const Terminal &r) { append(r); }
  void finish(Footer f) {
    check(!finished_, "archb duplicate footer");
    validator_.finish(f);
    f.body_hash = body_;
    write(encode(f), false);
    finished_ = true;
    auto output = out_;
    out_ = nullptr;
    check(gzclose(output) == Z_OK, "archb close");
  }
};

} // namespace archb
} // namespace fastsim::fst

#endif // FASTSIM_FST_ARCHITECTURAL_BOUNDARIES_CODEC
