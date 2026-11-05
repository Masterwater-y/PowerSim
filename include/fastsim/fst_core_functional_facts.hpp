#ifndef FASTSIM_FST_CORE_FUNCTIONAL_FACTS_CODEC
#define FASTSIM_FST_CORE_FUNCTIONAL_FACTS_CODEC
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>
#include <zlib.h>
#include "fastsim/fst_mmu_controls.hpp"
#include <type_traits>
#include "fastsim/retired_frontend_facts.hpp"

// Shared verbatim with the parent codec except its frontend-header include.
// Fixed little-endian format: no native register objects or timing state.
#if defined(__BYTE_ORDER__) && __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "Core functional facts codec requires a little-endian host"
#endif
namespace fastsim::fst {
inline constexpr std::uint64_t kCoreFunctionalFacts=1ull<<16;
inline constexpr std::uint64_t kCoreHashSeed=14695981039346656037ull;
inline constexpr unsigned kCoreMaxOperands=256;
// Register layout 1 follows the source-defined x86 flat architectural classes.
// Int includes the MicroEnd enum slot before Prodlow; it is not just 16+16+6.
// Native compile assertions bind these protocol expectations to real RegClasses.
inline constexpr std::array<std::uint32_t,7> kCoreX86RegistersV1{39,48,1,2,1,0,6};
inline constexpr std::uint32_t kCoreX86IntFoldBitV1=64;
enum CoreKnown : std::uint32_t {
    kCoreDecoderKnown=1u<<0, kCoreFrontendKnown=1u<<1,
    kCoreAllocationKnown=1u<<2, kCoreStaticKnown=1u<<3,
    kCoreFusionKnown=1u<<4, kCoreOperandsKnown=1u<<5,
    kCorePinsKnown=1u<<6, kCoreNormalizationKnown=1u<<7,
};
inline constexpr std::uint32_t kCoreKnownMask=255;
enum CoreStatic : std::uint32_t {
    kCoreSerializing=1u<<0, kCoreSerializeBefore=1u<<1,
    kCoreSerializeAfter=1u<<2, kCoreReadBarrier=1u<<3,
    kCoreWriteBarrier=1u<<4, kCoreNonSpeculative=1u<<5,
    kCoreStoreConditional=1u<<6, kCoreSquashAfter=1u<<7,
    kCoreMicrocodeControl=1u<<8, kCoreDelayedCommit=1u<<9,
    kCoreQuiesce=1u<<10, kCoreMicroop=1u<<11,
    kCoreFirstMicroop=1u<<12, kCoreLastMicroop=1u<<13,
    kCoreFullGprMove=1u<<14, kCoreFullXmmMove=1u<<15,
    kCoreArchitecturalLocked=1u<<16,
};
inline constexpr std::uint32_t kCoreStaticMask=(1u<<17)-1;
enum CoreAllocation : std::uint32_t { kCoreMicrofusedMemory=1, kCoreStackAllocation=2 };
enum class CoreFrontendOrigin : std::uint8_t { Unknown, ExistingRecord, ObserverRecord };
enum class CoreDecoderOrigin : std::uint8_t { Unknown, ExactByteWitness };
enum class CoreNormalizationStatus : std::uint8_t { Unknown, Partial, Complete, Conflicting };
enum class CoreNormalizationOrigin : std::uint8_t { Unknown, RenameOperand };
enum CoreOperandFlag : std::uint8_t {
    kCoreOriginalKnown=1, kCoreOriginalIsFlat=2, kCoreFlatKnown=4, kCorePinKnown=8
};
struct CoreFunctionalHeader {
    std::array<char,8> magic{'F','S','T','C','O','R','1',0};
    std::uint32_t version=1,header_size=96,core_id=0,row_size=128;
    std::array<std::uint8_t,32> policy_sha256{};
    std::uint16_t operand_size=24,macro_size=80,decoder_layout=1,register_layout=1;
    std::array<std::uint32_t,8> architectural_registers{};
};
struct CoreFunctionalRow {
    std::uint64_t record_ordinal=0,macro_begin=0,pc=0,code_identity=0;
    std::uint64_t semantic_prefix_identity=0,micro_pc=0,decoder_context=0,frontend_asid=0;
    std::uint32_t hot_record_hash=0,known_flags=0,static_flags=0,root_allocation_flags=0;
    std::uint16_t source_count=0,destination_count=0;
    std::uint32_t operand_count=0;
    std::uint16_t fusion_allowed_jcc_mask=0;
    std::uint8_t fusion_producer_family=0,jcc_condition=255,frontend_from_user=0;
    CoreFrontendOrigin frontend_origin=CoreFrontendOrigin::Unknown;
    CoreDecoderOrigin decoder_origin=CoreDecoderOrigin::Unknown;
    CoreNormalizationStatus normalization_status=CoreNormalizationStatus::Unknown;
    std::array<std::uint8_t,32> reserved{};
};
struct CoreFunctionalOperand {
    std::uint16_t operand_index=0;
    std::uint8_t original_class=255,flattened_class=255;
    std::uint32_t original_index=0,flattened_index=0;
    std::int32_t static_num_pinned_writes=0;
    std::uint8_t flags=0;
    CoreNormalizationOrigin normalization_origin=CoreNormalizationOrigin::Unknown;
    std::uint16_t reserved=0;
    std::uint32_t reserved2=0;
};
struct CoreFunctionalFooter {
    std::array<char,8> magic{'F','S','T','C','E','N','D','1'};
    std::uint64_t records=0,macros=0,stream_hash=kCoreHashSeed,payload_hash=kCoreHashSeed;
};
static_assert(sizeof(CoreFunctionalHeader)==96 && sizeof(CoreFunctionalRow)==128);
static_assert(sizeof(CoreFunctionalOperand)==24 && sizeof(CoreFunctionalFooter)==40);
static_assert(offsetof(CoreFunctionalHeader,architectural_registers)==64);
static_assert(offsetof(CoreFunctionalRow,reserved)==96);
static_assert(offsetof(CoreFunctionalOperand,flags)==16);
inline std::uint64_t core_stream_hash(std::uint64_t h,const void* p,std::size_t n) {
    const auto* bytes=static_cast<const unsigned char*>(p);
    for(std::size_t i=0;i<n;++i)h=(h^bytes[i])*1099511628211ull;
    return h;
}
inline std::uint32_t core_record_hash(const void* p) {
    std::uint32_t h=2166136261u;const auto* bytes=static_cast<const unsigned char*>(p);
    for(unsigned i=0;i<64;++i)h=(h^bytes[i])*16777619u;
    return h;
}
inline void validate_core_header(const CoreFunctionalHeader& h,std::uint32_t core) {
    const CoreFunctionalHeader expected;
    if(h.magic!=expected.magic || h.version!=1 || h.header_size!=96 || h.core_id!=core ||
       h.row_size!=128 || h.operand_size!=24 || h.macro_size!=80 ||
       h.decoder_layout!=1 || h.register_layout!=1 || h.policy_sha256==std::array<std::uint8_t,32>{})
        throw std::runtime_error("invalid core functional header/layout/policy");
    // These are source-defined x86 architectural classes, never physical counts.
    for(std::size_t i=0;i<kCoreX86RegistersV1.size();++i)
        if(h.architectural_registers[i]!=kCoreX86RegistersV1[i])
            throw std::runtime_error("invalid x86 architectural register layout");
    if(!h.architectural_registers[7] || h.architectural_registers[7]>65536)
        throw std::runtime_error("invalid x86 architectural register layout");
}
inline bool core_register_valid(const CoreFunctionalHeader& h,std::uint8_t cls,
                                std::uint32_t index,bool original,bool is_flat) {
    if(cls==255)return index==0;
    if(cls>7)return false;
    if(original && !is_flat && cls==0)index&=~kCoreX86IntFoldBitV1;
    const auto limit=h.architectural_registers[cls];
    return index<limit || (original && !is_flat && cls==1 && index>=limit && index-limit<8);
}
inline void validate_core_row(const CoreFunctionalHeader& h,const CoreFunctionalRow& r,
                              const std::vector<CoreFunctionalOperand>& ops) {
    if(r.known_flags&~kCoreKnownMask || r.static_flags&~kCoreStaticMask ||
       r.root_allocation_flags&~3u || r.reserved!=std::array<std::uint8_t,32>{} ||
       r.source_count>kCoreMaxOperands || r.destination_count>kCoreMaxOperands ||
       r.operand_count!=std::uint32_t(r.source_count)+r.destination_count || r.operand_count!=ops.size() ||
       r.macro_begin>r.record_ordinal || unsigned(r.frontend_origin)>2 || unsigned(r.decoder_origin)>1 ||
       unsigned(r.normalization_status)>3 || r.frontend_from_user>1 ||
       r.fusion_producer_family>3 || (r.jcc_condition>15 && r.jcc_condition!=255))
        throw std::runtime_error("invalid core functional row");
    if(bool(r.known_flags&kCoreDecoderKnown)!=(r.decoder_origin!=CoreDecoderOrigin::Unknown) ||
       (!(r.known_flags&kCoreDecoderKnown) && r.decoder_context) || (r.decoder_context&~0x3ff3full) ||
       bool(r.known_flags&kCoreFrontendKnown)!=(r.frontend_origin!=CoreFrontendOrigin::Unknown) ||
       (!(r.known_flags&kCoreFrontendKnown) && (r.frontend_asid || r.frontend_from_user)) ||
       (r.frontend_asid&0xfff) || (!(r.known_flags&kCoreStaticKnown) && r.static_flags) ||
       (!(r.known_flags&kCoreAllocationKnown) && (r.root_allocation_flags || (r.static_flags&kCoreArchitecturalLocked))) ||
       (!(r.known_flags&kCoreFusionKnown) && (r.fusion_producer_family || r.fusion_allowed_jcc_mask || r.jcc_condition!=255)) ||
       bool(r.known_flags&kCoreNormalizationKnown)!=(r.normalization_status==CoreNormalizationStatus::Complete))
        throw std::runtime_error("core functional known/provenance contradiction");
    const std::uint16_t fusion_masks[]={0,0xf0fc,0xffff,0xf030};
    if(r.fusion_allowed_jcc_mask!=fusion_masks[r.fusion_producer_family] ||
       ((r.static_flags&(kCoreSerializeBefore|kCoreSerializeAfter)) && !(r.static_flags&kCoreSerializing)) ||
       ((r.static_flags&(kCoreFirstMicroop|kCoreLastMicroop)) && !(r.static_flags&kCoreMicroop)))
        throw std::runtime_error("core static predicate contradiction");
    bool all_original=true,all_flat=true,all_pins=true;
    for(std::size_t i=0;i<ops.size();++i) {
        const auto& op=ops[i];const bool dest=i>=r.source_count;
        const bool original=op.flags&kCoreOriginalKnown,flat=op.flags&kCoreFlatKnown,pin=op.flags&kCorePinKnown;
        if(op.operand_index!=(dest?i-r.source_count:i) || op.flags&~15u || op.reserved || op.reserved2 ||
           unsigned(op.normalization_origin)>1 || op.static_num_pinned_writes<0 ||
           (!original && (op.original_class!=255 || op.original_index || (op.flags&kCoreOriginalIsFlat))) ||
           (!flat && (op.flattened_class!=255 || op.flattened_index)) ||
           (!pin && op.static_num_pinned_writes) || (!dest && pin) ||
           (flat && !original) || bool(flat)!=(op.normalization_origin==CoreNormalizationOrigin::RenameOperand) ||
           (original && !core_register_valid(h,op.original_class,op.original_index,true,op.flags&kCoreOriginalIsFlat)) ||
           (flat && (!core_register_valid(h,op.flattened_class,op.flattened_index,false,true) || op.flattened_class!=op.original_class)))
            throw std::runtime_error("invalid architectural core operand");
        if(flat) {
            // Register layout 1 follows native architectural flattening. Only
            // stack-relative x87 IDs depend on rename-time TOP; their captured
            // result must not be recomputed from a retirement-time context.
            const bool already_flat=op.flags&kCoreOriginalIsFlat;
            const bool stack_relative=!already_flat && op.original_class==1 &&
                op.original_index>=h.architectural_registers[1];
            const auto expected_index=!already_flat && op.original_class==0 ?
                op.original_index&~kCoreX86IntFoldBitV1 : op.original_index;
            if(!stack_relative && op.flattened_index!=expected_index)
                throw std::runtime_error("impossible architectural register normalization");
        }
        if(flat && (r.normalization_status==CoreNormalizationStatus::Unknown || r.normalization_status==CoreNormalizationStatus::Conflicting))
            throw std::runtime_error("unknown/conflicting normalization carries proven flat operand");
        all_original&=original;all_flat&=flat;if(dest)all_pins&=pin;
    }
    if(((r.known_flags&kCoreOperandsKnown) && !all_original) ||
       ((r.known_flags&kCorePinsKnown) && !all_pins) ||
       ((r.known_flags&kCoreNormalizationKnown) && !all_flat))
        throw std::runtime_error("incomplete architectural core operand population");
}

namespace core_v2 {
using mmuctl::Bytes;
using mmuctl::check;
using mmuctl::Cursor;
using mmuctl::put;
enum class Origin : std::uint8_t {
  Unknown,
  DecodedRoot,
  HardwareEntryRom,
  UnsupportedRom
};
enum class AllocationPolicy : std::uint8_t {
  Unknown,
  DecodedRootRules,
  IndependentRomMember
};
enum class RomKind : std::uint8_t {
  Unknown,
  Other,
  InternalBranch,
  ConditionalInternalBranch,
  UnconditionalEret,
  UnsupportedControl
};
struct Extension {
  std::uint32_t not_applicable_flags = 0;
  Origin semantic_origin = Origin::Unknown;
  AllocationPolicy allocation_policy = AllocationPolicy::Unknown;
  RomKind rom_kind = RomKind::Unknown;
  std::uint8_t extension_known = 0;
  std::uint64_t boundary_event = 0;
  std::uint32_t boundary_context = 0, boundary_generation = 0;
};
inline const std::string &layout_descriptor() {
  static const std::string s =
      R"FSTLAYOUT(Frozen wire descriptor spr-core-facts-v2

All integers below are explicit little-endian unsigned widths; offsets are bytes. No implicit C++ padding is on wire. Reserved bytes/unknown enum values/unsupported bits reject. `uN` means N-bit field, `zN` means N zero bytes. All rows use the existing 8-byte frame prefix `kind:u16,flags:u16=0,bytes:u32` unless explicitly stated otherwise. No native seq/pointer/tick/readiness/free-count/observed allocation outcome is serialized.

## 2. Hashes and identities

* `H32(hot)`: existing FNV-1a32 over exactly the original 64 hot bytes, seed 2166136261, prime 16777619. Every u64 hot-hash field is this value zero-extended; upper32 must be zero. `.core` keeps its existing u32 hot hash.
* `H64(bytes)`: existing FNV-1a64, seed 14695981039346656037, prime 1099511628211, modulo 2^64. `hot_hash` folds **all** original hot records, unmodified order, including warmup. Entry-member hot hash folds only the bound retained entry members in ordinal order, starting at the same seed.
* New body domains are ASCII including trailing NUL: `fst-archb-body-v1\0`, `fst-mmuctl-body-v2\0`, `fst-core-payload-v2\0`. Seed with H64(domain), then hash all nonfooter frames/tags and their exact encoded payload in actual wire order, excluding header and footer. Existing v1 domains/algorithms stay unchanged.
* Operation-list hash: H64 seeded with `fst-mmuctl-ops-v2\0`, followed by the exact encoded Write/Command frames in local index order. Empty operations have the domain seed, not zero. The same hash appears in controls Owner, archb InvocationSeal and archb Terminal when their operation list is known. No footer-to-footer hash dependency.
* Shared policy SHA256 is the maintained native source-policy digest. Each new layout has its own SHA256 of the final canonical UTF-8 layout descriptor. The curator must freeze the descriptor and its computed bytes before implementation release; do not reuse the v1 layout digest. Package manifests separately authenticate artifacts; FNV is an association/integrity check, not a cryptographic content certificate.
* Boundary identity is `(context:u32,generation:u32,event:u64)`. Generation nonzero after arming; event IDs begin at 1, monotonically increasing **within a context/generation**. Event numbering has no cross-core order meaning. UINT64_MAX is the absent ordinal sentinel; hashes accompanying absent ordinals are zero. Event 0 means no event. No “next ordinal” reconstruction.

## 5. `.core.gz` v2, still 128-byte members

Retain first96 member bytes exactly: existing identity, hot hash, Known/static/root fields and operands. Replace reserved offsets96–127 only in version2:

| Offset | Field |
|---|---|
|96| `not_applicable_flags:u32` |
|100| `semantic_origin:u8` |
|101| `allocation_policy:u8` |
|102| `rom_kind:u8` |
|103| `extension_known:u8` |
|104| `boundary_event:u64` |
|112| `boundary_context:u32` |
|116| `boundary_generation:u32` |
|120| `z8` |

N/A allowed bits are exactly Decoder(1), Allocation(4), Fusion(16), mask21, disjoint from known_flags. N/A only for source-proved no-decoded-root hardware ROM with validated boundary/ffe/fsem membership. Ordinary applicable absence remains Unknown. Keep canonical zeros in decoder/root/fusion fields when N/A; never set their Known bits or read dummy ExtMachInst/size4.

Origin Unknown=0,DecodedRoot=1,HardwareEntryRom=2,UnsupportedRom=3. AllocationPolicy Unknown=0,DecodedRootRules=1,IndependentRomMember=2. RomKind Unknown=0,Other=1,InternalBranch=2,ConditionalInternalBranch=3,UnconditionalEret=4,UnsupportedControl=5. extension_known bits AllocationPolicy=1,RomKind=2,BoundaryToken=4. Unknown extension values have zero payload. Hardware ROM's independent-member policy is an immutable source rule, not native allocationSlots result. DecodedRoot has no boundary token and does not receive blanket ROM N/A (software ROM with a decoded macro remains decoded-root domain). Unsupported nested/unowned ROM remains explicit Unknown/N/A-unqualified.

Header128: first96 bytes preserve existing core header fields/offsets except magic `FSTCOR2\0`, version2, header_size128; append layout_sha256[32] at96. Row_size128,operand_size24,macro_size80,decoder_layout1,register_layout1 unchanged. Existing tag1 row+operands and tag2 ffe macro remain. Footer64, no frame prefix: magic8 `FSTCEND2`; records:u64,macros:u64,hot_hash:u64,payload_hash:u64,na_rom_rows:u64,boundary_rows:u64,unknown_applicable_rows:u64. These eight fields total64. N/A/Known/Unknown group counters are computed for every group by the full-source reader; the footer row counters are a cross-check, not a replacement. UnknownApplicable excludes properly proved N/A, but `(known|N/A)==255` alone is insufficient.

For each token-bound core row require exactly one archb EntryMember with the same token, ordinal/hash/micro_pc/macro_begin/rom_kind. `.core` boundary_rows equals archb.member_rows. `.core` na_rom_rows equals archb.na_rom_rows and is a subset of boundary_rows. Existing `.ffe` macro-byte/context checks continue on DecoderKnown rows; source-proved N/A has zero bytes and Pseudo+Microcode, no DSB eligibility. Normalization/operands/pins/static/frontend remain applicable. Partial accepted entry can have true observed ROM origin but cannot satisfy complete bundle readiness; do not erase its rows to hide that status.


Core framing exception: core-v2 uses existing u8 tags1/2/255; payload includes tags1/2 plus exact row/operand/macro bytes; excludes header/tag255/footer. All other frames use specified8-byte prefix.
Epoch proof bit1 is NoO3ExecutionInEpoch in the completed takeover generation, not historical cold memory/TLB state.
)FSTLAYOUT";
  return s;
}
inline constexpr std::array<std::uint8_t, 32> LayoutDigest = {
    0x53, 0x19, 0xee, 0x9a, 0xae, 0xfa, 0xb9, 0x94, 0x5c, 0xd9, 0x18,
    0x9a, 0x2e, 0x36, 0x86, 0x8,  0x8f, 0xc2, 0xa5, 0x15, 0xa0, 0x38,
    0x41, 0x4a, 0x6,  0x1d, 0x13, 0x15, 0x27, 0x4,  0xaa, 0xaf};
inline std::uint64_t payload_seed() {
  static constexpr char domain[] = "fst-core-payload-v2";
  return core_stream_hash(kCoreHashSeed, domain, sizeof(domain));
}
inline Bytes encode_extension(const Extension &e) {
  Bytes b;
  put(b, e.not_applicable_flags, 4);
  put(b, unsigned(e.semantic_origin), 1);
  put(b, unsigned(e.allocation_policy), 1);
  put(b, unsigned(e.rom_kind), 1);
  put(b, e.extension_known, 1);
  put(b, e.boundary_event, 8);
  put(b, e.boundary_context, 4);
  put(b, e.boundary_generation, 4);
  b.resize(32);
  return b;
}
inline Extension extension(const CoreFunctionalRow &r) {
  Bytes b(r.reserved.begin(), r.reserved.end());
  Cursor c{b};
  Extension e;
  e.not_applicable_flags =
      static_cast<std::remove_reference_t<decltype(e.not_applicable_flags)>>(
          c.get(4));
  e.semantic_origin = Origin(c.get(1));
  e.allocation_policy = AllocationPolicy(c.get(1));
  e.rom_kind = RomKind(c.get(1));
  e.extension_known =
      static_cast<std::remove_reference_t<decltype(e.extension_known)>>(
          c.get(1));
  e.boundary_event =
      static_cast<std::remove_reference_t<decltype(e.boundary_event)>>(
          c.get(8));
  e.boundary_context =
      static_cast<std::remove_reference_t<decltype(e.boundary_context)>>(
          c.get(4));
  e.boundary_generation =
      static_cast<std::remove_reference_t<decltype(e.boundary_generation)>>(
          c.get(4));
  c.zero(8);
  return e;
}
inline void set_extension(CoreFunctionalRow &r, const Extension &e) {
  auto b = encode_extension(e);
  std::copy(b.begin(), b.end(), r.reserved.begin());
}
inline void validate_header(const CoreFunctionalHeader &h) {
  check(h.version == 2 && h.header_size == 128 &&
            h.magic ==
                std::array<char, 8>{'F', 'S', 'T', 'C', 'O', 'R', '2', 0},
        "core-v2 header version");
  auto legacy = h;
  legacy.version = 1;
  legacy.header_size = 96;
  legacy.magic[6] = '1';
  validate_core_header(legacy, h.core_id);
}
inline Bytes encode_header(const CoreFunctionalHeader &h) {
  validate_header(h);
  Bytes b;
  for (auto v : h.magic)
    put(b, v, 1);
  for (auto v : {h.version, h.header_size, h.core_id, h.row_size})
    put(b, v, 4);
  b.insert(b.end(), h.policy_sha256.begin(), h.policy_sha256.end());
  for (auto v :
       {h.operand_size, h.macro_size, h.decoder_layout, h.register_layout})
    put(b, v, 2);
  for (auto v : h.architectural_registers)
    put(b, v, 4);
  b.insert(b.end(), LayoutDigest.begin(), LayoutDigest.end());
  return b;
}
inline CoreFunctionalHeader decode_header(const Bytes &b) {
  check(b.size() == 128, "core-v2 header size");
  Cursor c{b};
  CoreFunctionalHeader h;
  for (auto &v : h.magic)
    v = static_cast<std::remove_reference_t<decltype(v)>>(c.get(1));
  for (auto v : {&h.version, &h.header_size, &h.core_id, &h.row_size})
    *v = static_cast<std::remove_reference_t<decltype(*v)>>(c.get(4));
  for (auto &v : h.policy_sha256)
    v = static_cast<std::remove_reference_t<decltype(v)>>(c.get(1));
  for (auto v :
       {&h.operand_size, &h.macro_size, &h.decoder_layout, &h.register_layout})
    *v = static_cast<std::remove_reference_t<decltype(*v)>>(c.get(2));
  for (auto &v : h.architectural_registers)
    v = static_cast<std::remove_reference_t<decltype(v)>>(c.get(4));
  for (auto v : LayoutDigest)
    check(c.get(1) == v, "core-v2 layout");
  validate_header(h);
  return h;
}
inline void validate_row(const CoreFunctionalHeader &h,
                         const CoreFunctionalRow &r,
                         const std::vector<CoreFunctionalOperand> &ops) {
  auto e = extension(r);
  auto legacy = r;
  legacy.reserved = {};
  validate_core_row(h, legacy, ops);
  check(!(e.not_applicable_flags & ~21u) &&
            !(e.not_applicable_flags & r.known_flags) &&
            unsigned(e.semantic_origin) <= 3 &&
            unsigned(e.allocation_policy) <= 2 && unsigned(e.rom_kind) <= 5 &&
            !(e.extension_known & ~7u),
        "core-v2 applicability enums");
  check(bool(e.extension_known & 1) ==
                (e.allocation_policy != AllocationPolicy::Unknown) &&
            bool(e.extension_known & 2) == (e.rom_kind != RomKind::Unknown),
        "core-v2 extension knowledge");
  check((e.extension_known & 4) ? e.boundary_event && e.boundary_generation
                                : !e.boundary_event && !e.boundary_context &&
                                      !e.boundary_generation,
        "core-v2 boundary token");
  if (e.semantic_origin == Origin::HardwareEntryRom) {
    check(e.not_applicable_flags == 21 && e.extension_known == 7 &&
              e.allocation_policy == AllocationPolicy::IndependentRomMember &&
              e.rom_kind != RomKind::UnsupportedControl,
          "core-v2 unproved ROM applicability");
    check(!(r.known_flags & 21) && !r.decoder_context &&
              r.decoder_origin == CoreDecoderOrigin::Unknown &&
              !r.root_allocation_flags && !r.fusion_producer_family &&
              !r.fusion_allowed_jcc_mask && r.jcc_condition == 255,
          "core-v2 fabricated ROM root");
  } else {
    check(!e.not_applicable_flags, "core-v2 N/A without ROM source");
    check(e.semantic_origin != Origin::DecodedRoot ||
              (!(e.extension_known & 4) &&
               e.allocation_policy != AllocationPolicy::IndependentRomMember),
          "decoded-root boundary contradiction");
    check(e.semantic_origin != Origin::Unknown || !e.extension_known,
          "unknown source extension");
  }
}
inline bool unknown_applicable(const CoreFunctionalRow &r) {
  auto e = extension(r);
  return (r.known_flags | e.not_applicable_flags) != 255 ||
         e.semantic_origin == Origin::Unknown ||
         e.semantic_origin == Origin::UnsupportedRom ||
         !(e.extension_known & 1);
}
inline Bytes encode_row(const CoreFunctionalRow &r) {
  Bytes b;
  put(b, std::uint64_t(r.record_ordinal), 8);
  put(b, std::uint64_t(r.macro_begin), 8);
  put(b, std::uint64_t(r.pc), 8);
  put(b, std::uint64_t(r.code_identity), 8);
  put(b, std::uint64_t(r.semantic_prefix_identity), 8);
  put(b, std::uint64_t(r.micro_pc), 8);
  put(b, std::uint64_t(r.decoder_context), 8);
  put(b, std::uint64_t(r.frontend_asid), 8);
  put(b, std::uint64_t(r.hot_record_hash), 4);
  put(b, std::uint64_t(r.known_flags), 4);
  put(b, std::uint64_t(r.static_flags), 4);
  put(b, std::uint64_t(r.root_allocation_flags), 4);
  put(b, std::uint64_t(r.source_count), 2);
  put(b, std::uint64_t(r.destination_count), 2);
  put(b, std::uint64_t(r.operand_count), 4);
  put(b, std::uint64_t(r.fusion_allowed_jcc_mask), 2);
  put(b, std::uint64_t(r.fusion_producer_family), 1);
  put(b, std::uint64_t(r.jcc_condition), 1);
  put(b, std::uint64_t(r.frontend_from_user), 1);
  put(b, std::uint64_t(r.frontend_origin), 1);
  put(b, std::uint64_t(r.decoder_origin), 1);
  put(b, std::uint64_t(r.normalization_status), 1);
  b.insert(b.end(), r.reserved.begin(), r.reserved.end());
  return b;
}
inline CoreFunctionalRow decode_row(const Bytes &b) {
  check(b.size() == 128, "core-v2 row size");
  Cursor c{b};
  CoreFunctionalRow r;
  r.record_ordinal = static_cast<decltype(r.record_ordinal)>(c.get(8));
  r.macro_begin = static_cast<decltype(r.macro_begin)>(c.get(8));
  r.pc = static_cast<decltype(r.pc)>(c.get(8));
  r.code_identity = static_cast<decltype(r.code_identity)>(c.get(8));
  r.semantic_prefix_identity =
      static_cast<decltype(r.semantic_prefix_identity)>(c.get(8));
  r.micro_pc = static_cast<decltype(r.micro_pc)>(c.get(8));
  r.decoder_context = static_cast<decltype(r.decoder_context)>(c.get(8));
  r.frontend_asid = static_cast<decltype(r.frontend_asid)>(c.get(8));
  r.hot_record_hash = static_cast<decltype(r.hot_record_hash)>(c.get(4));
  r.known_flags = static_cast<decltype(r.known_flags)>(c.get(4));
  r.static_flags = static_cast<decltype(r.static_flags)>(c.get(4));
  r.root_allocation_flags =
      static_cast<decltype(r.root_allocation_flags)>(c.get(4));
  r.source_count = static_cast<decltype(r.source_count)>(c.get(2));
  r.destination_count = static_cast<decltype(r.destination_count)>(c.get(2));
  r.operand_count = static_cast<decltype(r.operand_count)>(c.get(4));
  r.fusion_allowed_jcc_mask =
      static_cast<decltype(r.fusion_allowed_jcc_mask)>(c.get(2));
  r.fusion_producer_family =
      static_cast<decltype(r.fusion_producer_family)>(c.get(1));
  r.jcc_condition = static_cast<decltype(r.jcc_condition)>(c.get(1));
  r.frontend_from_user = static_cast<decltype(r.frontend_from_user)>(c.get(1));
  r.frontend_origin = static_cast<decltype(r.frontend_origin)>(c.get(1));
  r.decoder_origin = static_cast<decltype(r.decoder_origin)>(c.get(1));
  r.normalization_status =
      static_cast<decltype(r.normalization_status)>(c.get(1));
  for (auto &v : r.reserved) {
    v = static_cast<std::remove_reference_t<decltype(v)>>(c.get(1));
  }
  extension(r);
  return r;
}
inline Bytes encode_operand(const CoreFunctionalOperand &r) {
  Bytes b;
  put(b, std::uint64_t(r.operand_index), 2);
  put(b, std::uint64_t(r.original_class), 1);
  put(b, std::uint64_t(r.flattened_class), 1);
  put(b, std::uint64_t(r.original_index), 4);
  put(b, std::uint64_t(r.flattened_index), 4);
  put(b, std::uint64_t(r.static_num_pinned_writes), 4);
  put(b, std::uint64_t(r.flags), 1);
  put(b, std::uint64_t(r.normalization_origin), 1);
  put(b, std::uint64_t(r.reserved), 2);
  put(b, std::uint64_t(r.reserved2), 4);
  return b;
}
inline CoreFunctionalOperand decode_operand(const Bytes &b) {
  check(b.size() == 24, "core-v2 operand size");
  Cursor c{b};
  CoreFunctionalOperand r;
  r.operand_index = static_cast<decltype(r.operand_index)>(c.get(2));
  r.original_class = static_cast<decltype(r.original_class)>(c.get(1));
  r.flattened_class = static_cast<decltype(r.flattened_class)>(c.get(1));
  r.original_index = static_cast<decltype(r.original_index)>(c.get(4));
  r.flattened_index = static_cast<decltype(r.flattened_index)>(c.get(4));
  r.static_num_pinned_writes =
      static_cast<decltype(r.static_num_pinned_writes)>(c.get(4));
  r.flags = static_cast<decltype(r.flags)>(c.get(1));
  r.normalization_origin =
      static_cast<decltype(r.normalization_origin)>(c.get(1));
  r.reserved = static_cast<decltype(r.reserved)>(c.get(2));
  r.reserved2 = static_cast<decltype(r.reserved2)>(c.get(4));
  return r;
}
inline Bytes encode_macro(const RetiredFrontendMacro &r) {
  Bytes b;
  for (auto v : {r.begin_record, r.end_record, r.pc, r.address_space_id,
                 r.code_identity, r.semantic_identity})
    put(b, v, 8);
  put(b, r.flags, 4);
  put(b, r.known_flags, 4);
  put(b, r.instruction_size, 1);
  b.insert(b.end(), r.bytes.begin(), r.bytes.end());
  put(b, r.observed_uops, 4);
  put(b, r.reserved, 4);
  return b;
}
inline RetiredFrontendMacro decode_macro(const Bytes &b) {
  check(b.size() == 80, "core-v2 macro size");
  Cursor c{b};
  RetiredFrontendMacro r;
  for (auto v : {&r.begin_record, &r.end_record, &r.pc, &r.address_space_id,
                 &r.code_identity, &r.semantic_identity})
    *v = static_cast<std::remove_reference_t<decltype(*v)>>(c.get(8));
  r.flags = static_cast<std::remove_reference_t<decltype(r.flags)>>(c.get(4));
  r.known_flags =
      static_cast<std::remove_reference_t<decltype(r.known_flags)>>(c.get(4));
  r.instruction_size =
      static_cast<std::remove_reference_t<decltype(r.instruction_size)>>(
          c.get(1));
  for (auto &v : r.bytes) {
    v = static_cast<std::remove_reference_t<decltype(v)>>(c.get(1));
  }
  r.observed_uops =
      static_cast<std::remove_reference_t<decltype(r.observed_uops)>>(c.get(4));
  r.reserved =
      static_cast<std::remove_reference_t<decltype(r.reserved)>>(c.get(4));
  return r;
}
struct Footer {
  std::uint64_t records = 0, macros = 0, hot_hash = kCoreHashSeed,
                payload_hash = payload_seed(), na_rom_rows = 0,
                boundary_rows = 0, unknown_applicable_rows = 0;
};
inline Bytes encode_footer(const Footer &f) {
  Bytes b{'F', 'S', 'T', 'C', 'E', 'N', 'D', '2'};
  for (auto v : {f.records, f.macros, f.hot_hash, f.payload_hash, f.na_rom_rows,
                 f.boundary_rows, f.unknown_applicable_rows})
    put(b, v, 8);
  return b;
}
inline Footer decode_footer(const Bytes &b) {
  check(b.size() == 64, "core-v2 footer size");
  Cursor c{b};
  for (auto v : std::array<char, 8>{'F', 'S', 'T', 'C', 'E', 'N', 'D', '2'})
    check(c.get(1) == unsigned(v), "core-v2 footer magic");
  Footer f;
  for (auto v : {&f.records, &f.macros, &f.hot_hash, &f.payload_hash,
                 &f.na_rom_rows, &f.boundary_rows, &f.unknown_applicable_rows})
    *v = static_cast<std::remove_reference_t<decltype(*v)>>(c.get(8));
  return f;
}
} // namespace core_v2

