#include "fastsim/retired_frontend_facts.hpp"
#include "fastsim/types.hpp"
#include <fstream>
#include <algorithm>
#include <limits>
namespace fastsim::fst {
namespace {
[[noreturn]] void bad(const char* reason) { throw std::runtime_error(std::string("retired frontend companion: ")+reason); }
class Reader {
  std::ifstream input_;
  z_stream z_{};
  std::array<unsigned char, 65536> input_buffer_{}, output_{};
  std::size_t cursor_ = 0, end_ = 0;
  bool done_ = false;

public:
  explicit Reader(const std::string &p) : input_(p, std::ios::binary) {
    if (!input_)
      bad("missing .ffe.gz");
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
void header(Reader& r, std::uint32_t core) {
    const char magic[8]={'F','S','T','F','F','E','1',0};
    for(char c:magic) if(r.byte()!=static_cast<unsigned char>(c)) bad("invalid magic");
    if(r.le(4)!=1 || r.le(4)!=32 || r.le(4)!=core || r.le(4)!=80 || r.le(8)) bad("invalid header/core/version");
}
RetiredFrontendMacro row(Reader& r) {
    RetiredFrontendMacro m;
    m.begin_record=r.le(8); m.end_record=r.le(8); m.pc=r.le(8); m.address_space_id=r.le(8);
    m.code_identity=r.le(8); m.semantic_identity=r.le(8);
    m.flags=static_cast<std::uint32_t>(r.le(4)); m.known_flags=static_cast<std::uint32_t>(r.le(4));
    m.instruction_size=r.byte(); for(auto& b:m.bytes) b=r.byte();
    m.observed_uops=static_cast<std::uint32_t>(r.le(4)); m.reserved=static_cast<std::uint32_t>(r.le(4));
    if(m.reserved || m.instruction_size>15 || (m.flags&~kFrontendValueFlags) || (m.flags&~m.known_flags) ||
       (m.known_flags&~kFrontendRequiredKnown) || m.end_record<=m.begin_record ||
       m.end_record-m.begin_record!=m.observed_uops) bad("invalid macro flags/span");
    if((m.known_flags&kFrontendInstructionBytes) &&
       std::any_of(m.bytes.begin()+m.instruction_size,m.bytes.end(),[](auto b){return b!=0;})) bad("noncanonical unused bytes");
    if((m.known_flags&kFrontendInstructionBytes) && !m.instruction_size &&
       !((m.known_flags&kFrontendPseudo) && (m.flags&kFrontendPseudo))) bad("ordinary macro has no bytes");
    if((m.known_flags&kFrontendCodeIdentity) && !m.code_identity) bad("missing known code identity");
    if((m.known_flags&kFrontendSemanticIdentity) && !m.semantic_identity) bad("missing known semantic identity");
    return m;
}
}
struct RetiredFrontendFacts::Impl {
    std::unique_ptr<Reader> reader;
    RetiredFrontendMacro current{};
    bool valid=false;
    std::uint64_t next=0;
};
RetiredFrontendFacts::~RetiredFrontendFacts()=default;
RetiredFrontendFacts::RetiredFrontendFacts(const std::string& path, std::uint32_t core,
    std::uint64_t records, const std::function<std::uint64_t(std::uint64_t)>& asid):impl_(std::make_unique<Impl>()) {
    Reader r(path+".ffe.gz"); header(r,core);
    std::ifstream hot(path,std::ios::binary); hot.seekg(72);
    std::uint64_t count=0, rows=0, hash=kFrontendHashSeed;
    for(;;) {
        auto tag=r.byte();
        if(tag==255) {
            if(r.le(8)!=records || r.le(8)!=rows || r.le(8)!=hash || count!=records) bad("footer/hot-stream identity mismatch");
            r.eof(); break;
        }
        if(tag!=1) bad("invalid tag/missing footer");
        auto m=row(r);
        if(m.begin_record!=count || m.end_record>records) bad("missing/overlapping macro coverage");
        for(;count<m.end_record;++count) {
            TraceRecord rec;
            if(!hot.read(reinterpret_cast<char*>(&rec),sizeof(rec))) bad("truncated hot stream");
            hash=frontend_hash(hash,&rec,sizeof(rec));
            if(rec.pc!=m.pc || asid(count)!=m.address_space_id) bad("macro PC/ASID disagrees with raw stream");
            const bool last=count+1==m.end_record;
            if(!rec.retires()) bad("nonretired functional record");
            const bool complete=!has_flag(rec.flags,kMicroOp) || has_flag(rec.flags,kLastMicroOp);
            if(complete && !last) bad("macro span crosses completed macro");
            if((m.flags&kFrontendComplete) && last && !complete) bad("macro completion disagrees with retired records");
        }
        ++rows;
    }
    impl_->reader=std::make_unique<Reader>(path+".ffe.gz"); header(*impl_->reader,core);
}
const RetiredFrontendMacro* RetiredFrontendFacts::resolve(std::uint64_t ordinal,const void* raw) {
    auto& p=*impl_;
    if(ordinal!=p.next++) bad("nonsequential source ordinal");
    if(!p.valid || ordinal==p.current.end_record) {
        if(p.reader->byte()!=1) bad("missing current macro descriptor");
        p.current=row(*p.reader); p.valid=true;
    }
    if(ordinal<p.current.begin_record || ordinal>=p.current.end_record ||
       static_cast<const TraceRecord*>(raw)->pc!=p.current.pc) bad("current macro/raw ordinal mismatch");
    return &p.current;
}
}
