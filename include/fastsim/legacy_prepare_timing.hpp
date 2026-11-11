#pragma once

#include <algorithm>
#include <array>
#include <cstdint>
#include <limits>
#include <vector>

#include "fastsim/spr_store_window.hpp"
#include "fastsim/parent_service.hpp"

namespace fastsim {

struct LegacyPrepareIdentity {
    std::uint64_t sequence;
    std::uint32_t ordinal;
    std::uint64_t line;
};
enum class LegacyPrepareRole { Direct, Dropped, Takeover, Prepared };
struct LegacyPrepareResult {
    LegacyPrepareRole role;
    std::uint64_t prepare, permission, send, response;
};
struct LegacyPrepareDiagnostics {
    std::uint64_t direct_at_own_eligible=0, direct_scan_blocked=0;
    std::uint64_t direct_scan_wait_cycles=0, direct_extra_events=0;
    std::uint64_t direct_extra_cycles=0, direct_saved_events=0, direct_saved_cycles=0;
    std::uint64_t window_requests=0, barrier_wait_events=0, barrier_wait_cycles=0;
    std::uint64_t barrier_marginal_events=0,barrier_marginal_cycles=0;
    std::uint64_t ordered_wait_events=0, ordered_wait_cycles=0, port_wait_events=0, port_wait_cycles=0;
    std::uint64_t capacity_wait_events=0, capacity_wait_cycles=0, blocker_lifetime_cycles=0, blocker_service_cycles=0;
    std::uint64_t inherited_barrier_events=0,inherited_barrier_cycles=0;
    std::array<std::uint64_t,6> direct_levels{}, blocker_levels{};
    std::array<std::uint64_t,7> blocker_paths{}, blocker_wait_by_path{};
    // Unknown, local no-access, locked, other nonregular, regular fallback.
    std::array<std::uint64_t,5> barrier_kinds{},barrier_wait_by_kind{},barrier_marginal_by_kind{},barrier_parent_roles{},barrier_wait_by_parent_role{};
    std::array<std::uint64_t,7> barrier_parent_paths{},barrier_wait_by_parent_path{};
    // These sums are weighted by blocked followers, not unique parent stores.
    std::array<std::uint64_t,7> barrier_parent_queue_by_path{},barrier_parent_response_by_path{},barrier_parent_service_by_path{};
    // Unknown/not prepared, direct, dropped, takeover, prepared.
    std::array<std::uint64_t,5> blocker_roles{}, blocker_wait_by_role{};
    std::array<std::uint64_t,6> blocker_wait_by_level{}, blocker_lifetime_buckets{};
    LegacyPrepareDiagnostics& operator+=(const LegacyPrepareDiagnostics& o) {
        direct_at_own_eligible+=o.direct_at_own_eligible; direct_scan_blocked+=o.direct_scan_blocked;
        direct_scan_wait_cycles+=o.direct_scan_wait_cycles; direct_extra_events+=o.direct_extra_events;
        direct_extra_cycles+=o.direct_extra_cycles;direct_saved_events+=o.direct_saved_events;direct_saved_cycles+=o.direct_saved_cycles;
        window_requests+=o.window_requests;barrier_wait_events+=o.barrier_wait_events;barrier_wait_cycles+=o.barrier_wait_cycles;
        barrier_marginal_events+=o.barrier_marginal_events;barrier_marginal_cycles+=o.barrier_marginal_cycles;
        ordered_wait_events+=o.ordered_wait_events;ordered_wait_cycles+=o.ordered_wait_cycles;
        port_wait_events+=o.port_wait_events;port_wait_cycles+=o.port_wait_cycles;
        capacity_wait_events+=o.capacity_wait_events;capacity_wait_cycles+=o.capacity_wait_cycles;
        blocker_lifetime_cycles+=o.blocker_lifetime_cycles;blocker_service_cycles+=o.blocker_service_cycles;
        inherited_barrier_events+=o.inherited_barrier_events;inherited_barrier_cycles+=o.inherited_barrier_cycles;
        for(std::size_t i=0;i<6;++i) {direct_levels[i]+=o.direct_levels[i];blocker_levels[i]+=o.blocker_levels[i];
            blocker_wait_by_level[i]+=o.blocker_wait_by_level[i];blocker_lifetime_buckets[i]+=o.blocker_lifetime_buckets[i];}
        for(std::size_t i=0;i<7;++i){blocker_paths[i]+=o.blocker_paths[i];blocker_wait_by_path[i]+=o.blocker_wait_by_path[i];
            barrier_parent_paths[i]+=o.barrier_parent_paths[i];barrier_wait_by_parent_path[i]+=o.barrier_wait_by_parent_path[i];
            barrier_parent_queue_by_path[i]+=o.barrier_parent_queue_by_path[i];barrier_parent_response_by_path[i]+=o.barrier_parent_response_by_path[i];
            barrier_parent_service_by_path[i]+=o.barrier_parent_service_by_path[i];}
        for(std::size_t i=0;i<5;++i){blocker_roles[i]+=o.blocker_roles[i];blocker_wait_by_role[i]+=o.blocker_wait_by_role[i];
            barrier_kinds[i]+=o.barrier_kinds[i];barrier_wait_by_kind[i]+=o.barrier_wait_by_kind[i];
            barrier_marginal_by_kind[i]+=o.barrier_marginal_by_kind[i];
            barrier_parent_roles[i]+=o.barrier_parent_roles[i];barrier_wait_by_parent_role[i]+=o.barrier_wait_by_parent_role[i];}
        return *this;
    }
    LegacyPrepareDiagnostics difference(const LegacyPrepareDiagnostics& o) const {
        LegacyPrepareDiagnostics d=*this;
#define DIFF_FIELD(name) d.name-=o.name
        DIFF_FIELD(direct_at_own_eligible);DIFF_FIELD(direct_scan_blocked);DIFF_FIELD(direct_scan_wait_cycles);
        DIFF_FIELD(direct_extra_events);DIFF_FIELD(direct_extra_cycles);DIFF_FIELD(direct_saved_events);DIFF_FIELD(direct_saved_cycles);
        DIFF_FIELD(window_requests);DIFF_FIELD(barrier_wait_events);DIFF_FIELD(barrier_wait_cycles);
        DIFF_FIELD(barrier_marginal_events);DIFF_FIELD(barrier_marginal_cycles);
        DIFF_FIELD(ordered_wait_events);DIFF_FIELD(ordered_wait_cycles);DIFF_FIELD(port_wait_events);DIFF_FIELD(port_wait_cycles);
        DIFF_FIELD(capacity_wait_events);DIFF_FIELD(capacity_wait_cycles);DIFF_FIELD(blocker_lifetime_cycles);DIFF_FIELD(blocker_service_cycles);
        DIFF_FIELD(inherited_barrier_events);DIFF_FIELD(inherited_barrier_cycles);
#undef DIFF_FIELD
        for(std::size_t i=0;i<6;++i){d.direct_levels[i]-=o.direct_levels[i];d.blocker_levels[i]-=o.blocker_levels[i];
            d.blocker_wait_by_level[i]-=o.blocker_wait_by_level[i];d.blocker_lifetime_buckets[i]-=o.blocker_lifetime_buckets[i];}
        for(std::size_t i=0;i<7;++i){d.blocker_paths[i]-=o.blocker_paths[i];d.blocker_wait_by_path[i]-=o.blocker_wait_by_path[i];
            d.barrier_parent_paths[i]-=o.barrier_parent_paths[i];d.barrier_wait_by_parent_path[i]-=o.barrier_wait_by_parent_path[i];
            d.barrier_parent_queue_by_path[i]-=o.barrier_parent_queue_by_path[i];d.barrier_parent_response_by_path[i]-=o.barrier_parent_response_by_path[i];
            d.barrier_parent_service_by_path[i]-=o.barrier_parent_service_by_path[i];}
        for(std::size_t i=0;i<5;++i){d.blocker_roles[i]-=o.blocker_roles[i];d.blocker_wait_by_role[i]-=o.blocker_wait_by_role[i];
            d.barrier_kinds[i]-=o.barrier_kinds[i];d.barrier_wait_by_kind[i]-=o.barrier_wait_by_kind[i];
            d.barrier_marginal_by_kind[i]-=o.barrier_marginal_by_kind[i];
            d.barrier_parent_roles[i]-=o.barrier_parent_roles[i];d.barrier_wait_by_parent_role[i]-=o.barrier_wait_by_parent_role[i];}
        return d;
    }
};
struct LegacyPrepareStats {
    std::uint64_t accepted=0, dropped=0, direct=0, takeover=0, prepared=0;
    LegacyPrepareDiagnostics diagnostics;
};

// Per-core timing approximation: the backing hierarchy is still replayed once
// by legacy. Copies form a trial state; only accepted feedback is committed.
class LegacyPrepareTiming {
public:
    explicit LegacyPrepareTiming(std::size_t credits=16, std::size_t entries=112)
        : credits_(credits), entries_(entries) { pending_.reserve(std::min(credits,entries)); }

