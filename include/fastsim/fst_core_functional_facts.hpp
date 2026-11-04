#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>
#include <zlib.h>
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

// One row and one current macro accumulator, independent of ROI/macro length.
class CoreFunctionalWriter {
    gzFile out_=nullptr;
    CoreFunctionalHeader header_;
    std::uint64_t records_=0,macros_=0,macro_begin_=0;
    std::uint64_t hot_hash_=kCoreHashSeed,payload_hash_=kCoreHashSeed;
    std::uint64_t macro_pc_=0,macro_code_=0,last_semantic_=0;
    void write(const void* p,unsigned n,bool payload=true) {
        if(!out_ || gzwrite(out_,p,n)!=static_cast<int>(n))throw std::runtime_error("core functional write failed");
        if(payload)payload_hash_=core_stream_hash(payload_hash_,p,n);
    }
public:
    CoreFunctionalWriter(const std::string& path,const CoreFunctionalHeader& header):header_(header) {
        validate_core_header(header_,header_.core_id);
        out_=gzopen(path.c_str(),"wb1");if(!out_)throw std::runtime_error("cannot create core functional companion");
        try {write(&header_,sizeof(header_),false);}catch(...){gzclose(out_);out_=nullptr;throw;}
    }
    ~CoreFunctionalWriter(){if(out_)gzclose(out_);}
    CoreFunctionalWriter(const CoreFunctionalWriter&)=delete;
    CoreFunctionalWriter& operator=(const CoreFunctionalWriter&)=delete;
    void observe(const void* hot,CoreFunctionalRow row,const std::vector<CoreFunctionalOperand>& ops) {
        row.record_ordinal=records_;row.hot_record_hash=core_record_hash(hot);
        validate_core_row(header_,row,ops);
        if(row.macro_begin!=macro_begin_)throw std::runtime_error("core functional macro start mismatch");
        std::uint64_t hot_pc=0;std::memcpy(&hot_pc,hot,8);
        if(row.pc!=hot_pc)throw std::runtime_error("core functional hot PC mismatch");
        if(records_==macro_begin_){macro_pc_=row.pc;macro_code_=row.code_identity;}
        else if(row.pc!=macro_pc_ || row.code_identity!=macro_code_)throw std::runtime_error("core functional macro identity changed");
        const unsigned char tag=1;write(&tag,1);write(&row,sizeof(row));
        if(!ops.empty())write(ops.data(),static_cast<unsigned>(ops.size()*sizeof(CoreFunctionalOperand)));
        last_semantic_=row.semantic_prefix_identity;
        hot_hash_=core_stream_hash(hot_hash_,hot,64);++records_;
    }
    void append_macro(const RetiredFrontendMacro& m) {
        if(m.begin_record!=macro_begin_ || m.end_record!=records_ || m.end_record<=m.begin_record ||
           m.end_record-m.begin_record!=m.observed_uops || m.pc!=macro_pc_ || m.code_identity!=macro_code_ ||
           m.semantic_identity!=last_semantic_ || m.reserved || m.instruction_size>15 ||
           m.flags&~kFrontendValueFlags || m.known_flags&~kFrontendRequiredKnown || (m.flags&~m.known_flags))
            throw std::runtime_error("core functional macro binding mismatch");
        const unsigned char tag=2;write(&tag,1);write(&m,sizeof(m));macro_begin_=records_;++macros_;
    }
    void finish(std::uint64_t records) {
        if(!out_)return;
        if(records!=records_ || macro_begin_!=records_)throw std::runtime_error("incomplete core functional population");
        CoreFunctionalFooter f;f.records=records_;f.macros=macros_;f.stream_hash=hot_hash_;f.payload_hash=payload_hash_;
        const unsigned char tag=255;write(&tag,1,false);write(&f,sizeof(f),false);
        auto out=out_;out_=nullptr;if(gzclose(out)!=Z_OK)throw std::runtime_error("core functional close failed");
    }
};
} // namespace fastsim::fst
