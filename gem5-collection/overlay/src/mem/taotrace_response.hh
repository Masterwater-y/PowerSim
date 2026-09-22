/*
 * Oracle-only Ruby response facts carried on a Request. These fields never
 * enter the functional trace and do not affect cache behavior.
 */

#ifndef __MEM_TAOTRACE_RESPONSE_HH__
#define __MEM_TAOTRACE_RESPONSE_HH__

#include <algorithm>
#include <array>
#include <cstdint>
#include <memory>
#include <unordered_map>
#include <unordered_set>

#include "base/extensible.hh"
#include "mem/request.hh"
#include "sim/cur_tick.hh"

namespace gem5
{

enum class TaoTraceNativeTerminalReason : uint32_t
{
    None = 0,
    PredicatedOff = 1u << 0,
    NoRequest = 1u << 1,
    StoreForward = 1u << 2,
    LocalAccess = 1u << 3,
    FailedStoreConditional = 1u << 4,
    ZeroSize = 1u << 5,
    PrefetchSkipped = 1u << 6,
};

enum class TaoTraceNativeCacheOutcome : uint32_t
{
    Hit = 0,
    TagMiss = 1,
    PermissionUpgrade = 2,
    MergedMiss = 3,
    RemoteSupply = 4,
};

struct TaoTraceNativeHierarchyFacts
{
    static constexpr unsigned Levels = 3;
    std::array<uint32_t, Levels> accesses{};
    std::array<uint32_t, Levels> hits{};
    std::array<uint32_t, Levels> tagMisses{};
    std::array<uint32_t, Levels> permissionUpgrades{};
    std::array<uint32_t, Levels> mergedMisses{};
    std::array<uint32_t, Levels> remoteSupplies{};
    uint32_t uniqueFills = 0;
    uint32_t rubyMemoryFetches = 0;
    uint32_t memoryReadTransactions = 0;

    void
    observe(unsigned level, TaoTraceNativeCacheOutcome outcome)
    {
        if (level >= Levels) return;
        ++accesses[level];
        switch (outcome) {
          case TaoTraceNativeCacheOutcome::Hit: ++hits[level]; break;
          case TaoTraceNativeCacheOutcome::TagMiss: ++tagMisses[level]; break;
          case TaoTraceNativeCacheOutcome::PermissionUpgrade:
            ++permissionUpgrades[level];
            break;
          case TaoTraceNativeCacheOutcome::MergedMiss:
            ++mergedMisses[level];
            break;
          case TaoTraceNativeCacheOutcome::RemoteSupply:
            ++remoteSupplies[level];
            break;
        }
    }

