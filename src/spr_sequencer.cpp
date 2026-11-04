#include "fastsim/spr_sequencer.hpp"
#include <algorithm>
#include <limits>

namespace fastsim::spr {
namespace {
bool is_read(SequencerKind kind) {
    return kind == SequencerKind::Read || kind == SequencerKind::Ifetch;
}
bool same_lease(const ProtocolLease& a, const ProtocolLease& b) {
    return a.owner == b.owner && a.service_generation == b.service_generation &&
           a.demand.level == b.demand.level;
}
bool same_demand(const ProtocolDemand& a, const ProtocolDemand& b) {
    return a.level == b.level && a.tbe == b.tbe && a.data_fill == b.data_fill;
}
}

OnlineSequencer::OnlineSequencer(std::uint32_t core, const ResourceLimits& limits,
                                 std::uint64_t line_bytes)
    : core_(core), capacity_(limits.ordinary_requests), line_bytes_(line_bytes) {
    if (!capacity_ || !line_bytes_ || (line_bytes_ & (line_bytes_ - 1)))
        throw std::invalid_argument("Sequencer requires positive requests and power-of-two line bytes");
}

SequencerAdmission OnlineSequencer::peek(const SequencerRequest& request) const {
    SequencerAdmission result{request, SequencerStatus::NewService, revision_};
    if (request.owner.core != core_) result.status = SequencerStatus::WrongCore;
    else if ((!is_read(request.kind) && request.kind != SequencerKind::Write) ||
             (request.ordered_store && request.kind != SequencerKind::Write))
        result.status = SequencerStatus::Unsupported;
    else {
        for (const auto& entry : lines_)
            for (const auto& live : entry.second.requests)
                if (live.owner == request.owner) {
                    result.status = SequencerStatus::Duplicate;
                    return result;
                }
        // makeRequest tests capacity before insertRequest even for aliases.
        if (outstanding_ >= capacity_) result.status = SequencerStatus::Full;
        else if (lines_.count(request.address & ~(line_bytes_ - 1)))
            result.status = SequencerStatus::Follower;
    }
    return result;
}

SequencerUpdate OnlineSequencer::grant(const SequencerAdmission& candidate,
                                      bool backend_accepted, std::uint64_t cycle) {
    SequencerUpdate result;
    if (candidate.revision != revision_) return result;
    const auto current = peek(candidate.request);
    if (current.status != candidate.status) return result;
    result.status = current.status;
    if (current.status != SequencerStatus::NewService &&
        current.status != SequencerStatus::Follower) return result;
    if (current.status == SequencerStatus::NewService && !backend_accepted) {
        result.status = SequencerStatus::BackendRefused;
        return result;
    }
    if (next_service_ == std::numeric_limits<std::uint64_t>::max())
        throw std::overflow_error("Sequencer service generation exhausted");
    const auto line = candidate.request.address & ~(line_bytes_ - 1);
    auto& entry = lines_[line];
    auto accepted_request=candidate.request;accepted_request.model_accept_cycle=cycle;
    entry.requests.push_back(accepted_request);
    if (candidate.request.ordered_store) ordered_.push_back(candidate.request.owner);
    ++outstanding_; ++accepted_; ++revision_;
    if (current.status == SequencerStatus::NewService) {
        entry.service = {candidate.request.owner, line, next_service_++,
                         candidate.request.kind, cycle};
        entry.inflight = true;
        entry.admitted_cycle = cycle;
        result.actions.push_back({SequencerActionKind::Issued, candidate.request.owner, entry.service});
    } else {
        result.actions.push_back({SequencerActionKind::FollowerAccepted, candidate.request.owner, entry.service});
    }
    return result;
}

bool OnlineSequencer::matches(const SequencerService& a, const SequencerService& b) {
    return a.owner == b.owner && a.line == b.line && a.generation == b.generation &&
           a.kind == b.kind && a.earliest_cycle == b.earliest_cycle && a.entry_overlap == b.entry_overlap;
}

std::optional<SequencerService> OnlineSequencer::peek_service() const {
    std::optional<SequencerService> result;
    for (const auto& pair : lines_) {
        const auto& entry = pair.second;
        if (entry.inflight || entry.parked) continue;
        if (!result || entry.service.generation < result->generation) result = entry.service;
    }
    return result;
}

bool OnlineSequencer::grant_service(const SequencerService& service,
                                    bool backend_accepted, std::uint64_t cycle) {
    const auto it = lines_.find(service.line);
    if (!backend_accepted || it == lines_.end() || it->second.inflight ||
        it->second.parked || !matches(service, it->second.service) ||
        cycle < service.earliest_cycle) return false;
    it->second.inflight = true;
    it->second.admitted_cycle = cycle;
    ++revision_;
    return true;
}

void OnlineSequencer::reissue(std::uint64_t line, Line& entry,
                             std::uint64_t cycle, SequencerUpdate& result, bool overlap) {
    if (next_service_ == std::numeric_limits<std::uint64_t>::max())
        throw std::overflow_error("Sequencer service generation exhausted");
    const auto& request = entry.requests.front();
    entry.service = {request.owner, line, next_service_++, request.kind, cycle,
                     overlap ? cycle-request.model_accept_cycle : 0};
    entry.inflight = false;
    entry.parked = false;
    result.actions.push_back({SequencerActionKind::ReissueReady, request.owner, entry.service});
}

SequencerUpdate OnlineSequencer::retry_service(const SequencerService& service,
                                               std::uint64_t cycle) {
    SequencerUpdate result;
    auto it=lines_.find(service.line);
    if(it==lines_.end() || !it->second.inflight ||
       !matches(service,it->second.service) || cycle<it->second.admitted_cycle)return result;
    result.callback_accepted=true;result.status=SequencerStatus::NewService;
    ++revision_;reissue(service.line,it->second,cycle,result);
    return result;
}

SequencerUpdate OnlineSequencer::callback(const SequencerService& service,
                                         SequencerCallback kind, std::uint64_t cycle,
                                         bool no_coales) {
    SequencerUpdate result;
    auto it = lines_.find(service.line);
    if (it == lines_.end() || !it->second.inflight ||
        !matches(service, it->second.service) || cycle < it->second.admitted_cycle ||
        (kind == SequencerCallback::Read) != is_read(service.kind)) return result;
    result.callback_accepted = true;
    result.status = SequencerStatus::NewService;
    auto& entry = it->second;
    entry.inflight = false;
    ++revision_;
    bool completed_any = false;
    while (!entry.requests.empty()) {
        const auto request = entry.requests.front();
        if (kind == SequencerCallback::Read && !is_read(request.kind)) {
            reissue(service.line, entry, cycle, result);
            break;
        }
        if (kind == SequencerCallback::Write && request.ordered_store) {
            if (ordered_.empty()) throw std::logic_error("ordered store missing FIFO owner");
            if (ordered_.front() != request.owner) {
                entry.parked = true;
                result.actions.push_back({SequencerActionKind::Parked, request.owner, entry.service});
                break;
            }
            if (visible_line_ && visible_line_->first == cycle &&
                visible_line_->second != service.line) {
                // Native issueRequest re-enters L0 now; backend latency decides
                // its next callback, rather than imposing a fabricated cycle.
                reissue(service.line, entry, cycle, result, true);
                break;
            }
        }
        if (kind == SequencerCallback::Write && no_coales && completed_any) {
            reissue(service.line, entry, cycle, result);
            break;
        }
        if (request.ordered_store) {
            ordered_.pop_front();
            visible_line_ = std::make_pair(cycle, service.line);
        }
        result.wrote |= request.kind == SequencerKind::Write;
        result.actions.push_back({SequencerActionKind::Completed, request.owner, entry.service});
        entry.requests.pop_front();
        --outstanding_; ++released_; completed_any = true;
    }
    if (entry.requests.empty()) lines_.erase(it);
    if (kind == SequencerCallback::Write && !ordered_.empty()) {
        // Native wakeParkedOrderedHead only after writeCallback. Never reuse a
        // permission observed by the old callback: issue a fresh L0 service.
        for (auto& pair : lines_) {
            if (pair.second.parked && pair.second.requests.front().owner == ordered_.front()) {
                reissue(pair.first, pair.second, cycle, result, true);
                break;
            }
        }
    }
    return result;
}

void OnlineSequencer::validate() const {
    std::size_t count = 0, ordered_count = 0;
    std::vector<PendingOwner> owners;
    for (const auto& pair : lines_) {
        const auto& entry = pair.second;
        if (entry.requests.empty() || (entry.parked && entry.inflight))
            throw std::logic_error("invalid Sequencer line state");
        if (!entry.parked && entry.service.owner != entry.requests.front().owner)
            throw std::logic_error("Sequencer service lost head owner");
        for (const auto& request : entry.requests) {
            if (request.owner.core != core_ ||
                (request.address & ~(line_bytes_ - 1)) != pair.first ||
                std::find(owners.begin(), owners.end(), request.owner) != owners.end())
                throw std::logic_error("Sequencer owner/line identity mismatch");
            owners.push_back(request.owner);
            ++count;
            if (request.ordered_store) {
                ++ordered_count;
                if (std::count(ordered_.begin(), ordered_.end(), request.owner) != 1)
                    throw std::logic_error("ordered store missing/duplicate FIFO membership");
            }
        }
    }
    if (count != outstanding_ || count > capacity_ ||
        accepted_ - released_ != count || ordered_count != ordered_.size())
        throw std::logic_error("Sequencer request conservation failed");
}

ProtocolCredits::ProtocolCredits(const ResourceLimits& limits) : limits_(limits) {
    if (!limits_.l1_tbes || !limits_.l2_tbes || !limits_.llc_tbes ||
        !limits_.l1_data_fills || !limits_.l2_data_fills)
        throw std::invalid_argument("protocol credit capacities must have positive typed units");
}
std::size_t ProtocolCredits::tbes(ProtocolLevel level) const {
    return static_cast<std::size_t>(std::count_if(leases_.begin(), leases_.end(),
        [level](const auto& lease) { return lease.demand.level == level && lease.demand.tbe; }));
}
std::size_t ProtocolCredits::fills(ProtocolLevel level) const {
    return static_cast<std::size_t>(std::count_if(leases_.begin(), leases_.end(),
        [level](const auto& lease) { return lease.demand.level == level && lease.demand.data_fill; }));
}
ProtocolStatus ProtocolCredits::peek(const ProtocolLease& lease) const {
    const auto& demand = lease.demand;
    std::uint32_t tbe_capacity = 0, fill_capacity = 0;
    switch (demand.level) {
    case ProtocolLevel::L1: tbe_capacity = limits_.l1_tbes; fill_capacity = limits_.l1_data_fills; break;
    case ProtocolLevel::L2: tbe_capacity = limits_.l2_tbes; fill_capacity = limits_.l2_data_fills; break;
    case ProtocolLevel::Llc: tbe_capacity = limits_.llc_tbes; break;
    default: return ProtocolStatus::Invalid;
    }
    if (!lease.service_generation || !demand.tbe || (demand.data_fill && !fill_capacity))
        return ProtocolStatus::Invalid;
    for (const auto& live : leases_) if (same_lease(live, lease)) return ProtocolStatus::Duplicate;
    if (tbes(demand.level) >= tbe_capacity) return ProtocolStatus::TbeFull;
    if (demand.data_fill && fills(demand.level) >= fill_capacity) return ProtocolStatus::FillFull;
    return ProtocolStatus::Ready;
}
bool ProtocolCredits::acquire(const ProtocolLease& lease) {
    if (peek(lease) != ProtocolStatus::Ready) return false;
    leases_.push_back(lease);
    return true;
}
bool ProtocolCredits::release(const ProtocolLease& lease) {
    const auto it = std::find_if(leases_.begin(), leases_.end(),
        [&](const auto& live) { return same_lease(live, lease) && same_demand(live.demand, lease.demand); });
    if (it == leases_.end()) return false;
    leases_.erase(it);
    return true;
}
void ProtocolCredits::validate() const {
    ProtocolCredits rebuilt(limits_);
    for (const auto& lease : leases_)
        if (!rebuilt.acquire(lease)) throw std::logic_error("protocol credit conservation failed");
}
} // namespace fastsim::spr
