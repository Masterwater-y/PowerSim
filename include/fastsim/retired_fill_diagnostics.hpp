#pragma once
#include "fastsim/llc_audit_writer.hpp"
#include "fastsim/retired_cache_miss_estimator.hpp"
#include <array>
#include <map>

namespace fastsim {
struct RetiredFillSample {
    unsigned core=0,fragment=0,own=0,private_level=0,input_fallback_reasons=0;
    std::uint64_t ordinal=0,pc=0,line=0,issue=0,response=0,canonical=0;
    std::uint64_t selected_issue=0,selected_response=0,final_issue=UINT64_MAX;
    bool measured=false,task=false,kernel=false,forwarded=false,alias=false;
};
// Observer-only. Full member/transition counts are separate from capped samples.
// Envelope is llc-fill-audit-v1; producer distinguishes these decision records.
class RetiredFillDiagnostics {
    llcaudit::Writer writer_{"retired-fill-model","FASTSIM_RETIRED_FILL_AUDIT"};
    struct Counts {std::uint64_t members=0,unknown_private=0,input_fallback=0,final_unavailable=0;
        std::array<std::uint64_t,8> input_reasons{};std::array<std::uint64_t,7> decisions{};
        std::array<std::uint64_t,64> transitions{};};
    std::map<unsigned,Counts> counts_;
    bool finished_=false;
    static void write_counts(std::ostream& out,const Counts& c){
        out<<"\"members\":"<<c.members
           <<",\"unknown_private_members\":"<<c.unknown_private
           <<",\"input_fallback_members\":"<<c.input_fallback
           <<",\"final_issue_unavailable_members\":"<<c.final_unavailable
           <<",\"input_fallback_reason_members\":[";
        for(unsigned i=0;i<8;++i){if(i)out<<',';out<<c.input_reasons[i];}out<<"],\"decisions\":[";
        for(unsigned i=0;i<7;++i){if(i)out<<',';out<<c.decisions[i];}
        out<<"],\"transitions\":[";
        for(unsigned i=0;i<64;++i){if(i)out<<',';out<<c.transitions[i];}out<<']';
    }
  public:
    RetiredFillDiagnostics()=default;
    RetiredFillDiagnostics(std::ostream& out,std::uint64_t stride,std::uint64_t cap)
        :writer_(out,"retired-fill-model",stride,cap){}
    bool enabled()const{return writer_.enabled();}
    std::uint64_t members()const{std::uint64_t n=0;for(const auto& x:counts_)n+=x.second.members;return n;}
    void member(const RetiredFillSample& m,const RetiredCacheMissEstimator::Audit& a,
                const RetiredCacheMissEstimator::Result& r){
        if(!enabled())return;
        auto& c=counts_[m.core*8+unsigned(m.measured)*4+unsigned(m.task)*2+unsigned(m.kernel)];
        ++c.members;c.unknown_private+=m.private_level==5;
        c.input_fallback+=m.input_fallback_reasons!=0;c.final_unavailable+=m.final_issue==UINT64_MAX;
        for(unsigned i=0;i<8;++i)c.input_reasons[i]+=bool(m.input_fallback_reasons&(1u<<i));
        ++c.decisions[unsigned(a.decision)];++c.transitions[m.own*8+r.misses];
        if(!writer_.selected(m.line))return;
        std::ostringstream out;
        out<<"{\"type\":\"decision\",\"core\":"<<m.core<<",\"ordinal\":"<<m.ordinal
           <<",\"fragment\":"<<m.fragment<<",\"pc\":"<<m.pc<<",\"line\":"<<m.line
           <<",\"measurement\":"<<m.measured<<",\"task\":"<<m.task<<",\"kernel\":"<<m.kernel
           <<",\"own\":"<<m.own<<",\"result\":"<<r.misses<<",\"decision\":"<<unsigned(a.decision)
           <<",\"issue\":"<<m.issue<<",\"response\":"<<m.response<<",\"canonical\":"<<m.canonical
           <<",\"final_issue\":";
        if(m.final_issue==UINT64_MAX)out<<"null";else out<<m.final_issue;
        out<<",\"selected_issue\":"<<m.selected_issue<<",\"selected_response\":"<<m.selected_response
           <<",\"private_level\":"<<m.private_level<<",\"forwarded\":"<<m.forwarded<<",\"alias\":"<<m.alias
           <<",\"parent_valid\":"<<a.valid<<",\"parent_line\":"<<a.prior_line
           <<",\"parent_ordinal\":";
        if(a.owner_ordinal==UINT64_MAX)out<<"null";else out<<a.owner_ordinal;
        out<<",\"shadow_generation\":"<<a.shadow_generation
           <<",\"parent_begin\":"<<a.begin<<",\"parent_end\":"<<a.end
           <<",\"input_fallback_reasons\":"<<m.input_fallback_reasons
           <<",\"shadow_fallback_reasons\":"<<r.fallback_reasons
           <<",\"fallback_reasons\":"<<(m.input_fallback_reasons|r.fallback_reasons)<<'}';
        writer_.row(out.str());
    }
    void write_summary(std::ostream& out)const{
        out<<"{\"schema\":\"retired-fill-decisions-v2\",\"unit\":\"load-member-fragment\",\"source_identity\":\""<<llcaudit::source_identity()<<"\",\"groups\":[";
        bool first=true;for(const auto& x:counts_){if(!first)out<<',';first=false;
            out<<"{\"core\":"<<x.first/8<<",\"measurement\":"<<bool(x.first&4)
               <<",\"task\":"<<bool(x.first&2)<<",\"kernel\":"<<bool(x.first&1)<<',';
            write_counts(out,x.second);out<<'}';}out<<"]}\n";
    }
    void finish(){
        if(!enabled()||finished_)return;
        finished_=true;
        // Uncapped fragment counters are not architectural macro PMU scores.
        if(const auto p=std::getenv("FASTSIM_RETIRED_FILL_AUDIT")){
            std::ofstream out(std::string(p)+".summary.json");
            if(!out)throw std::runtime_error("cannot open retired fill summary");
            write_summary(out);
            if(!out)throw std::runtime_error("retired fill summary write failed");
        }
        writer_.finish(true);
    }
};
}
