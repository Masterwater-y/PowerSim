#pragma once
#include <array>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>
#include <zlib.h>
namespace fastsim::fst {
constexpr std::uint64_t kStaticUopFacts=1ull<<14;
constexpr std::uint32_t kStaticExecutionKnown=1u<<0;
constexpr std::uint32_t kStaticOperandRolesKnown=1u<<1;
constexpr std::uint32_t kStaticAddressDataKnown=1u<<2;
constexpr std::uint32_t kStaticStorePairKnown=1u<<3;
constexpr std::uint32_t kStaticAbandonStoreGroups=1u<<4;
constexpr std::uint32_t kStaticKnownFlags=31;
enum class StaticStoreRole : std::uint8_t { None,Unified,Address,Data };
struct StaticUopHeader {
    std::array<char,8> magic{'F','S','T','U','O','P','1',0};
    std::uint32_t version=1,header_size=64,core_id=0,row_size=64;
    std::array<std::uint8_t,32> policy_sha256{};
    std::uint64_t reserved=0;
};
struct StaticUopFactsRow {
    std::uint64_t record_ordinal=0;
    std::uint32_t hot_record_hash=0,flags=0;
    std::uint64_t store_group=0,address_operands=0,data_operands=0;
    std::uint16_t candidate_ports=0,nominal_latency=1,source_count=0,destination_count=0;
    // Native IntExecutionFamily: None, ALU, SHFT, SlowInt, BM, Branch, Divider.
    std::uint8_t integer_family=0,zero_idiom=0;
    StaticStoreRole store_role=StaticStoreRole::None;
    std::uint8_t reserved=0;
    std::uint32_t operand_count=0;
    std::uint64_t reserved2=0;
};
struct StaticUopOperand {
    std::uint32_t producer_distance=0;
    std::uint16_t consumer_operand=0;
    std::uint8_t producer_role=5,reg_class=255; // native static role, never a ready time
};
struct StaticUopFooter {
    std::array<char,8> magic{'F','S','T','U','E','N','D','1'};
    std::uint64_t records=0,stream_hash=14695981039346656037ull;
};
static_assert(sizeof(StaticUopHeader)==64);
static_assert(sizeof(StaticUopFactsRow)==64);
static_assert(sizeof(StaticUopOperand)==8);
static_assert(sizeof(StaticUopFooter)==24);
inline std::uint64_t static_uop_stream_hash(std::uint64_t h,const void* record) {
    const auto* b=static_cast<const unsigned char*>(record);
    for(unsigned i=0;i<64;++i) h=(h^b[i])*1099511628211ull;
    return h;
}
inline std::uint32_t static_uop_record_hash(const void* record) {
    std::uint32_t h=2166136261u;const auto* b=static_cast<const unsigned char*>(record);
    for(unsigned i=0;i<64;++i) h=(h^b[i])*16777619u;
    return h;
}
inline void validate_static_uop(const StaticUopFactsRow& row,const std::vector<StaticUopOperand>& operands) {
    if(row.flags&~kStaticKnownFlags || row.reserved || row.reserved2 || row.integer_family>6 || row.zero_idiom>4 ||
       static_cast<unsigned>(row.store_role)>3 || row.candidate_ports>0xfff || !row.nominal_latency ||
       row.operand_count!=operands.size() || row.source_count!=operands.size() || operands.size()>256)
        throw std::runtime_error("invalid static UOP descriptor");
    if((row.flags&kStaticExecutionKnown) && ((row.integer_family==0)!=(row.candidate_ports==0)))
        throw std::runtime_error("static family/candidate policy contradiction");
    if((row.flags&kStaticAddressDataKnown) && (row.source_count>64 ||
       (row.source_count<64 && ((row.address_operands|row.data_operands)>>row.source_count))))
        throw std::runtime_error("static address/data operand mask exceeds source count");
    if((row.flags&kStaticStorePairKnown) && (row.store_role==StaticStoreRole::Address || row.store_role==StaticStoreRole::Data) && !row.store_group)
        throw std::runtime_error("known split store pair lacks logical group");
    if(row.store_role==StaticStoreRole::None && row.store_group)
        throw std::runtime_error("nonstore UOP carries a store group");
    for(std::size_t i=0;i<operands.size();++i) {
        const auto& op=operands[i];
        if(op.consumer_operand!=i || op.producer_distance>row.record_ordinal || op.producer_role>5 ||
           (op.reg_class>3 && op.reg_class!=255) || (op.reg_class==255 && op.producer_distance))
            throw std::runtime_error("invalid static UOP operand provenance");
    }
}
// Header-only writer is shared verbatim with the independent native project.
// One row per retained hot record; finish closes the compressed stream.
class StaticUopWriter {
    gzFile out_=nullptr;
    std::uint64_t records_=0,hash_=14695981039346656037ull;
    void write(const void* p,unsigned n) {
        if(!out_ || gzwrite(out_,p,n)!=static_cast<int>(n)) throw std::runtime_error("static UOP companion write failed");
    }
public:
    StaticUopWriter(const std::string& path,std::uint32_t core,const std::array<std::uint8_t,32>& policy) {
        if(policy==std::array<std::uint8_t,32>{}) throw std::invalid_argument("static UOP source policy is undeclared");
        out_=gzopen(path.c_str(),"wb1");if(!out_) throw std::runtime_error("cannot create static UOP companion");
        StaticUopHeader h;h.core_id=core;h.policy_sha256=policy;write(&h,sizeof(h));
    }
    ~StaticUopWriter(){if(out_)gzclose(out_);}
    void observe(const void* record,StaticUopFactsRow row,const std::vector<StaticUopOperand>& operands) {
        row.record_ordinal=records_;row.hot_record_hash=static_uop_record_hash(record);
        row.operand_count=static_cast<std::uint32_t>(operands.size());
        validate_static_uop(row,operands);
        write(&row,sizeof(row));
        if(!operands.empty())write(operands.data(),static_cast<unsigned>(operands.size()*sizeof(StaticUopOperand)));
        hash_=static_uop_stream_hash(hash_,record);++records_;
    }
    void finish(std::uint64_t records) {
        if(!out_)return;
        if(records!=records_)throw std::runtime_error("static UOP population mismatch");
        StaticUopFooter f;f.records=records_;f.stream_hash=hash_;write(&f,sizeof(f));
        const auto code=gzclose(out_);out_=nullptr;
        if(code!=Z_OK)throw std::runtime_error("static UOP companion finalization failed");
    }
};
} // namespace fastsim::fst
