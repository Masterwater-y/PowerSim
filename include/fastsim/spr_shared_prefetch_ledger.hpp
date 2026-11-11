#pragma once
#include "fastsim/spr_transaction_id.hpp"
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <tuple>
#include <vector>

namespace fastsim {
// Bounded shared-line prefetch transaction ledger. It contains NO cache,
// directory, DRAM or resource model. In particular, accepting an issue does
// not touch cache tags and selecting a future response does not make data
// visible. The owner supplies a certified, globally monotone service frontier;
// time_epoch horizon / last observed event alone is not such a certificate.
//
// The owner must snapshot this ledger with its cache/directory/timing/writeback
// transaction and separately maintain all waiting demand requests. A demand
// finding a parent is only a join proposal; on cancellation the caller must
// reissue/wake its waiters. The stored generation is supplied by a coherence
// tracker, not inferred from a tag or bitmap. IDs stay unique across copies and
// rollback. No information here is taken from native OoO execution.
class SprSharedPrefetchLedger {
 public:
    enum class Issue { kAccepted, kDuplicate, kCapacity, kFuture };
    enum class Select { kSelected, kDuplicate, kStale };
    enum class Commit { kPublished, kPermissionLost, kRetry };
    struct Issued { Issue status; std::uint64_t ticket=0; };
    struct Entry {
        std::uint64_t ticket;
        std::uint32_t core;
        std::uint64_t line, issue, grant_generation;
        std::optional<std::uint64_t> response;
    };
    struct Completed { std::size_t published=0,cancelled=0,deferred=0; };
    explicit SprSharedPrefetchLedger(std::size_t capacity):capacity_(capacity) {
        if(!capacity)throw std::invalid_argument("empty shared prefetch ledger");
        entries_.reserve(capacity);
    }
    Issued issue(std::uint64_t now,std::uint32_t core,std::uint64_t line,
                 std::uint64_t issue_time,std::uint64_t grant_generation) {
        advance(now);
        if(issue_time>now)return {Issue::kFuture};
        if(issue_time<now)throw std::invalid_argument("shared prefetch issue behind frontier");
        if(!grant_generation)throw std::invalid_argument("missing coherence generation");
        for(const auto& e:entries_)if(e.line==line)return {Issue::kDuplicate,e.ticket};
        if(entries_.size()==capacity_)return {Issue::kCapacity};
        const auto ticket=ids_.allocate();
        entries_.push_back({ticket,core,line,issue_time,grant_generation,std::nullopt});
        return {Issue::kAccepted,ticket};
    }
    // Return a parent ticket, not a cache hit. Unknown caller ownership or
    // target/request semantics must reject joining before calling this method.
    std::optional<std::uint64_t> demand(std::uint64_t now,std::uint64_t line) {
        advance(now);
        for(const auto& e:entries_)if(e.line==line)return e.ticket;
        return std::nullopt;
    }
    Select select(std::uint64_t ticket,std::uint64_t response) {
        for(auto& e:entries_)if(e.ticket==ticket) {
            if(e.response) {
                if(*e.response!=response)throw std::logic_error("conflicting shared prefetch response");
                return Select::kDuplicate;
            }
            if(response<e.issue || response<frontier_)
                throw std::invalid_argument("shared prefetch response behind frontier");
            e.response=response;return Select::kSelected;
        }
        return Select::kStale;
    }
    std::optional<std::uint64_t> cancel_line(std::uint64_t now,std::uint64_t line) {
        advance(now);
        for(std::size_t i=0;i<entries_.size();++i)if(entries_[i].line==line) {
            const auto id=entries_[i].ticket;entries_.erase(entries_.begin()+i);return id;
        }
        return std::nullopt;
    }
    // Callback must atomically publish cache, directory and victim/writeback
    // at the supplied `now`, never backdate them to a response selected before
    // this frontier. The owner must propagate any publication delay to joined
    // demands. kRetry must leave external state untouched. On exception restore
    // the WHOLE outer transaction including this ledger and its caller's waiters.
    template<class Callback> Completed complete(std::uint64_t now,Callback&& callback) {
        advance(now);Completed result;
        std::vector<std::uint64_t> retry;
        while(true) {
            std::size_t best=entries_.size();
            for(std::size_t i=0;i<entries_.size();++i) {
                const auto& e=entries_[i];
                if(!e.response || *e.response>now ||
                   std::find(retry.begin(),retry.end(),e.ticket)!=retry.end())continue;
                if(best==entries_.size() ||
                   std::tie(*e.response,e.ticket)<std::tie(*entries_[best].response,entries_[best].ticket))best=i;
            }
            if(best==entries_.size())break;
            const auto e=entries_[best];const auto outcome=callback(e,now);
            if(outcome==Commit::kRetry){retry.push_back(e.ticket);++result.deferred;continue;}
            if(outcome==Commit::kPublished)++result.published;else ++result.cancelled;
            entries_.erase(entries_.begin()+best);
        }
        return result;
    }
    std::uint64_t frontier() const {return frontier_;}
    std::size_t size() const {return entries_.size();}
 private:
    void advance(std::uint64_t now) {
        if(now<frontier_)throw std::invalid_argument("shared prefetch frontier moved backwards");
        frontier_=now;
    }
    std::size_t capacity_;
    std::uint64_t frontier_=0;
    SprTransactionId ids_;
    std::vector<Entry> entries_;
};
} // namespace fastsim
