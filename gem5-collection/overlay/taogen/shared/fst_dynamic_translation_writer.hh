#pragma once
#include "fst_translation_facts.hh"
#include <zlib.h>
#include <algorithm>
#include <cstring>
#include <map>
#include <stdexcept>
#include <string>

namespace fastsim::fst {
class DynamicTranslationWriter {
    gzFile file_=nullptr;
    std::vector<unsigned char> buffer_;
    std::map<TranslationMapping,std::uint64_t> dictionary_;
    std::map<std::uint32_t,std::uint64_t> token_state_;
    std::uint64_t definitions_=0,bindings_=0,memory_=0,unknown_=0,last_ordinal_=0;
    bool finished_=false;
    void flush() {
        if(!buffer_.empty()) {
            if(gzwrite(file_,buffer_.data(),static_cast<unsigned>(buffer_.size()))!=static_cast<int>(buffer_.size()))
                throw std::runtime_error("dynamic translation gzip write failed");
            buffer_.clear();
        }
    }
    void byte(unsigned char b) {buffer_.push_back(b);if(buffer_.size()==65536)flush();}
    void var(std::uint64_t x) {while(x>=128){byte(static_cast<unsigned char>((x&127)|128));x>>=7;}byte(static_cast<unsigned char>(x));}
    void fixed(std::uint64_t x,unsigned n) {for(unsigned i=0;i<n;++i){byte(static_cast<unsigned char>(x&255));x>>=8;}}
    static std::uint32_t hash(const void* record) {
        auto* p=static_cast<const unsigned char*>(record);std::uint32_t h=2166136261u;
        for(unsigned i=0;i<64;++i)h=(h^p[i])*16777619u;
        return h;
    }
    static bool specialPhysical(const TranslationMapping& m,std::uint64_t va,
                                std::uint32_t size,std::uint64_t& pa) {
        if(va>=m.request_virtual_base) {
            const auto delta=va-m.request_virtual_base;
            if(delta>UINT64_MAX-m.request_physical_base)return false;
            pa=m.request_physical_base+delta;
        } else {
            const auto delta=m.request_virtual_base-va;
            if(delta>m.request_physical_base)return false;
            pa=m.request_physical_base-delta;
        }
        return size && size-1<=UINT64_MAX-pa;
    }
    static bool valid(const TranslationFragment& f,std::uint64_t asid) {
        const auto& m=f.mapping;
        if(static_cast<unsigned>(m.kind)>6 ||
           (m.kind==TranslationKind::kPaged && (m.request_physical_base || m.request_virtual_base)))return false;
        std::uint64_t special_pa=0;
        if(m.kind!=TranslationKind::kPaged && !specialPhysical(m,f.virtual_address,f.size,special_pa))return false;
        if(m.kind!=TranslationKind::kPaged && !(m.flags&kTranslationLeafKnown)) {
            if(!f.size || m.address_space_id!=asid || m.flags || m.page_bits || m.levels || m.walk_root || m.physical_base ||
               (m.virtual_base&4095) || f.virtual_address<m.virtual_base ||
               f.virtual_address-m.virtual_base>4095 || f.size-1>4095-(f.virtual_address-m.virtual_base) ||
               f.physical_address!=special_pa)return false;
            for(auto p:m.pte_addresses)if(p)return false;
            return true;
        }
        if(!f.size || m.address_space_id!=asid || !(m.flags&kTranslationLeafKnown) ||
           (m.flags&~7) || (m.page_bits!=12 && m.page_bits!=21 && m.page_bits!=22 && m.page_bits!=30) ||m.levels>8)
            return false;
        const auto mask=(std::uint64_t(1)<<m.page_bits)-1;
        if((m.virtual_base&mask)||(m.physical_base&mask)|| f.virtual_address<m.virtual_base ||
           f.virtual_address-m.virtual_base>mask || f.size-1>mask-(f.virtual_address-m.virtual_base) ||
           f.physical_address!=(m.kind==TranslationKind::kPaged ? m.physical_base+(f.virtual_address-m.virtual_base) :
             special_pa))return false;
        if(m.flags&kTranslationPathKnown) {
            if(!m.levels || !m.walk_root)return false;
            for(std::size_t i=0;i<m.levels;++i)if(m.pte_addresses[i]&3)return false;
        } else if(m.levels || m.walk_root)return false;
        for(std::size_t i=m.levels;i<8;++i)if(m.pte_addresses[i])return false;
        return true;
    }
    std::uint64_t intern(const TranslationMapping& m) {
        const auto found=dictionary_.find(m);if(found!=dictionary_.end())return found->second;
        const auto id=++definitions_;dictionary_.emplace(m,id);
        byte(1);var(id);var(m.address_space_id);var(m.walk_root);var(m.virtual_base);var(m.physical_base);var(m.pcid);
        byte(m.page_bits);byte(m.levels);byte(m.flags);
        for(std::size_t i=0;i<m.levels;++i)var(m.pte_addresses[i]);
        byte(static_cast<unsigned char>(m.kind));var(m.request_physical_base);var(m.request_virtual_base);
        return id;
    }
    void bind(std::uint64_t ordinal,const void* record,std::uint32_t token,
              std::uint64_t va,std::uint32_t size,std::uint64_t id,bool force) {
        const auto old=token_state_.find(token);
        if(!force && token && old!=token_state_.end() && old->second==id)return;
        if(ordinal<last_ordinal_)throw std::runtime_error("dynamic translation ordinal reversed");
        byte(2);var(ordinal-last_ordinal_);fixed(hash(record),4);var(token);var(va);var(size);var(id);
        last_ordinal_=ordinal;++bindings_;
        if(token)token_state_[token]=id;
    }
public:
    explicit DynamicTranslationWriter(const std::string& path,std::uint32_t core) {
        file_=gzopen(path.c_str(),"wb1");if(!file_)throw std::runtime_error("cannot open dynamic translation companion");
        gzbuffer(file_,1u<<20);buffer_.reserve(65536);
        for(unsigned char c:std::string("FSTXLT1\0",8))byte(c);
        fixed(2,4);fixed(32,4);fixed(core,4);fixed(1,4);fixed(0,8);
    }
    DynamicTranslationWriter(const DynamicTranslationWriter&)=delete;
    DynamicTranslationWriter& operator=(const DynamicTranslationWriter&)=delete;
    ~DynamicTranslationWriter() {if(file_)gzclose(file_);}
    void observe(std::uint64_t ordinal,const void* record,std::uint32_t token,std::uint64_t asid,
                 std::uint64_t va,std::uint32_t size,std::vector<TranslationFragment> fragments) {
        if(finished_ || !size || va+size<va)throw std::runtime_error("invalid dynamic translation observation");
        ++memory_;
        std::sort(fragments.begin(),fragments.end(),[](const auto& a,const auto& b){return a.virtual_address<b.virtual_address;});
        std::uint64_t next=va;bool complete=!fragments.empty(),known=true;
        for(const auto& f:fragments) {
            if(!f.size || f.virtual_address!=next || next+f.size<next || next+f.size>va+size){complete=false;break;}
            next+=f.size;known=known && valid(f,asid) && ((f.mapping.flags&3)==3 || (f.mapping.kind!=TranslationKind::kPaged && !f.mapping.flags));
        }
        complete=complete && next==va+size;
        std::uint64_t hot_pa=0;std::memcpy(&hot_pa,static_cast<const unsigned char*>(record)+8,8);
        if(complete && fragments.front().physical_address!=hot_pa)complete=false;
        if(!complete) {
            ++unknown_;bind(ordinal,record,token,va,size,0,token==0);return;
        }
        if(!known)++unknown_;
        const bool force=!token || fragments.size()!=1;
        for(const auto& f:fragments)bind(ordinal,record,token,f.virtual_address,f.size,valid(f,asid)?intern(f.mapping):0,force);
        if(force && token)token_state_.erase(token);
    }
    void finish(std::uint64_t count,std::uint64_t full_hash) {
        if(finished_)return;
        byte(255);var(count);var(memory_);var(definitions_);var(bindings_);var(unknown_);fixed(full_hash,8);flush();
        const auto status=gzclose(file_);file_=nullptr;finished_=true;
        if(status!=Z_OK)throw std::runtime_error("dynamic translation gzip close failed");
    }
    std::uint64_t definitions()const{return definitions_;}
    std::uint64_t bindings()const{return bindings_;}
    std::uint64_t unknown_records()const{return unknown_;}
};
}
