#pragma once
#include "fastsim/fst_instruction_fetch.hpp"
#include "fastsim/retired_frontend_facts.hpp"
#include <cstring>
#include <functional>
#include <string>
#include <zlib.h>

namespace fastsim::fst {
inline constexpr std::uint64_t kInstructionTranslations=1ull<<15;
inline constexpr unsigned kInstructionTranslationHeaderSize=64;
inline bool same_instruction_fetch(const InstructionFetchFacts& a,const InstructionFetchFacts& b) {
    if(a.pc!=b.pc || a.bytes!=b.bytes || a.status!=b.status || a.reason!=b.reason ||
       a.path_complete!=b.path_complete || a.fragments.size()!=b.fragments.size() ||
       a.decode_context_known!=b.decode_context_known || a.decode_address_space_id!=b.decode_address_space_id) return false;
    for(std::size_t i=0;i<a.fragments.size();++i) {
        const auto& x=a.fragments[i];const auto& y=b.fragments[i];
        if(x.virtual_address!=y.virtual_address || x.physical_address!=y.physical_address ||
           x.size!=y.size || x.mapping.identity()!=y.mapping.identity())return false;
    }
    return true;
}
inline bool instruction_origin_decode_asid_mismatch(const InstructionFetchFacts& f) {
    return f.decode_context_known && std::any_of(f.fragments.begin(),f.fragments.end(),[&](const auto& piece) {
        return piece.mapping.address_space_id!=f.decode_address_space_id;
    });
}
inline bool instruction_origin_retirement_asid_mismatch(const InstructionFetchFacts& f,const RetiredFrontendMacro& m) {
    return std::any_of(f.fragments.begin(),f.fragments.end(),[&](const auto& piece) {
        return piece.mapping.address_space_id!=m.address_space_id;
    });
}
inline void validate_instruction_binding(const RetiredFrontendMacro& m,const InstructionFetchFacts& f,unsigned version=2) {
    auto bad=[](){throw std::runtime_error("invalid instruction translation macro binding");};
    if(m.end_record<=m.begin_record || m.end_record-m.begin_record!=m.observed_uops ||
       m.reserved || m.instruction_size>15 || f.pc!=m.pc || f.bytes!=m.instruction_size ||
       (m.flags&~kFrontendValueFlags) || (m.flags&~m.known_flags) ||
       (m.known_flags&~kFrontendRequiredKnown) || f.fragments.size()>2)bad();
    if((version!=1 && version!=2) || (!f.decode_context_known && f.decode_address_space_id) ||
       (version==1 && f.decode_context_known))bad();
    const bool pseudo=(m.known_flags&kFrontendPseudo) && (m.flags&kFrontendPseudo);
    if(f.status==InstructionFetchStatus::NotApplicable) {
        if(!pseudo || f.bytes || f.reason!=InstructionFetchUnknown::None ||
           !f.fragments.empty() || f.path_complete || f.decode_context_known)bad();
    } else if(f.status==InstructionFetchStatus::Unknown) {
        if(f.reason==InstructionFetchUnknown::None ||
           unsigned(f.reason)>unsigned(InstructionFetchUnknown::InconsistentGeometry) ||
           !f.fragments.empty() || f.path_complete)bad();
    } else if(f.status==InstructionFetchStatus::Mapped) {
        if(pseudo || !f.bytes || f.reason!=InstructionFetchUnknown::None || f.fragments.empty())bad();
        if(version==2 && !f.decode_context_known)bad();
        InstructionByteWitness witness;
        if(version==1)witness.begin(m.pc,m.address_space_id);
        else witness.begin_observed(m.pc,f.decode_address_space_id);
        std::uint64_t next=m.pc;
        for(const auto& fragment:f.fragments) {
            if(fragment.virtual_address!=next || !fragment.size ||
               fragment.size>f.bytes || next>UINT64_MAX-fragment.size)bad();
            const auto& mapping=fragment.mapping;
            if(mapping.kind!=TranslationKind::kPaged || mapping.request_physical_base ||
               mapping.request_virtual_base || mapping.pcid>4095)bad();
            for(unsigned i=mapping.levels;i<mapping.pte_addresses.size();++i)
                if(mapping.pte_addresses[i])bad();
            if(!(mapping.flags&kTranslationPathKnown) && mapping.levels)bad();
            witness.observe(fragment);next+=fragment.size;
        }
        if(next-m.pc!=f.bytes || !same_instruction_fetch(*witness.snapshot(f.bytes),f))bad();
    } else bad();
}
// Versions 1/2: explicit LE header, rows, fragments and footer. V2 preserves
// decoder/selection ownership ASID separately from Execute-origin fragment ASID
// and the unchanged .ffe retirement ASID; it never relabels a historical path.
// Every row binds the exact 80-byte .ffe descriptor. No source/hit/time fields.
class InstructionTranslationWriter {
    gzFile out_=nullptr;
    unsigned version_=2;
    std::uint64_t rows_=0,end_=0,records_=0,hot_hash_=kFrontendHashSeed,frontend_hash_=kFrontendHashSeed;
    void write(const void* p,unsigned n) {
        if(!out_ || gzwrite(out_,p,n)!=int(n))throw std::runtime_error("instruction translation write failed");
    }
    void le(std::uint64_t value,unsigned bytes) {
        unsigned char encoded[8]{};
        for(unsigned i=0;i<bytes;++i)encoded[i]=static_cast<unsigned char>(value>>(i*8));
        write(encoded,bytes);
    }
    void fragment(const TranslationFragment& f) {
        le(f.virtual_address,8);le(f.physical_address,8);le(f.size,4);
        const auto& m=f.mapping;
        le(m.request_physical_base,8);le(m.request_virtual_base,8);le(m.address_space_id,8);
        le(m.walk_root,8);le(m.virtual_base,8);le(m.physical_base,8);
        for(const auto address:m.pte_addresses)le(address,8);
        le(m.pcid,2);le(m.page_bits,1);le(m.levels,1);le(m.flags,1);le(unsigned(m.kind),1);le(0,2);
    }
public:
    InstructionTranslationWriter(const std::string& path,std::uint32_t core,const std::array<std::uint8_t,32>& policy,unsigned version=2):version_(version) {
        if(version!=1 && version!=2)throw std::invalid_argument("unsupported instruction translation output version");
        if(policy==std::array<std::uint8_t,32>{})throw std::invalid_argument("instruction translation policy undeclared");
        out_=gzopen(path.c_str(),"wb1");if(!out_)throw std::runtime_error("cannot create instruction translation companion");
        const char magic[8]={'F','S','T','I','X','L',char('0'+version_),0};write(magic,8);
        le(version_,4);le(kInstructionTranslationHeaderSize,4);le(core,4);le(0,4);
        write(policy.data(),32);le(0,8);
    }
    ~InstructionTranslationWriter(){if(out_)gzclose(out_);}
    void observe_record(const void* raw){hot_hash_=frontend_hash(hot_hash_,raw,64);++records_;}
    void append(const RetiredFrontendMacro& m,const InstructionFetchFacts& f) {
        validate_instruction_binding(m,f,version_);
        if(m.begin_record!=end_)throw std::runtime_error("instruction translation missing/duplicate span");
        le(1,1);
        // RetiredFrontendMacro is the pre-existing fixed LE .ffe wire contract.
        write(&m,sizeof(m));frontend_hash_=frontend_hash(frontend_hash_,&m,sizeof(m));
        le(unsigned(f.status),1);le(unsigned(f.reason),1);le(f.path_complete,1);le(f.fragments.size(),1);
        if(version_==1)le(0,4);
        else {le(f.decode_context_known,1);le(0,3);le(f.decode_address_space_id,8);}
        for(const auto& piece:f.fragments)fragment(piece);
        end_=m.end_record;++rows_;
    }
    void finish(std::uint64_t records) {
        if(!out_)return;
        if(records!=records_ || end_!=records)throw std::runtime_error("incomplete instruction translation population");
        le(255,1);le(records,8);le(rows_,8);le(hot_hash_,8);le(frontend_hash_,8);
        auto output=out_;out_=nullptr;
        if(gzclose(output)!=Z_OK)throw std::runtime_error("instruction translation close failed");
    }
};
// Parent reader prevalidates the full bound stream and footer before exposure;
// current-row streaming retains O(1) translation state, not a per-UOP table.
class InstructionTranslations {
public:
    InstructionTranslations(const std::string& hot_path,std::uint32_t core,std::uint64_t records,
        const std::string& policy,const std::function<std::uint64_t(std::uint64_t)>& asid);
    ~InstructionTranslations();
    unsigned version() const;
    std::shared_ptr<const InstructionFetchFacts> resolve(std::uint64_t ordinal,const RetiredFrontendMacro& macro);
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
}
