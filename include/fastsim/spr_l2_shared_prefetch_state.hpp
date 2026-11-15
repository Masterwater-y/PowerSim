#pragma once
#include "fastsim/spr_l2_ruby_prefetcher.hpp"
#include "fastsim/spr_controller_resources.hpp"
#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <queue>
#include <unordered_map>
#include <vector>

namespace fastsim {
// Copy together with SharedSystem transaction and core counters before replay.
struct SprL2SharedPrefetchState {
    struct Pending {
        std::uint32_t core;
        std::uint64_t line, issue, response;
        bool llc_source = false;
        bool consumed_by_demand = false;
        std::uint64_t ticket = 0;
        std::uint64_t parent_service = 0;
        bool invalidated = false;
    };
    struct DemandFill { std::uint32_t core; std::uint64_t line, response; };
    explicit SprL2SharedPrefetchState(std::uint32_t cores, std::size_t queue_limit,
                                   std::size_t inflight_per_core)
        : capacity(cores * inflight_per_core) {
        predictors.reserve(cores);
        pending.reserve(capacity);
        for (std::uint32_t core = 0; core < cores; ++core)
            predictors.emplace_back(queue_limit);
    }
    std::vector<SprL2RubyPrefetcher> predictors;
    SprTransactionId ids;
    SprControllerResources resources;
    std::vector<Pending> pending;
    std::vector<DemandFill> demand_fills;
    // Conservative cross-window availability of shared demand fills. Hash
    // collisions retain the later callback and may suppress opportunities.
    std::array<std::uint64_t,4096> source_ready{};
    void note_source_response(std::uint64_t line,std::uint64_t response) {
        auto& slot=source_ready[line % source_ready.size()];
        slot=std::max(slot,response);
    }
    std::size_t capacity;
    std::uint64_t frontier=0, issued=0, completed=0, cancelled_demand=0;
    std::uint64_t cancelled_state=0, dropped=0, trained=0, initial_pending=0;
    std::uint64_t clamped_events=0, clamped_cycles=0;
    std::uint64_t skipped_past_frontier=0;
    std::uint64_t unsupported_input=0, initial_queue_overflow=0;
    std::uint64_t cancelled_cross_core=0, cancelled_write=0, dirty_writebacks=0;
    std::uint64_t followed=0, llc_issued=0, dram_issued=0;
    std::uint64_t demand_fill_dropped=0;
    std::uint64_t source_waits=0, source_unresolved_dropped=0;
    std::uint64_t store_line_skipped=0;
    std::uint64_t suppressed=0;
    std::uint64_t resource_wait_cycles=0,resource_retries=0,shared_parent_follows=0;
    std::uint64_t unused_evictions=0;
    using StoreEdge=std::pair<std::uint64_t,std::uint64_t>;
    std::unordered_map<std::uint64_t,std::uint64_t> store_line_until;
    std::priority_queue<StoreEdge,std::vector<StoreEdge>,std::greater<StoreEdge>> store_edges;
    struct FrfcfsCheckpoint { std::uint64_t issued; std::size_t pending; };
    FrfcfsCheckpoint frfcfs_checkpoint() const { return {issued, pending.size()}; }
    // A demand-only DRAM replay cannot preserve prefetch reservations.
    bool frfcfs_requires_fallback(FrfcfsCheckpoint before) const {
        return before.pending != 0 || !pending.empty() || issued != before.issued;
    }
    void invalidate_demand_fill(std::uint32_t core, std::uint64_t line) {
        const auto before=demand_fills.size();
        demand_fills.erase(std::remove_if(demand_fills.begin(),demand_fills.end(),
            [core,line](const auto& fill) { return fill.core==core && fill.line==line; }),
            demand_fills.end());
        demand_fill_dropped += before-demand_fills.size();
    }
    // The accepted write wins ties with any not-yet-published prefetch.
    // Already reserved shared-service traffic is deliberately retained.
    void cancel_before_write(std::uint64_t line, std::uint32_t writer) {
        resources.invalidate(line);
        const auto before=demand_fills.size();
        demand_fills.erase(std::remove_if(demand_fills.begin(), demand_fills.end(),
            [line](const auto& fill) { return fill.line == line; }), demand_fills.end());
        demand_fill_dropped += before-demand_fills.size();
        for (auto it=pending.begin(); it!=pending.end();) {
            if (it->line!=line || it->invalidated) { ++it; continue; }
            if (it->core!=writer) ++cancelled_cross_core;
            ++cancelled_write;
            ++cancelled_demand;
            it->invalidated=true;
            ++it;
        }
    }
    // Store publication mutates shared cache/permissions. Later batches must
    // not service requests on the pre-publication side of this model edge.
    void note_store_publication(std::uint64_t line, std::uint32_t writer,
                                std::uint64_t reference_cycle) {
        cancel_before_write(line, writer);
        frontier = std::max(frontier, reference_cycle);
    }
    void note_local_store_publication(std::uint64_t line, std::uint32_t writer,
                                      std::uint64_t reference_cycle) {
        cancel_before_write(line,writer);
        if (reference_cycle<=frontier) return;
        auto& until=store_line_until[line];
        if (reference_cycle>until) {
            until=reference_cycle;
            store_edges.emplace(until,line);
        }
    }
    void expire_store_edges(std::uint64_t now) {
        while (!store_edges.empty() && store_edges.top().first<=now) {
            const auto [until,line]=store_edges.top(); store_edges.pop();
            const auto found=store_line_until.find(line);
            if (found!=store_line_until.end() && found->second==until)
                store_line_until.erase(found);
        }
    }
    void reset_counters() {
        issued=completed=cancelled_demand=cancelled_state=dropped=trained=0;
        clamped_events=clamped_cycles=0;
        skipped_past_frontier=0;
        unsupported_input=0; initial_queue_overflow=0;
        cancelled_cross_core=cancelled_write=dirty_writebacks=0;
        followed=llc_issued=dram_issued=0;
        demand_fill_dropped=0;
        source_waits=source_unresolved_dropped=0;
        store_line_skipped=0;
        suppressed=0;
        resource_wait_cycles=resource_retries=shared_parent_follows=0;
        unused_evictions=0;
        for (const auto& predictor : predictors) initial_queue_overflow+=predictor.overflow();
        initial_pending=pending.size();
    }
};
} // namespace fastsim
