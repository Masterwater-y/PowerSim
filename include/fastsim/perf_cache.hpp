#pragma once
#include <cstdint>

namespace fastsim {
// SPR V1.40 binding. Python checks this immutable binding against the dictionary.
inline constexpr const char* kPerfCacheContract = "spr-perf-cache-v2";
inline constexpr const char* kPerfCacheEventTableSha256 =
    "95caad89cbe4c96647a540897251e1998edc7eee8efb2dc84a5b13f7d94ce102";
// Local lookup facts are deliberately independent of the eventual responder.
enum class PerfCacheLookup { NotObserved, Hit, Miss, Merge, Unknown };
enum class PerfCacheRequestKind { DataRead, Rfo, CodeRead, SoftwarePrefetch,
    HardwarePrefetchL1, HardwarePrefetchL2, HardwarePrefetchL3,
    PageTableRead, L1DirtyEviction, L2DirtyEviction, CoherenceWriteback,
    QualifiedL2Writeback, Unknown };
struct PerfCacheContext {
    bool kernel=false, owner_known=false, workload_task=false, known=false;
};
struct PerfCacheRequest {
    PerfCacheRequestKind kind=PerfCacheRequestKind::Unknown;
    PerfCacheLookup l2=PerfCacheLookup::NotObserved, llc=PerfCacheLookup::NotObserved;
    PerfCacheContext context;
    std::uint32_t origin_core=0;
    std::uint64_t logical_request_id=0, fragment_id=0, attempt_id=0;
    std::uint64_t transaction_id=0, parent_transaction_id=0;
    bool accepted=true, in_window=true, core_origin=true;
    bool target_squashed=false, remote_supply=false;
};
struct PerfCacheCounts {
    std::uint64_t l2_accesses=0,l2_demand_misses=0,l2_writebacks=0;
    std::uint64_t llc_accesses=0,llc_misses=0;
    std::uint64_t l2_merges=0,llc_merges=0,l2_unknown=0,llc_unknown=0;
    std::uint64_t page_table_requests=0,l1_dirty_evictions=0,l2_dirty_evictions=0;
    std::uint64_t coherence_writebacks=0,unknown_requests=0,unknown_context_requests=0;
    std::uint64_t data_reads=0,rfo=0,code_reads=0,software_prefetch=0;
    std::uint64_t hardware_prefetch_l1=0,hardware_prefetch_l2=0,hardware_prefetch_l3=0;
    PerfCacheCounts& operator+=(const PerfCacheCounts& b) {
#define FASTSIM_PC_ADD(x) x+=b.x
        FASTSIM_PC_ADD(l2_accesses);FASTSIM_PC_ADD(l2_demand_misses);FASTSIM_PC_ADD(l2_writebacks);
        FASTSIM_PC_ADD(llc_accesses);FASTSIM_PC_ADD(llc_misses);FASTSIM_PC_ADD(l2_merges);FASTSIM_PC_ADD(llc_merges);
        FASTSIM_PC_ADD(l2_unknown);FASTSIM_PC_ADD(llc_unknown);FASTSIM_PC_ADD(page_table_requests);
        FASTSIM_PC_ADD(l1_dirty_evictions);FASTSIM_PC_ADD(l2_dirty_evictions);FASTSIM_PC_ADD(coherence_writebacks);
        FASTSIM_PC_ADD(unknown_requests);FASTSIM_PC_ADD(unknown_context_requests);
        FASTSIM_PC_ADD(data_reads);FASTSIM_PC_ADD(rfo);FASTSIM_PC_ADD(code_reads);FASTSIM_PC_ADD(software_prefetch);
        FASTSIM_PC_ADD(hardware_prefetch_l1);FASTSIM_PC_ADD(hardware_prefetch_l2);FASTSIM_PC_ADD(hardware_prefetch_l3);
#undef FASTSIM_PC_ADD
        return *this;
    }
    void observe(const PerfCacheRequest& f) {
        if(!f.accepted || !f.in_window) return;
        if(!f.context.known) ++unknown_context_requests;
        bool demand=false,reference=true,llc_reference=true;
        switch(f.kind) {
          case PerfCacheRequestKind::DataRead:++data_reads;demand=true;break;
          case PerfCacheRequestKind::Rfo:++rfo;demand=true;break;
          case PerfCacheRequestKind::CodeRead:++code_reads;demand=true;break;
          case PerfCacheRequestKind::SoftwarePrefetch:++software_prefetch;break;
          case PerfCacheRequestKind::HardwarePrefetchL1:++hardware_prefetch_l1;break;
          case PerfCacheRequestKind::HardwarePrefetchL2:++hardware_prefetch_l2;break;
          case PerfCacheRequestKind::HardwarePrefetchL3:++hardware_prefetch_l3;llc_reference=false;break;
          case PerfCacheRequestKind::PageTableRead:++page_table_requests;reference=false;break;
          case PerfCacheRequestKind::L1DirtyEviction:++l1_dirty_evictions;return;
          case PerfCacheRequestKind::L2DirtyEviction:++l2_dirty_evictions;return;
          case PerfCacheRequestKind::CoherenceWriteback:++coherence_writebacks;return;
          case PerfCacheRequestKind::QualifiedL2Writeback:++l2_writebacks;return;
          default:++unknown_requests;reference=false;break;
        }
        // PTE and unknown classes remain separate until target eligibility has
        // evidence. Merges record their actual layer; they are not true misses.
        if(!reference) return;
        if(f.l2==PerfCacheLookup::Hit || f.l2==PerfCacheLookup::Miss) {
            ++l2_accesses;if(demand && f.l2==PerfCacheLookup::Miss)++l2_demand_misses;
        } else if(f.l2==PerfCacheLookup::Merge)++l2_merges;
        else if(f.l2==PerfCacheLookup::Unknown)++l2_unknown;
        if(!f.core_origin || !llc_reference)return;
        if(f.llc==PerfCacheLookup::Hit || f.llc==PerfCacheLookup::Miss) {
            ++llc_accesses;if(f.llc==PerfCacheLookup::Miss)++llc_misses;
        } else if(f.llc==PerfCacheLookup::Merge)++llc_merges;
        else if(f.llc==PerfCacheLookup::Unknown)++llc_unknown;
    }
};
// Value semantics are essential: existing CoreCounters transaction snapshots
// roll these counters back with functional cache state. A target squash never
// calls rollback; responses do not count a second time or extend the ROI.
struct PerfCacheScopes {
    PerfCacheCounts physical,user,kernel,workload_task,other_task;
    void observe(const PerfCacheRequest& f) {
        physical.observe(f);
        if(f.context.known)(f.context.kernel?kernel:user).observe(f);
        if(f.context.known && f.context.owner_known)
            (f.context.workload_task?workload_task:other_task).observe(f);
    }
    PerfCacheScopes& operator+=(const PerfCacheScopes& b) {
        physical+=b.physical;user+=b.user;kernel+=b.kernel;
        workload_task+=b.workload_task;other_task+=b.other_task;return *this;
    }
};
} // namespace fastsim
