#pragma once
#include "fastsim/spr_transaction_id.hpp"
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <initializer_list>
#include <limits>
#include <optional>
#include <stdexcept>
#include <vector>

namespace fastsim {
// Pure bounded codec: low bits identify a slot, high bits are a monotonic
// nonzero serial. Decoding a slot alone NEVER certifies a live ticket.
class SprStoreCreditTicketCodec {
 public:
    explicit SprStoreCreditTicketCodec(std::size_t capacity):capacity_(capacity) {
        if(!capacity || capacity>std::numeric_limits<std::uint32_t>::max())
            throw std::invalid_argument("invalid shared store credit capacity");
        auto value=capacity-1;
        while(value){++bits_;value>>=1;}
        mask_=(std::uint64_t{1}<<bits_)-1;
    }
    std::uint64_t encode(std::uint64_t serial,std::uint32_t slot)const {
        if(!serial || serial>max_serial())
            throw std::overflow_error("shared store ticket generation exhausted");
        if(static_cast<std::size_t>(slot)>=capacity_)
            throw std::out_of_range("shared store ticket slot");
        return (serial<<bits_)|slot;
    }
    std::uint32_t candidate_slot(std::uint64_t id)const {
        return static_cast<std::uint32_t>(id&mask_);
    }
    unsigned slot_bits()const{return bits_;}
    std::uint64_t max_serial()const {
        return std::numeric_limits<std::uint64_t>::max()>>bits_;
    }
 private:
    std::size_t capacity_;
    unsigned bits_=0;
    std::uint64_t mask_=0;
};
// Capacity-only shared SQ+committed-buffer budget. This is NOT the FIFO SQ,
// a store sequencer, a cache model, or a native callback observer. Callers
// must admit every competing store (ordinary, special and unknown), maintain
// the distinct SQ FIFO/barrier, and supply only model-generated final edges.
//
// Time advances exclusively when the caller certifies that all prior events
// have been discovered. Future reservations consume nothing; planned releases
// retain the credit until advance reaches the event edge. Snapshot copies share
// a non-rollback ID allocator, so discarded tickets cannot be reused within
// that ledger lineage. IDs must be paired with the correct core/ledger owner;
// separately constructed ledgers do not share a global ID namespace. The
// whole outer model transaction must snapshot/restore this object together with
// SQ, cache/directory, queues, feedback, and counters.
class SprSharedStoreCredits {
 public:
    enum class Kind { kOrdinary, kSpecial, kUnknown };
    enum class Admission { kAccepted, kFull, kFuture };
    struct Result {
        Admission status;
        std::uint64_t id=0;
        std::optional<std::uint64_t> next_release;
    };
    explicit SprSharedStoreCredits(std::size_t capacity)
        :slots_(checked_capacity(capacity)),capacity_(capacity),codec_(capacity) {
        free_.reserve(capacity);events_.reserve(capacity);
        for(std::size_t i=capacity;i>0;--i)free_.push_back(static_cast<std::uint32_t>(i-1));
    }
    // Single-owner, per-core timing feedback may probe its current certified
    // dispatch lower bound before other gates choose the final dispatch edge.
    // Probe consumes no credit. If full with no selected release, the caller
    // must fall back or wait for an external completion, never invent a time.
    Result probe(std::uint64_t now) {
        advance(now);
        return free_.empty()?Result{Admission::kFull,0,next_release()}
                            :Result{Admission::kAccepted,0,std::nullopt};
    }
    // A producer issue in the future must remain outside this ledger. It can
    // neither claim capacity nor manufacture a final release in advance.
    Result admit(std::uint64_t now,std::uint64_t issue,Kind kind) {
        if(issue<now)throw std::invalid_argument("shared store admission behind frontier");
        advance(now);
        if(issue>now)return {Admission::kFuture,0,std::nullopt};
        if(free_.empty())return {Admission::kFull,0,next_release()};
        const auto serial=ids_.allocate();
        const auto index=free_.back();
        const auto id=codec_.encode(serial,index);
        free_.pop_back();
        slots_[index]=Slot{id,now,kind,true,std::nullopt};
        return {Admission::kAccepted,id,std::nullopt};
    }
    // An unmerged, unknown or special store retains its one shared credit to
    // the caller-selected final model completion; its FIFO barrier is external.
    bool schedule_single(std::uint64_t id,std::uint64_t callback) {
        const auto index=find(id);validate_release(index,callback);
        events_.reserve(capacity_); // all potential allocation precedes mutation
        install_release(index,callback);return true;
    }
    // Group is [owner, followers...]. All members must be live, ordinary and
    // not previously scheduled. The caller independently proves same-line
    // eligibility and determines actual model offload/callback edges. A
    // follower can free before an older owner, so this ledger deliberately
    // uses a minimum-release heap rather than an SQ head pointer.
    bool schedule_group(std::initializer_list<std::uint64_t> ids,
                        std::uint64_t offload,std::uint64_t callback) {
        return schedule_group(ids.begin(),ids.size(),offload,callback);
    }
    bool schedule_group(const std::uint64_t* ids,std::size_t size,
                        std::uint64_t offload,std::uint64_t callback) {
        if(!ids || size<2 || size>4 || callback<offload)
            throw std::logic_error("invalid shared store group edges");
        std::uint32_t indices[4]={};std::size_t count=0;
        for(std::size_t position=0;position<size;++position) {
            const auto id=ids[position];
            const auto index=find(id);
            if(slots_[index].kind!=Kind::kOrdinary)
                throw std::logic_error("special or unknown store cannot merge");
            for(std::size_t i=0;i<count;++i)if(indices[i]==index)
                throw std::logic_error("duplicate shared store group member");
            validate_release(index,count==0?callback:offload);
            indices[count++]=index;
        }
        // Group offload is no earlier than EVERY source admission, including
        // owner (whose own callback might happen later).
        if(offload<frontier_ || offload<slots_[indices[0]].admission)
            throw std::logic_error("store offload precedes frontier or owner admission");
        events_.reserve(capacity_);
        for(std::size_t i=0;i<count;++i)install_release(indices[i],i==0?callback:offload);
        return true;
    }
    // Only visits explicitly scheduled release events, never individual target
    // cycles. Equality releases before a same-edge admission.
    void advance(std::uint64_t now) {
        if(now<frontier_)throw std::invalid_argument("shared store frontier moved backwards");
        free_.reserve(capacity_);
        while(!events_.empty() && events_.front().when<=now) {
            std::pop_heap(events_.begin(),events_.end(),Later{});
            const auto event=events_.back();events_.pop_back();
            auto& slot=slots_[event.index];
            if(!slot.active || slot.id!=event.id || !slot.release || *slot.release!=event.when)
                throw std::logic_error("stale shared store release event");
            slot.active=false;slot.release.reset();free_.push_back(event.index);
        }
        frontier_=now;
    }
    std::optional<std::uint64_t> next_release()const {
        return events_.empty()?std::nullopt:std::optional<std::uint64_t>(events_.front().when);
    }
    std::size_t used()const{return capacity_-free_.size();}
    std::uint64_t frontier()const{return frontier_;}
 private:
    struct Slot {
        std::uint64_t id=0,admission=0;
        Kind kind=Kind::kUnknown;
        bool active=false;
        std::optional<std::uint64_t> release;
    };
    struct Event {std::uint64_t when,id;std::uint32_t index;};
    struct Later {
        bool operator()(const Event& a,const Event& b)const {
            return a.when!=b.when?a.when>b.when:a.id>b.id;
        }
    };
    // Low bits encode the immutable slot, high bits the unique serial. A
    // stale callback is rejected by the full serial comparison. This is O(1)
    // and allocates no lookup nodes; copies retain the same slot table and
    // share the non-rollback serial source. Checked exhaustion prevents wrap.
    std::uint32_t find(std::uint64_t id)const {
        const auto index=codec_.candidate_slot(id);
        if(id && index<slots_.size() && slots_[index].active && slots_[index].id==id)
            return index;
        throw std::logic_error("unknown or stale shared store ticket");
    }
    static std::size_t checked_capacity(std::size_t capacity) {
        if(!capacity || capacity>std::numeric_limits<std::uint32_t>::max())
            throw std::invalid_argument("invalid shared store credit capacity");
        return capacity;
    }
    void validate_release(std::uint32_t index,std::uint64_t release)const {
        const auto& slot=slots_[index];
        if(slot.release || release<frontier_ || release<slot.admission)
            throw std::logic_error("invalid or duplicate shared store release");
    }
    void install_release(std::uint32_t index,std::uint64_t release) {
        auto& slot=slots_[index];slot.release=release;
        events_.push_back(Event{release,slot.id,index});
        std::push_heap(events_.begin(),events_.end(),Later{});
    }
    std::vector<Slot> slots_;
    std::vector<std::uint32_t> free_;
    std::vector<Event> events_;
    std::size_t capacity_;
    std::uint64_t frontier_=0;
    SprStoreCreditTicketCodec codec_;
    SprTransactionId ids_;
};
} // namespace fastsim