    void
    merge(const TaoTraceNativeHierarchyFacts &other)
    {
        for (unsigned level = 0; level < Levels; ++level) {
            accesses[level] += other.accesses[level];
            hits[level] += other.hits[level];
            tagMisses[level] += other.tagMisses[level];
            permissionUpgrades[level] += other.permissionUpgrades[level];
            mergedMisses[level] += other.mergedMisses[level];
            remoteSupplies[level] += other.remoteSupplies[level];
        }
        uniqueFills += other.uniqueFills;
        rubyMemoryFetches += other.rubyMemoryFetches;
        memoryReadTransactions += other.memoryReadTransactions;
    }
};

inline constexpr uint32_t
taoTraceNativeReasonMask(TaoTraceNativeTerminalReason reason)
{
    return static_cast<uint32_t>(reason);
}

class TaoTraceRubyResponseExtension
    : public Extension<Request, TaoTraceRubyResponseExtension>
{
  public:
    uint32_t admissions = 0;
    uint32_t aliasedAdmissions = 0;
    uint32_t hierarchyRequests = 0;
    uint32_t responses = 0;
    uint32_t externalHits = 0;
    uint32_t externalMisses = 0;
    uint32_t coalesced = 0;
    uint64_t responderMachineMask = 0;
    uint32_t responderMachineUnknown = 0;
    bool hasAdmissionTick = false;
    uint64_t firstAdmissionTick = 0;
    uint64_t lastAdmissionTick = 0;
    bool hasResponseTick = false;
    uint64_t lastResponseTick = 0;
    bool issuanceClosed = false;
    bool terminalNoRuby = false;
    uint32_t terminalReasonMask = 0;
    TaoTraceNativeHierarchyFacts hierarchy;

    void
    admit(bool aliased)
    {
        const uint64_t tick = uint64_t(curTick());
        ++admissions;
        if (aliased) ++aliasedAdmissions;
        if (!hasAdmissionTick) {
            hasAdmissionTick = true;
            firstAdmissionTick = tick;
            lastAdmissionTick = tick;
        } else {
            firstAdmissionTick = std::min(firstAdmissionTick, tick);
            lastAdmissionTick = std::max(lastAdmissionTick, tick);
        }
    }

    void
    observe(bool external_hit, bool was_coalesced, int machine)
    {
        const uint64_t tick = uint64_t(curTick());
        ++responses;
        if (external_hit) {
            ++externalMisses;
        } else {
            ++externalHits;
        }
        if (was_coalesced) ++coalesced;
        if (machine >= 0 && machine < 64) {
            responderMachineMask |= uint64_t(1) << unsigned(machine);
        } else {
            ++responderMachineUnknown;
        }
        hasResponseTick = true;
        lastResponseTick = std::max(lastResponseTick, tick);
    }

    void
    merge(const TaoTraceRubyResponseExtension &other)
    {
        admissions += other.admissions;
        aliasedAdmissions += other.aliasedAdmissions;
        hierarchyRequests += other.hierarchyRequests;
        responses += other.responses;
        externalHits += other.externalHits;
        externalMisses += other.externalMisses;
        coalesced += other.coalesced;
        responderMachineMask |= other.responderMachineMask;
        responderMachineUnknown += other.responderMachineUnknown;
        if (other.hasAdmissionTick) {
            if (!hasAdmissionTick) {
                hasAdmissionTick = true;
                firstAdmissionTick = other.firstAdmissionTick;
                lastAdmissionTick = other.lastAdmissionTick;
            } else {
                firstAdmissionTick = std::min(
                    firstAdmissionTick, other.firstAdmissionTick);
                lastAdmissionTick = std::max(
                    lastAdmissionTick, other.lastAdmissionTick);
            }
        }
        if (other.hasResponseTick) {
            hasResponseTick = true;
            lastResponseTick = std::max(
                lastResponseTick, other.lastResponseTick);
        }
        issuanceClosed = issuanceClosed || other.issuanceClosed;
        terminalNoRuby = terminalNoRuby || other.terminalNoRuby;
        terminalReasonMask |= other.terminalReasonMask;
        hierarchy.merge(other.hierarchy);
    }

    std::unique_ptr<ExtensionBase>
    clone() const override
    {
        return std::make_unique<TaoTraceRubyResponseExtension>(*this);
    }
};

/**
 * Measurement-only join table for O3 request lifecycle facts.
 *
 * Ruby sees a Request before retirement, while TaoTrace decides at commit
 * whether the UOP belongs to the measured population.  Keeping this table
 * outside the functional trace lets the two observers join by
 * (ContextID, InstSeqNum) without changing timing or FST contents.  Tracking
 * is enabled only during the measurement interval and disabled per context
 * at its exact target boundary.  Existing identities remain live after that
 * boundary so committed stores can finish draining.
 */
class TaoTraceNativeAccessRegistry
{
  public:
    struct Snapshot
    {
        bool found = false;
        bool committed = false;
        bool squashed = false;
        uint32_t admissions = 0;
        uint32_t aliasedAdmissions = 0;
        uint32_t hierarchyRequests = 0;
        uint32_t responses = 0;
        uint32_t externalHits = 0;
        uint32_t externalMisses = 0;
        uint32_t coalesced = 0;
        uint64_t responderMachineMask = 0;
        uint32_t responderMachineUnknown = 0;
        bool hasAdmissionTick = false;
        uint64_t firstAdmissionTick = 0;
        uint64_t lastAdmissionTick = 0;
        bool hasResponseTick = false;
        uint64_t lastResponseTick = 0;
        bool issuanceClosed = false;
        bool terminalNoRuby = false;
        uint32_t terminalReasonMask = 0;
        TaoTraceNativeHierarchyFacts hierarchy;

        bool
        resolved() const
        {
            return found && responses == admissions &&
                (issuanceClosed || terminalNoRuby);
        }
    };

