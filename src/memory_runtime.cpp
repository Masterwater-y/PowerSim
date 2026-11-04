#include "fastsim/memory_runtime.hpp"
#include <limits>
#include <stdexcept>
#include <tuple>
#include <utility>
namespace fastsim::spr {
SourcePublication SourcePublication::blocked_on(std::vector<PendingOwner> tokens, SafeKey successor) {
    if (tokens.empty()) throw std::invalid_argument("blocked source needs real prerequisite tokens");
    return {successor,std::move(tokens),false};
}
MemoryRuntime::MemoryRuntime(std::uint32_t sources) : sources_(sources) {
    if (!sources) throw std::invalid_argument("memory runtime needs all registered event sources");
}
void MemoryRuntime::publish(std::uint32_t source, SourcePublication p) {
    if (source>=sources_.size()) throw std::out_of_range("unregistered memory source");
    const auto& old=sources_[source];
    if (old.done) throw std::logic_error("finished memory source republished");
    if (!p.done && (p.earliest<old.earliest || (closed_ && p.earliest<=*closed_)))
        throw std::logic_error("source publication regresses a certified frontier");
    sources_[source]=std::move(p);
}
void MemoryRuntime::enqueue(MemoryEvent event) {
    if (closed_ && event.key<=*closed_)
        throw std::logic_error("late event attempts to reopen a closed target phase");
    if (order_==std::numeric_limits<std::uint64_t>::max())
        throw std::overflow_error("memory event order exhausted");
    events_.push({event,order_++});
}
std::optional<SafeKey> MemoryRuntime::safe_frontier() const {
    std::optional<SafeKey> key;
    for (const auto& p:sources_)
        if (!p.done && (!key || p.earliest<*key)) key=p.earliest;
    return key;
}
bool MemoryRuntime::ready() const {
    if (events_.empty()) return false;
    const auto frontier=safe_frontier();
    return !frontier || events_.top().event.key<*frontier;
}
std::optional<MemoryEvent> MemoryRuntime::pop_ready() {
    if (!ready()) return std::nullopt;
    const auto result=events_.top().event;
    events_.pop(); closed_=result.key;
    return result;
}
std::vector<MemoryEvent> MemoryRuntime::pop_ready_group() {
    std::vector<MemoryEvent> result;
    if (!ready()) return result;
    const auto key=events_.top().event.key;
    do { result.push_back(events_.top().event); events_.pop(); }
    while (!events_.empty() && events_.top().event.key==key);
    closed_=key;
    return result;
}
bool CacheEventAdapter::OwnerLess::operator()(const PendingOwner& a,const PendingOwner& b) const {
    return std::tie(a.core,a.sequence,a.fragment,a.generation)<std::tie(b.core,b.sequence,b.fragment,b.generation);
}
CacheProposal CacheEventAdapter::prepare(PendingOwner owner, std::uint64_t line, bool write) const {
    return {owner,cache_.prepare_lookup(line),line,invalidation_generation_,write};
}
std::optional<CacheAccessResult> CacheEventAdapter::admit(const CacheProposal& p) {
    if (pending_.find(p.owner)!=pending_.end() || p.invalidation_generation!=invalidation_generation_) return std::nullopt;
    const auto result=cache_.commit_probe(p.lookup,p.write,counters_);
    if (result && !result->hit) pending_.emplace(p.owner,Fill{p.line,p.write,true});
    return result;
}
bool CacheEventAdapter::complete(PendingOwner owner) {
    const auto i=pending_.find(owner);
    if (i==pending_.end()) return false;
    const auto fill=i->second; pending_.erase(i);
    if (!fill.valid) return false;
    cache_.complete_fill(fill.line,fill.write,counters_);
    return true;
}
void CacheEventAdapter::invalidate(std::uint64_t line) {
    if (invalidation_generation_==std::numeric_limits<std::uint64_t>::max())
        throw std::overflow_error("cache proposal generation exhausted");
    ++invalidation_generation_;
    for (auto& entry : pending_)
        if (entry.second.line==line) entry.second.valid=false;
    cache_.invalidate(line);
}
} // namespace fastsim::spr
