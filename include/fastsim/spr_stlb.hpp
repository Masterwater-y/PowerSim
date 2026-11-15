#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <utility>

namespace fastsim {
// Bounded functional-state approximation of the gem5-spr per-core shared
// I/D STLB geometry. Only functional VA/ASID/page size and global facts enter;
// caller owns timing, bound MMU controls and rollback/commit decisions.
// ASID-scoped global reuse is deliberately conservative until target PCID
// semantics are proved. Large-page placement follows gem5's proxy.
class SprStlb {
  public:
    explicit SprStlb(unsigned index_shift = 0) : index_shift_(index_shift) {
        if (index_shift_ >= 64)
            throw std::invalid_argument("SPR STLB index shift must be less than 64");
    }
    struct Entry {
        std::uint64_t asid=0, virtual_base=0;
        std::uint16_t pcid=0;
        std::uint8_t page_bits=0;
        bool global=false, valid=false;
        std::uint64_t physical_base=0, walk_root=0;
        bool mapping_known=false;
    };
    static unsigned domain(std::uint64_t virtual_address,unsigned bits,unsigned index_shift=0) {
        check_page_bits(bits);
        if(index_shift>=64) throw std::invalid_argument("invalid SPR STLB index shift");
        auto page=virtual_address>>bits;
        if(bits==12) page = fold_page(page, index_shift);
        return bits==12 ? unsigned((page>>7)&1u) : bits==30 ? 1u : 0u;
    }
    static unsigned set(std::uint64_t virtual_address,unsigned bits,unsigned index_shift=0) {
        check_page_bits(bits);
        if(index_shift>=64) throw std::invalid_argument("invalid SPR STLB index shift");
        auto page=virtual_address>>bits;
        if(bits==12) page = fold_page(page, index_shift);
        return unsigned(page&127u);
    }
    std::optional<Entry> lookup(std::uint64_t asid,unsigned pcid,std::uint64_t virtual_address,
                                bool update_lru=true) {
        check_pcid(pcid);
        // gem5 Trie::lookupHandle returns the first matching prefix: broadest first.
        for (const auto bits : {30u,22u,21u,12u}) {
            auto& group=groups_[domain(virtual_address,bits,index_shift_)][set(virtual_address,bits,index_shift_)];
            const auto base=virtual_address & ~((std::uint64_t{1}<<bits)-1);
            for (std::size_t i=0;i<group.size();++i) {
                if (!group[i].valid || group[i].asid!=asid || group[i].pcid!=pcid ||
                    group[i].page_bits!=bits || group[i].virtual_base!=base) continue;
                const auto found=group[i];
                if (update_lru) {for (auto j=i;j>0;--j) group[j]=group[j-1];group[0]=found;}
                return found;
            }
        }
        return std::nullopt;
    }
    // A duplicate matching trie entry is retained and does not update LRU,
    // matching TLB::insert's early return. Returns true for a new entry.
    bool insert(std::uint64_t asid,unsigned pcid,std::uint64_t virtual_address,unsigned bits,bool global,
                std::optional<std::pair<std::uint64_t,std::uint64_t>> mapping = std::nullopt) {
        check_page_bits(bits);
        if (lookup(asid,pcid,virtual_address,false)) return false;
        auto& group=groups_[domain(virtual_address,bits,index_shift_)][set(virtual_address,bits,index_shift_)];
        for (auto i=group.size()-1;i>0;--i) group[i]=group[i-1];
        group[0]=Entry{asid,virtual_address & ~((std::uint64_t{1}<<bits)-1),
                       static_cast<std::uint16_t>(pcid),static_cast<std::uint8_t>(bits),global,true};
        if (mapping) { group[0].physical_base=mapping->first; group[0].walk_root=mapping->second; group[0].mapping_known=true; }
        return true;
    }
    void flush_all() {
        for (auto& domain_groups:groups_) for (auto& group:domain_groups) group={};
        ++generation_;
    }
    void flush_non_global() {
        for (auto& domain_groups:groups_) for (auto& group:domain_groups) {
            std::size_t out=0;
            for (const auto& entry:group) if (entry.valid && entry.global) group[out++]=entry;
            while (out<group.size()) group[out++]={};
        }
        ++generation_;
    }
    // Use only for an MMU event proven to bind this ASID and virtual page.
    bool invalidate_address(std::uint64_t asid,unsigned pcid,std::uint64_t virtual_address) {
        check_pcid(pcid);
        ++generation_;
        for (const auto bits : {30u,22u,21u,12u}) {
            auto& group=groups_[domain(virtual_address,bits,index_shift_)][set(virtual_address,bits,index_shift_)];
            const auto base=virtual_address & ~((std::uint64_t{1}<<bits)-1);
            for (std::size_t i=0;i<group.size();++i) {
                if (!group[i].valid || group[i].asid!=asid || group[i].pcid!=pcid ||
                    group[i].page_bits!=bits || group[i].virtual_base!=base) continue;
                for (auto j=i;j+1<group.size();++j) group[j]=group[j+1];
                group.back()={};return true;
            }
        }
        return false;
    }
    std::uint64_t generation() const {return generation_;}
  private:
    // The SPR 8-bit profile folds all bytes of the unsigned virtual page
    // number.  Keep the existing non-SPR shifts' single-XOR semantics.
    static std::uint64_t fold_page(std::uint64_t page, unsigned shift) {
        if (shift == 8) {
            page ^= page >> 32;
            page ^= page >> 16;
            page ^= page >> 8;
        } else if (shift) {
            page ^= page >> shift;
        }
        return page;
    }
    static void check_pcid(unsigned pcid) {
        if (pcid>=4096) throw std::invalid_argument("invalid x86 PCID");
    }
    static void check_page_bits(unsigned bits) {
        if(bits!=12 && bits!=21 && bits!=22 && bits!=30)
            throw std::invalid_argument("unsupported SPR STLB page size");
    }
    using Set=std::array<Entry,8>;
    unsigned index_shift_;
    std::array<std::array<Set,128>,2> groups_{};
    std::uint64_t generation_=0;
};
}
