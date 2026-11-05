#pragma once
#include "fastsim/config.hpp"
#include "fastsim/types.hpp"
#include <array>
#include <ostream>
#include <string>

namespace fastsim {
struct PerfCacheBinding {
    const char* name;unsigned event,umask;const char* domain;const char* unit;
};
inline constexpr std::array<PerfCacheBinding,10> kPerfCacheBindings{{
    {"perf_retired_loads",208,129,"retirement","architectural-retired-macro-instruction"},
    {"perf_retired_stores",208,130,"retirement","architectural-retired-macro-instruction"},
    {"perf_l1d_load_hits",209,1,"retirement","architectural-retired-macro-instruction"},
    {"perf_l1d_load_misses",209,8,"retirement","architectural-retired-macro-instruction"},
    {"perf_l1d_load_fb_hits",209,64,"retirement","architectural-retired-macro-instruction"},
    {"perf_l2_accesses",36,255,"request","cache-request"},
    {"perf_l2_demand_misses",36,39,"request","cache-request"},
    {"perf_l2_writebacks",35,64,"request","cache-writeback-transaction"},
    {"perf_llc_accesses_core",46,79,"request","cache-request"},
    {"perf_llc_misses_core",46,65,"request","cache-request"}
}};
inline void write_perf_cache_metric(std::ostream& out,
    const PerfRetirementCounts& r,const PerfCacheCounts& q,const char* scope,
    const std::vector<std::uint32_t>& cores,const std::vector<std::uint64_t>& tasks,
    bool runtime,bool retirement_owner) {
    out<<"{\"schema\":\"fastsim-perf-cache-v2\",\"contract_id\":\""<<kPerfCacheContract
       <<"\",\"hardware_target\":{\"vendor\":\"GenuineIntel\",\"family\":6,\"model\":143,"
         "\"event_table_version\":\"1.40\",\"event_table_sha256\":\""<<kPerfCacheEventTableSha256
       <<"\"},\"evidence_source\":\"model-self-generated-events\",\"fst_input\":false,\"oracle_only\":false,"
         "\"measurement\":{\"scope\":\""<<scope
       <<"\",\"window_id\":\"accepted-model-window-v1\",\"core_ids\":[";
    for(std::size_t i=0;i<cores.size();++i){if(i)out<<',';out<<cores[i];}
    out<<"],\"task_ids\":[";
    // Task sets have evidence only for workload_task, not the unknown set of
    // all other tasks. Independent pairing must bind the source window.
    if(std::string(scope)=="workload_task")
        for(std::size_t i=0;i<tasks.size();++i){if(i)out<<',';out<<tasks[i];}
    out<<"],\"context_policy\":\"event-edge-context-v1\"},\"retired_macro_instructions\":"
       <<r.retired_macro_instructions<<",\"events\":{";
    const std::array<std::uint64_t,10> values{{r.perf_retired_loads,r.perf_retired_stores,
        r.l1_hit_qualified,r.l1_miss_qualified,r.l1_fb_qualified,
        q.l2_accesses,q.l2_demand_misses,q.l2_writebacks,q.llc_accesses,q.llc_misses}};
    const std::array<std::uint64_t,3> unknown{{r.l1_hit_unknown,r.l1_miss_unknown,r.l1_fb_unknown}};
    const bool task_scope=std::string(scope)=="workload_task" || std::string(scope)=="other_task";
    const bool retirement_context=runtime && !r.scope_crossing_macros && (!task_scope || retirement_owner);
    for(std::size_t i=0;i<kPerfCacheBindings.size();++i) {
        if(i)out<<',';const auto& b=kPerfCacheBindings[i];
        out<<'"'<<b.name<<"\":{\"event\":"<<b.event<<",\"umask\":"<<b.umask
           <<",\"unit\":\""<<b.unit<<"\",\"domain\":\""<<b.domain
           <<"\",\"diagnostic_value\":"<<values[i];
        // No target ISA eligibility metadata exists yet for ROM, REP,
        // software prefetch, masks and fault/restart. It cannot be inferred
        // from a successful functional macro join, including an empty ROI.
        if(i>=2 && i<=4)out<<",\"qualification_unknown_macros\":"<<unknown[i-2];
        // A self-generated model prediction is useful before complete target
        // qualification. Keep it separate from the strictly qualified value:
        // omitted and unclassified events contribute zero only to this point
        // estimate, never to a claim of complete coverage or hardware identity.
        out<<",\"estimated_value\":";
        const bool estimate_available=runtime && (i<5 || std::string(scope)=="physical");
        if(estimate_available)out<<values[i];else out<<"null";
        out<<",\"estimate_status\":\""<<(estimate_available?"approximate":"unavailable")
           <<"\",\"estimate_policy\":\"accepted-model-events-zero-unmodeled-v1\",\"assumptions\":["
             "\"Counts accepted model events in the source sequence window; event-clock ROI edges are approximate\"";
        if(!runtime)out<<",\"This core model does not provide the runtime event observer\"";
        if(i<5) {
            out<<",\"Architectural memory members stand in for target ISA eligibility; ROM, REP, masks and fault restart are unverified\"";
            if(i>=2)out<<",\"Only positively qualified L1 members contribute; unknown source membership contributes zero to this estimate\"";
            if(r.incomplete_macros || r.warmup_spanning_macros)
                out<<",\"Incomplete and warmup-spanning macros do not supply complete measured membership\"";
            if(!retirement_context)
                out<<",\"Retirement context or task ownership is incomplete for this scope\"";
        } else {
            if(std::string(scope)!="physical")out<<",\"Event-clock owner and privilege mapping are unavailable for this scope\"";
            out<<",\"Unmodeled wrongpath data requests and speculative code requests contribute zero\"";
            if(i!=6 && i!=7)out<<",\"Unmodeled software and hardware prefetch requests contribute zero\"";
            out<<",\"Unclassified requests and unqualified page table accesses are excluded\""
                 ",\"Unattributed event contexts are excluded from user, kernel and task scopes\""
                 ",\"Observed request merge and retry treatment is not hardware qualified\"";
            if(i==7)out<<",\"Only explicitly qualified L2 writeback transactions count; dirty evictions alone do not establish event direction\"";
            if(i>=8)out<<",\"Only observed local LLC lookups count; unknown lookups and remote supply alone do not establish LLC hit or miss\"";
        }
        out<<"],\"value\":null,\"hardware_validated\":false,\"coverage\":{";
        std::vector<const char*> missing;
        bool first=true;
        const auto cap=[&](const char* name,bool yes){
            if(!first)out<<',';first=false;out<<'"'<<name<<"\":"<<(yes?"true":"false");
            if(!yes)missing.push_back(name);
        };
        if(i<5) {
            cap("macro_units",runtime && r.incomplete_macros==0 && r.warmup_spanning_macros==0);
            cap("isa_membership",false);
            cap("retirement_context",retirement_context);
            // Source-window identity and first-micro entry are not yet
            // carried by the FST; publish counts only as diagnostics.
            cap("roi_edges",false);
            if(i>=2){cap("member_service_facts",runtime && unknown[i-2]==0);
                cap("source_qualification",runtime && unknown[i-2]==0);}
        } else if(i==7) {
            cap("writeback_observer",false);cap("writeback_direction_mapping",false);
            cap("event_context",false);cap("roi_edges",false);
        } else {
            cap("data_reads",runtime);cap("rfo",false);
            cap("code_reads",false);cap("speculative_code",false);cap("speculative_data",false);
            cap("merge_policy",false);cap("retry_policy",false);cap("event_context",false);cap("roi_edges",false);
            if(i!=6){cap("software_prefetch",false);cap("hardware_prefetch_l1_l2",false);}
            if(i>=8){cap("core_origin_filter",runtime);cap("llc_data_state_mapping",false);}
        }
        out<<"},\"missing_components\":[";
        for(std::size_t j=0;j<missing.size();++j){if(j)out<<',';out<<'"'<<missing[j]<<'"';}
        out<<"]}";
    }
    out<<"},\"diagnostic_requests\":{";
#define FASTSIM_PC_WRITE(x) out<<"\"" #x "\":"<<q.x
    FASTSIM_PC_WRITE(l2_merges);out<<',';FASTSIM_PC_WRITE(llc_merges);out<<',';
    FASTSIM_PC_WRITE(l2_unknown);out<<',';FASTSIM_PC_WRITE(llc_unknown);out<<',';
    FASTSIM_PC_WRITE(page_table_requests);out<<',';FASTSIM_PC_WRITE(l1_dirty_evictions);out<<',';
    FASTSIM_PC_WRITE(l2_dirty_evictions);out<<',';FASTSIM_PC_WRITE(coherence_writebacks);out<<',';
    FASTSIM_PC_WRITE(unknown_requests);out<<',';FASTSIM_PC_WRITE(unknown_context_requests);out<<',';
    FASTSIM_PC_WRITE(data_reads);out<<',';FASTSIM_PC_WRITE(rfo);out<<',';FASTSIM_PC_WRITE(code_reads);out<<',';
    FASTSIM_PC_WRITE(software_prefetch);out<<',';FASTSIM_PC_WRITE(hardware_prefetch_l1);out<<',';
    FASTSIM_PC_WRITE(hardware_prefetch_l2);out<<',';FASTSIM_PC_WRITE(hardware_prefetch_l3);
#undef FASTSIM_PC_WRITE
    out<<"}}";
}
inline void write_perf_cache_report(std::ostream& out,const SimulationStats& stats,
                                    const SimulatorConfig& config) {
    const bool runtime=config.core_model=="interval_weave";
    bool owner=runtime && stats.thread_state_enabled && stats.thread_state_accounting_complete &&
        stats.thread_state_accounting_contract=="task-oncpu-switch-entry-v1";
    PerfRetirementScopes retired;PerfCacheScopes requests;
    std::vector<std::uint32_t> cores;
    for(std::size_t i=0;i<stats.cores.size();++i) {
        retired+=stats.cores[i].perf_retirement;requests+=stats.cores[i].perf_cache;
        cores.push_back(static_cast<std::uint32_t>(i));
        owner=owner && stats.cores[i].perf_retirement.physical.scope_crossing_macros==0;
    }
    const auto scopes=[&](const PerfRetirementScopes& r,const PerfCacheScopes& q,
                          const std::vector<std::uint32_t>& core_ids) {
        out<<'{';bool first=true;
        const auto metric=[&](const char* name,const PerfRetirementCounts& a,const PerfCacheCounts& b){
            if(!first)out<<',';first=false;out<<'"'<<name<<"\":";
            write_perf_cache_metric(out,a,b,name,core_ids,stats.thread_state_task_ids,runtime,owner);
        };
        metric("physical",r.physical,q.physical);metric("user",r.user,q.user);metric("kernel",r.kernel,q.kernel);
        metric("workload_task",r.workload_task,q.workload_task);metric("other_task",r.other_task,q.other_task);
        out<<'}';
    };
    out<<"{\"schema\":\"fastsim-perf-cache-report-v2\",\"contract_id\":\""<<kPerfCacheContract
       <<"\",\"diagnostic_population\":\"accepted-model-request-lookups-source-sequence-window\","
         "\"request_context_status\":\"unavailable-no-event-clock-owner-map\",\"scopes\":";
    scopes(retired,requests,cores);
    out<<",\"per_core\":[";
    for(std::size_t i=0;i<stats.cores.size();++i){if(i)out<<',';
        out<<"{\"core_id\":"<<i<<",\"scopes\":";
        scopes(stats.cores[i].perf_retirement,stats.cores[i].perf_cache,
               {static_cast<std::uint32_t>(i)});out<<'}';}
    out<<"]}";
}
} // namespace fastsim