    static void
    prepareContext(uint32_t context_id)
    {
        records_.erase(context_id);
        enabled_contexts_.erase(context_id);
        pretracking_contexts_.insert(context_id);
    }

    static void
    enableContext(uint32_t context_id)
    {
        // Keep the finite set of requests still in flight or resident in the
        // ROB at the warmup/measurement boundary. Premeasurement retirement
        // removes older identities, so retaining this map does not import the
        // warmup population into the measured window.
        pretracking_contexts_.erase(context_id);
        enabled_contexts_.insert(context_id);
    }

    static void
    disableContext(uint32_t context_id)
    {
        enabled_contexts_.erase(context_id);
        pretracking_contexts_.erase(context_id);
    }

    static void
    noteAdmission(const RequestPtr &req, bool aliased)
    {
        auto extension = ensureExtension(req);
        if (extension) extension->admit(aliased);
        uint32_t context_id = 0;
        uint64_t seq_num = 0;
        if (!identity(req, context_id, seq_num) ||
            !shouldAdmit(context_id, seq_num)) {
            return;
        }
        auto &facts = records_[context_id][seq_num];
        facts.found = true;
        const uint64_t tick = uint64_t(curTick());
        ++facts.admissions;
        if (aliased) ++facts.aliasedAdmissions;
        if (!facts.hasAdmissionTick) {
            facts.hasAdmissionTick = true;
            facts.firstAdmissionTick = tick;
            facts.lastAdmissionTick = tick;
        } else {
            facts.firstAdmissionTick = std::min(
                facts.firstAdmissionTick, tick);
            facts.lastAdmissionTick = std::max(
                facts.lastAdmissionTick, tick);
        }
    }

    static void
    noteResponse(const RequestPtr &req, bool external_hit,
                 bool was_coalesced, int machine)
    {
        auto extension = ensureExtension(req);
        if (extension) {
            extension->observe(external_hit, was_coalesced, machine);
        }
        uint32_t context_id = 0;
        uint64_t seq_num = 0;
        if (!identity(req, context_id, seq_num) ||
            !shouldTrack(context_id, seq_num)) {
            return;
        }
        auto &facts = records_[context_id][seq_num];
        facts.found = true;
        const uint64_t tick = uint64_t(curTick());
        ++facts.responses;
        if (external_hit) {
            ++facts.externalMisses;
        } else {
            ++facts.externalHits;
        }
        if (was_coalesced) ++facts.coalesced;
        if (machine >= 0 && machine < 64) {
            facts.responderMachineMask |= uint64_t(1) << unsigned(machine);
        } else {
            ++facts.responderMachineUnknown;
        }
        facts.hasResponseTick = true;
        facts.lastResponseTick = std::max(facts.lastResponseTick, tick);
        if (facts.squashed && facts.resolved()) {
            erase(context_id, seq_num);
        }
    }

    static void
    noteHierarchyRequest(const RequestPtr &req)
    {
        auto extension = ensureExtension(req);
        if (extension) ++extension->hierarchyRequests;
        mutate(req, [](Snapshot &facts) { ++facts.hierarchyRequests; });
    }

    static void
    noteHierarchy(uint32_t context_id, uint64_t seq_num, unsigned level,
                  TaoTraceNativeCacheOutcome outcome)
    {
        mutate(context_id, seq_num, [level, outcome](Snapshot &facts) {
            facts.hierarchy.observe(level, outcome);
        });
    }

    static void
    noteUniqueFill(uint32_t context_id, uint64_t seq_num)
    {
        mutate(context_id, seq_num, [](Snapshot &facts) {
            ++facts.hierarchy.uniqueFills;
        });
    }

    static void
    noteRubyMemoryFetch(uint32_t context_id, uint64_t seq_num)
    {
        mutate(context_id, seq_num, [](Snapshot &facts) {
            ++facts.hierarchy.rubyMemoryFetches;
        });
    }

    static void
    noteMemoryReadTransaction(uint32_t context_id, uint64_t seq_num)
    {
        mutate(context_id, seq_num, [](Snapshot &facts) {
            ++facts.hierarchy.memoryReadTransactions;
        });
    }

