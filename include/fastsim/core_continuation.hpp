#pragma once
#include <cstdint>
#include <algorithm>
#include <array>
#include <map>
#include <limits>
#include <stdexcept>
#include <memory>
#include <optional>
#include <string>
#include <vector>
#include "fastsim/config.hpp"
#include "fastsim/pending_resources.hpp"
#include "fastsim/types.hpp"
#include "fastsim/op_traits.hpp"
#include "fastsim/retired_frontend_facts.hpp"
#include "fastsim/spr_store_buffer.hpp"
#include "fastsim/spr_core_resources.hpp"
#include "fastsim/spr_sequencer.hpp"

namespace fastsim::spr {

// Only functional dependencies and model parameters cross this boundary.
// Producer issue/completion/retire reservations deliberately have no field.
struct FunctionalCoreUop {
    std::uint64_t sequence = 0;
    // Owned copy of production functional metadata; sequence is the exact source ordinal.
    std::optional<fst::RetiredFrontendMacro> frontend;
    // Static, model-derived metadata only. No native grants/fusion outcomes.
    int canonical_op = -1;
    std::string iq_op_class;
    std::uint16_t candidate_ports = 0;
    unsigned integer_family = 0;
    std::uint8_t zero_idiom = 0;
    std::uint16_t destination_count = 0; // accounting only; physical free-list remains unsupported
    std::uint32_t issue_interval = 1;
    bool static_resources_complete = false;
    std::optional<std::uint64_t> model_allocation_group;

