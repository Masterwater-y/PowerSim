#include "fastsim/fst_static_uop_reader.hpp"
#include "fastsim/types.hpp"
#include <algorithm>
namespace fastsim::fst {
void validate_static_uop_projection(const StaticUopFactsRow& row,const std::vector<StaticUopOperand>& operands,
                                   const void* record,const std::vector<std::uint32_t>& extra) {
    validate_static_uop(row,operands);
    if(!(row.flags&kStaticOperandRolesKnown))return;
    const auto& hot=*static_cast<const fastsim::TraceRecord*>(record);
    std::vector<std::uint32_t> expected,actual;
    for(auto d:hot.producer_dists)if(d)expected.push_back(d);
    for(auto d:extra)if(d)expected.push_back(d);
    for(const auto& op:operands)if(op.producer_distance)actual.push_back(op.producer_distance);
    auto canonical=[](auto& v){std::sort(v.begin(),v.end());v.erase(std::unique(v.begin(),v.end()),v.end());};
    canonical(expected);canonical(actual);
    if(expected!=actual)throw std::runtime_error("static operand dependencies disagree with complete functional RAW set");
}
void StaticUopReader::read(void* p,unsigned n) {
    if(!in_ || gzread(in_,p,n)!=static_cast<int>(n))throw std::runtime_error("truncated static UOP companion");
}
StaticUopReader::StaticUopReader(const std::string& path,std::uint32_t core,std::uint64_t records):records_(records) {
    in_=gzopen(path.c_str(),"rb");if(!in_)throw std::runtime_error("missing static UOP companion: "+path);
    try {
        read(&header_,sizeof(header_));StaticUopHeader expected;
        if(header_.magic!=expected.magic || header_.version!=1 || header_.header_size!=sizeof(header_) ||
           header_.core_id!=core || header_.row_size!=sizeof(StaticUopFactsRow) || header_.reserved ||
           header_.policy_sha256==std::array<std::uint8_t,32>{})
            throw std::runtime_error("invalid/undeclared static UOP source policy");
    } catch(...) {gzclose(in_);in_=nullptr;throw;}
}
StaticUopReader::~StaticUopReader(){if(in_)gzclose(in_);}
void StaticUopReader::observe(std::uint64_t ordinal,const void* record,const std::vector<std::uint32_t>& extra) {
    if(finished_ || ordinal!=observed_ || ordinal>=records_)throw std::logic_error("static UOP source ordinal regressed");
    StaticUopFactsRow row;read(&row,sizeof(row));
    if(row.record_ordinal!=ordinal || row.hot_record_hash!=static_uop_record_hash(record) || row.operand_count>256)
        throw std::runtime_error("static UOP hot-record identity mismatch");
    operands_.resize(row.operand_count);
    if(!operands_.empty())read(operands_.data(),static_cast<unsigned>(operands_.size()*sizeof(StaticUopOperand)));
    validate_static_uop_projection(row,operands_,record,extra);current_=row;
    hash_=static_uop_stream_hash(hash_,record);++observed_;
    if(observed_==records_)finish();
}
void StaticUopReader::finish() {
    if(finished_)return;
    if(observed_!=records_)throw std::runtime_error("static UOP source population incomplete");
    StaticUopFooter footer,expected;read(&footer,sizeof(footer));
    if(footer.magic!=expected.magic || footer.records!=records_ || footer.stream_hash!=hash_ || gzgetc(in_)!=-1 || !gzeof(in_))
        throw std::runtime_error("static UOP full-stream identity/footer mismatch");
    finished_=true;
}
std::string StaticUopReader::policy_identity() const {
    static constexpr char hex[]="0123456789abcdef";std::string result;result.reserve(64);
    for(auto byte:header_.policy_sha256){result.push_back(hex[byte>>4]);result.push_back(hex[byte&15]);}
    return result;
}
} // namespace fastsim::fst
