#pragma once
#include "fastsim/spr_bounded_line_set.hpp"
#include "fastsim/spr_stream_prefetch.hpp"
#include "fastsim/spr_transaction_id.hpp"
#include <algorithm>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <stdexcept>
#include <utility>
#include <vector>

namespace fastsim {
// Independent L2 SLICC predictor and controller-input queue. Observations and
// cache state MUST come from the model or functional metadata, not native OoO
// outcomes. This is not a cache, coherence controller, TBE or completion model.
// The owner must include predictor/queue state in its outer transaction and
// restore the whole snapshot after exceptions: training can precede enqueue.
// A host-capacity drop is counted and still trains predictor state. Queue
// capacity is per instance and is multiplied by cores and live snapshots.
class SprL2RubyPrefetcher {
 public:
    enum class Action { kMiss, kHit, kLateLoad, kUpgrade };
    enum class Request { kGetS, kGetX, kOther };
    struct Observation {
        Action action;
        Request request;
        std::uint64_t address,cycle;
        std::optional<bool> train_eligible;
        // For eligible GETS/GETX hits only: the MODEL's live L2 prefetch bit.
        std::optional<bool> prefetched;
    };
    struct Observed {bool observed=false,clear_prefetched=false;};
    enum class LineState { kInvalid,kPresent,kUnknown }; // Present includes transient
    enum class Decision { kEmpty,kNotReady,kUnknown,kDrop,kRetry,kNeedEviction,kReady };
    struct HeadFacts {
        std::optional<bool> directory_valid;
        LineState line_state=LineState::kUnknown;
        std::optional<bool> fill_credit_available;
        std::optional<bool> cache_has_space;
        std::optional<bool> victim_stable;
    };
    // Handles are scoped to one process-local owner lifetime and its rollback snapshots.
    // Naked per-owner tickets must never be routed across cores/instances.
    struct Handle {std::uint64_t owner,ticket;};
    struct Head {std::uint64_t line;Handle handle;std::uint64_t ready;};
    // Explicit nonzero host bound; not a claim about the target's unbounded
    // MessageBuffer. Full-capacity drops are separately counted.
    explicit SprL2RubyPrefetcher(std::size_t host_queue_limit)
        :queued_(host_queue_limit),limit_(host_queue_limit),owner_(allocate_owner()) {queue_.reserve(limit_);}
    SprL2RubyPrefetcher(const SprL2RubyPrefetcher& other)
        :stream_(other.stream_),ids_(other.ids_),queue_(other.queue_),queued_(other.queued_),
         limit_(other.limit_),owner_(other.owner_),frontier_(other.frontier_),duplicates_(other.duplicates_),
         overflow_(other.overflow_),unknown_(other.unknown_) {queue_.reserve(limit_);}
    SprL2RubyPrefetcher& operator=(const SprL2RubyPrefetcher& other) {
        if(this!=&other) {SprL2RubyPrefetcher copy(other);swap(copy);}
        return *this;
    }
    SprL2RubyPrefetcher(SprL2RubyPrefetcher&&)=default;
    SprL2RubyPrefetcher& operator=(SprL2RubyPrefetcher&&)=default;