// One row and one current macro accumulator, independent of ROI/macro length.
class CoreFunctionalWriter {
  gzFile out_ = nullptr;
  CoreFunctionalHeader header_;
  std::uint64_t records_ = 0, macros_ = 0, macro_begin_ = 0;
  std::uint64_t hot_hash_ = kCoreHashSeed, payload_hash_ = kCoreHashSeed;
  std::uint64_t macro_pc_ = 0, macro_code_ = 0, last_semantic_ = 0;
  std::uint64_t na_rom_rows_ = 0, boundary_rows_ = 0,
                unknown_applicable_rows_ = 0;
  void write(const void *p, unsigned n, bool payload = true) {
    if (!out_ || gzwrite(out_, p, n) != static_cast<int>(n))
      throw std::runtime_error("core functional write failed");
    if (payload)
      payload_hash_ = core_stream_hash(payload_hash_, p, n);
  }

public:
  CoreFunctionalWriter(const std::string &path,
                       const CoreFunctionalHeader &header)
      : header_(header) {
    if (header_.version == 2) {
      core_v2::validate_header(header_);
      payload_hash_ = core_v2::payload_seed();
    } else
      validate_core_header(header_, header_.core_id);
    out_ = gzopen(path.c_str(), "wb1");
    if (!out_)
      throw std::runtime_error("cannot create core functional companion");
    try {
      if (header_.version == 2) {
        auto b = core_v2::encode_header(header_);
        write(b.data(), static_cast<unsigned>(b.size()), false);
      } else
        write(&header_, sizeof(header_), false);
    } catch (...) {
      gzclose(out_);
      out_ = nullptr;
      throw;
    }
  }
  ~CoreFunctionalWriter() {
    if (out_)
      gzclose(out_);
  }
  CoreFunctionalWriter(const CoreFunctionalWriter &) = delete;
  CoreFunctionalWriter &operator=(const CoreFunctionalWriter &) = delete;
  void observe(const void *hot, CoreFunctionalRow row,
               const std::vector<CoreFunctionalOperand> &ops) {
    row.record_ordinal = records_;
    row.hot_record_hash = core_record_hash(hot);
    if (header_.version == 2)
      core_v2::validate_row(header_, row, ops);
    else
      validate_core_row(header_, row, ops);
    if (row.macro_begin != macro_begin_)
      throw std::runtime_error("core functional macro start mismatch");
    std::uint64_t hot_pc = 0;
    std::memcpy(&hot_pc, hot, 8);
    if (row.pc != hot_pc)
      throw std::runtime_error("core functional hot PC mismatch");
    if (records_ == macro_begin_) {
      macro_pc_ = row.pc;
      macro_code_ = row.code_identity;
    } else if (row.pc != macro_pc_ || row.code_identity != macro_code_)
      throw std::runtime_error("core functional macro identity changed");
    const unsigned char tag = 1;
    write(&tag, 1);
    if (header_.version == 2) {
      auto b = core_v2::encode_row(row);
      write(b.data(), static_cast<unsigned>(b.size()));
      for (const auto &op : ops) {
        auto ob = core_v2::encode_operand(op);
        write(ob.data(), static_cast<unsigned>(ob.size()));
      }
      const auto e = core_v2::extension(row);
      na_rom_rows_ += bool(e.not_applicable_flags);
      boundary_rows_ += bool(e.extension_known & 4);
      unknown_applicable_rows_ += core_v2::unknown_applicable(row);
    } else {
      write(&row, sizeof(row));
      if (!ops.empty())
        write(ops.data(), static_cast<unsigned>(ops.size() *
                                                sizeof(CoreFunctionalOperand)));
    }
    last_semantic_ = row.semantic_prefix_identity;
    hot_hash_ = core_stream_hash(hot_hash_, hot, 64);
    ++records_;
  }
  void append_macro(const RetiredFrontendMacro &m) {
    if (m.begin_record != macro_begin_ || m.end_record != records_ ||
        m.end_record <= m.begin_record ||
        m.end_record - m.begin_record != m.observed_uops || m.pc != macro_pc_ ||
        m.code_identity != macro_code_ ||
        m.semantic_identity != last_semantic_ || m.reserved ||
        m.instruction_size > 15 || m.flags & ~kFrontendValueFlags ||
        m.known_flags & ~kFrontendRequiredKnown || (m.flags & ~m.known_flags))
      throw std::runtime_error("core functional macro binding mismatch");
    const unsigned char tag = 2;
    write(&tag, 1);
    if (header_.version == 2) {
      auto b = core_v2::encode_macro(m);
      write(b.data(), static_cast<unsigned>(b.size()));
    } else
      write(&m, sizeof(m));
    macro_begin_ = records_;
    ++macros_;
  }
  void finish(std::uint64_t records) {
    if (!out_)
      return;
    if (records != records_ || macro_begin_ != records_)
      throw std::runtime_error("incomplete core functional population");
    CoreFunctionalFooter f;
    f.records = records_;
    f.macros = macros_;
    f.stream_hash = hot_hash_;
    f.payload_hash = payload_hash_;
    const unsigned char tag = 255;
    write(&tag, 1, false);
    if (header_.version == 2) {
      core_v2::Footer v;
      v.records = records_;
      v.macros = macros_;
      v.hot_hash = hot_hash_;
      v.payload_hash = payload_hash_;
      v.na_rom_rows = na_rom_rows_;
      v.boundary_rows = boundary_rows_;
      v.unknown_applicable_rows = unknown_applicable_rows_;
      auto b = core_v2::encode_footer(v);
      write(b.data(), static_cast<unsigned>(b.size()), false);
    } else
      write(&f, sizeof(f), false);
    auto out = out_;
    out_ = nullptr;
    if (gzclose(out) != Z_OK)
      throw std::runtime_error("core functional close failed");
  }
};
} // namespace fastsim::fst

#endif // FASTSIM_FST_CORE_FUNCTIONAL_FACTS_CODEC