    static void
    noteIssuanceClosed(const RequestPtr &req)
    {
        auto extension = ensureExtension(req);
        if (extension) extension->issuanceClosed = true;
        mutate(req, [](Snapshot &facts) { facts.issuanceClosed = true; });
    }

    static void
    noteTerminal(const RequestPtr &req, TaoTraceNativeTerminalReason reason)
    {
        auto extension = ensureExtension(req);
        if (extension) {
            extension->terminalNoRuby = true;
            extension->terminalReasonMask |= taoTraceNativeReasonMask(reason);
        }
        mutate(req, [reason](Snapshot &facts) {
            facts.terminalNoRuby = true;
            facts.terminalReasonMask |= taoTraceNativeReasonMask(reason);
        });
    }

    static void
    noteCommitted(uint32_t context_id, uint64_t seq_num)
    {
        auto &facts = records_[context_id][seq_num];
        facts.found = true;
        facts.committed = true;
    }

    static void
    noteSquashed(uint32_t context_id, uint64_t seq_num)
    {
        auto context = records_.find(context_id);
        if (context == records_.end()) return;
        auto entry = context->second.find(seq_num);
        if (entry == context->second.end()) return;
        entry->second.squashed = true;
        if (entry->second.resolved()) erase(context_id, seq_num);
    }

    static void
    noteObservedComplete(uint32_t context_id, uint64_t seq_num,
                         uint32_t hierarchy_requests, uint32_t responses,
                         uint32_t external_hits,
                         uint32_t external_misses, uint32_t coalesced,
                         uint64_t responder_machine_mask,
                         uint32_t responder_machine_unknown,
                         bool has_admission_tick,
                         uint64_t first_admission_tick,
                         uint64_t last_admission_tick,
                         bool has_response_tick,
                         uint64_t last_response_tick,
                         const TaoTraceNativeHierarchyFacts &hierarchy)
    {
        auto &facts = records_[context_id][seq_num];
        facts.found = true;
        // This path covers requests already in flight at the warmup/measure
        // boundary. Their Request extension proves that every physical
        // fragment completed even though registry tracking was not yet on.
        // Normal measured requests were already observed exactly once by the
        // registry.  Do not replace their instruction-wide hierarchy with the
        // extension carried by the last packet of a split access.  Import the
        // extension only for a request already in flight at measurement start.
        // Its admission can precede enableContext() while its response follows
        // it, so the registry may contain a response but no admission. Replace
        // (rather than merge) that partial snapshot with the Request
        // extension's complete lifecycle; merging would double-count the
        // post-boundary response.
        if (facts.admissions == 0) {
            facts.admissions = responses;
            facts.hierarchyRequests = hierarchy_requests;
            facts.responses = responses;
            facts.externalHits = external_hits;
            facts.externalMisses = external_misses;
            facts.coalesced = coalesced;
            facts.responderMachineMask = responder_machine_mask;
            facts.responderMachineUnknown = responder_machine_unknown;
            facts.hasAdmissionTick = has_admission_tick;
            facts.firstAdmissionTick = first_admission_tick;
            facts.lastAdmissionTick = last_admission_tick;
            facts.hasResponseTick = has_response_tick;
            facts.lastResponseTick = last_response_tick;
            facts.hierarchy = hierarchy;
        }
        facts.issuanceClosed = true;
    }

