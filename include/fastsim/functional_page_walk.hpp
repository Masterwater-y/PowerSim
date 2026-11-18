#pragma once
#include "fastsim/config.hpp"
#include "fastsim/fst_translation_facts.hpp"
#include <algorithm>
#include <array>
#include <limits>
#include <stdexcept>
#include <vector>

namespace fastsim {
// Bounded PTE-only cache shadows. Physical paths use captured functional PTE
// addresses; structural paths use page-table prefixes, never invented guest
// physical addresses. No native outcomes/timestamps or architectural traffic.
// Data-cache interference, coherence and DRAM row/queue state are approximated.
class FunctionalPageWalk {
    struct Key {
        std::uint64_t root=0, line=0;
        unsigned level=0;
        bool structural=false;
        bool operator==(const Key& o) const {
            return root==o.root && line==o.line && level==o.level && structural==o.structural;
        }
    };
    struct Entry {Key key;std::uint64_t ready=0,used=0;bool valid=false;};
    using Set=std::array<Entry,8>;
    std::array<std::vector<Set>,3> caches_;
    std::array<std::uint64_t,3> latency_;
    std::uint64_t cold_,sequence_=0;
    unsigned line_bytes_;
    static std::uint64_t add(std::uint64_t a,std::uint64_t b) {
        if(a>std::numeric_limits<std::uint64_t>::max()-b)
            throw std::overflow_error("functional PTE service overflow");
        return a+b;
    }
    std::uint64_t read(const Key& key,unsigned tier,std::uint64_t now) {
        if(tier==3)return add(now,cold_);
        const auto index=(key.line ^ key.root ^ key.level)%caches_[tier].size();
        auto& set=caches_[tier][index];
        for(auto& entry:set)if(entry.valid && entry.key==key) {
            entry.used=++sequence_;
            return std::max(add(now,latency_[tier]),entry.ready);
        }
        const auto ready=read(key,tier+1,add(now,latency_[tier]));
        auto victim=std::min_element(set.begin(),set.end(),[](const Entry& a,const Entry& b){
            return (!a.valid && b.valid) || (a.valid==b.valid && a.used<b.used);
        });
        *victim=Entry{key,ready,++sequence_,true};
        return ready;
    }
 public:
    struct Result {std::uint64_t ready;unsigned levels;bool structural;};
    explicit FunctionalPageWalk(const SimulatorConfig& c)
        :latency_{c.l1d.hit_latency,c.l2.hit_latency,c.llc.hit_latency},
         cold_(std::uint64_t(c.dram.t_rp)+c.dram.t_rcd+c.dram.t_cl+
               c.dram.burst_cycles+c.dram.frontend_latency+c.dram.backend_latency),
         line_bytes_(c.l1d.line_size) {
        const std::array<std::uint64_t,3> bytes{c.l1d.size_bytes,c.l2.size_bytes,c.llc.size_bytes};
        for(unsigned tier=0;tier<3;++tier)
            caches_[tier].resize(std::max<std::uint64_t>(1,std::min<std::uint64_t>(512,bytes[tier]/line_bytes_/8)));
    }
    Result walk(const fst::TranslationMapping& mapping,std::uint64_t va,std::uint64_t start) {
        const bool physical=(mapping.flags & fst::kTranslationPathKnown) && mapping.levels;
        const unsigned bits=mapping.page_bits?mapping.page_bits:12;
        const unsigned levels=physical?mapping.levels:(bits>=30?2:bits>=21?3:4);
        if(!levels || levels>mapping.pte_addresses.size() || bits>39)
            throw std::invalid_argument("unsupported functional page-table depth");
        auto ready=start;
        for(unsigned i=0;i<levels;++i) {
            Key key;
            if(physical)key.line=mapping.pte_addresses[i]/line_bytes_;
            else {
                key.structural=true;key.root=mapping.walk_root?mapping.walk_root:mapping.address_space_id;
                key.level=i;const auto shift=bits+9*(levels-1-i);
                key.line=(shift>=64?0:va>>shift)/(line_bytes_/8);
            }
            ready=read(key,0,ready);
        }
        return {ready,levels,!physical};
    }
};
}
