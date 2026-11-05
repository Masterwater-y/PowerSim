#include "fastsim/fst_core_functional_reader.hpp"
#include "fastsim/types.hpp"
#include "fastsim/fst_dependencies.hpp"
#include "fastsim/fst_static_uop_reader.hpp"
#include <algorithm>
#include <fstream>
namespace fastsim::fst {
namespace {
[[noreturn]] void bad(const char* s){throw std::runtime_error(std::string("core functional companion: ")+s);}
// Reject concatenated gzip members and physical trailing bytes as well as
// decoded trailing payload. Memory is bounded independently of source length.
class Reader {
    std::ifstream file_;z_stream z_{};
    std::array<unsigned char,65536> input_{},output_{};
    std::size_t cursor_=0,end_=0;bool done_=false;
    bool refill(){
        if(cursor_<end_)return true;
        if(done_)return false;
        cursor_=end_=0;
        while(!end_&&!done_){
            if(!z_.avail_in){file_.read(reinterpret_cast<char*>(input_.data()),input_.size());
                if(!file_.gcount())bad("truncated gzip");
                z_.next_in=input_.data();z_.avail_in=static_cast<uInt>(file_.gcount());}
            z_.next_out=output_.data();z_.avail_out=static_cast<uInt>(output_.size());
            int status=inflate(&z_,Z_NO_FLUSH);end_=output_.size()-z_.avail_out;
            if(status==Z_STREAM_END){done_=true;if(z_.avail_in||file_.peek()!=std::char_traits<char>::eof())bad("trailing gzip member/bytes");}
            else if(status!=Z_OK)bad("gzip integrity failure");
        }return end_!=0;
    }
public:
    explicit Reader(const std::string& path):file_(path,std::ios::binary){
        if(!file_)bad("missing sidecar");
        if(inflateInit2(&z_,31)!=Z_OK)bad("gzip initialization");}
    ~Reader(){inflateEnd(&z_);}
    void read(void* p,std::size_t n){auto* out=static_cast<unsigned char*>(p);
        while(n){if(!refill())bad("truncated field/footer");auto take=std::min(n,end_-cursor_);
            std::memcpy(out,output_.data()+cursor_,take);cursor_+=take;out+=take;n-=take;}}
    template<class T>T get(){T value;read(&value,sizeof(value));return value;}
    unsigned char byte(){return get<unsigned char>();}
    mmuctl::Bytes bytes(unsigned n){mmuctl::Bytes b(n);read(b.data(),n);return b;}
    void eof(){if(refill())bad("trailing payload");}
};
CoreFunctionalHeader read_core_header(Reader& input){auto first=input.bytes(96);mmuctl::Cursor c{first};c.p=8;const auto version=c.get(4);if(version==2){auto tail=input.bytes(32);first.insert(first.end(),tail.begin(),tail.end());return core_v2::decode_header(first);}CoreFunctionalHeader h;std::memcpy(&h,first.data(),96);validate_core_header(h,h.core_id);return h;}
CoreFunctionalRow read_core_row(Reader& input,bool v2){if(v2)return core_v2::decode_row(input.bytes(128));return input.get<CoreFunctionalRow>();}
RetiredFrontendMacro read_core_macro(Reader& input,bool v2){if(v2)return core_v2::decode_macro(input.bytes(80));return input.get<RetiredFrontendMacro>();}
void read_core_operands(Reader& input,bool v2,std::vector<CoreFunctionalOperand>& ops){if(v2){for(auto& op:ops)op=core_v2::decode_operand(input.bytes(24));}else if(!ops.empty())input.read(ops.data(),ops.size()*sizeof(CoreFunctionalOperand));}
// Sparse extension cursor retains at most one bounded record's RAW edges.
// RAW equivalence itself remains defined by validate_static_uop_projection.
class Dependencies {
    std::ifstream input_;
    DependencyHeader header_;
    DependencyRow next_;
    std::vector<std::uint32_t> extra_;
    std::uint64_t rows_=0,distances_=0;
    bool pending_=false;
    template<class T> void read(T& value) {
        input_.read(reinterpret_cast<char*>(&value),sizeof(value));
        if(!input_)bad("truncated dependency companion");
    }
    void advance() {
        extra_.clear();pending_=false;
        if(rows_==header_.extension_count) {
            if(distances_!=header_.extra_distance_count || input_.peek()!=std::char_traits<char>::eof())
                bad("dependency population/trailing data mismatch");
            return;
        }
        const auto previous=next_.record_ordinal;read(next_);
        if(next_.record_ordinal>=header_.record_count || (rows_ && next_.record_ordinal<=previous) ||
           !next_.extra_count || next_.extra_count>251 || next_.extra_count>header_.extra_distance_count-distances_)
            bad("invalid dependency extension row");
        extra_.resize(next_.extra_count);
        input_.read(reinterpret_cast<char*>(extra_.data()),static_cast<std::streamsize>(extra_.size()*4));
        if(!input_)bad("truncated dependency edges");
        ++rows_;distances_+=next_.extra_count;pending_=true;
    }
public:
    Dependencies(const std::string& path,std::uint32_t core,std::uint64_t records):input_(path,std::ios::binary) {
        read(header_);const DependencyHeader expected;
        if(header_.magic!=expected.magic || header_.version!=1 || header_.header_size!=48 ||
           header_.core_id!=core || header_.flags || header_.record_count!=records ||
           header_.extension_count>records || header_.extra_distance_count<header_.extension_count)
            bad("dependency header/population mismatch");
        advance();
    }
    std::vector<std::uint32_t> take(std::uint64_t ordinal,const TraceRecord& hot) {
        if(!pending_ || next_.record_ordinal!=ordinal)return {};
        if(next_.hot_record_hash!=dependency_record_hash(&hot))bad("dependency hot-record binding mismatch");
        auto result=std::move(extra_);advance();return result;
    }
    void finish() const {if(pending_)bad("unconsumed dependency extension");}
};
std::string hex_policy(const std::array<std::uint8_t,32>& policy){
    static constexpr char hex[]="0123456789abcdef";std::string result;
    for(auto b:policy){result+=hex[b>>4];result+=hex[b&15];}return result;
}
void add_payload(std::uint64_t& h,unsigned char tag,const void* p,std::size_t n){h=core_stream_hash(h,&tag,1);h=core_stream_hash(h,p,n);}
void read_ffe_header(Reader& reader,std::uint32_t core){
    std::array<unsigned char,32> h{};reader.read(h.data(),h.size());
    const std::array<char,8> magic{'F','S','T','F','F','E','1',0};
    if(std::memcmp(h.data(),magic.data(),8))bad("frontend magic");
    std::uint32_t fields[4];std::memcpy(fields,h.data()+8,16);
    std::uint64_t reserved=0;std::memcpy(&reserved,h.data()+24,8);
    if(fields[0]!=1||fields[1]!=32||fields[2]!=core||fields[3]!=80||reserved)bad("frontend header");
}
void validate_macro(const RetiredFrontendMacro& m,std::uint64_t begin,std::uint64_t count,
                    std::uint64_t pc,std::uint64_t code,std::uint64_t semantic){
    if(m.begin_record!=begin || m.end_record!=count || begin==count || count-begin!=m.observed_uops ||
       m.pc!=pc || m.code_identity!=code || m.semantic_identity!=semantic || m.reserved ||
       m.instruction_size>15 || m.flags&~kFrontendValueFlags || m.known_flags&~kFrontendRequiredKnown || m.flags&~m.known_flags)
        bad("macro identity/span mismatch");
}
}
void validate_core_projection(const CoreFunctionalRow& row,const std::vector<CoreFunctionalOperand>& operands,
                              const void* raw,const StaticUopFactsRow& stat,const std::vector<StaticUopOperand>& source_ops){
    const auto& hot=*static_cast<const fastsim::TraceRecord*>(raw);
    if(row.pc!=hot.pc || !hot.retires() || row.hot_record_hash!=core_record_hash(raw) ||
       row.source_count!=stat.source_count || row.destination_count!=stat.destination_count ||
       source_ops.size()!=row.source_count)bad("hot/static-UOP projection mismatch");
    if(row.known_flags&kCoreStaticKnown){
        if(bool(row.static_flags&kCoreMicroop)!=has_flag(hot.flags,kMicroOp) ||
           bool(row.static_flags&kCoreLastMicroop)!=has_flag(hot.flags,kLastMicroOp) ||
           bool(row.static_flags&kCoreSerializing)!=has_flag(hot.flags,kSerialize))bad("hot static-control mismatch");
    }
    for(unsigned i=0;i<row.source_count;++i)if(operands[i].flags&kCoreOriginalKnown){
        const auto cls=operands[i].original_class;
        // Existing .uops uses Int/Float/Vec/CC -> 0/1/2/3, all others 255.
        const unsigned projected=cls==0?0:cls==1?1:cls==2?2:cls==6?3:255;
        if(source_ops[i].reg_class!=projected)bad("ordered source class projection mismatch");
    }
}
struct CoreFunctionalReader::Impl {
    CoreFunctionalHeader header;CoreFunctionalCoverage coverage;
    std::unique_ptr<Reader> reader;std::optional<CoreFunctionalRow> current;
    std::vector<CoreFunctionalOperand> operands;std::uint64_t next=0;
};
CoreFunctionalReader::~CoreFunctionalReader()=default;
CoreFunctionalReader::CoreFunctionalReader(const std::string& path,std::uint32_t core,std::uint64_t records,const std::string& policy,bool architectural_bundle):impl_(std::make_unique<Impl>()){
    Reader input(path+".core.gz"),ffe(path+".ffe.gz"),uops(path+".uops.gz");
    auto& header=impl_->header;header=read_core_header(input);const bool v2=header.version==2;
    if(header.core_id!=core || v2!=architectural_bundle)bad("core version/bundle mismatch");
    if(hex_policy(header.policy_sha256)!=policy)bad("source policy mismatch");
    read_ffe_header(ffe,core);
    const auto uh=uops.get<StaticUopHeader>();const StaticUopHeader expected_uh;
    if(uh.magic!=expected_uh.magic||uh.version!=1||uh.header_size!=64||uh.core_id!=core||uh.row_size!=64||uh.reserved||uh.policy_sha256!=header.policy_sha256)bad("static-UOP header/policy mismatch");
    Dependencies dependencies(path+".deps",core,records);
    std::ifstream hot(path,std::ios::binary);hot.seekg(72);
    std::uint64_t count=0,macro_begin=0,macros=0,pc=0,code=0,semantic=0;
    RetiredFrontendMacro native_macro;
    std::uint64_t payload=v2?core_v2::payload_seed():kCoreHashSeed,hash=kCoreHashSeed;
    for(;;){
        const auto tag=input.byte();
        if(tag==255){if(v2){const auto f=core_v2::decode_footer(input.bytes(64));const auto& c=impl_->coverage;if(f.records!=records || count!=records || macro_begin!=count || f.macros!=macros || f.hot_hash!=hash || f.payload_hash!=payload || f.na_rom_rows!=c.na_rom_rows || f.boundary_rows!=c.boundary_rows || f.unknown_applicable_rows!=c.unknown_applicable_rows)bad("core-v2 footer/population/hash mismatch");input.eof();break;}
            const auto f=input.get<CoreFunctionalFooter>();const CoreFunctionalFooter expected;
            if(f.magic!=expected.magic||f.records!=records||count!=records||macro_begin!=count||f.macros!=macros||f.stream_hash!=hash||f.payload_hash!=payload)bad("footer/population/hash mismatch");
            input.eof();break;}
        if(tag==2){const auto m=read_core_macro(input,v2);add_payload(payload,tag,&m,sizeof(m));
            validate_macro(m,macro_begin,count,pc,code,semantic);
            if(std::memcmp(&native_macro,&m,sizeof(m)))bad("independent frontend macro binding mismatch");
            macro_begin=count;++macros;continue;}
        if(tag!=1 || count>=records)bad("invalid tag/extra row");
        const auto row=read_core_row(input,v2);add_payload(payload,tag,&row,sizeof(row));
        if(count==macro_begin) {
            if(ffe.byte()!=1)bad("frontend missing macro binding");
            native_macro=ffe.get<RetiredFrontendMacro>();
        }
        if(row.record_ordinal!=count || row.macro_begin!=macro_begin || row.source_count>256 || row.destination_count>256 || row.operand_count!=std::uint32_t(row.source_count)+row.destination_count)bad("row identity/operand bound mismatch");
        std::vector<CoreFunctionalOperand> operands(row.operand_count);read_core_operands(input,v2,operands);
        payload=core_stream_hash(payload,operands.data(),operands.size()*sizeof(CoreFunctionalOperand));if(v2)core_v2::validate_row(header,row,operands);else validate_core_row(header,row,operands);
        if(native_macro.begin_record!=macro_begin || native_macro.end_record<=count || native_macro.end_record>records ||
           row.pc!=native_macro.pc || row.code_identity!=native_macro.code_identity)bad("full-source macro row binding mismatch");
        if(row.known_flags&kCoreDecoderKnown){
            if(native_macro.flags&kFrontendPseudo || native_macro.instruction_size==0 || native_macro.instruction_size>15)bad("invalid known decoder origin");
            auto decoded=core_stream_hash(kCoreHashSeed,&row.decoder_context,8);
            decoded=core_stream_hash(decoded,native_macro.bytes.data(),native_macro.instruction_size);
            if(decoded!=native_macro.code_identity)bad("full-source decoder byte witness mismatch");
        }
        if((row.known_flags&kCoreAllocationKnown) &&
           bool(row.root_allocation_flags)!=bool(native_macro.flags&kFrontendSharedAllocation))bad("root allocation projection mismatch");
        TraceRecord record;hot.read(reinterpret_cast<char*>(&record),sizeof(record));if(!hot)bad("truncated hot stream");
        const auto stat=uops.get<StaticUopFactsRow>();
        if(stat.operand_count>256 || stat.record_ordinal!=count || stat.hot_record_hash!=core_record_hash(&record))bad("static-UOP row binding mismatch");
        std::vector<StaticUopOperand> source_ops(stat.operand_count);if(!source_ops.empty())uops.read(source_ops.data(),source_ops.size()*sizeof(StaticUopOperand));
        const auto extra=dependencies.take(count,record);
        validate_static_uop_projection(stat,source_ops,&record,extra);
        validate_core_projection(row,operands,&record,stat,source_ops);
        if(count==macro_begin){pc=row.pc;code=row.code_identity;}else if(pc!=row.pc||code!=row.code_identity)bad("macro row identity changed");
        semantic=row.semantic_prefix_identity;hash=core_stream_hash(hash,&record,64);++count;
        auto& coverage=impl_->coverage;++coverage.records;
        if(row.known_flags&kCoreFrontendKnown)coverage.frontend_retirement_mismatch+=row.frontend_asid!=native_macro.address_space_id;
        const auto extension=v2?core_v2::extension(row):core_v2::Extension{};
        for(unsigned i=0;i<8;++i){coverage.known[i]+=bool(row.known_flags&(1u<<i));coverage.not_applicable[i]+=bool(extension.not_applicable_flags&(1u<<i));coverage.unknown[i]+=!((row.known_flags|extension.not_applicable_flags)&(1u<<i));}
        coverage.na_rom_rows+=bool(extension.not_applicable_flags);coverage.boundary_rows+=bool(extension.extension_known&4);coverage.unknown_applicable_rows+=v2?core_v2::unknown_applicable(row):row.known_flags!=255;
        if(extension.not_applicable_flags && ((native_macro.flags&(kFrontendPseudo|kFrontendMicrocode))!=(kFrontendPseudo|kFrontendMicrocode) || native_macro.instruction_size || native_macro.bytes!=std::array<std::uint8_t,15>{} || (native_macro.flags&kFrontendDsbEligible)))bad("core-v2 N/A frontend contradiction");
        ++coverage.normalization.at(unsigned(row.normalization_status));
        for(unsigned i=0;i<operands.size();++i)if(operands[i].flags&kCoreOriginalKnown){
            auto& classes=i<row.source_count?coverage.source_classes:coverage.destination_classes;
            ++classes.at(operands[i].original_class==255?8:operands[i].original_class);}
        coverage.gpr_candidates+=bool(row.static_flags&kCoreFullGprMove);coverage.xmm_candidates+=bool(row.static_flags&kCoreFullXmmMove);
        coverage.microfused+=bool(row.root_allocation_flags&kCoreMicrofusedMemory);coverage.stack+=bool(row.root_allocation_flags&kCoreStackAllocation);
        coverage.serialize_before+=bool(row.static_flags&kCoreSerializeBefore);coverage.serialize_after+=bool(row.static_flags&kCoreSerializeAfter);
        coverage.read_barriers+=bool(row.static_flags&kCoreReadBarrier);coverage.write_barriers+=bool(row.static_flags&kCoreWriteBarrier);
        if((row.known_flags&(kCoreDecoderKnown|kCoreFrontendKnown))==(kCoreDecoderKnown|kCoreFrontendKnown))
            coverage.decoder_frontend_user_mismatch+=bool(((row.decoder_context>>4)&3)==3)!=bool(row.frontend_from_user);
    }
    if(ffe.byte()!=255 || ffe.get<std::uint64_t>()!=records || ffe.get<std::uint64_t>()!=macros || ffe.get<std::uint64_t>()!=hash)bad("frontend footer mismatch");
    ffe.eof();
    const auto uf=uops.get<StaticUopFooter>();const StaticUopFooter expected_uf;
    if(uf.magic!=expected_uf.magic||uf.records!=records||uf.stream_hash!=hash)bad("static-UOP footer mismatch");
    uops.eof();dependencies.finish();
    impl_->coverage.macros=macros;impl_->reader=std::make_unique<Reader>(path+".core.gz");
    const auto second=read_core_header(*impl_->reader);if(std::memcmp(&second,&header,sizeof(header)))bad("source changed after validation");
}
void CoreFunctionalReader::observe(std::uint64_t ordinal,const void* hot,const RetiredFrontendMacro& macro,
                                  const StaticUopFactsRow& stat,const std::vector<StaticUopOperand>& source_ops){
    auto& p=*impl_;const bool v2=p.header.version==2;clear_current();if(ordinal!=p.next||ordinal>=p.coverage.records)bad("source ordinal mismatch");
    auto tag=p.reader->byte();if(tag==2){read_core_macro(*p.reader,v2);tag=p.reader->byte();}
    if(tag!=1)bad("missing replay row");
    const auto row=read_core_row(*p.reader,v2);
    if(row.operand_count>512||row.source_count>256||row.destination_count>256)bad("replay operand bound");
    p.operands.resize(row.operand_count);read_core_operands(*p.reader,v2,p.operands);
    if(v2)core_v2::validate_row(p.header,row,p.operands);else validate_core_row(p.header,row,p.operands);validate_core_projection(row,p.operands,hot,stat,source_ops);
    if(row.record_ordinal!=ordinal||row.macro_begin!=macro.begin_record||row.pc!=macro.pc||row.code_identity!=macro.code_identity)bad("replay macro binding mismatch");
    if(ordinal+1==macro.end_record && row.semantic_prefix_identity!=macro.semantic_identity)bad("replay semantic binding mismatch");
    if(row.known_flags&kCoreDecoderKnown){
        auto code=core_stream_hash(kCoreHashSeed,&row.decoder_context,8);code=core_stream_hash(code,macro.bytes.data(),macro.instruction_size);
        if(code!=macro.code_identity)bad("decoder byte witness mismatch");}
    p.current=row;++p.next;
}
void CoreFunctionalReader::clear_current(){impl_->current.reset();impl_->operands.clear();}
const CoreFunctionalRow* CoreFunctionalReader::current()const{return impl_->current?&*impl_->current:nullptr;}
const std::vector<CoreFunctionalOperand>& CoreFunctionalReader::operands()const{return impl_->operands;}
const CoreFunctionalHeader& CoreFunctionalReader::header()const{return impl_->header;}
const CoreFunctionalCoverage& CoreFunctionalReader::coverage()const{return impl_->coverage;}
std::string CoreFunctionalReader::policy_identity()const{return hex_policy(impl_->header.policy_sha256);}
} // namespace fastsim::fst
