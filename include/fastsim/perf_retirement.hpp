#pragma once

#include <cstdint>
#include <array>
#include <stdexcept>
#include "fastsim/retired_cache_fallback.hpp"

namespace fastsim {

// Architectural functional units, independent of the historical per-line
// perf classifier. Source fields are model estimates, never native PMU truth.
struct PerfRetirementCounts {
    std::uint64_t retired_macro_instructions=0, retired_uops=0;
    std::uint64_t perf_retired_loads=0, perf_retired_stores=0;
    std::uint64_t perf_l1d_load_hits=0, perf_l1d_load_misses=0, perf_l1d_load_fb_hits=0;
    std::uint64_t l2_hit_qualified=0, l2_hit_unknown=0, l2_miss_qualified=0, l2_miss_unknown=0, l3_hit_qualified=0, l3_hit_unknown=0, l3_miss_qualified=0, l3_miss_unknown=0;
    // V2 independent event qualifications, not an exclusive source partition.
    std::uint64_t l1_hit_qualified=0, l1_miss_qualified=0, l1_fb_qualified=0;
    std::uint64_t l1_hit_unknown=0, l1_miss_unknown=0, l1_fb_unknown=0;
    std::uint64_t classified_loads=0, unclassified_loads=0;
    std::uint64_t incomplete_macros=0, incomplete_uops=0;
    std::uint64_t mixed_source_loads=0, forwarded_loads=0;
    std::uint64_t scope_crossing_macros=0, warmup_spanning_macros=0;
    // Separate point estimates; the qualified/unknown evidence above is intact.
    std::uint64_t estimated_l1_misses=0, estimated_l2_misses=0, estimated_l3_misses=0;
    std::uint64_t estimated_miss_fallback_loads=0, estimated_merged_loads=0;
    std::array<std::uint64_t,3> spr_estimated_member{}, spr_estimated_nonmember{};
    std::uint64_t fallback_missing_source_loads=0, fallback_missing_source_l1_misses=0;
    std::uint64_t fallback_unknown_private_loads=0, fallback_unknown_private_l1_misses=0;
    std::uint64_t fallback_unknown_shared_loads=0, fallback_unknown_shared_l1_misses=0;
    std::uint64_t fallback_missing_shared_clock_loads=0, fallback_missing_shared_clock_l1_misses=0;
    std::uint64_t fallback_future_parent_loads=0, fallback_future_parent_l1_misses=0;
    std::uint64_t fallback_invalid_interval_loads=0, fallback_invalid_interval_l1_misses=0;
    std::uint64_t fallback_collision_loads=0, fallback_collision_l1_misses=0;
    std::uint64_t fallback_unclassified_loads=0, fallback_unclassified_l1_misses=0;
    PerfRetirementCounts& operator+=(const PerfRetirementCounts& b) {
        for(unsigned l=0;l<3;++l) {
            spr_estimated_member[l]+=b.spr_estimated_member[l];
            spr_estimated_nonmember[l]+=b.spr_estimated_nonmember[l];
        }
#define FASTSIM_ADD_RETIREMENT(field) field+=b.field
        FASTSIM_ADD_RETIREMENT(retired_macro_instructions); FASTSIM_ADD_RETIREMENT(retired_uops);
        FASTSIM_ADD_RETIREMENT(perf_retired_loads); FASTSIM_ADD_RETIREMENT(perf_retired_stores);
        FASTSIM_ADD_RETIREMENT(perf_l1d_load_hits); FASTSIM_ADD_RETIREMENT(perf_l1d_load_misses);
        FASTSIM_ADD_RETIREMENT(perf_l1d_load_fb_hits); FASTSIM_ADD_RETIREMENT(classified_loads);
        FASTSIM_ADD_RETIREMENT(unclassified_loads); FASTSIM_ADD_RETIREMENT(incomplete_macros);
        FASTSIM_ADD_RETIREMENT(incomplete_uops); FASTSIM_ADD_RETIREMENT(mixed_source_loads);
        FASTSIM_ADD_RETIREMENT(forwarded_loads); FASTSIM_ADD_RETIREMENT(scope_crossing_macros);
        FASTSIM_ADD_RETIREMENT(warmup_spanning_macros);
        FASTSIM_ADD_RETIREMENT(estimated_l1_misses);
        FASTSIM_ADD_RETIREMENT(estimated_l2_misses);
        FASTSIM_ADD_RETIREMENT(estimated_l3_misses);
        FASTSIM_ADD_RETIREMENT(estimated_miss_fallback_loads);
        FASTSIM_ADD_RETIREMENT(estimated_merged_loads);
        FASTSIM_ADD_RETIREMENT(fallback_missing_source_loads); FASTSIM_ADD_RETIREMENT(fallback_missing_source_l1_misses);
        FASTSIM_ADD_RETIREMENT(fallback_unknown_private_loads); FASTSIM_ADD_RETIREMENT(fallback_unknown_private_l1_misses);
        FASTSIM_ADD_RETIREMENT(fallback_unknown_shared_loads); FASTSIM_ADD_RETIREMENT(fallback_unknown_shared_l1_misses);
        FASTSIM_ADD_RETIREMENT(fallback_missing_shared_clock_loads); FASTSIM_ADD_RETIREMENT(fallback_missing_shared_clock_l1_misses);
        FASTSIM_ADD_RETIREMENT(fallback_future_parent_loads); FASTSIM_ADD_RETIREMENT(fallback_future_parent_l1_misses);
        FASTSIM_ADD_RETIREMENT(fallback_invalid_interval_loads); FASTSIM_ADD_RETIREMENT(fallback_invalid_interval_l1_misses);
        FASTSIM_ADD_RETIREMENT(fallback_collision_loads); FASTSIM_ADD_RETIREMENT(fallback_collision_l1_misses);
        FASTSIM_ADD_RETIREMENT(fallback_unclassified_loads); FASTSIM_ADD_RETIREMENT(fallback_unclassified_l1_misses);

        FASTSIM_ADD_RETIREMENT(l1_hit_qualified); FASTSIM_ADD_RETIREMENT(l1_miss_qualified);
        FASTSIM_ADD_RETIREMENT(l1_fb_qualified); FASTSIM_ADD_RETIREMENT(l1_hit_unknown);
        FASTSIM_ADD_RETIREMENT(l1_miss_unknown); FASTSIM_ADD_RETIREMENT(l1_fb_unknown);
        FASTSIM_ADD_RETIREMENT(l2_hit_qualified);
        FASTSIM_ADD_RETIREMENT(l2_hit_unknown);
        FASTSIM_ADD_RETIREMENT(l2_miss_qualified);
        FASTSIM_ADD_RETIREMENT(l2_miss_unknown);
        FASTSIM_ADD_RETIREMENT(l3_hit_qualified);
        FASTSIM_ADD_RETIREMENT(l3_hit_unknown);
        FASTSIM_ADD_RETIREMENT(l3_miss_qualified);
        FASTSIM_ADD_RETIREMENT(l3_miss_unknown);
#undef FASTSIM_ADD_RETIREMENT
        return *this;
    }
};
struct PerfRetirementScopes {
    PerfRetirementCounts physical, user, kernel, workload_task, other_task;
    PerfRetirementScopes& operator+=(const PerfRetirementScopes& b) {
        physical+=b.physical; user+=b.user; kernel+=b.kernel;
        workload_task+=b.workload_task; other_task+=b.other_task;
        return *this;
    }
};

// Only accepted UOP prefixes enter this ledger, after trial rollback and final
// suffix selection. No trial cache replay mutates it. It retains a macro across
// chunks and across warmup reset; a missing last-micro never invents a retire.
class PerfRetirementLedger {
  public:
    enum Source : unsigned { Unknown=1, L1=2, Miss=4, FillBuffer=8, Forwarded=16,
        L2Hit=32, L2Miss=64, L3Hit=128, L3Miss=256, L2Unknown=512, L3Unknown=1024 };
    void accept(bool completes, bool read, bool write, bool kernel,
                bool owner_known, bool task, unsigned source,
                unsigned event_source=~0u, unsigned miss_estimate=~0u,
                bool estimate_fallback=false, bool estimated_merged=false, unsigned fallback_reasons=0,
                unsigned estimated_mask=~0u) {
        if (pending_uops_ && (kernel_!=kernel || owner_known_!=owner_known || task_!=task))
            scope_crossing_=true;
        kernel_=kernel; owner_known_=owner_known; task_=task;
        ++pending_uops_;++measured_pending_uops_;
        PerfRetirementCounts uop;
        uop.retired_uops=1;
        add_scopes(counts_,uop);
        read_|=read; write_|=write;
        if (read) {
            if (++read_members_>1) estimated_mask_|=2;
            source_|=source ? source : Unknown;
            // A pure complete forwarding descriptor proves local data.
            // Mixed/partial legacy descriptors still need explicit evidence.
            if(event_source==~0u) event_source=(source==Forwarded || source==(L1|Forwarded))
                ? (L1|Forwarded) : ((source&Forwarded) ? ((source&~L1)|Unknown) : source);
            event_source_|=event_source ? event_source : Unknown;
            if (miss_estimate==~0u) {
                miss_estimate=((event_source&Miss)?1u:0u)|
                    ((event_source&L2Miss)?2u:0u)|((event_source&L3Miss)?4u:0u);
                estimate_fallback=true;
            }
            miss_estimate_|=miss_estimate;
            estimated_mask_|=estimated_mask==~0u ?
                ((estimate_fallback || estimated_merged || (source&Forwarded)) ? 7u : 0u) :
                estimated_mask;
            estimate_fallback_|=estimate_fallback;
            fallback_reasons_|=fallback_reasons;
            estimated_merged_|=estimated_merged;
        }
        if (!completes) return;
        PerfRetirementCounts delta;
        delta.retired_macro_instructions=1;
        delta.perf_retired_loads=read_; delta.perf_retired_stores=write_;
        delta.scope_crossing_macros=scope_crossing_;
        delta.warmup_spanning_macros=warmup_spanning_;
        if (read_) {
            for(unsigned l=0;l<3;++l) {
                delta.spr_estimated_member[l]=bool(estimated_mask_&(1u<<l)) &&
                    bool(miss_estimate_&(1u<<l));
                delta.spr_estimated_nonmember[l]=bool(estimated_mask_&(1u<<l)) &&
                    !(miss_estimate_&(1u<<l));
            }
            delta.estimated_l1_misses=bool(miss_estimate_&1u);
            delta.estimated_l2_misses=bool(miss_estimate_&2u);
            delta.estimated_l3_misses=bool(miss_estimate_&4u);
            delta.estimated_miss_fallback_loads=estimate_fallback_;
            delta.estimated_merged_loads=estimated_merged_;
            delta.fallback_missing_source_loads=bool(fallback_reasons_&kRetiredFallbackMissingSource);
            delta.fallback_missing_source_l1_misses=delta.fallback_missing_source_loads && delta.estimated_l1_misses;
            delta.fallback_unknown_private_loads=bool(fallback_reasons_&kRetiredFallbackUnknownPrivate);
            delta.fallback_unknown_private_l1_misses=delta.fallback_unknown_private_loads && delta.estimated_l1_misses;
            delta.fallback_unknown_shared_loads=bool(fallback_reasons_&kRetiredFallbackUnknownShared);
            delta.fallback_unknown_shared_l1_misses=delta.fallback_unknown_shared_loads && delta.estimated_l1_misses;
            delta.fallback_missing_shared_clock_loads=bool(fallback_reasons_&kRetiredFallbackMissingSharedClock);
            delta.fallback_missing_shared_clock_l1_misses=delta.fallback_missing_shared_clock_loads && delta.estimated_l1_misses;
            delta.fallback_future_parent_loads=bool(fallback_reasons_&kRetiredFallbackFutureParent);
            delta.fallback_future_parent_l1_misses=delta.fallback_future_parent_loads && delta.estimated_l1_misses;
            delta.fallback_invalid_interval_loads=bool(fallback_reasons_&kRetiredFallbackInvalidInterval);
            delta.fallback_invalid_interval_l1_misses=delta.fallback_invalid_interval_loads && delta.estimated_l1_misses;
            delta.fallback_collision_loads=bool(fallback_reasons_&kRetiredFallbackCollision);
            delta.fallback_collision_l1_misses=delta.fallback_collision_loads && delta.estimated_l1_misses;
            delta.fallback_unclassified_loads=bool(fallback_reasons_&kRetiredFallbackUnclassified);
            delta.fallback_unclassified_l1_misses=delta.fallback_unclassified_loads && delta.estimated_l1_misses;

            delta.forwarded_loads=(source_&Forwarded)!=0;
            const bool unknown=event_source_&Unknown;
            delta.l1_hit_qualified=(event_source_&L1)!=0;
            delta.l1_miss_qualified=(event_source_&Miss)!=0;
            delta.l1_fb_qualified=(event_source_&FillBuffer)!=0;
            delta.l1_hit_unknown=unknown && !delta.l1_hit_qualified;
            delta.l1_miss_unknown=unknown && !delta.l1_miss_qualified;
            delta.l1_fb_unknown=unknown && !delta.l1_fb_qualified;
            delta.l2_hit_qualified=bool(event_source_&L2Hit);
            delta.l2_hit_unknown=bool(event_source_&L2Unknown) && !delta.l2_hit_qualified;
            delta.l2_miss_qualified=bool(event_source_&L2Miss);
            delta.l2_miss_unknown=bool(event_source_&L2Unknown) && !delta.l2_miss_qualified;
            delta.l3_hit_qualified=bool(event_source_&L3Hit);
            delta.l3_hit_unknown=bool(event_source_&L3Unknown) && !delta.l3_hit_qualified;
            delta.l3_miss_qualified=bool(event_source_&L3Miss);
            delta.l3_miss_unknown=bool(event_source_&L3Unknown) && !delta.l3_miss_qualified;
            const unsigned classes=source_&(L1|Miss|FillBuffer);
            delta.mixed_source_loads=classes && (classes&(classes-1));
            if ((source_&Unknown) || !classes) delta.unclassified_loads=1;
            else {
                delta.classified_loads=1;
                // Explicit scalar model policy: any true fragment miss wins;
                // otherwise a genuine modeled fill follower wins over L1.
                if(source_&Miss) delta.perf_l1d_load_misses=1;
                else if(source_&FillBuffer) delta.perf_l1d_load_fb_hits=1;
                else delta.perf_l1d_load_hits=1;
            }
        }
        add_scopes(counts_,delta);
        clearPending();
    }
    void abandonPreciseFaultPrefix() {
        if (!pending_uops_) throw std::runtime_error("precise fault origin lacks an open macro prefix");
        PerfRetirementCounts tail;
        tail.incomplete_macros=bool(measured_pending_uops_);tail.incomplete_uops=measured_pending_uops_;
        add_scopes(counts_,tail);clearPending();
    }
    void begin_measurement() {
        counts_={};measured_pending_uops_=0;
        warmup_spanning_=pending_uops_!=0;
    }
    PerfRetirementScopes snapshot() const {
        auto result=counts_;
        if(pending_uops_) {
            PerfRetirementCounts tail;
            tail.incomplete_macros=bool(measured_pending_uops_); tail.incomplete_uops=measured_pending_uops_;
            add_scopes(result,tail);
        }
        return result;
    }
  private:
    void clearPending() {
        pending_uops_=measured_pending_uops_=0;
        read_=write_=scope_crossing_=warmup_spanning_=false;source_=event_source_=0;
        miss_estimate_=estimated_mask_=read_members_=0;fallback_reasons_=0;estimate_fallback_=estimated_merged_=false;
    }
    void add_scopes(PerfRetirementScopes& scopes, const PerfRetirementCounts& delta) const {
        scopes.physical+=delta; (kernel_?scopes.kernel:scopes.user)+=delta;
        if(owner_known_) (task_?scopes.workload_task:scopes.other_task)+=delta;
    }
    PerfRetirementScopes counts_;
    std::uint64_t pending_uops_=0,measured_pending_uops_=0;
    unsigned source_=0, event_source_=0, miss_estimate_=0, fallback_reasons_=0;
    unsigned estimated_mask_=0;
    unsigned read_members_=0;
    bool estimate_fallback_=false, estimated_merged_=false;
    bool read_=false, write_=false, kernel_=false, owner_known_=false, task_=false;
    bool scope_crossing_=false, warmup_spanning_=false;
};
} // namespace fastsim
