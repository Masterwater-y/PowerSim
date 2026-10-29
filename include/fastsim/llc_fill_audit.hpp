#pragma once
#include "fastsim/llc_audit_writer.hpp"
#include <map>
namespace fastsim {
struct LlcAuditMember {
    std::uint64_t ordinal=0,pc=0,address=0;
    unsigned size=0,flags=0;bool measurement=false;
};
class ModelLlcFillAudit {
    llcaudit::Writer writer_{"model","FASTSIM_LLC_AUDIT"};
    struct Population {std::uint64_t records=0,last=0,begin=0;bool measured=false;};
    std::map<unsigned,Population> accepted_;

  public:
    bool enabled()const{return writer_.enabled();}
    void accept(unsigned core,const LlcAuditMember& m) {
        if(!enabled())return;
        auto& p=accepted_[core];
        if(!p.records) {
            if(m.ordinal)throw std::runtime_error("LLC audit requires full source prefix");
            p.records=1;p.last=0;
        } else if(m.ordinal!=p.last) {
            if(m.ordinal!=p.last+1)throw std::runtime_error("LLC audit accepted ordinal gap");
            ++p.records;p.last=m.ordinal;
        }
        if(m.measurement&&!p.measured){p.measured=true;p.begin=m.ordinal;}
    }
    void finish() {
        if(!enabled())return;
        for(const auto& item:accepted_) {
            std::ostringstream s;s<<"{\"type\":\"core_end\",\"core\":"<<item.first
                <<",\"records\":"<<item.second.records<<",\"warmup_records\":"<<item.second.begin<<'}';
            writer_.row(s.str());
        }
        writer_.finish(true);
    }
    void member(unsigned core,const LlcAuditMember& m,std::uint64_t line,
                unsigned estimate,unsigned private_level,std::uint64_t issue,std::uint64_t response,
                bool fallback,bool merged,bool forwarded) {
        if(!writer_.selected(line))return;std::ostringstream s;
        s<<"{\"type\":\"member\",\"core\":"<<core<<",\"ordinal\":"<<m.ordinal
         <<",\"pc\":"<<m.pc<<",\"line\":"<<line<<",\"split\":"<<(((m.address>>6)!=((m.address+m.size-1)>>6))?"true":"false")<<",\"flags\":"<<m.flags
         <<",\"measurement\":"<<(m.measurement?"true":"false")<<",\"estimate\":"<<estimate
         <<",\"private_level\":"<<private_level<<",\"issue_cycle\":"<<issue<<",\"response_cycle\":"<<response
         <<",\"fallback\":"<<(fallback?"true":"false")<<",\"merged\":"<<(merged?"true":"false")
         <<",\"forwarded\":"<<(forwarded?"true":"false")<<'}';writer_.row(s.str());
    }
};
}
