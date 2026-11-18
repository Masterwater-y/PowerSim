#pragma once
#include <array>
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <limits>
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
    struct Entry {
        std::uint64_t asid=0, virtual_base=0;
        std::uint16_t pcid=0;
        std::uint8_t page_bits=0;
        bool global=false, valid=false;
        std::uint64_t physical_base=0, walk_root=0;
        bool mapping_known=false;
        std::uint64_t ready_cycle=0;
    };
    static unsigned domain(std::uint64_t virtual_address,unsigned bits) {
        check_page_bits(bits);
        const auto page=virtual_address>>bits;
        return bits==12 ? unsigned((page>>7)&1u) : bits==30 ? 1u : 0u;
    }
    static unsigned set(std::uint64_t virtual_address,unsigned bits) {
        check_page_bits(bits);
        return unsigned((virtual_address>>bits)&127u);
    }
    std::optional<Entry> lookup(std::uint64_t asid,unsigned pcid,std::uint64_t virtual_address,
                                bool update_lru=true,std::uint64_t now=std::numeric_limits<std::uint64_t>::max()) {
        check_pcid(pcid);
        // gem5 Trie::lookupHandle returns the first matching prefix: broadest first.
        for (const auto bits : {30u,22u,21u,12u}) {
            auto& group=groups_[domain(virtual_address,bits)][set(virtual_address,bits)];
            const auto base=virtual_address & ~((std::uint64_t{1}<<bits)-1);
            for (std::size_t i=0;i<group.size();++i) {
                if (!group[i].valid || group[i].asid!=asid || group[i].pcid!=pcid ||
                    group[i].page_bits!=bits || group[i].virtual_base!=base || group[i].ready_cycle>now) continue;
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
                std::optional<std::pair<std::uint64_t,std::uint64_t>> mapping = std::nullopt,
                std::uint64_t ready_cycle=0) {
        check_page_bits(bits);
        if (const auto existing=lookup(asid,pcid,virtual_address,false)) {
            auto& group=groups_[domain(existing->virtual_base,existing->page_bits)][set(existing->virtual_base,existing->page_bits)];
            for(auto& entry:group)if(entry.valid && entry.asid==asid && entry.pcid==pcid &&
                entry.virtual_base==existing->virtual_base && entry.page_bits==existing->page_bits)
                entry.ready_cycle=std::min(entry.ready_cycle,ready_cycle);
            return false;
        }
        auto& group=groups_[domain(virtual_address,bits)][set(virtual_address,bits)];
        for (auto i=group.size()-1;i>0;--i) group[i]=group[i-1];
        group[0]=Entry{asid,virtual_address & ~((std::uint64_t{1}<<bits)-1),
                       static_cast<std::uint16_t>(pcid),static_cast<std::uint8_t>(bits),global,true};
        if (mapping) { group[0].physical_base=mapping->first; group[0].walk_root=mapping->second; group[0].mapping_known=true; }
        group[0].ready_cycle=ready_cycle;
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
            auto& group=groups_[domain(virtual_address,bits)][set(virtual_address,bits)];
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
    static void check_pcid(unsigned pcid) {
        if (pcid>=4096) throw std::invalid_argument("invalid x86 PCID");
    }
    static void check_page_bits(unsigned bits) {
        if(bits!=12 && bits!=21 && bits!=22 && bits!=30)
            throw std::invalid_argument("unsupported SPR STLB page size");
    }
    using Set=std::array<Entry,8>;
    std::array<std::array<Set,128>,2> groups_{};
    std::uint64_t generation_=0;
};
}