    std::vector<std::uint64_t> dependencies;
    // A producer sequence is a functional version. Keep class-sensitive
    // edges separate from the ordinary (class-independent) RAW set.
    std::vector<std::uint64_t> float_dependencies, unclassified_dependencies;
    bool float_add_destination = false;
    std::uint64_t functional_dispatch_floor = 0;
    std::uint32_t execution_latency = 1;
    IntervalFuPool fu_pool = IntervalFuPool::kInteger;
    std::uint32_t fu_occupancy_cycles = 1;
    bool memory_read = false;
    bool memory_write = false;
    // Split stores retain two functional members and one architectural SQ token.
    // A nonzero group is certified by the selected static source policy.
    std::uint64_t store_group = 0;
    bool store_data_member = false;
    std::uint64_t memory_address = 0;
    std::uint32_t memory_bytes = 0;
    StoreFacts store_facts{};
    std::uint64_t store_address = 0;
    std::vector<PendingOwner> memory_requests;
};
// Extracted from simulator.cpp SparseIssueResourceCalendar: the same issue,
// FU occupancy, load/store-port and writeback counts. Final scheduling calls
// reserve only after all dependencies are resolved. No producer reservation
// is imported, and old cycles are discarded once the frontier passes them.
class FinalIssueCalendar {
    static std::uint64_t checked_add(std::uint64_t a,std::uint64_t b) {
        if(b>std::numeric_limits<std::uint64_t>::max()-a) throw std::overflow_error("core issue calendar overflow");
        return a+b;
    }
    using Counts=std::map<std::uint64_t,std::uint32_t>;
    std::uint32_t issue_width_, writeback_width_, load_ports_, store_ports_;
    std::array<std::uint32_t,static_cast<std::size_t>(IntervalFuPool::kCount)> capacity_;
    Counts issue_, load_, store_, writeback_;
    std::array<Counts,static_cast<std::size_t>(IntervalFuPool::kCount)> fu_;
    static std::uint32_t count(const Counts& c,std::uint64_t t) { auto it=c.find(t); return it==c.end()?0:it->second; }
    bool available(const FunctionalCoreUop& u,std::uint64_t t) const {
        if(count(issue_,t)>=issue_width_ ||
           (u.memory_write && count(store_,t)>=store_ports_) ||
           (!u.memory_write && u.memory_read && count(load_,t)>=load_ports_)) return false;
        const auto pool=static_cast<std::size_t>(u.fu_pool);
        for(std::uint32_t i=0;i<std::max(1u,u.fu_occupancy_cycles);++i)
            if(count(fu_[pool],checked_add(t,i))>=capacity_[pool]) return false;
        return true;
    }
public:
    explicit FinalIssueCalendar(const SimulatorConfig& c):
        issue_width_(c.issue_width),writeback_width_(c.writeback_width),
        load_ports_(c.cache_load_ports),store_ports_(c.cache_store_ports),capacity_{
        c.integer_alu_units,c.integer_multiply_units,c.float_simple_units,c.float_complex_units,
        c.simd_units,c.predicate_units,c.memory_units,c.system_units} {}
    std::uint64_t earliest(const FunctionalCoreUop& u,std::uint64_t t) const {
        if(static_cast<std::size_t>(u.fu_pool)>=capacity_.size()) throw std::invalid_argument("invalid core FU pool");
        if(!issue_width_ || !capacity_[static_cast<std::size_t>(u.fu_pool)] ||
           (u.memory_write && !store_ports_) || (!u.memory_write && u.memory_read && !load_ports_))
            throw std::invalid_argument("core continuation zero issue/FU capacity");
        while(!available(u,t)) t=checked_add(t,1);
        return t;
    }
    void reserve(const FunctionalCoreUop& u,std::uint64_t t) {
        if(!available(u,t)) throw std::logic_error("core issue reservation not available");
        ++issue_[t];
        if(u.memory_write) ++store_[t]; else if(u.memory_read) ++load_[t];
        for(std::uint32_t i=0;i<std::max(1u,u.fu_occupancy_cycles);++i)
            ++fu_[static_cast<std::size_t>(u.fu_pool)][checked_add(t,i)];
    }
    bool writeback(std::uint64_t t) {
        if(!writeback_width_) throw std::invalid_argument("core continuation zero writeback width");
        if(count(writeback_,t)>=writeback_width_) return false;
        ++writeback_[t]; return true;
    }
    template<class Bound>
    std::uint64_t allocate_issue(const Bound& bound,std::uint64_t cycle) {
        FunctionalCoreUop u;
        u.fu_pool=bound.fu_pool;u.fu_occupancy_cycles=bound.fu_occupancy_cycles;
        u.memory_read=bound.memory_read;u.memory_write=bound.memory_write;
        cycle=earliest(u,cycle);reserve(u,cycle);return cycle;
    }
    std::uint64_t allocate_writeback(std::uint64_t cycle) {
        while(!writeback(cycle)) cycle=checked_add(cycle,1);
        return cycle;
    }
    void discard_before(std::uint64_t t) {
        auto discard=[t](Counts& c){c.erase(c.begin(),c.lower_bound(t));};
        discard(issue_);discard(load_);discard(store_);discard(writeback_);
        for(auto& c:fu_) discard(c);
    }
};

struct CoreMemoryIssue { PendingOwner owner; std::uint64_t cycle; };
struct CoreRetirement { std::uint64_t sequence, issue_cycle, completion_cycle, retire_cycle; };
struct CoreResourceEvent {
    const char* kind;
    std::uint64_t cycle, sequence;
    unsigned domain;
    std::uint64_t value;
};
struct CoreInstructionIssue {
    std::uint64_t request_id=0, sequence=0, cycle=0;
    std::uint32_t fragment=0;
    InstructionAddress address;
};
struct CorePublication {
    std::vector<CoreInstructionIssue> instruction_requests;
    std::vector<CoreResourceEvent> resources;
    std::vector<CoreMemoryIssue> requests;
    std::vector<CoreRetirement> retired;
    std::vector<PendingOwner> blocked_on;
    std::optional<std::uint64_t> next_internal_cycle;
    std::uint64_t not_before = 0;
    bool needs_source = false;
    bool finished = false;
};
struct CoreContinuationCounters {
    std::uint64_t renamed = 0, max_rename_inflight = 0;
    std::uint64_t appended = 0, dispatched = 0, issued = 0, completed = 0, retired = 0;
    std::uint64_t inspected_entries = 0, internal_events = 0, max_live_rob = 0;
    std::uint64_t zero_eliminated = 0, destinations_allocated = 0, destinations_retired = 0, max_live_destinations = 0;
    std::uint64_t fu_attempts = 0, fu_failed_attempts = 0, skipped_failed_attempts = 0;
    std::uint64_t paired_stores = 0, store_address_uops = 0, store_data_uops = 0;
    std::uint64_t store_commit_publications = 0, store_writeback_publications = 0;
    std::array<std::uint64_t,3> max_iq_used{};
};

// Persistent final backend state for generic and explicit partial SPR resource
// modes. The caller must provide a proven dispatch floor for unseen input,
// and advance only to the coordinator's safe event frontier.
// With causal_retired_frontend, SprFrontend owns delivered IDQ credits until
// actual rename reserves ROB/IQ/LQ/SQ capacity. Separate in-flight reservations
// transfer to the existing backend allocation edge at rename_to_dispatch; no
// free-list or native timing outcome is inferred. Copied FFE facts and the live
// partial macro survive source pauses. Shared backend group semantics reject.
// Queued input is an in-order dispatch prefix certificate: a younger unseen
// fact cannot pass that prefix. When it empties, the core stops at that exact
// cycle for refill. Functional dispatch floors are minima, not appointments;
// a refilled fact may have a minimum older than this committed prefix.
class CoreContinuation {
public:
    CoreContinuation(const SimulatorConfig&, std::uint32_t core, StoreBuffer* stores = nullptr);
    ~CoreContinuation();
    CoreContinuation(CoreContinuation&&) noexcept;
    CoreContinuation& operator=(CoreContinuation&&) noexcept;
    CoreContinuation(const CoreContinuation&) = delete;
    CoreContinuation& operator=(const CoreContinuation&) = delete;
    static std::vector<std::string> unsupported(const SimulatorConfig&);
    void append(std::vector<FunctionalCoreUop>, std::uint64_t unseen_dispatch_not_before,
                bool source_finished = false);
    // A functional graph cut certifies that no younger source can execute.
    // Existing entries continue; release uses the coordinator's current cycle.
    void park_source(bool parked, std::uint64_t cycle);
    CorePublication advance_until_blocked(std::uint64_t cycle_limit);
    void bind_instruction_owner(std::uint64_t request_id, PendingOwner);
    void on_instruction_completion(std::uint64_t request_id, PendingOwner, std::uint64_t response_cycle, bool stale=false);
    void on_completion(PendingOwner, std::uint64_t response_cycle);
    // Coordinator-certified external resource wake; never replay dispatch at
    // the previous call frontier after a store-buffer credit was released.
    void on_store_buffer_response(std::uint64_t cycle);
    const CoreContinuationCounters& counters() const;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
}