    Observed observe(const Observation& event) {
        advance(event.cycle);
        if(event.action==Action::kUpgrade)return {true,false};
        if(event.action==Action::kHit && event.request==Request::kOther)return {true,false};
        if(!event.train_eligible){++unknown_;return {};}
        const bool train=*event.train_eligible;
        if(event.action==Action::kHit && train && !event.prefetched) {++unknown_;return {};}
        if(event.action==Action::kMiss || event.action==Action::kLateLoad) {
            if(train) {
                enqueue(stream_.observe_miss(event.address,event.cycle),event.cycle);
                adjacent(event.address,event.cycle);
            }
            // PF_IS+Load invokes observePfMiss after miss/adjacent, regardless
            // of TrainPrefetch. PF_IS+Store stalls and is not this action.
            if(event.action==Action::kLateLoad)
                enqueue(stream_.observe_prefetch_access(event.address,event.cycle),event.cycle);
            return {true,false};
        }
        if(!train)return {true,false};
        const bool clear=*event.prefetched;
        if(clear)enqueue(stream_.observe_prefetch_access(event.address,event.cycle),event.cycle);
        adjacent(event.address,event.cycle);
        return {true,clear};
    }
    std::optional<Head> peek(std::uint64_t now) {
        advance(now);
        if(queue_.empty() || queue_.front().ready>now)return std::nullopt;
        const auto& h=queue_.front();return Head{h.line,Handle{owner_,h.ticket},h.ready};
    }
    std::optional<std::uint64_t> next_ready()const {
        return queue_.empty()?std::nullopt:
            std::optional<std::uint64_t>(std::max(queue_.front().ready,frontier_));
    }
    std::size_t queued()const{return queue_.size();}
    std::uint64_t duplicates()const{return duplicates_;}
    std::uint64_t overflow()const{return overflow_;}
    std::uint64_t unknown_observations()const{return unknown_;}