    LegacyPrepareResult store(LegacyPrepareIdentity id, std::uint64_t eligible,
            std::uint64_t send, std::uint64_t replay_permission,
            std::uint64_t service, std::uint64_t publish) {
        pending_.erase(std::remove_if(pending_.begin(),pending_.end(),
            [eligible](const Pending& p){return p.response<=eligible;}),pending_.end());
        if(send<=eligible) {
            ++stats_.direct;
            return {LegacyPrepareRole::Direct,0,0,send,add(send,service)};
        }
        if(pending_.size()>=credits_ || pending_.size()>=entries_) {
            ++stats_.dropped;
            return {LegacyPrepareRole::Dropped,0,0,send,add(send,service)};
        }
        const auto permission=std::max(replay_permission,add(eligible,service));
        pending_.push_back({id,permission});
        ++stats_.accepted;
        if(send<permission) {
            ++stats_.takeover;
            return {LegacyPrepareRole::Takeover,eligible,permission,send,permission};
        }
        ++stats_.prepared;
        return {LegacyPrepareRole::Prepared,eligible,permission,send,add(send,publish)};
    }
    // The credit list contains service references and request-credit return
    // edges only. Permission/controller/data times have one owner in pool.
    LegacyPrepareResult store_unified(LegacyPrepareIdentity id,
            std::uint64_t eligible, std::uint64_t send,
            std::uint64_t replay_permission, std::uint64_t service,
            std::uint64_t publish, ParentServicePool::Facts facts,
            bool source_certified, bool apply, bool target_valid,
            ParentServicePool& pool, UnifiedParentCounters& counters,
            ParentServicePool::SourceReject source_reject =
                ParentServicePool::SourceReject::kAdmissionMismatch) {
        using P = ParentServicePool;
        credits_v2_.erase(std::remove_if(credits_v2_.begin(),credits_v2_.end(),
            [&](const Credit& credit) {
                if (credit.return_at > eligible) return false;
                pool.unpin(credit.service);
                ++counters.credit_releases;
                return true;
            }),credits_v2_.end());
        const auto incoming = target_valid ? pool.indexed_id(id.line) : P::none;
        const auto* transient = pool.get(incoming);
        if (apply && transient && transient->permission_time != P::Time::kUnknown &&
            (transient->action == P::Action::kFetchExclusive || transient->action == P::Action::kUpgrade) &&
            transient->admission <= eligible && transient->permission_ready > eligible) {
            const auto permission = transient->permission_ready;
            const auto response = send < permission ? permission : add(send,publish);
            pool.takeover(incoming,response,id.sequence,std::max(send,permission));
            ++counters.prepare_attaches;
            counters.store_takeovers += send < permission;
            if (send < permission) ++stats_.takeover;
            else ++stats_.prepared;
            return {send < permission ? LegacyPrepareRole::Takeover : LegacyPrepareRole::Prepared,
                eligible,permission,send,response};
        }
        // Immediate functional E/M after an unknown outstanding transaction
        // cannot certify that permission is already available.
        const bool no_transaction = facts.action == P::Action::kNone &&
            !(transient && transient->controller_ready > eligible);
        counters.prepare_no_transaction += no_transaction;
        counters.prepare_unknown += facts.action == P::Action::kUnknown;
        auto permission = std::max(replay_permission,add(eligible,service));
        LegacyPrepareRole role;
        std::uint64_t response;
        if (send <= eligible) {
            ++stats_.direct;
            role = LegacyPrepareRole::Direct;
            response = add(send,service);
        } else if (apply && no_transaction) {
            ++stats_.prepared;
            role = LegacyPrepareRole::Prepared;
            permission = eligible;
            response = add(send,publish);
        } else if (credits_v2_.size() >= credits_ || credits_v2_.size() >= entries_) {
            ++stats_.dropped;
            role = LegacyPrepareRole::Dropped;
            response = add(send,service);
        } else {
            ++stats_.accepted;
            role = send < permission ? LegacyPrepareRole::Takeover : LegacyPrepareRole::Prepared;
            response = role == LegacyPrepareRole::Takeover ? permission : add(send,publish);
            if (role == LegacyPrepareRole::Takeover) ++stats_.takeover;
            else ++stats_.prepared;
        }
        const bool optional = role == LegacyPrepareRole::Takeover ||
                              (role == LegacyPrepareRole::Prepared && !(apply && no_transaction));
        // Mandatory traffic still exists when optional preparation drops.
        // No transaction is invented for E/M confirmation in apply mode.
        if (!(apply && no_transaction)) {
            const auto admission = optional ? eligible : send;
            const auto done = optional ? permission : response;
            auto parent = target_valid ? pool.indexed_id(id.line) : P::none;
            const auto* previous = pool.get(parent);
            const bool attach = apply && optional && previous &&
                previous->permission_time != P::Time::kUnknown &&
                previous->action == P::Action::kFetchExclusive &&
                previous->admission <= admission && previous->permission_ready > admission;
            if (attach) {
                permission = previous->permission_ready;
                response = send < permission ? permission : add(send,publish);
                ++counters.prepare_attaches;
            } else {
                const auto origin = optional ? P::Role::kPrepare : P::Role::kStore;
                auto decision = !target_valid ? P::SourceDecision::kTargetUnknown :
                    facts.action==P::Action::kUnknown ? P::SourceDecision::kActionUnknown :
                    !source_certified ? (source_reject==P::SourceReject::kCarried
                        ? P::SourceDecision::kCarried : P::SourceDecision::kAdmissionMismatch) :
                    !facts.lower_return_valid ? P::SourceDecision::kLowerMissing :
                    facts.lower_return<admission ? P::SourceDecision::kBeforeAdmission :
                    facts.lower_return>done ? P::SourceDecision::kAfterLegacyDone :
                    (facts.action!=P::Action::kFetchExclusive && facts.action!=P::Action::kUpgrade)
                        ? P::SourceDecision::kUnsupportedAction : P::SourceDecision::kSplit;
                parent = pool.create(id.line,id.sequence,admission,std::max(admission,done),
                    P::Time::kUnknown,false);
                if (!parent) {
                    counters.source_decision(origin,facts.action,P::SourceDecision::kHostCapacity);
                    throw std::runtime_error("unified prepare service pool exhausted");
                }
                ++counters.services;
                const auto quality = source_certified && target_valid &&
                    facts.action != P::Action::kUnknown
                    ? P::Time::kLegacyCoupled : P::Time::kUnknown;
                const auto data = facts.action == P::Action::kUpgrade ? admission : done;
                pool.set_service(parent,facts.action,
                    optional ? P::Role::kPrepare : P::Role::kStore,
                    data,done,done,quality);
                bool split=false;
                if (quality!=P::Time::kUnknown && facts.lower_return_valid &&
                    facts.lower_return>=admission && facts.lower_return<=done &&
                    (facts.action==P::Action::kFetchExclusive ||
                     facts.action==P::Action::kUpgrade)) {
                    const bool fill=facts.action==P::Action::kFetchExclusive;
                    pool.set_boundaries(parent,fill ? facts.lower_return : admission,
                        P::Time::kSelectedFixed,facts.lower_return,P::Time::kSelectedFixed,
                        facts.lower_return,P::Time::kSelectedFixed);
                    decision=pool.own_decision(fill,admission);
                    if (decision==P::SourceDecision::kSplit) {
                        pool.own(parent,fill,admission);
                        ++counters.tbe_allocations;
                        counters.fill_allocations+=fill;
                        ++counters.split_services;
                        split=true;
                    } else {
                        // Capacity cannot move an already selected lower
                        // transaction. Preserve mandatory timing and deny
                        // leader use rather than fabricate a hardware stall.
                        pool.set_boundaries(parent,data,P::Time::kUnknown,
                            done,P::Time::kUnknown,done,P::Time::kUnknown);
                        ++counters.resource_source_fallbacks;
                    }
                }
                if (pool.get(parent)->data_time == P::Time::kUnknown) ++counters.source_fallbacks;
                else ++counters.certified_services;
                // Lower completion is not yet a physically split fill edge.
                // This is the selected legacy controller-completion boundary.
                counters.legacy_coupled_services+=!split;
                counters.prepare_fetches += optional && facts.action == P::Action::kFetchExclusive;
                counters.prepare_upgrades += optional && facts.action == P::Action::kUpgrade;
                counters.source_decision(origin,facts.action,decision);
                pool.pin(parent);
                if (target_valid && !no_transaction) pool.index_service(parent,std::max(done,response));
            }
            if (parent) {
                // A prepared resident store writes at send; callback follows
                // its existing local service. An in-flight legacy callback
                // has no independently proved earlier write and stays coupled.
                const auto* service_record=pool.get(parent);
                const auto write=service_record->controller_time==P::Time::kSelectedFixed
                    ? std::max({send,service_record->data_ready,service_record->permission_ready})
                    : send>=permission ? send : response;
                pool.takeover(parent,response,id.sequence,write);
                counters.store_takeovers += optional && send < permission;
                if (optional) {
                    if (attach) pool.pin(parent);
                    credits_v2_.push_back({parent,apply && send < permission ? send : permission});
                } else pool.unpin(parent);
            }
        }
        counters.max_records = std::max(counters.max_records,
            static_cast<std::uint64_t>(pool.peak()));
        return {role,optional ? eligible : 0,
            optional || (apply && no_transaction) ? permission : 0,send,response};
    }
    const LegacyPrepareStats& stats() const { return stats_; }
    void observe_direct(const LegacyPrepareResult& result,std::uint64_t own_eligible,
            std::uint64_t scan_ready,std::uint64_t replay_permission,
            std::uint64_t publish,std::uint32_t level) {
        if(result.role!=LegacyPrepareRole::Direct)return;
        auto& d=stats_.diagnostics;
        if(result.send<=own_eligible)++d.direct_at_own_eligible;
        else {++d.direct_scan_blocked;d.direct_scan_wait_cycles+=scan_ready>own_eligible?scan_ready-own_eligible:0;}
        ++d.direct_levels[std::min<std::uint32_t>(level,5)];
        const auto old_response=std::max(replay_permission,add(result.send,publish));
        if(result.response>old_response){++d.direct_extra_events;d.direct_extra_cycles+=result.response-old_response;}
        else if(result.response<old_response){++d.direct_saved_events;d.direct_saved_cycles+=old_response-result.response;}
    }
    void observe_window(const SprStoreWindow::Observation& o) {
        auto& d=stats_.diagnostics;++d.window_requests;
        d.barrier_wait_events+=o.barrier_wait!=0;d.barrier_wait_cycles+=o.barrier_wait;
        if(o.barrier_wait) {
            const auto kind=std::min<std::uint32_t>(o.barrier.kind,4);
            ++d.barrier_kinds[kind];d.barrier_wait_by_kind[kind]+=o.barrier_wait;
            d.barrier_marginal_events+=o.barrier_marginal_wait!=0;
            d.barrier_marginal_cycles+=o.barrier_marginal_wait;
            d.barrier_marginal_by_kind[kind]+=o.barrier_marginal_wait;
            if(o.barrier.inherited) {
                ++d.inherited_barrier_events;d.inherited_barrier_cycles+=o.barrier_wait;
                const auto role=std::min<std::uint32_t>(o.barrier.parent.role,4);
                const auto path=std::min<std::uint32_t>(o.barrier.parent.path,6);
                ++d.barrier_parent_roles[role];d.barrier_wait_by_parent_role[role]+=o.barrier_wait;
                ++d.barrier_parent_paths[path];d.barrier_wait_by_parent_path[path]+=o.barrier_wait;
                if(o.barrier.parent.send>=o.barrier.parent.eligible)
                    d.barrier_parent_queue_by_path[path]+=o.barrier.parent.send-o.barrier.parent.eligible;
                if(o.barrier.parent.release>=o.barrier.parent.send)
                    d.barrier_parent_response_by_path[path]+=o.barrier.parent.release-o.barrier.parent.send;
                d.barrier_parent_service_by_path[path]+=o.barrier.parent.service;
            }
        }
        d.ordered_wait_events+=o.ordered_wait!=0;d.ordered_wait_cycles+=o.ordered_wait;
        d.port_wait_events+=o.port_wait!=0;d.port_wait_cycles+=o.port_wait;
        if(!o.capacity_wait)return;
        ++d.capacity_wait_events;d.capacity_wait_cycles+=o.capacity_wait;
        const auto role=std::min<std::uint32_t>(o.blocker.role,4),level=std::min<std::uint32_t>(o.blocker.level,5);
        ++d.blocker_roles[role];d.blocker_wait_by_role[role]+=o.capacity_wait;
        ++d.blocker_levels[level];d.blocker_wait_by_level[level]+=o.capacity_wait;
        const auto path=std::min<std::uint32_t>(o.blocker.path,6);
        ++d.blocker_paths[path];d.blocker_wait_by_path[path]+=o.capacity_wait;
        const auto lifetime=o.blocker_release>=o.blocker.send?o.blocker_release-o.blocker.send:0;
        d.blocker_lifetime_cycles+=lifetime;
        d.blocker_service_cycles+=o.blocker.service;
        const std::size_t bucket=lifetime<=4?0:lifetime<=16?1:lifetime<=64?2:lifetime<=256?3:lifetime<=1024?4:5;
        ++d.blocker_lifetime_buckets[bucket];
    }

private:
    struct Credit { ParentServicePool::Id service; std::uint64_t return_at; };
    struct Pending { LegacyPrepareIdentity id; std::uint64_t response; };
    static std::uint64_t add(std::uint64_t a,std::uint64_t b) {
        return b>std::numeric_limits<std::uint64_t>::max()-a
            ? std::numeric_limits<std::uint64_t>::max() : a+b;
    }
    std::size_t credits_,entries_;
    std::vector<Pending> pending_;
    std::vector<Credit> credits_v2_;
    LegacyPrepareStats stats_;
};
}
