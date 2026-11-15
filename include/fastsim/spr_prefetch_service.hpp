#pragma once
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <stdexcept>
#include <tuple>
#include <vector>

namespace fastsim {
// A per-simulation bounded prefetch transaction summary. No cache, directory,
// timing resource or PMU is touched here. A selected response must represent
// data actually available from the shared hierarchy, not a lower bound.
// The stage-two owner reserves those
// resources before begin and publishes them in one external transaction from
// the completion callback. Ticket allocation is external, monotonically
// increasing and NOT rolled back with this object. Retaining a copy permits
// an outer speculative batch to restore the summary with its cache/directory.
class SprPrefetchService {
  public:
    enum class Level { kL1, kL2 };
    enum class Begin { kAccepted, kDuplicateLine, kCapacity, kDeferred };
    enum class Select { kSelected, kDuplicate, kStale };
    enum class Commit { kPublished, kPermissionLost, kRetry };
    struct Entry {
        std::uint64_t ticket;
        std::uint32_t core;
        std::uint64_t line, issue;
        Level level;
        std::uint64_t grant_generation;
        std::optional<std::uint64_t> response;
        bool consumed=false;
    };
    // A demand retains this accepted service identity after the active entry
    // is retired. The caller must invalidate the reference when coherence or
    // rollback invalidates delivery to that particular demand.
    struct Follower { std::uint64_t ticket, ready; Level level=Level::kL2; bool prefetch=true; };
    struct Completed {std::size_t published=0, cancelled=0, deferred=0;};

    explicit SprPrefetchService(std::size_t capacity):capacity_(capacity) {
        if(!capacity)throw std::invalid_argument("empty prefetch service summary");
        entries_.reserve(capacity);
    }
    // The caller supplies a certified globally monotone stage-two frontier.
    // Future requests must remain outside the summary and reserve nothing.
    Begin begin_at(std::uint64_t now,std::uint64_t ticket,std::uint32_t core,std::uint64_t line,
                   std::uint64_t issue,Level level,std::uint64_t grant_generation) {
        advance(now);
        if(issue>now)return Begin::kDeferred;
        if(issue<now)throw std::invalid_argument("prefetch admission precedes frontier");
        if(!ticket || ticket<=last_ticket_)
            throw std::invalid_argument("prefetch ticket must increase monotonically");
        for(const auto& e:entries_)if(e.core==core && e.line==line)return Begin::kDuplicateLine;
        if(entries_.size()==capacity_)return Begin::kCapacity;
        entries_.push_back(Entry{ticket,core,line,issue,level,grant_generation,std::nullopt});
        last_ticket_=ticket;return Begin::kAccepted;
    }
    Select select_response(std::uint64_t ticket,std::uint64_t response) {
        for(auto& e:entries_)if(e.ticket==ticket) {
            if(e.response) {
                if(*e.response!=response)throw std::logic_error("prefetch response already selected");
                return Select::kDuplicate;
            }
            if(response<e.issue || response<frontier_)
                throw std::invalid_argument("prefetch response precedes issue or frontier");
            e.response=response;return Select::kSelected;
        }
        return Select::kStale;
    }
    // A request in the future is not a parent. Even if a selected response is
    // due, this remains pending until the owner publishes it through callback.
    const Entry* waiting(std::uint32_t core,std::uint64_t line,std::uint64_t at) const {
        for(const auto& e:entries_)if(e.core==core && e.line==line && e.issue<=at)return &e;
        return nullptr;
    }
    // Evaluate against the absolute selected response after CPU issue feedback.
    // The owner must separately certify data/permission and its return path.
    // This lookup neither allocates a CPU request nor publishes a cache line.
    std::optional<std::uint64_t> follower_response(
        std::uint64_t ticket, std::uint32_t core, std::uint64_t line,
        std::uint64_t issue, std::uint64_t local_latency,
        std::uint64_t return_latency) const {
        const auto follower = attach_follower(ticket, core, line, issue);
        if (!follower) return std::nullopt;
        return response_for(*follower, issue, local_latency, return_latency);
    }
    std::optional<Follower> attach_follower(std::uint64_t ticket,
        std::uint32_t core, std::uint64_t line, std::uint64_t issue) const {
        for (const auto& e : entries_)
            if (e.ticket == ticket && e.core == core && e.line == line &&
                issue >= e.issue && e.response)
                return Follower{ticket, *e.response,e.level};
        return std::nullopt;
    }
    static std::uint64_t response_for(const Follower& follower,
        std::uint64_t issue, std::uint64_t local_latency,
        std::uint64_t return_latency) {
        const auto max = std::numeric_limits<std::uint64_t>::max();
        if (issue > max-local_latency || follower.ready > max-return_latency)
            throw std::overflow_error("prefetch follower response overflow");
        return std::max(issue + local_latency, follower.ready + return_latency);
    }
    bool cancel(std::uint64_t ticket) {
        for(std::size_t i=0;i<entries_.size();++i)if(entries_[i].ticket==ticket) {
            entries_.erase(entries_.begin()+i);return true;
        }
        return false;
    }
    // Callback must either atomically complete directory/cache/writeback and
    // return Published, leave all state untouched on Retry, or confirm that
    // permission is lost. On exceptions the caller must restore the complete
    // batch snapshot, including this summary and all external model state.
    template<class Callback> Completed complete_through(std::uint64_t now,Callback&& commit) {
        advance(now);
        Completed result;
        if(entries_.empty()) return result;
        // No allocation on the common path: a deferred list is only needed
        // if at least one publisher reports temporary uncertainty.
        std::vector<std::uint64_t> deferred;
        while(true) {
            std::size_t selected=entries_.size();
            for(std::size_t i=0;i<entries_.size();++i) {
                const auto& e=entries_[i];
                if(!e.response || *e.response>now)continue;
                bool skip=false;
                for(auto id:deferred)if(id==e.ticket){skip=true;break;}
                if(skip)continue;
                if(selected==entries_.size() ||
                   std::tie(*e.response,e.ticket)<std::tie(*entries_[selected].response,entries_[selected].ticket))selected=i;
            }
            if(selected==entries_.size())break;
            const auto e=entries_[selected];
            const auto status=commit(e);
            if(status==Commit::kRetry) {deferred.push_back(e.ticket);++result.deferred;continue;}
            if(status==Commit::kPublished)++result.published;else ++result.cancelled;
            entries_.erase(entries_.begin()+selected);
        }
        return result;
    }
    std::size_t size() const {return entries_.size();}
    void consume(std::uint64_t ticket) {
        for(auto& entry:entries_)if(entry.ticket==ticket){entry.consumed=true;return;}
    }
    std::optional<std::uint64_t> next_response(std::uint64_t after=0) const {
        after=std::max(after,frontier_);
        std::optional<std::uint64_t> next;
        for(const auto& entry:entries_)
            if(entry.response && *entry.response>after && (!next || *entry.response<*next))
                next=entry.response;
        return next;
    }
  private:
    void advance(std::uint64_t now) {
        if(now<frontier_)throw std::invalid_argument("prefetch frontier moved backwards");
        frontier_=now;
    }
    std::size_t capacity_;
    std::uint64_t last_ticket_=0,frontier_=0;
    std::vector<Entry> entries_;
};
}