    static void
    noteObservedLifecycle(
        uint32_t context_id, uint64_t seq_num,
        const TaoTraceRubyResponseExtension &extension)
    {
        // A load may finish in Ruby (or through store forwarding/local
        // access) before the process-wide measurement marker, remain in a
        // different core's ROB, and retire just after the marker.  The
        // Request extension is the authoritative finite lifecycle ledger for
        // that exact boundary case.  Import only a complete extension and
        // only when registry tracking has no admission, so normal measured
        // requests cannot be counted twice.
        const bool complete = extension.terminalNoRuby ||
            (extension.issuanceClosed &&
             extension.responses == extension.admissions &&
             extension.responses != 0);
        if (!complete) return;
        auto &facts = records_[context_id][seq_num];
        facts.found = true;
        if (facts.admissions != 0 || facts.responses != 0 ||
            facts.terminalNoRuby) {
            return;
        }
        facts.admissions = extension.admissions;
        facts.aliasedAdmissions = extension.aliasedAdmissions;
        facts.hierarchyRequests = extension.hierarchyRequests;
        facts.responses = extension.responses;
        facts.externalHits = extension.externalHits;
        facts.externalMisses = extension.externalMisses;
        facts.coalesced = extension.coalesced;
        facts.responderMachineMask = extension.responderMachineMask;
        facts.responderMachineUnknown = extension.responderMachineUnknown;
        facts.hasAdmissionTick = extension.hasAdmissionTick;
        facts.firstAdmissionTick = extension.firstAdmissionTick;
        facts.lastAdmissionTick = extension.lastAdmissionTick;
        facts.hasResponseTick = extension.hasResponseTick;
        facts.lastResponseTick = extension.lastResponseTick;
        facts.issuanceClosed = extension.issuanceClosed;
        facts.terminalNoRuby = extension.terminalNoRuby;
        facts.terminalReasonMask = extension.terminalReasonMask;
        facts.hierarchy = extension.hierarchy;
    }

    static void
    noteTerminal(uint32_t context_id, uint64_t seq_num,
                 TaoTraceNativeTerminalReason reason)
    {
        auto &facts = records_[context_id][seq_num];
        facts.found = true;
        facts.terminalNoRuby = true;
        facts.terminalReasonMask |= taoTraceNativeReasonMask(reason);
    }

    static Snapshot
    snapshot(uint32_t context_id, uint64_t seq_num)
    {
        const auto context = records_.find(context_id);
        if (context == records_.end()) return {};
        const auto entry = context->second.find(seq_num);
        return entry == context->second.end() ? Snapshot{} : entry->second;
    }

    static void
    erase(uint32_t context_id, uint64_t seq_num)
    {
        auto context = records_.find(context_id);
        if (context == records_.end()) return;
        context->second.erase(seq_num);
        if (context->second.empty() &&
            enabled_contexts_.count(context_id) == 0) {
            records_.erase(context);
        }
    }

  private:
    using ContextRecords = std::unordered_map<uint64_t, Snapshot>;
    inline static std::unordered_map<uint32_t, ContextRecords> records_;
    inline static std::unordered_set<uint32_t> enabled_contexts_;
    inline static std::unordered_set<uint32_t> pretracking_contexts_;

    static bool
    identity(const RequestPtr &req, uint32_t &context_id, uint64_t &seq_num)
    {
        if (!req || !req->hasContextId() || !req->hasInstSeqNum()) {
            return false;
        }
        context_id = uint32_t(req->contextId());
        seq_num = uint64_t(req->getReqInstSeqNum());
        return true;
    }

    static bool
    shouldAdmit(uint32_t context_id, uint64_t seq_num)
    {
        return enabled_contexts_.count(context_id) != 0 ||
            pretracking_contexts_.count(context_id) != 0 ||
            shouldTrack(context_id, seq_num);
    }

    static bool
    shouldTrack(uint32_t context_id, uint64_t seq_num)
    {
        if (enabled_contexts_.count(context_id) != 0) return true;
        const auto context = records_.find(context_id);
        return context != records_.end() &&
            context->second.count(seq_num) != 0;
    }

    static std::shared_ptr<TaoTraceRubyResponseExtension>
    ensureExtension(const RequestPtr &req)
    {
        if (!req) return {};
        auto extension =
            req->getExtension<TaoTraceRubyResponseExtension>();
        if (!extension) {
            extension = std::make_shared<TaoTraceRubyResponseExtension>();
            req->setExtension(extension);
        }
        return extension;
    }

    template <class Callback>
    static void
    mutate(const RequestPtr &req, Callback callback)
    {
        uint32_t context_id = 0;
        uint64_t seq_num = 0;
        if (!identity(req, context_id, seq_num) ||
            !shouldTrack(context_id, seq_num)) {
            return;
        }
        mutate(context_id, seq_num, callback);
    }

