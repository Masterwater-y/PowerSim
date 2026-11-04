#pragma once
#include "fastsim/fst_translation_facts.hpp"
#include <algorithm>
#include <array>
#include <cstdint>
#include <limits>
#include <memory>
#include <stdexcept>
#include <vector>

namespace fastsim::fst {
enum class InstructionFetchStatus : std::uint8_t { Unknown, Mapped, NotApplicable };
enum class InstructionFetchUnknown : std::uint8_t {
    None, MissingBytes, MissingLeaf, ConflictingBytes, ContextMismatch, InconsistentGeometry
};
// Immutable functional byte provenance. It contains neither a native frontend
// source/hit nor a translation/cache timing field. A known leaf may lack a path.
struct InstructionFetchFacts {
    std::uint64_t pc=0;
    std::uint8_t bytes=0;
    InstructionFetchStatus status=InstructionFetchStatus::Unknown;
    InstructionFetchUnknown reason=InstructionFetchUnknown::MissingBytes;
    bool path_complete=false;
    std::vector<TranslationFragment> fragments;
    static std::shared_ptr<const InstructionFetchFacts> pseudo(std::uint64_t pc) {
        auto value=std::make_shared<InstructionFetchFacts>();value->pc=pc;
        value->status=InstructionFetchStatus::NotApplicable;value->reason=InstructionFetchUnknown::None;
        return value;
    }
};
// At most three eight-byte decoder chunks cover a <=15-byte x86 instruction.
// Four slots leave room for the same two-line DSB witness. Call clear() at the
// native decoder-reset boundary; a PC equality test alone cannot detect replay.
class InstructionByteWitness {
    std::array<TranslationFragment,4> pieces_{};
    std::size_t count_=0;
    std::uint64_t pc_=0,space_=0;
    bool active_=false,invalid_=false;
    static bool same(const TranslationFragment& a,const TranslationFragment& b) {
        return a.virtual_address==b.virtual_address && a.physical_address==b.physical_address &&
            a.size==b.size && a.mapping.identity()==b.mapping.identity();
    }
    static bool geometry(const TranslationFragment& f) {
        const auto& m=f.mapping;
        if(m.kind!=TranslationKind::kPaged || (m.flags&~std::uint8_t(7)) ||
           (m.page_bits!=12 && m.page_bits!=21 && m.page_bits!=22 && m.page_bits!=30) ||
           m.levels>m.pte_addresses.size() || ((m.flags&kTranslationPathKnown) && !m.levels))return false;
        const auto mask=(std::uint64_t(1)<<m.page_bits)-1;
        if((m.virtual_base&mask) || (m.physical_base&mask) || f.virtual_address<m.virtual_base)return false;
        const auto offset=f.virtual_address-m.virtual_base;
        if(offset>mask || !f.size || f.size-1>mask-offset ||
           offset>std::numeric_limits<std::uint64_t>::max()-m.physical_base ||
           f.physical_address!=m.physical_base+offset)return false;
        if(f.size-1>std::numeric_limits<std::uint64_t>::max()-f.physical_address)return false;
        // Native paths can use four- or eight-byte entries. Entry width is not
        // carried here; flags/levels establish presence even at physical zero.
        if(m.flags&kTranslationPathKnown)for(unsigned i=0;i<m.levels;++i)
            if(m.pte_addresses[i]&3)return false;
        return true;
    }
public:
    void clear(){count_=0;active_=false;invalid_=false;}
    void begin(std::uint64_t pc,std::uint64_t address_space) {
        clear();pc_=pc;space_=address_space;active_=true;
        invalid_=pc>std::numeric_limits<std::uint64_t>::max()-14;
    }
    void observe(const TranslationFragment& value) {
        if(!active_)throw std::logic_error("instruction byte witness has no decode owner");
        if(invalid_)return;
        if(!value.size || value.size-1>std::numeric_limits<std::uint64_t>::max()-value.virtual_address) {
            invalid_=true;return;
        }
        const auto first=std::max(pc_,value.virtual_address);
        const auto end=std::min(pc_+14,value.virtual_address+std::uint64_t(value.size)-1);
        if(first>end)return;
        auto clipped=value;const auto offset=first-value.virtual_address;
        if(offset>std::numeric_limits<std::uint64_t>::max()-value.physical_address){invalid_=true;return;}
        clipped.virtual_address=first;clipped.physical_address+=offset;
        clipped.size=static_cast<std::uint32_t>(end-first+1);
        for(std::size_t i=0;i<count_;++i)if(same(pieces_[i],clipped))return;
        if(count_==pieces_.size()){invalid_=true;return;}
        pieces_[count_++]=clipped;
    }
    std::shared_ptr<const InstructionFetchFacts> snapshot(unsigned bytes)const {
        if(!bytes || bytes>15)throw std::invalid_argument("invalid x86 instruction byte length");
        auto out=std::make_shared<InstructionFetchFacts>();out->pc=pc_;out->bytes=static_cast<std::uint8_t>(bytes);
        auto unknown=[&](InstructionFetchUnknown reason)->std::shared_ptr<const InstructionFetchFacts> {
            out->reason=reason;out->fragments.clear();out->path_complete=false;return out;
        };
        if(!active_)return unknown(InstructionFetchUnknown::MissingBytes);
        if(invalid_)return unknown(InstructionFetchUnknown::InconsistentGeometry);
        for(unsigned byte=0;byte<bytes;++byte) {
            const auto va=pc_+byte;const TranslationFragment* chosen=nullptr;
            for(std::size_t p=0;p<count_;++p) {
                const auto& piece=pieces_[p];
                if(va<piece.virtual_address || va-piece.virtual_address>=piece.size)continue;
                if(!(piece.mapping.flags&kTranslationLeafKnown))return unknown(InstructionFetchUnknown::MissingLeaf);
                if(!geometry(piece))return unknown(InstructionFetchUnknown::InconsistentGeometry);
                if(piece.mapping.address_space_id!=space_)return unknown(InstructionFetchUnknown::ContextMismatch);
                if(chosen && (chosen->mapping.identity()!=piece.mapping.identity() ||
                    chosen->physical_address+(va-chosen->virtual_address)!=piece.physical_address+(va-piece.virtual_address)))
                    return unknown(InstructionFetchUnknown::ConflictingBytes);
                chosen=&piece;
            }
            if(!chosen)return unknown(InstructionFetchUnknown::MissingBytes);
            const auto pa=chosen->physical_address+(va-chosen->virtual_address);
            if(!out->fragments.empty() && out->fragments.back().mapping.identity()==chosen->mapping.identity() &&
               out->fragments.back().physical_address+out->fragments.back().size==pa)++out->fragments.back().size;
            else {
                if(out->fragments.size()==2)return unknown(InstructionFetchUnknown::InconsistentGeometry);
                auto fragment=*chosen;fragment.virtual_address=va;fragment.physical_address=pa;fragment.size=1;
                out->fragments.push_back(fragment);
            }
        }
        out->status=InstructionFetchStatus::Mapped;out->reason=InstructionFetchUnknown::None;
        out->path_complete=std::all_of(out->fragments.begin(),out->fragments.end(),[](const auto& f) {
            return bool(f.mapping.flags&kTranslationPathKnown);
        });
        return out;
    }
};
} // namespace fastsim::fst
