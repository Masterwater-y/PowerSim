#pragma once
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <limits>
#include <optional>
#include <stdexcept>
#include <vector>
#include "fastsim/spr_transaction_id.hpp"
#include <utility>

namespace fastsim {
// Bounded stage-two summary of the SPR committed-store buffer. Callers supply
// ordinary committed stores and modeled order; no instruction data, native
// timing, cache prediction, per-cycle execution or retries are performed here.
// Caller admits every SQ store in program order and represents special or
// unknown stores using admit_special. Offload eligibility, SQ-head ordering,
// already-outstanding requests and actual service remain the caller's contract.
class SprCommittedStoreBuffer {
 public:
    struct Store {
        std::uint64_t sequence, line, context, requestor, flags, byte_mask;
    };
    // When not accepted, cycle>proposed is an offload-width bound. A cycle
    // equal to proposed means the SQ-head special barrier has no known
    // release edge: wait for its external completion, not a same-time retry.
    struct Offload { std::uint64_t id, cycle; bool merged, accepted; };
    explicit SprCommittedStoreBuffer(std::size_t capacity, unsigned offload_width,
                                     std::size_t line_window)
        : capacity_(capacity), offload_width_(offload_width), line_window_(line_window) {
        if (!capacity || !offload_width || !line_window)
            throw std::invalid_argument("invalid committed-store capacity");
    }
    bool admit_sq() {
        if (budget_used()>=capacity_) return false;
        sq_.push_back(SqEntry{});return true;
    }
    // An unknown/special store consumes an SQ slot and blocks younger
    // offloads. It never becomes an ordinary committed-buffer line.
    std::optional<std::uint64_t> admit_special(std::uint64_t sequence) {
        if (budget_used()>=capacity_) return std::nullopt;
        const auto id=ids_.allocate();
        sq_.push_back(SqEntry{true,false,id,sequence});return id;
    }
    // Caller proves the special is ready for its conservative legacy path.
    // The actual controller/port/data service is external to this summary.
    std::optional<std::uint64_t> begin_special(std::uint64_t now) {
        if(now<frontier_)throw std::logic_error("special store precedes frontier");
        if(sq_.empty() || !sq_.front().special || sq_.front().issued || !entries_.empty())
            return std::nullopt;
        advance(now);sq_.front().issued=true;return sq_.front().id;
    }
    void complete_special(std::uint64_t id,std::uint64_t now) {
        if(sq_.empty() || !sq_.front().special || !sq_.front().issued ||
           sq_.front().id!=id || !entries_.empty())
            throw std::logic_error("invalid special store completion");
        advance(now);sq_.pop_front();
    }
    Offload offload(const Store& store, std::uint64_t proposed) {
        if (sq_.empty()) throw std::logic_error("offload requires an occupied SQ slot");
        if (proposed<frontier_) throw std::logic_error("offload precedes store frontier");
        if (sq_.front().special) return {0,proposed,false,false};
        auto at=std::max(proposed,last_offload_);
        if (at==last_offload_ && offloaded_==offload_width_) {
            if(at==std::numeric_limits<std::uint64_t>::max())
                throw std::overflow_error("offload time overflow");
            ++at;
        }
        // Do not apply a future offload before intervening events have been
        // processed. Caller retries once its modeled event frontier reaches at.
        if(at>proposed) return {0,at,false,false};
        frontier_=proposed;
        if(at!=last_offload_) {last_offload_=at;offloaded_=0;}
        ++offloaded_;
        const bool merge=!entries_.empty() && !entries_.back().issued &&
            entries_.back().store.line==store.line &&
            entries_.back().store.context==store.context &&
            entries_.back().store.requestor==store.requestor &&
            entries_.back().store.flags==store.flags;
        sq_.pop_front();
        if(merge) {
            entries_.back().store.byte_mask|=store.byte_mask;
            entries_.back().last_sequence=store.sequence;
            return {entries_.back().id,at,true,true};
        }
        const auto id=ids_.allocate();
        entries_.push_back(Entry{store,store.sequence,id,false,false});
        return {id,at,false,true};
    }
    std::optional<std::uint64_t> send_next(std::uint64_t now) {
        advance(now);
        auto item=std::find_if(entries_.begin(),entries_.end(),[](const Entry& e){return !e.issued;});
        if(item==entries_.end())return std::nullopt;
        auto line=std::find_if(inflight_.begin(),inflight_.end(),[&](const auto& p){return p.first==item->store.line;});
        if(line==inflight_.end()) {
            if(inflight_.size()>=line_window_)return std::nullopt;
            inflight_.emplace_back(item->store.line,1);
        } else {
            ++line->second;
        }
        item->issued=true;return item->id;
    }
    void complete(std::uint64_t id, std::uint64_t now) {
        auto it=std::find_if(entries_.begin(),entries_.end(),[id](const Entry& e){return e.id==id;});
        if(it==entries_.end() || !it->issued || it->complete)
            throw std::logic_error("invalid committed-store completion");
        advance(now);
        auto line=std::find_if(inflight_.begin(),inflight_.end(),[&](const auto& p){return p.first==it->store.line;});
        if(line==inflight_.end() || !line->second)throw std::logic_error("missing store line credit");
        if(--line->second==0)inflight_.erase(line);
        it->complete=true;
        while(!entries_.empty() && entries_.front().complete)entries_.pop_front();
    }
    std::uint64_t byte_mask(std::uint64_t id) const {
        auto it=std::find_if(entries_.begin(),entries_.end(),[id](const Entry& e){return e.id==id;});
        if(it==entries_.end())throw std::logic_error("unknown committed store");
        return it->store.byte_mask;
    }
    std::size_t sq_entries() const{return sq_.size();}
    std::size_t buffer_entries() const{return entries_.size();}
    std::size_t budget_used() const{return sq_.size()+entries_.size();}
 private:
    void advance(std::uint64_t now) {
        if(now<frontier_)throw std::logic_error("store event precedes frontier");
        frontier_=now;
    }
    struct Entry { Store store; std::uint64_t last_sequence,id;bool issued,complete; };
    struct SqEntry {bool special=false,issued=false;std::uint64_t id=0,sequence=0;};
    std::size_t capacity_;
    unsigned offload_width_;
    std::size_t line_window_;
    unsigned offloaded_=0;
    std::uint64_t last_offload_=0,frontier_=0;
    SprTransactionId ids_;
    std::deque<SqEntry> sq_;
    std::deque<Entry> entries_;
    // At most line_window_ entries; same-line requests share one line credit.
    std::vector<std::pair<std::uint64_t,std::size_t>> inflight_;
};
} // namespace fastsim