    template <class Callback>
    static void
    mutate(uint32_t context_id, uint64_t seq_num, Callback callback)
    {
        if (!shouldTrack(context_id, seq_num)) return;
        auto &facts = records_[context_id][seq_num];
        facts.found = true;
        callback(facts);
        if (facts.squashed && facts.resolved()) {
            erase(context_id, seq_num);
        }
    }
};

/**
 * Exact CPL-window instruction-fetch ledger.
 *
 * Unlike gem5's reset/dump statistics, this registry is enabled and frozen by
 * each TaoTrace instance at its first CPL event and functional target.
 * Fetch supplies lifecycle events, so the accounting observes real O3 ITLB
 * and I-cache requests (including wrong-path requests) without changing the
 * functional trace or the timing state machine.
 */
class TaoTraceFrontendRegistry
{
  public:
    static constexpr unsigned StatusCount = 13;

    enum class Terminal : unsigned
    {
        TranslationSquash,
        TranslationFault,
        NoGoodAddress,
        RetryDiscard,
        Response,
        SquashedResponse,
        Count
    };

    struct Snapshot
    {
        bool found = false;
        uint64_t inflightAtStart = 0;
        uint64_t requestsStarted = 0;
        uint64_t userModeRequestsStarted = 0;
        uint64_t kernelModeRequestsStarted = 0;
        uint64_t invalidSameBlockRefetches = 0;
        uint64_t invalidNewBlockRequests = 0;
        uint64_t validBlockChanges = 0;
        uint64_t translationsCompleted = 0;
        uint64_t sendAttempts = 0;
        uint64_t requestsSent = 0;
        uint64_t sendRejects = 0;
        std::array<uint64_t, static_cast<unsigned>(Terminal::Count)>
            terminals{};
        uint64_t inflightAtEnd = 0;
        uint64_t squashEvents = 0;
        uint64_t squashEventsWithOutstanding = 0;
        std::array<uint64_t, StatusCount> statusCycles{};
        uint64_t requestToResponseTicks = 0;
        uint64_t requestToResponseSamples = 0;

        uint64_t
        terminalCount() const
        {
            uint64_t total = 0;
            for (uint64_t value : terminals) total += value;
            return total;
        }

        uint64_t
        statusCycleCount() const
        {
            uint64_t total = 0;
            for (uint64_t value : statusCycles) total += value;
            return total;
        }

        bool
        requestPopulationConserved() const
        {
            return inflightAtStart + requestsStarted ==
                terminalCount() + inflightAtEnd;
        }

        bool
        requestModeConserved() const
        {
            return requestsStarted ==
                userModeRequestsStarted + kernelModeRequestsStarted;
        }

        bool
        requestReasonConserved() const
        {
            return requestsStarted == invalidSameBlockRefetches +
                invalidNewBlockRequests + validBlockChanges;
        }

        bool
        sendAccountingConserved() const
        {
            return sendAttempts == requestsSent + sendRejects;
        }
    };

    static void
    prepareContext(uint32_t context_id)
    {
        auto &context = contexts_[context_id];
        context = ContextState{};
        context.pretracking = true;
    }

    static void
    enableContext(uint32_t context_id)
    {
        auto &context = contexts_[context_id];
        context.snapshot = Snapshot{};
        context.snapshot.found = true;
        context.pretracking = false;
        context.enabled = true;
        for (auto &entry : context.requests) {
            entry.second.counted = true;
            entry.second.startedInWindow = false;
            ++context.snapshot.inflightAtStart;
        }
    }

    static void
    disableContext(uint32_t context_id)
    {
        auto found = contexts_.find(context_id);
        if (found == contexts_.end()) return;
        auto &context = found->second;
        if (!context.enabled) return;
        context.snapshot.inflightAtEnd = 0;
        for (auto &entry : context.requests) {
            if (entry.second.counted) {
                ++context.snapshot.inflightAtEnd;
                entry.second.counted = false;
            }
        }
        context.enabled = false;
        context.pretracking = false;
    }

