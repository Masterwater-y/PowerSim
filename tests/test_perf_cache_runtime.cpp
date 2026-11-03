#include "fastsim/perf_cache.hpp"
#include "fastsim/perf_retirement.hpp"
#include "fastsim/perf_cache_report.hpp"
#include <sstream>
#include <iostream>
#include <stdexcept>
using namespace fastsim;
static void check(bool yes,const char* why){if(!yes)throw std::runtime_error(why);}
// Inspect the actual serialized event object, rather than checking serializer
// constants or deriving the expected prediction with the production helper.
static std::string event_json(const std::string& json,const char* name) {
    const auto key=std::string("\"")+name+"\":{";
    const auto begin=json.find(key);
    check(begin!=std::string::npos,"event missing from serialized report");
    const auto object=begin+key.size()-1;
    unsigned depth=0;
    for(auto i=object;i<json.size();++i) {
        if(json[i]=='{')++depth;
        if(json[i]=='}' && --depth==0)return json.substr(object,i-object+1);
    }
    throw std::runtime_error("unterminated serialized event");
}
static void check_estimated(const std::string& report,const char* name,const char* count) {
    const auto event=event_json(report,name);
    check(event.find(std::string("\"estimated_value\":")+count)!=std::string::npos,
          "available runtime count must be published as an approximate prediction");
    check(event.find("\"estimate_status\":\"approximate\"")!=std::string::npos,
          "estimate must be labeled approximate");
    check(event.find("\"estimate_policy\":\"accepted-model-events-zero-unmodeled-v1\"")!=std::string::npos,
          "estimate policy must identify omitted modeled events");
    check(event.find("\"assumptions\":[\"")!=std::string::npos,"estimate needs explicit assumptions");
    check(event.find("\"missing_components\":[\"")!=std::string::npos,"estimate must retain missing coverage");
    check(event.find("\"value\":null")!=std::string::npos,"estimate cannot promote strict value");
    check(event.find("\"hardware_validated\":false")!=std::string::npos,
          "estimate availability does not certify hardware equivalence");
}
int main(int argc,char**){try{
    PerfCacheScopes counts;
    PerfCacheRequest f; f.kind=PerfCacheRequestKind::DataRead;
    f.l2=PerfCacheLookup::Miss; f.llc=PerfCacheLookup::Unknown;
    f.remote_supply=true;
    counts.observe(f);
    check(counts.physical.l2_demand_misses==1,"remote supply cannot cancel local L2 miss");
    check(counts.physical.llc_misses==0 && counts.physical.llc_unknown==1,"remote supply alone cannot classify LLC");
    f.llc=PerfCacheLookup::Miss;counts.observe(f);
    f.llc=PerfCacheLookup::Hit;counts.observe(f);
    check(counts.physical.llc_misses==1 && counts.physical.llc_accesses==2,"local LLC data lookup independent of supply");
    auto before=counts;
    f.accepted=false;counts.observe(f);
    check(counts.physical.l2_accesses==before.physical.l2_accesses,"backpressure rejected attempt not target event");
    f.accepted=true; f.in_window=false;counts.observe(f);
    check(counts.physical.l2_accesses==before.physical.l2_accesses,"response drain cannot extend window");
    f.in_window=true; f.target_squashed=true;counts.observe(f);
    check(counts.physical.l2_accesses==before.physical.l2_accesses+1,"accepted target wrongpath retained after squash");
    counts=before;check(counts.physical.l2_accesses==before.physical.l2_accesses,"model trial counters rollback by value");
    f.target_squashed=false;f.l2=PerfCacheLookup::Merge;f.llc=PerfCacheLookup::NotObserved;counts.observe(f);
    check(counts.physical.l2_merges==1 && counts.physical.llc_merges==0,"L2 merge cannot create LLC merge");
    f.l2=PerfCacheLookup::Miss;f.llc=PerfCacheLookup::Merge;counts.observe(f);
    check(counts.physical.llc_merges==1,"LLC merge still follows real local L2 miss");
    f.kind=PerfCacheRequestKind::CodeRead;f.llc=PerfCacheLookup::Miss;f.context={true,true,false,true};counts.observe(f);
    check(counts.kernel.l2_demand_misses==1 && counts.other_task.l2_demand_misses==1,"count at event context independent of origin task");
    f.kind=PerfCacheRequestKind::Rfo;counts.observe(f);
    check(counts.kernel.l2_demand_misses==2,"RFO qualifies as demand");
    f.kind=PerfCacheRequestKind::SoftwarePrefetch;counts.observe(f);
    check(counts.kernel.l2_demand_misses==2 && counts.kernel.l2_accesses==3,"software prefetch not demand");
    f.kind=PerfCacheRequestKind::HardwarePrefetchL3;counts.observe(f);
    check(counts.kernel.llc_accesses==3,"L3-targeted hardware prefetch excluded from core LLC mask");
    f.kind=PerfCacheRequestKind::PageTableRead;counts.observe(f);
    check(counts.kernel.page_table_requests==1 && counts.kernel.l2_demand_misses==2,"PTE mapping is not invented");
    f.kind=PerfCacheRequestKind::L2DirtyEviction;counts.observe(f);
    check(counts.kernel.l2_dirty_evictions==1 && counts.kernel.l2_writebacks==0,"dirty eviction is not certified L2 WB direction");
    PerfRetirementLedger ledger;
    ledger.accept(false,true,false,false,true,true,PerfRetirementLedger::L1);
    ledger.accept(true,true,false,false,true,true,PerfRetirementLedger::Miss);
    auto c=ledger.snapshot().physical;
    check(c.l1_hit_qualified==1 && c.l1_miss_qualified==1 && c.l1_fb_qualified==0,"mixed sources independently qualify hardware events");
    ledger.accept(true,true,false,false,true,true,PerfRetirementLedger::Unknown|PerfRetirementLedger::Miss);
    c=ledger.snapshot().physical;
    check(c.l1_miss_qualified==2 && c.l1_miss_unknown==0 && c.l1_hit_unknown==1,"known positive qualification survives unrelated unknown member");
    ledger.accept(true,true,false,false,true,true,PerfRetirementLedger::L1|PerfRetirementLedger::Forwarded);
    c=ledger.snapshot().physical;
    check(c.l1_hit_qualified==2 && c.l1_hit_unknown==1 && c.l1_fb_qualified==0,
          "complete forwarding qualifies local data without inventing an FB hit");
    SimulationStats stats; stats.cores.resize(1);stats.cores[0].perf_cache=counts;
    stats.cores[0].perf_retirement=ledger.snapshot();SimulatorConfig cfg;cfg.core_model="interval_weave";
    std::ostringstream json;write_perf_cache_report(json,stats,cfg);
    check(json.str().find("fastsim-perf-cache-v2")!=std::string::npos,"v2 serializer");
    check(json.str().find("\"value\":null")!=std::string::npos,"incomplete events serialized null");
    // The ledger contains two miss-qualified macros; unknown hit membership
    // must remain explicit and must not suppress that separate miss estimate.
    check_estimated(json.str(),"perf_l1d_load_misses","2");
    const auto hit=event_json(json.str(),"perf_l1d_load_hits");
    check(hit.find("\"qualification_unknown_macros\":1")!=std::string::npos,
          "unknown retirement members cannot silently become known zeroes");
    check(hit.find("source_qualification")!=std::string::npos,
          "unknown L1 membership needs an explicit estimate limitation");
    PerfCacheScopes remote;PerfCacheRequest remote_read;
    remote_read.kind=PerfCacheRequestKind::DataRead;
    remote_read.l2=PerfCacheLookup::Miss;remote_read.llc=PerfCacheLookup::Unknown;
    remote_read.remote_supply=true;remote.observe(remote_read);
    std::ostringstream remote_json;
    write_perf_cache_metric(remote_json,{},remote.physical,"physical",{0},{},true,false);
    check_estimated(remote_json.str(),"perf_l2_demand_misses","1");
    check_estimated(remote_json.str(),"perf_llc_misses_core","0");
    check(event_json(remote_json.str(),"perf_llc_misses_core").find("llc_data_state_mapping")!=std::string::npos,
          "unclassified remote lookup cannot be reported as a qualified LLC hit");
    check(event_json(remote_json.str(),"perf_l2_demand_misses").find("wrongpath data requests")!=std::string::npos,
          "unmodeled speculative demand must be disclosed");
    check(event_json(remote_json.str(),"perf_llc_misses_core").find("prefetch requests")!=std::string::npos,
          "unmodeled prefetch population must be disclosed");
    check(remote_json.str().find("\"fst_input\":false")!=std::string::npos,
          "native reference observations must not become model input");
    std::ostringstream unattributed;
    write_perf_cache_metric(unattributed,{},remote.workload_task,"workload_task",{0},{101},true,true);
    const auto task_request=event_json(unattributed.str(),"perf_l2_demand_misses");
    check(task_request.find("\"estimated_value\":null")!=std::string::npos &&
          task_request.find("\"estimate_status\":\"unavailable\"")!=std::string::npos,
          "unknown event context cannot publish a task zero prediction");
    std::ostringstream inactive;
    write_perf_cache_metric(inactive,{},remote.physical,"physical",{0},{},false,false);
    for(const auto& binding:kPerfCacheBindings) {
        const auto event=event_json(inactive.str(),binding.name);
        check(event.find("\"estimated_value\":null")!=std::string::npos,
              "unsupported runtime cannot publish a zero or stale estimate");
        check(event.find("\"estimate_status\":\"unavailable\"")!=std::string::npos,
              "unsupported runtime must explain unavailable prediction");
    }
    if(argc>1)std::cout<<json.str()<<'\n';
    else std::cout<<"perf cache runtime tests passed\n";
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
