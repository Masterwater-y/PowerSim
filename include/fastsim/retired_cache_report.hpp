#pragma once
#include <optional>
#include "fastsim/config.hpp"
#include "fastsim/types.hpp"
#include "fastsim/spr_retired_load_miss.hpp"
#include <ostream>
namespace fastsim {
inline void write_retired_cache_miss_event(std::ostream& out,std::uint64_t known,
    std::uint64_t unknown,std::uint64_t estimated,unsigned mask,
    const PerfRetirementCounts& r,bool runtime) {
 const unsigned level=mask==8 ? 0 : (mask==16 ? 1 : 2);
 out<<"{\"count\":"<<known<<",\"unknown\":"<<unknown
    <<",\"event\":209,\"umask\":"<<mask
    <<",\"estimate_available\":"<<(runtime?"true":"false")
    <<",\"estimated_count\":"<<estimated
    <<",\"estimate_policy\":\""<<spr_retired::Policy<<"\""
    <<",\"prediction_nonmember\":"<<(r.perf_retired_loads-estimated)
    <<",\"prediction_unknown\":0"
    <<",\"prediction_exact_count\":"<<(estimated-r.spr_estimated_member[level])
    <<",\"prediction_estimated_count\":"<<r.spr_estimated_member[level]
    <<",\"prediction_exact_nonmember\":"<<(r.perf_retired_loads-estimated-r.spr_estimated_nonmember[level])
    <<",\"prediction_estimated_nonmember\":"<<r.spr_estimated_nonmember[level]
    <<",\"estimate_fallback_loads\":"<<r.estimated_miss_fallback_loads
    <<",\"estimated_merged_loads\":"<<r.estimated_merged_loads
    <<",\"fallback_missing_source_loads\":"<<r.fallback_missing_source_loads
    <<",\"fallback_missing_source_l1_misses\":"<<r.fallback_missing_source_l1_misses
    <<",\"fallback_unknown_private_loads\":"<<r.fallback_unknown_private_loads
    <<",\"fallback_unknown_private_l1_misses\":"<<r.fallback_unknown_private_l1_misses
    <<",\"fallback_unknown_shared_loads\":"<<r.fallback_unknown_shared_loads
    <<",\"fallback_unknown_shared_l1_misses\":"<<r.fallback_unknown_shared_l1_misses
    <<",\"fallback_missing_shared_clock_loads\":"<<r.fallback_missing_shared_clock_loads
    <<",\"fallback_missing_shared_clock_l1_misses\":"<<r.fallback_missing_shared_clock_l1_misses
    <<",\"fallback_future_parent_loads\":"<<r.fallback_future_parent_loads
    <<",\"fallback_future_parent_l1_misses\":"<<r.fallback_future_parent_l1_misses
    <<",\"fallback_invalid_interval_loads\":"<<r.fallback_invalid_interval_loads
    <<",\"fallback_invalid_interval_l1_misses\":"<<r.fallback_invalid_interval_l1_misses
    <<",\"fallback_collision_loads\":"<<r.fallback_collision_loads
    <<",\"fallback_collision_l1_misses\":"<<r.fallback_collision_l1_misses
    <<",\"fallback_unclassified_loads\":"<<r.fallback_unclassified_loads
    <<",\"fallback_unclassified_l1_misses\":"<<r.fallback_unclassified_l1_misses<<'}';
}
inline void write_retired_cache_scope(std::ostream& out,const PerfRetirementCounts& r,bool runtime) {
 out<<"{\"retired_macro_instructions\":"<<r.retired_macro_instructions
    <<",\"retired_uops\":"<<r.retired_uops
    <<",\"incomplete_macros\":"<<r.incomplete_macros
    <<",\"warmup_spanning_macros\":"<<r.warmup_spanning_macros
    <<",\"scope_crossing_macros\":"<<r.scope_crossing_macros
    <<",\"events\":{";
 out<<"\"retired_loads\":{\"count\":"<<r.perf_retired_loads<<",\"unknown\":"<<0<<",\"event\":208,\"umask\":129,\"estimate_available\":"<<(runtime?"true":"false")<<"}";
 out<<",\"retired_stores\":{\"count\":"<<r.perf_retired_stores<<",\"unknown\":"<<0<<",\"event\":208,\"umask\":130,\"estimate_available\":"<<(runtime?"true":"false")<<"}";
 out<<",\"retired_l1_load_hits\":{\"count\":"<<r.l1_hit_qualified<<",\"unknown\":"<<r.l1_hit_unknown<<",\"event\":209,\"umask\":1,\"estimate_available\":"<<(runtime?"true":"false")<<"}";
 out<<",\"retired_l1_load_misses\":";
 write_retired_cache_miss_event(out,r.l1_miss_qualified,r.l1_miss_unknown,r.estimated_l1_misses,8,r,runtime);
 out<<",\"retired_load_fb_hits\":{\"count\":"<<r.l1_fb_qualified<<",\"unknown\":"<<r.l1_fb_unknown<<",\"event\":209,\"umask\":64,\"estimate_available\":"<<(runtime?"true":"false")<<"}";
 out<<",\"retired_l2_load_hits\":{\"count\":"<<r.l2_hit_qualified<<",\"unknown\":"<<r.l2_hit_unknown<<",\"event\":209,\"umask\":2,\"estimate_available\":"<<(runtime?"true":"false")<<"}";
 out<<",\"retired_l2_load_misses\":";
 write_retired_cache_miss_event(out,r.l2_miss_qualified,r.l2_miss_unknown,r.estimated_l2_misses,16,r,runtime);
 out<<",\"retired_l3_load_hits\":{\"count\":"<<r.l3_hit_qualified<<",\"unknown\":"<<r.l3_hit_unknown<<",\"event\":209,\"umask\":4,\"estimate_available\":"<<(runtime?"true":"false")<<"}";
 out<<",\"retired_l3_load_misses\":";
 write_retired_cache_miss_event(out,r.l3_miss_qualified,r.l3_miss_unknown,r.estimated_l3_misses,32,r,runtime);
 out<<"}}";
}
inline void write_retired_cache_report(std::ostream& out,const SimulationStats& stats,const SimulatorConfig& config) {
 const bool runtime=config.core_model=="interval_weave";
 const bool owner=stats.thread_state_enabled && stats.thread_state_accounting_complete &&
     stats.thread_state_accounting_contract=="task-oncpu-switch-entry-v1";
 PerfRetirementScopes sum;
 for(const auto& c:stats.cores)sum+=c.perf_retirement;
 const auto scopes=[&](const PerfRetirementScopes& r) {
  out<<'{';
  out<<"\"physical\":";write_retired_cache_scope(out,r.physical,runtime);
  out<<",\"user\":";write_retired_cache_scope(out,r.user,runtime);
  out<<",\"kernel\":";write_retired_cache_scope(out,r.kernel,runtime);
  out<<",\"workload_task\":";write_retired_cache_scope(out,r.workload_task,runtime&&owner);
  out<<",\"other_task\":";write_retired_cache_scope(out,r.other_task,runtime&&owner);
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
