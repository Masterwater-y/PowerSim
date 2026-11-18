#pragma once
#include "fastsim/types.hpp"
#include "fastsim/config.hpp"
#include "fastsim/spr_retired_load_miss.hpp"
#include <iomanip>
#include <sstream>

namespace fastsim {
inline void write_retired_service_report(std::ostream& destination,const SimulationStats& stats,
                                        const SimulatorConfig& config,const std::string& identity){
    std::ostringstream out;out<<std::setprecision(17);
    const bool enabled=config.retired_cache_service_model!="off";
    out<<"{\"schema\":\"retired-service-expectation-v1\",\"available\":"<<(enabled?"true":"false")
       <<",\"model\":\""<<config.retired_cache_service_model<<"\",\"hardware_equivalence\":\"unverified\""
       <<",\"semantic_policy\":\""<<spr_retired::Policy<<"\""
       <<",\"unit\":\"architectural-retired-macro-instruction\",\"scale\":"<<kServiceQ
       <<",\"input_identity\":\""<<identity<<"\",\"timing_feedback\":false,\"synthetic_requests\":false"
       <<",\"owner_coverage\":\""<<(stats.thread_state_enabled && stats.thread_state_accounting_complete?"complete":"unavailable")<<"\""
       <<",\"calibration\":\"analytic-priors; no evaluation-label fitting\""
       <<",\"bounds\":\"Frechet correlation bounds conditional on estimated marginals; not confidence intervals\"";
    if(!enabled){out<<'}';destination<<out.str();return;}
    if(stats.retired_service_expectation.size()!=stats.cores.size())throw std::logic_error("service expectation core coverage missing");
    const char* names[]={"physical","user","kernel","workload_task","other_task"};
    const char* events[]={"retired_l1_load_misses","retired_l2_load_misses","retired_l3_load_misses"};
    const auto raw=[](const PerfRetirementScopes& p){return std::array<const PerfRetirementCounts*,5>{&p.physical,&p.user,&p.kernel,&p.workload_task,&p.other_task};};
    const auto write_scopes=[&](const std::array<ServiceExpectationCounts,5>& c,const PerfRetirementScopes& population){
        const auto base=raw(population);out<<'{';for(unsigned s=0;s<5;++s){if(s)out<<',';
            if(c[s].macro_loads!=base[s]->perf_retired_loads)throw std::logic_error("service expectation eligible population mismatch");
            out<<'"'<<names[s]<<"\":{\"retired_macro_instructions\":"<<base[s]->retired_macro_instructions
               <<",\"retired_uops\":"<<base[s]->retired_uops<<",\"eligible_loads\":"<<c[s].macro_loads
               <<",\"unknown_source_loads\":"<<c[s].unknown_source_loads
               <<",\"clock_unavailable_loads\":"<<c[s].clock_unavailable_loads
               <<",\"correlation_uncertain_loads\":"<<c[s].correlation_uncertain_loads
               <<",\"fallback_loads\":"<<c[s].fallback_loads<<",\"events\":{";
            for(unsigned l=0;l<3;++l){if(l)out<<',';out<<'"'<<events[l]<<"\":{\"expected_q16\":"<<c[s].expected[l]
                <<",\"lower_q16\":"<<c[s].lower[l]<<",\"upper_q16\":"<<c[s].upper[l]
                <<",\"expected_count\":"<<static_cast<double>(c[s].expected[l])/kServiceQ<<'}';}out<<"}}";
        }out<<'}';
    };
    std::array<ServiceExpectationCounts,5> sum{};PerfRetirementScopes population;
    for(std::size_t i=0;i<stats.cores.size();++i){population+=stats.cores[i].perf_retirement;
        for(unsigned s=0;s<5;++s)sum[s]+=stats.retired_service_expectation[i].scopes[s];}
    out<<",\"scopes\":";write_scopes(sum,population);out<<",\"task_ids\":[";
    for(std::size_t i=0;i<stats.thread_state_task_ids.size();++i){if(i)out<<',';out<<stats.thread_state_task_ids[i];}
    out<<"],\"per_core\":[";
    for(std::size_t i=0;i<stats.cores.size();++i){if(i)out<<',';const auto& c=stats.retired_service_expectation[i];
        out<<"{\"core_id\":"<<i<<",\"observations\":"<<c.observations<<",\"interval_joins\":"<<c.interval_joins
           <<",\"writes\":"<<c.writes<<",\"warm_mixes\":"<<c.warm_mixes<<",\"pollution_mixes\":"<<c.pollution_mixes
           <<",\"future_intervals\":"<<c.future_intervals<<",\"reversed_clocks\":"<<c.reversed_clocks
           <<",\"history_replacements\":"<<c.history_replacements<<",\"scopes\":";
        write_scopes(c.scopes,stats.cores[i].perf_retirement);out<<'}';}
    out<<"]}";destination<<out.str();
}
}