    // Inspect only a candidate that has reached its model queue-ready edge.
    // Unknown facts are not manufactured; the caller supplies model state.
    Decision inspect(std::uint64_t now,const HeadFacts& facts) {
        const auto h=peek(now);
        if(!h)return queue_.empty()?Decision::kEmpty:Decision::kNotReady;
        if((facts.directory_valid && !*facts.directory_valid) ||
           facts.line_state==LineState::kPresent)return Decision::kDrop;
        if(!facts.directory_valid || facts.line_state==LineState::kUnknown)return Decision::kUnknown;
        if(!facts.fill_credit_available)return Decision::kUnknown;
        if(!*facts.fill_credit_available)return Decision::kRetry;
        if(!facts.cache_has_space)return Decision::kUnknown;
        if(*facts.cache_has_space)return Decision::kReady;
        if(!facts.victim_stable)return Decision::kUnknown;
        return *facts.victim_stable?Decision::kNeedEviction:Decision::kRetry;
    }
    // Only after confirmed controller drop or accepted issue. Compare ticket,
    // not address, to reject an old callback after a snapshot rollback.
    bool consume(std::uint64_t now,Handle handle) {
        if(!is_current_head(handle))return false; // stale callbacks cannot advance time
        const auto h=peek(now);if(!h)return false;
        std::pop_heap(queue_.begin(),queue_.end(),Later{});
        const auto entry=queue_.back();queue_.pop_back();
        if(!queued_.erase(entry.line))throw std::logic_error("L2 prefetch dedup state mismatch");
        return true;
    }
    // Source recycle keeps the original message counter and retains queue
    // deduplication. To avoid cycle-by-cycle retry, require the caller's
    // certified earliest edge at which ANY head-decision fact can change,
    // including line/directory state, victim state and all relevant credits.
    // The caller must drive this from real MODEL events: passing now+1 in a
    // retry loop would violate the high-throughput contract. This component
    // cannot validate the caller's certificate or guarantee overall throughput.
    // The owner must not advance beyond undiscovered earlier events; external
    // wakeups cannot roll time or cache state backwards. An
    // unknown/no-later edge refuses without modifying the queue; caller must
    // wait for an external event or take an explicitly accounted fallback.
    bool recycle(std::uint64_t now,Handle handle,
                 std::optional<std::uint64_t> resource_change) {
        if(!is_current_head(handle) || !resource_change || *resource_change<=now)
            return false;
        const auto h=peek(now);if(!h)return false;
        const auto earliest=add_one(now);
        std::pop_heap(queue_.begin(),queue_.end(),Later{});
        auto& item=queue_.back();item.retry_floor=earliest;item.ready=std::max(earliest,*resource_change);
        std::push_heap(queue_.begin(),queue_.end(),Later{});return true;
    }
    // A model line/directory event can reveal an earlier relevant change than
    // a previously scheduled retry. Never wake before the original +1 queue
    // latency. O(queue capacity), and only executed when that line is queued.
    bool notify_line_change(std::uint64_t now,std::uint64_t address) {
        advance(now);const auto line=address & ~std::uint64_t{63};
        if(!queued_.contains(line))return false;
        for(auto& entry:queue_)if(entry.line==line) {
            const auto ready=std::max(now,entry.retry_floor);
            if(ready>=entry.ready)return false;
            entry.ready=ready;std::make_heap(queue_.begin(),queue_.end(),Later{});return true;
        }
        throw std::logic_error("L2 prefetch dedup state mismatch");
    }
    // A controller-wide credit/victim event may affect any queued candidate.
    // The integration calls this only for a relevant MODEL state change.
    std::size_t notify_resource_change(std::uint64_t now) {
        advance(now);std::size_t changed=0;
        for(auto& entry:queue_) {
            const auto ready=std::max(now,entry.retry_floor);
            if(ready<entry.ready){entry.ready=ready;++changed;}
        }
        if(changed)std::make_heap(queue_.begin(),queue_.end(),Later{});
        return changed;
    }
 private:
    struct Entry {std::uint64_t line,ready,ticket,retry_floor;};
    struct Later {
        bool operator()(const Entry& a,const Entry& b)const {
            return a.ready!=b.ready?a.ready>b.ready:a.ticket>b.ticket;
        }
    };
    static std::uint64_t add_one(std::uint64_t now) {
        if(now==std::numeric_limits<std::uint64_t>::max())
            throw std::overflow_error("L2 prefetch queue time overflow");
        return now+1;
    }
    static std::uint64_t allocate_owner() {
        auto value=next_owner_.load(std::memory_order_relaxed);
        do {
            if(value==std::numeric_limits<std::uint64_t>::max())
                throw std::overflow_error("L2 prefetch owner ID exhausted");
        } while(!next_owner_.compare_exchange_weak(value,value+1,
                    std::memory_order_relaxed,std::memory_order_relaxed));
        return value;
    }
    bool is_current_head(Handle handle)const {
        return handle.owner==owner_ && !queue_.empty() && queue_.front().ticket==handle.ticket;
    }
    void advance(std::uint64_t now) {
        if(now<frontier_)throw std::invalid_argument("L2 prefetch event time moved backwards");
        frontier_=now;
    }
    void enqueue(std::optional<std::uint64_t> candidate,std::uint64_t now) {
        if(!candidate)return;
        const auto line=*candidate & ~std::uint64_t{63};
        if(queued_.contains(line)){++duplicates_;return;}
        if(queue_.size()>=limit_){++overflow_;return;}
        const auto ready=add_one(now);
        const auto ticket=ids_.allocate();
        if(queued_.insert(line)!=SprBoundedLineSet::Insert::kInserted)
            throw std::logic_error("L2 prefetch dedup insertion mismatch");
        // Capacity is reserved at construction and after snapshot copies.
        queue_.push_back({line,ready,ticket,ready});
        std::push_heap(queue_.begin(),queue_.end(),Later{});
    }
    void adjacent(std::uint64_t address,std::uint64_t now) {
        const auto line=address & ~std::uint64_t{63};
        const auto target=line^std::uint64_t{64};
        if((line>>12)==(target>>12))enqueue(target,now);
    }
    void swap(SprL2RubyPrefetcher& other) {
        using std::swap;
        swap(stream_,other.stream_);swap(ids_,other.ids_);swap(queue_,other.queue_);
        swap(queued_,other.queued_);swap(limit_,other.limit_);swap(owner_,other.owner_);swap(frontier_,other.frontier_);
        swap(duplicates_,other.duplicates_);swap(overflow_,other.overflow_);swap(unknown_,other.unknown_);
    }
    SprStreamPrefetch stream_;
    SprTransactionId ids_;
    std::vector<Entry> queue_;
    SprBoundedLineSet queued_;
    std::size_t limit_;
    std::uint64_t owner_;
    std::uint64_t frontier_=0,duplicates_=0,overflow_=0,unknown_=0;
    inline static std::atomic<std::uint64_t> next_owner_{1};
};
} // namespace fastsim
