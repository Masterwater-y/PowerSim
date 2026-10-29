#pragma once
// Oracle-only observer. Never serialized into functional FST input.
#include "../../taogen/shared/llc_audit_writer.hh"
#include <map>
#include <utility>
#include <set>

namespace gem5 { namespace llc_fill_audit {
struct Native { unsigned source=1025;std::uint64_t admission=0,response=0;bool resolved=false; };
class Audit {
    fastsim::llcaudit::Writer writer_;
    std::map<std::pair<int,std::uint64_t>,Native> pending_;
    std::set<int> begun_,ended_;
    bool complete_=true;
  public:
    Audit():writer_("native","FASTSIM_NATIVE_LLC_AUDIT"){}
    Audit(std::ostream& out,std::uint64_t stride,std::uint64_t cap):writer_(out,"native",stride,cap){}
    ~Audit(){writer_.finish(!begun_.empty()&&begun_==ended_&&complete_);}
    bool enabled()const{return writer_.enabled();}
    void begin(int core,std::uint64_t tick) {
        if(!enabled())return;
        std::ostringstream s;
        begun_.insert(core);
        s<<"{\"type\":\"core_begin\",\"core\":"<<core<<",\"tick\":"<<tick<<'}';writer_.row(s.str());
    }
    void measurement(int core,std::uint64_t ordinal,std::uint64_t tick) {
        if(!enabled())return;
        std::ostringstream s;
        s<<"{\"type\":\"measurement\",\"core\":"<<core<<",\"ordinal\":"<<ordinal<<",\"tick\":"<<tick<<'}';writer_.row(s.str());
    }
    void fill(std::uint64_t address,int role,int core,bool valid,int context,std::uint64_t sequence,std::uint64_t tick) {
        if(!writer_.selected(address>>6))return;
        std::ostringstream s;
        s<<"{\"type\":\"fill\",\"line\":"<<(address>>6)<<",\"role\":"<<role
         <<",\"core\":"<<core<<",\"identity_valid\":"<<(valid?"true":"false")
         <<",\"context\":"<<context<<",\"sequence\":"<<sequence<<",\"tick\":"<<tick<<'}';writer_.row(s.str());
    }
    void evict(std::uint64_t address,std::uint64_t tick) {
        if(!writer_.selected(address>>6))return;
        std::ostringstream s;
        s<<"{\"type\":\"evict\",\"line\":"<<(address>>6)<<",\"tick\":"<<tick<<'}';writer_.row(s.str());
    }
    void native(int core,std::uint64_t sequence,Native n) {
        if(!enabled())return;
        // At most a small commit batch can await FST emission. Refuse an
        // accidental lifetime history instead of silently dropping facts.
        if(pending_.size()>4096)throw std::runtime_error("LLC native audit pending overflow");
        pending_[{core,sequence}]=n;
    }
    void member(int core,int context,std::uint64_t sequence,std::uint64_t ordinal,
                std::uint64_t pc,std::uint64_t address,unsigned size,unsigned flags,
                bool measurement,std::uint64_t tick) {
        if(!enabled())return;
        const auto it=pending_.find({core,sequence});const Native n=it==pending_.end()?Native{}:it->second;
        if(it!=pending_.end())pending_.erase(it);
        if(!(flags&14)||!size||address>UINT64_MAX-(size-1))return;
        const auto last=(address+size-1)>>6;
        for(auto line=address>>6;line<=last;++line) {
            if(!writer_.selected(line))continue;
            std::ostringstream s;
            s<<"{\"type\":\"member\",\"core\":"<<core<<",\"context\":"<<context
             <<",\"sequence\":"<<sequence<<",\"ordinal\":"<<ordinal<<",\"pc\":"<<pc
             <<",\"line\":"<<line<<",\"split\":"<<(((address>>6)!=last)?"true":"false")<<",\"flags\":"<<flags<<",\"measurement\":"<<(measurement?"true":"false")
             <<",\"tick\":"<<tick<<",\"source\":"<<n.source
             <<",\"admission_tick\":"<<n.admission<<",\"response_tick\":"<<n.response
             <<",\"resolved\":"<<(n.resolved?"true":"false")<<'}';writer_.row(s.str());
        }
    }
    void squash(int core,std::uint64_t cutoff,std::uint64_t youngest,std::uint64_t tick) {
        if(!enabled()||youngest<=cutoff)return;
        std::ostringstream s;
        s<<"{\"type\":\"squash\",\"core\":"<<core<<",\"cutoff\":"<<cutoff
         <<",\"youngest\":"<<youngest<<",\"tick\":"<<tick<<'}';writer_.row(s.str());
    }
    void endCore(int core,std::uint64_t begin,std::uint64_t records,std::uint64_t tick,std::uint64_t hash=0,bool complete=true) {
        if(!enabled())return;
        std::ostringstream s;
        ended_.insert(core);complete_&=complete;
        s<<"{\"type\":\"core_end\",\"core\":"<<core<<",\"warmup_records\":"<<begin
         <<",\"records\":"<<records<<",\"hot_hash\":"<<hash<<",\"tick\":"<<tick<<'}';writer_.row(s.str());
    }
};
inline Audit& observer(){static Audit audit;return audit;}
}} // gem5::llc_fill_audit
