#pragma once
#include "fastsim/retired_service_counts.hpp"
#include <algorithm>
#include <vector>

namespace fastsim {
using ServiceProbability=std::array<std::uint64_t,3>;
struct ServiceObservation {
    std::uint64_t line=0,pc=0,selected_issue=0,selected_response=0,final_issue=UINT64_MAX;
    unsigned own_mask=0,legacy_mask=0;
    bool known_source=true,clock_available=true,forwarded=false;
};
struct ServiceModelResult {ServiceProbability p{};bool fallback=false;};

// A single latent service per line avoids assuming independent fragments.
// Distinct uncertain lines use Frechet bounds, not a product of hit rates.
class ServiceMacroUnion {
    struct Group {std::uint64_t line=0,generation=0;ServiceProbability p{};};
    std::array<Group,16> groups_{};std::size_t size_=0;
    ServiceProbability overflow_max_{},overflow_sum_{};
  public:
    struct Estimate {ServiceProbability point{},lower{},upper{};bool uncertain=false,overflow=false;};
    void clear(){size_=0;overflow_max_={};overflow_sum_={};}
    void add(std::uint64_t line,const ServiceProbability& p,std::uint64_t generation=0){
        if(p[0]>kServiceQ || p[1]>p[0] || p[2]>p[1])throw std::invalid_argument("non-nested service probability");
        for(std::size_t i=0;i<size_;++i)if(groups_[i].line==line && groups_[i].generation==generation){
            for(unsigned l=0;l<3;++l)groups_[i].p[l]=std::max(groups_[i].p[l],p[l]);return;}
        if(size_<groups_.size()){groups_[size_++]={line,generation,p};return;}
        for(unsigned l=0;l<3;++l){overflow_max_[l]=std::max(overflow_max_[l],p[l]);
            overflow_sum_[l]=std::min(kServiceQ,overflow_sum_[l]+p[l]);}
    }
    Estimate finish()const{
        Estimate r;r.lower=overflow_max_;r.upper=overflow_sum_;
        r.overflow=overflow_sum_[0]!=0;
        for(std::size_t i=0;i<size_;++i)for(unsigned l=0;l<3;++l){
            r.lower[l]=std::max(r.lower[l],groups_[i].p[l]);r.upper[l]=std::min(kServiceQ,r.upper[l]+groups_[i].p[l]);}
        for(unsigned l=0;l<3;++l){r.point[l]=r.lower[l]+(r.upper[l]-r.lower[l])/2;r.uncertain|=r.lower[l]!=r.upper[l];}
        return r;
    }
};

// One-pass PMU observer. It never sends a request, installs a cache line, or
// modifies any model clock. The optional residual is an uncalibrated analytic
// hypothesis, frozen before workload evaluation, not a native-derived label.
class RetiredServiceModel {
    struct Entry {std::uint64_t line=0,last=0,begin=0,end=0,generation=0;bool valid=false,fill=false;};
    struct Pc {std::uint64_t tag=0,line=0;std::int64_t stride=0;unsigned stable=0;bool valid=false;};
    std::vector<std::array<Entry,4>> sets_;
    std::array<Pc,256> pcs_{};
    bool residual_=false,branch_seen_=false;
    std::uint64_t rob_=0,l1_lines_=0,l2_lines_=0,target_sets_=0,assoc_=0;
    std::uint64_t event_=0,macro_=0,last_branch_=0,generation_=0;
    ServiceMacroUnion pending_;
    bool read_=false,unknown_=false,clock_missing_=false,fallback_=false,branch_pending_=false;
    bool has_fragment_=false;
    ServiceExpectationCore counts_;
    static std::uint64_t mul(std::uint64_t a,std::uint64_t b){return a*b/kServiceQ;}
    static ServiceProbability bits(unsigned mask){
        if(mask!=0 && mask!=1 && mask!=3 && mask!=7)throw std::invalid_argument("unsupported service miss mask");
        return {mask&1?kServiceQ:0,mask&2?kServiceQ:0,mask&4?kServiceQ:0};
    }
    static std::uint64_t ratio(std::uint64_t a,std::uint64_t b){
        if(!b || a>=b)return kServiceQ;
        // Use long double only for bounded conversion, avoiding a*Q overflow.
        return static_cast<std::uint64_t>(static_cast<long double>(a)*kServiceQ/b);
    }
    static std::uint64_t overlap(std::uint64_t lo,std::uint64_t hi,std::uint64_t begin,std::uint64_t end){
        if(lo==hi)return begin<=lo && lo<end?kServiceQ:0;
        const auto a=std::max(lo,begin),b=std::min(hi,end);
        return b>a?ratio(b-a,hi-lo):0;
    }
  public:
    RetiredServiceModel(bool residual,std::uint64_t rob,std::uint64_t l1_lines,
                        std::uint64_t l2_lines,std::uint64_t target_sets,std::uint64_t assoc,
                        std::size_t capacity=4096)
        :sets_(capacity/4),residual_(residual),rob_(rob),l1_lines_(l1_lines),l2_lines_(l2_lines),
         target_sets_(target_sets),assoc_(assoc){
        if(capacity<4 || capacity%4 || !rob || !l1_lines || !l2_lines || !target_sets || !assoc)
            throw std::invalid_argument("empty bounded service geometry");
    }
    void note_branch_miss(){branch_seen_=true;last_branch_=macro_;}
    ServiceModelResult observe(const ServiceObservation& x){
        ++counts_.observations;has_fragment_=true;unknown_|=!x.known_source;clock_missing_|=!x.clock_available || x.final_issue==UINT64_MAX;
        ServiceModelResult r;
        if(!x.known_source){r.p=bits(x.legacy_mask);r.fallback=true;fallback_=true;pending_.add(x.line,r.p,++generation_);return r;}
        if(x.forwarded){pending_.add(x.line,r.p);return r;}
        ++event_;auto& set=sets_[(x.line%target_sets_)%sets_.size()];Entry* hit=nullptr;Entry* victim=&set[0];
        std::uint64_t recent_same_set=0;
        for(auto& e:set){
            if(e.valid && e.line==x.line)hit=&e;
            if(!e.valid || (victim->valid && e.last<victim->last))victim=&e;
            if(residual_ && e.valid && e.line%target_sets_==x.line%target_sets_ && event_-e.last<=rob_)++recent_same_set;
        }
        const auto reuse=residual_ && hit?kServiceQ-ratio(event_-hit->last,l2_lines_):0;
        std::uint64_t stream=0;
        if(residual_) {
        auto& pc=pcs_[(x.pc^(x.pc>>11))%pcs_.size()];
        const auto delta=x.line>=pc.line?static_cast<std::int64_t>(std::min<std::uint64_t>(x.line-pc.line,INT64_MAX)):
            -static_cast<std::int64_t>(std::min<std::uint64_t>(pc.line-x.line,INT64_MAX));
        if(pc.valid && pc.tag==x.pc && delta==pc.stride && delta>=-4 && delta<=4 && pc.stable>=2)stream=kServiceQ;
        if(pc.valid && pc.tag==x.pc && delta==pc.stride)pc.stable=std::min(3u,pc.stable+1);
        else pc.stable=0;
        pc.tag=x.pc;pc.line=x.line;pc.stride=delta;pc.valid=true;
        }
        const auto reuse_l1=residual_ && hit?kServiceQ-ratio(event_-hit->last,l1_lines_):0;
        const auto own=bits(x.own_mask);r.p=own;
        const auto child=x.final_issue==UINT64_MAX?x.selected_issue:x.final_issue;
        if(child<x.selected_issue){++counts_.reversed_clocks;r.p=bits(x.legacy_mask);r.fallback=true;}
        else if(hit && hit->fill){
            if(x.selected_issue<hit->begin){++counts_.future_intervals;r.p=bits(x.legacy_mask);r.fallback=true;}
            const auto pending=r.fallback?0:overlap(x.selected_issue,child,hit->begin,hit->end);
            if(pending){++counts_.interval_joins;r.p[0]=std::max(own[0],pending);
                r.p[1]=mul(own[1],kServiceQ-pending);r.p[2]=mul(own[2],kServiceQ-pending);}
        }
        if(residual_ && !r.fallback && branch_seen_ && macro_-last_branch_<rob_){
            const auto exposure=kServiceQ-ratio(macro_-last_branch_,rob_);
            const auto locality=std::max(reuse,stream);
            // Symmetric maximum-uncertainty prior: at most half the mass moves.
            // No fitted workload coefficient and no synthetic fill or traffic.
            const auto warm=mul(exposure,locality)/2;
            const auto pressure=ratio(recent_same_set,assoc_);
            const auto cold=mul(mul(exposure,kServiceQ-reuse_l1),pressure)/2;
            const ServiceProbability before=r.p;
            const auto mass0=kServiceQ-before[0],mass1=before[0]-before[1],mass2=before[1]-before[2];
            for(unsigned l=1;l<3;++l)r.p[l]-=mul(r.p[l],warm);
            r.p[0]+=mul(mass0,cold);r.p[1]+=mul(mass1,cold);r.p[2]+=mul(mass2,cold);
            r.p[1]=std::min(r.p[0],r.p[1]);r.p[2]=std::min(r.p[1],r.p[2]);
            counts_.warm_mixes+=warm!=0;counts_.pollution_mixes+=cold!=0;
        }
        if(!hit){hit=victim;counts_.history_replacements+=hit->valid;*hit={x.line,event_,0,0,0,true,false};}
        hit->last=event_;
        // A follower does not restart or extend the same selected owner.
        if(x.own_mask && x.selected_response>x.selected_issue &&
           (!hit->fill || x.selected_issue>=hit->end)){
            hit->begin=x.selected_issue;hit->end=x.selected_response;hit->fill=true;hit->generation=++generation_;
        }
        if(!hit->generation)hit->generation=++generation_;
        fallback_|=r.fallback;pending_.add(x.line,r.p,hit->generation);return r;
    }
    void invalidate(std::uint64_t line){
        ++counts_.writes;auto& set=sets_[(line%target_sets_)%sets_.size()];
        for(auto& e:set)if(e.valid && e.line==line){e.fill=false;e.generation=++generation_;}
    }
    void finish_uop(bool complete,bool read,bool kernel,bool owner_known,bool task,bool branch_miss,
                    unsigned fallback_mask=0){
        read_|=read;branch_pending_|=branch_miss;
        if(read && !has_fragment_){pending_.add(UINT64_MAX,bits(fallback_mask));unknown_=true;fallback_=true;}
        has_fragment_=false;
        if(!complete)return;
        if(read_){const auto p=pending_.finish();ServiceExpectationCounts delta;
            delta.macro_loads=1;delta.unknown_source_loads=unknown_;delta.clock_unavailable_loads=clock_missing_;
            delta.correlation_uncertain_loads=p.uncertain;delta.fallback_loads=fallback_||p.overflow;
            delta.expected=p.point;delta.lower=p.lower;delta.upper=p.upper;
            counts_.scopes[0]+=delta;counts_.scopes[kernel?2:1]+=delta;
            if(owner_known)counts_.scopes[task?3:4]+=delta;}
        if(branch_pending_)note_branch_miss();++macro_;clear_pending();
    }
    void abandon(){clear_pending();}
    void begin_measurement(){counts_={};}
    const ServiceExpectationCore& snapshot()const{return counts_;}
  private:
    void clear_pending(){pending_.clear();read_=unknown_=clock_missing_=fallback_=branch_pending_=has_fragment_=false;}
};
}