    static void
    noteRequestStart(const RequestPtr &req, bool in_user_mode,
                     bool buffer_valid, bool same_buffer_block,
                     uint64_t tick)
    {
        uint32_t context_id = 0;
        if (!identity(req, context_id)) return;
        auto found = contexts_.find(context_id);
        if (found == contexts_.end() ||
            (!found->second.pretracking && !found->second.enabled)) {
            return;
        }
        auto &context = found->second;
        RequestState request;
        request.startTick = tick;
        request.counted = context.enabled;
        request.startedInWindow = context.enabled;
        context.requests[req.get()] = request;
        if (!context.enabled) return;

        auto &snapshot = context.snapshot;
        ++snapshot.requestsStarted;
        if (in_user_mode) ++snapshot.userModeRequestsStarted;
        else ++snapshot.kernelModeRequestsStarted;
        if (!buffer_valid && same_buffer_block) {
            ++snapshot.invalidSameBlockRefetches;
        } else if (!buffer_valid) {
            ++snapshot.invalidNewBlockRequests;
        } else {
            ++snapshot.validBlockChanges;
        }
    }

    static void
    noteTranslationComplete(const RequestPtr &req)
    {
        mutateCounted(req, [](Snapshot &snapshot, RequestState &request) {
            if (request.translationCompleted) return;
            request.translationCompleted = true;
            ++snapshot.translationsCompleted;
        });
    }

    static void
    noteSendAttempt(const RequestPtr &req, bool accepted)
    {
        mutateCounted(req, [accepted](Snapshot &snapshot, RequestState &) {
            ++snapshot.sendAttempts;
            if (accepted) ++snapshot.requestsSent;
            else ++snapshot.sendRejects;
        });
    }

    static void
    noteSquash(uint32_t context_id, bool had_outstanding)
    {
        auto found = contexts_.find(context_id);
        if (found == contexts_.end() || !found->second.enabled) return;
        ++found->second.snapshot.squashEvents;
        if (had_outstanding) {
            ++found->second.snapshot.squashEventsWithOutstanding;
        }
    }

    static void
    noteStatusCycle(uint32_t context_id, unsigned status)
    {
        auto found = contexts_.find(context_id);
        if (found == contexts_.end() || !found->second.enabled ||
            status >= StatusCount) {
            return;
        }
        ++found->second.snapshot.statusCycles[status];
    }

    static void
    noteTerminal(const RequestPtr &req, Terminal terminal, uint64_t tick)
    {
        uint32_t context_id = 0;
        if (!identity(req, context_id)) return;
        auto context_it = contexts_.find(context_id);
        if (context_it == contexts_.end()) return;
        auto &context = context_it->second;
        auto request_it = context.requests.find(req.get());
        if (request_it == context.requests.end()) return;
        const RequestState request = request_it->second;
        if (context.enabled && request.counted) {
            ++context.snapshot.terminals[static_cast<unsigned>(terminal)];
            if (request.startedInWindow &&
                (terminal == Terminal::Response ||
                 terminal == Terminal::SquashedResponse) &&
                tick >= request.startTick) {
                context.snapshot.requestToResponseTicks +=
                    tick - request.startTick;
                ++context.snapshot.requestToResponseSamples;
            }
        }
        context.requests.erase(request_it);
    }

    static Snapshot
    snapshot(uint32_t context_id)
    {
        const auto found = contexts_.find(context_id);
        return found == contexts_.end() ? Snapshot{} : found->second.snapshot;
    }

  private:
    struct RequestState
    {
        uint64_t startTick = 0;
        bool counted = false;
        bool startedInWindow = false;
        bool translationCompleted = false;
    };

    struct ContextState
    {
        bool pretracking = false;
        bool enabled = false;
        Snapshot snapshot;
        std::unordered_map<const Request *, RequestState> requests;
    };

    inline static std::unordered_map<uint32_t, ContextState> contexts_;

    static bool
    identity(const RequestPtr &req, uint32_t &context_id)
    {
        if (!req || !req->hasContextId()) return false;
        context_id = uint32_t(req->contextId());
        return true;
    }

    template <class Callback>
    static void
    mutateCounted(const RequestPtr &req, Callback callback)
    {
        uint32_t context_id = 0;
        if (!identity(req, context_id)) return;
        auto context_it = contexts_.find(context_id);
        if (context_it == contexts_.end() || !context_it->second.enabled) {
            return;
        }
        auto &context = context_it->second;
        auto request_it = context.requests.find(req.get());
        if (request_it == context.requests.end() ||
            !request_it->second.counted) {
            return;
        }
        callback(context.snapshot, request_it->second);
    }
};

} // namespace gem5

#endif // __MEM_TAOTRACE_RESPONSE_HH__
