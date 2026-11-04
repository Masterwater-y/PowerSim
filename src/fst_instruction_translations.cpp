#include "fastsim/fst_instruction_translations.hpp"
#include "fastsim/types.hpp"
#include <fstream>
#include <sstream>
#include <iomanip>
namespace fastsim::fst {
namespace {
[[noreturn]] void bad(const char* reason) { throw std::runtime_error(std::string("instruction translation companion: ")+reason); }
class Reader {
  std::ifstream input_;
  z_stream z_{};
  std::array<unsigned char, 65536> input_buffer_{}, output_{};
  std::size_t cursor_ = 0, end_ = 0;
  bool done_ = false;

public:
  explicit Reader(const std::string &p) : input_(p, std::ios::binary) {
    if (!input_)
      bad("missing .ixlat.gz or .ffe.gz");
    if (inflateInit2(&z_, 31) != Z_OK)
      bad("gzip initialization failed");
  }
  ~Reader() { inflateEnd(&z_); }
  bool refill() {
    if (cursor_ < end_)
      return true;
    if (done_)
      return false;
    cursor_ = end_ = 0;
    while (!end_ && !done_) {
      if (!z_.avail_in) {
        input_.read(reinterpret_cast<char *>(input_buffer_.data()),
                    input_buffer_.size());
        auto count = input_.gcount();
        if (!count)
          bad("truncated gzip/CRC/footer");
        z_.next_in = input_buffer_.data();
        z_.avail_in = static_cast<uInt>(count);
      }
      z_.next_out = output_.data();
      z_.avail_out = static_cast<uInt>(output_.size());
      int status = inflate(&z_, Z_NO_FLUSH);
      end_ = output_.size() - z_.avail_out;
      if (status == Z_STREAM_END) {
        done_ = true;
        if (z_.avail_in || input_.peek() != std::char_traits<char>::eof())
          bad("trailing gzip member/bytes");
      } else if (status != Z_OK)
        bad("invalid gzip/CRC");
    }
    return end_ != 0;
  }
  unsigned char byte() {
    if (!refill())
      bad("truncated field/footer");
    return output_[cursor_++];
  }
  std::uint64_t le(unsigned n) {
    std::uint64_t v = 0;
    for (unsigned i = 0; i < n; i++)
      v |= std::uint64_t(byte()) << (8 * i);
    return v;
  }
  std::uint64_t uv() {
    std::uint64_t v = 0;
    for (unsigned i = 0; i < 10; i++) {
      auto b = byte();
      if (i == 9 && b > 1)
        bad("ULEB128 overflow");
      v |= std::uint64_t(b & 127) << (i * 7);
      if (!(b & 128))
        return v;
    }
    bad("ULEB128 overflow");
  }
  void eof() {
    if (refill())
      bad("trailing decompressed bytes");
  }
};
void bytes(Reader& r,void* target,unsigned count) {
    auto* out=static_cast<unsigned char*>(target);
    for(unsigned i=0;i<count;++i)out[i]=r.byte();
}
unsigned header(Reader& r,std::uint32_t core,const std::string& policy) {
    const char magic[6]={'F','S','T','I','X','L'};
    for(char c:magic)if(r.byte()!=static_cast<unsigned char>(c))bad("magic/version");
    const unsigned version=r.byte()-'0';
    if((version!=1 && version!=2) || r.byte()!=0)bad("unsupported version");
    if(r.le(4)!=version || r.le(4)!=kInstructionTranslationHeaderSize || r.le(4)!=core || r.le(4))bad("header/core/reserved");
    std::ostringstream identity;identity<<std::hex<<std::setfill('0');
    bool present=false;
    for(unsigned i=0;i<32;++i){auto b=r.byte();present|=b!=0;identity<<std::setw(2)<<unsigned(b);}
    if(!present || identity.str()!=policy || r.le(8))bad("source policy identity mismatch");
    return version;
}
void frontend_header(Reader& r,std::uint32_t core) {
    const char magic[8]={'F','S','T','F','F','E','1',0};
    for(char c:magic)if(r.byte()!=static_cast<unsigned char>(c))bad("frontend magic");
    if(r.le(4)!=1 || r.le(4)!=32 || r.le(4)!=core || r.le(4)!=80 || r.le(8))bad("frontend header");
}
TranslationFragment fragment(Reader& r) {
    TranslationFragment f;
    f.virtual_address=r.le(8);f.physical_address=r.le(8);f.size=static_cast<std::uint32_t>(r.le(4));
    auto& m=f.mapping;
    m.request_physical_base=r.le(8);m.request_virtual_base=r.le(8);m.address_space_id=r.le(8);
    m.walk_root=r.le(8);m.virtual_base=r.le(8);m.physical_base=r.le(8);
    for(auto& address:m.pte_addresses)address=r.le(8);
    m.pcid=static_cast<std::uint16_t>(r.le(2));m.page_bits=r.byte();m.levels=r.byte();m.flags=r.byte();
    m.kind=static_cast<TranslationKind>(r.byte());if(r.le(2))bad("fragment reserved");
    return f;
}
std::shared_ptr<const InstructionFetchFacts> row(Reader& r,RetiredFrontendMacro& macro,unsigned version) {
    bytes(r,&macro,sizeof(macro));
    auto facts=std::make_shared<InstructionFetchFacts>();facts->pc=macro.pc;facts->bytes=macro.instruction_size;
    facts->status=static_cast<InstructionFetchStatus>(r.byte());facts->reason=static_cast<InstructionFetchUnknown>(r.byte());
    const auto complete=r.byte(),count=r.byte();if(complete>1 || count>2)bad("flags/fragment bound");
    if(version==1) {if(r.le(4))bad("v1 reserved context fields");}
    else {
        const auto known=r.byte();if(known>1 || r.le(3))bad("decode context known/reserved fields");
        facts->decode_context_known=known;facts->decode_address_space_id=r.le(8);
    }
    facts->path_complete=complete;
    for(unsigned i=0;i<count;++i)facts->fragments.push_back(fragment(r));
    validate_instruction_binding(macro,*facts,version);
    return facts;
}
}
struct InstructionTranslations::Impl {
    std::unique_ptr<Reader> reader;
    RetiredFrontendMacro macro;
    std::shared_ptr<const InstructionFetchFacts> current;
    std::uint64_t next=0;
    unsigned version=0;
};
InstructionTranslations::~InstructionTranslations()=default;
unsigned InstructionTranslations::version() const {return impl_->version;}
InstructionTranslations::InstructionTranslations(const std::string& path,std::uint32_t core,
    std::uint64_t records,const std::string& policy,
    const std::function<std::uint64_t(std::uint64_t)>& asid):impl_(std::make_unique<Impl>()) {
    Reader r(path+".ixlat.gz"),frontend(path+".ffe.gz");impl_->version=header(r,core,policy);frontend_header(frontend,core);
    std::ifstream hot(path,std::ios::binary);hot.seekg(72);
    std::uint64_t count=0,rows=0,hot_hash=kFrontendHashSeed,ffe_hash=kFrontendHashSeed;
    for(;;) {
        const auto tag=r.byte();
        if(tag==255) {
            if(r.le(8)!=records || r.le(8)!=rows || r.le(8)!=hot_hash || r.le(8)!=ffe_hash || count!=records)
                bad("footer/population/hot/frontend hash mismatch");
            if(frontend.byte()!=255 || frontend.le(8)!=records || frontend.le(8)!=rows || frontend.le(8)!=hot_hash)
                bad("frontend footer mismatch");
            r.eof();frontend.eof();break;
        }
        if(tag!=1 || frontend.byte()!=1)bad("missing or extra macro span");
        RetiredFrontendMacro macro,expected;auto facts=row(r,macro,impl_->version);bytes(frontend,&expected,sizeof(expected));
        if(std::memcmp(&macro,&expected,sizeof(macro)) || macro.begin_record!=count || macro.end_record>records)
            bad("frontend macro/span/code identity mismatch");
        ffe_hash=frontend_hash(ffe_hash,&macro,sizeof(macro));
        for(;count<macro.end_record;++count) {
            TraceRecord raw;
            if(!hot.read(reinterpret_cast<char*>(&raw),sizeof(raw)))bad("truncated hot stream");
            hot_hash=frontend_hash(hot_hash,&raw,sizeof(raw));
            if(raw.pc!=macro.pc || asid(count)!=macro.address_space_id || !raw.retires())bad("raw PC/ASID/retirement mismatch");
        }
        ++rows;
    }
    impl_->reader=std::make_unique<Reader>(path+".ixlat.gz");
    if(header(*impl_->reader,core,policy)!=impl_->version)bad("source version changed during validation");
}
std::shared_ptr<const InstructionFetchFacts>
InstructionTranslations::resolve(std::uint64_t ordinal,const RetiredFrontendMacro& macro) {
    auto& p=*impl_;
    if(ordinal!=p.next++)bad("nonsequential source ordinal");
    if(!p.current || ordinal==p.macro.end_record) {
        if(p.reader->byte()!=1)bad("missing current macro binding");
        p.current=row(*p.reader,p.macro,p.version);
    }
    if(ordinal<p.macro.begin_record || ordinal>=p.macro.end_record ||
       std::memcmp(&macro,&p.macro,sizeof(macro)))bad("current frontend binding mismatch");
    return p.current;
}
}
