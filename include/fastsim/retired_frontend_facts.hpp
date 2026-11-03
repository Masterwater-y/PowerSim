#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <stdexcept>
#include <zlib.h>

namespace fastsim::fst {
inline constexpr std::uint64_t kRetiredFrontendFacts = 1ull << 11;
enum RetiredFrontendFlag : std::uint32_t {
    kFrontendSharedAllocation = 1u << 0,
    kFrontendMicrocode = 1u << 1,
    kFrontendPseudo = 1u << 2,
    kFrontendComplete = 1u << 3,
    kFrontendUnconditionalBranch = 1u << 4,
    kFrontendCodeIdentity = 1u << 5,
    kFrontendInstructionBytes = 1u << 6,
    kFrontendSemanticIdentity = 1u << 7,
    // The actual architectural leader is present in this observed span.
    // Descriptor creation alone never establishes this fact.
    kFrontendStartsMacro = 1u << 8,
    // Decoded control placement permits a reusable expansion. This is not
    // a native DSB hit, insertion, capacity, or source-choice observation.
    kFrontendDsbEligible = 1u << 9,
};
inline constexpr std::uint32_t kFrontendRequiredKnown = 1023;
inline constexpr std::uint32_t kFrontendValueFlags = 31u | kFrontendStartsMacro | kFrontendDsbEligible;
// Fixed little-endian row. One row describes an observed consecutive retired
// macro span; Complete=false explicitly describes an abandoned/truncated prefix.
// Code identity is decoder context plus exact code bytes. Semantic identity is
// the ordered static semantics of the observed functional UOPs. Neither is a
// native frontend source, hit result, or timestamp.
struct RetiredFrontendMacro {
    std::uint64_t begin_record=0, end_record=0, pc=0, address_space_id=0;
    std::uint64_t code_identity=0, semantic_identity=0;
    std::uint32_t flags=0, known_flags=0;
    std::uint8_t instruction_size=0;
    std::array<std::uint8_t,15> bytes{};
    std::uint32_t observed_uops=0, reserved=0;
};
static_assert(sizeof(RetiredFrontendMacro)==80);
inline std::uint64_t frontend_hash(std::uint64_t h, const void* p, std::size_t n) {
    const auto* b=static_cast<const unsigned char*>(p);
    for(std::size_t i=0;i<n;++i) h=(h^b[i])*1099511628211ull;
    return h;
}
inline constexpr std::uint64_t kFrontendHashSeed=14695981039346656037ull;
// Shared with the native producer. Rows and hot records are observed separately
// so a macro can finish only after several retired UOP records have been written.
class RetiredFrontendWriter {
    gzFile out_=nullptr;
    std::uint64_t rows_=0, end_=0, records_=0, hash_=kFrontendHashSeed;
    void write(const void* p, unsigned n) {
        if(!out_ || gzwrite(out_,p,n)!=static_cast<int>(n))
            throw std::runtime_error("retired frontend companion write failed");
    }
public:
    RetiredFrontendWriter(const std::string& path, std::uint32_t core) {
        out_=gzopen(path.c_str(),"wb1");
        if(!out_) throw std::runtime_error("cannot create retired frontend companion");
        const std::array<char,8> magic={'F','S','T','F','F','E','1',0};
        write(magic.data(),8);
        const std::uint32_t header[]={1,32,core,80}; write(header,16);
        const std::uint64_t zero=0; write(&zero,8);
    }
    ~RetiredFrontendWriter() { if(out_) gzclose(out_); }
    void observe_record(const void* record) { hash_=frontend_hash(hash_,record,64); ++records_; }
    void append(const RetiredFrontendMacro& m) {
        if(m.begin_record!=end_ || m.end_record<=m.begin_record ||
           m.end_record-m.begin_record!=m.observed_uops || m.reserved ||
           m.instruction_size>15 || (m.flags & ~kFrontendValueFlags) || (m.flags & ~m.known_flags) ||
           (m.known_flags & ~kFrontendRequiredKnown))
            throw std::runtime_error("invalid retired frontend macro span/facts");
        const unsigned char tag=1; write(&tag,1); write(&m,sizeof(m));
        end_=m.end_record; ++rows_;
    }
    void finish(std::uint64_t records) {
        if(!out_) return;
        if(records!=records_ || end_!=records) throw std::runtime_error("incomplete retired frontend coverage");
        const unsigned char tag=255; write(&tag,1);
        const std::uint64_t footer[]={records,rows_,hash_}; write(footer,sizeof(footer));
        const auto out=out_; out_=nullptr;
        if(gzclose(out)!=Z_OK) throw std::runtime_error("retired frontend gzip close failed");
    }
};
class RetiredFrontendFacts {
public:
    RetiredFrontendFacts(const std::string& hot_path, std::uint32_t core,
                         std::uint64_t records,
                         const std::function<std::uint64_t(std::uint64_t)>& asid);
    ~RetiredFrontendFacts();
    const RetiredFrontendMacro* resolve(std::uint64_t ordinal, const void* raw_record);
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
}
