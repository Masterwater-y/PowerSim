#pragma once
#include <optional>
#include "fastsim/config.hpp"
#include "fastsim/types.hpp"
#include <ostream>
#include <string>
namespace fastsim {
inline void write_retired_cache_miss_event(std::ostream& out,std::uint64_t known,
    std::uint64_t unknown,std::uint64_t estimated,unsigned mask,
    const PerfRetirementCounts& r,bool runtime,bool observed=true) {
 const auto number=[observed](std::uint64_t n){return observed?std::to_string(n):std::string("null");};
 out<<"{\"count\":"<<number(known)<<",\"unknown\":"<<number(unknown)
    <<",\"event\":209,\"umask\":"<<mask
    <<",\"estimate_available\":"<<(runtime?"true":"false")
    <<",\"estimated_count\":"<<number(estimated)
    <<",\"estimate_policy\":\"bounded-line-fill-v1\""
    <<",\"estimate_fallback_loads\":"<<number(r.estimated_miss_fallback_loads)
    <<",\"estimated_merged_loads\":"<<number(r.estimated_merged_loads)<<'}';
}
inline void write_retired_cache_scope(std::ostream& out,const PerfRetirementCounts& r,bool runtime,bool observed=true) {
 const auto number=[observed](std::uint64_t n){return observed?std::to_string(n):std::string("null");};
 out<<"{\"retired_macro_instructions\":"<<number(r.retired_macro_instructions)
    <<",\"retired_uops\":"<<number(r.retired_uops)
    <<",\"incomplete_macros\":"<<number(r.incomplete_macros)
    <<",\"warmup_spanning_macros\":"<<number(r.warmup_spanning_macros)
    <<",\"scope_crossing_macros\":"<<number(r.scope_crossing_macros)
    <<",\"events\":{";
 out<<"\"retired_loads\":{\"count\":"<<number(r.perf_retired_loads)<<",\"unknown\":"<<number(0)<<",\"event\":208,\"umask\":129,\"estimate_available\":"<<(runtime?"true":"false")<<"}";
 out<<",\"retired_stores\":{\"count\":"<<number(r.perf_retired_stores)<<",\"unknown\":"<<number(0)<<",\"event\":208,\"umask\":130,\"estimate_available\":"<<(runtime?"true":"false")<<"}";
 out<<",\"retired_l1_load_hits\":{\"count\":"<<number(r.l1_hit_qualified)<<",\"unknown\":"<<number(r.l1_hit_unknown)<<",\"event\":209,\"umask\":1,\"estimate_available\":"<<(runtime?"true":"false")<<"}";
 out<<",\"retired_l1_load_misses\":";
 write_retired_cache_miss_event(out,r.l1_miss_qualified,r.l1_miss_unknown,r.estimated_l1_misses,8,r,runtime,observed);
 out<<",\"retired_load_fb_hits\":{\"count\":"<<number(r.l1_fb_qualified)<<",\"unknown\":"<<number(r.l1_fb_unknown)<<",\"event\":209,\"umask\":64,\"estimate_available\":"<<(runtime?"true":"false")<<"}";
 out<<",\"retired_l2_load_hits\":{\"count\":"<<number(r.l2_hit_qualified)<<",\"unknown\":"<<number(r.l2_hit_unknown)<<",\"event\":209,\"umask\":2,\"estimate_available\":"<<(runtime?"true":"false")<<"}";
 out<<",\"retired_l2_load_misses\":";
 write_retired_cache_miss_event(out,r.l2_miss_qualified,r.l2_miss_unknown,r.estimated_l2_misses,16,r,runtime,observed);
 out<<",\"retired_l3_load_hits\":{\"count\":"<<number(r.l3_hit_qualified)<<",\"unknown\":"<<number(r.l3_hit_unknown)<<",\"event\":209,\"umask\":4,\"estimate_available\":"<<(runtime?"true":"false")<<"}";
 out<<",\"retired_l3_load_misses\":";
 write_retired_cache_miss_event(out,r.l3_miss_qualified,r.l3_miss_unknown,r.estimated_l3_misses,32,r,runtime,observed);
 out<<"}}";
}
inline void write_retired_cache_report(std::ostream& out,const SimulationStats& stats,const SimulatorConfig& config) {
 const bool runtime=config.core_model=="interval_weave" && !config.causal_runtime;
 const bool owner=stats.thread_state_enabled && stats.thread_state_accounting_complete &&
     stats.thread_state_accounting_contract=="task-oncpu-switch-entry-v1";
 PerfRetirementScopes sum;
 for(const auto& c:stats.cores)sum+=c.perf_retirement;
 const auto scopes=[&](const PerfRetirementScopes& r) {
  out<<'{';
  out<<"\"physical\":";write_retired_cache_scope(out,r.physical,runtime,!config.causal_runtime);
  out<<",\"user\":";write_retired_cache_scope(out,r.user,runtime,!config.causal_runtime);
  out<<",\"kernel\":";write_retired_cache_scope(out,r.kernel,runtime,!config.causal_runtime);
  out<<",\"workload_task\":";write_retired_cache_scope(out,r.workload_task,runtime&&owner,!config.causal_runtime);
  out<<",\"other_task\":";write_retired_cache_scope(out,r.other_task,runtime&&owner,!config.causal_runtime);
  out<<'}';
 };
 out<<"{\"schema\":\"retired-load-cache-report-v1\",\"hardware_equivalence\":\"unverified\","
      "\"source\":\"accepted-model-retirement-service-facts\",\"unit\":\"architectural-retired-macro-instruction\","
      "\"isa_membership\":\"unverified\",\"roi_policy\":\"source-macro-retirement-window\",\"scopes\":";
 scopes(sum);out<<",\"task_ids\":[";
 for(std::size_t i=0;i<stats.thread_state_task_ids.size();++i){if(i)out<<',';out<<stats.thread_state_task_ids[i];}
 out<<"],\"per_core\":[";
 for(std::size_t i=0;i<stats.cores.size();++i){if(i)out<<',';
  out<<"{\"core_id\":"<<i<<",\"scopes\":";scopes(stats.cores[i].perf_retirement);out<<'}';}
 out<<"]}";
}
}
