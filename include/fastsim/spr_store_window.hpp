#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <stdexcept>
#include <vector>

namespace fastsim {
// Approximate ordered issue window for ordinary committed store lines.
// This models only line concurrency and send width; merging, SQ offload,
// permission preparation and request identity are separate mechanisms.
class SprStoreWindow {
  public:
    struct CreditKey {
        std::uint64_t flags;
        std::uint32_t context, requestor;
        bool operator==(const CreditKey& rhs) const {
            return flags == rhs.flags && context == rhs.context && requestor == rhs.requestor;
        }
    };
    struct Ticket { std::uint64_t admission = 0; std::size_t slot = 0; bool credit_join = false; };
    // Optional observation only: the store that last extended a line slot.
    struct Origin {
        std::uint64_t sequence=0, send=0;
        std::uint32_t role=0, level=5;
        std::uint64_t service=0;
        std::uint32_t path=6; // six shared paths plus unknown
        std::uint64_t eligible=0, release=0;
    };
    // 1 local-no-access, 2 locked, 3 other nonregular, 4 regular fallback.
    struct BarrierOrigin { std::uint32_t kind=0; bool inherited=false; Origin parent; };
    struct Observation {
        std::uint64_t barrier_wait=0, barrier_marginal_wait=0, ordered_wait=0, port_wait=0, capacity_wait=0;
        std::uint64_t blocker_release=0;
        Origin blocker;
        BarrierOrigin barrier;
    };
    SprStoreWindow() = default;
    SprStoreWindow(std::size_t lines, std::uint32_t ports)
        : slots_(lines), ports_(ports) {
        if (!lines || !ports) throw std::invalid_argument("invalid store window");
    }
    Ticket reserve(std::uint64_t line, std::uint64_t proposed,
                   std::optional<CreditKey> key = std::nullopt) {
        if (slots_.empty()) throw std::logic_error("uninitialized store window");
        auto at = std::max({proposed, barrier_, last_send_});
        if (at == last_send_ && used_ == ports_) {
            if (at == std::numeric_limits<std::uint64_t>::max())
                throw std::overflow_error("store window send overflow");
            ++at;
        }
        expire(at);
        auto index = find_line(line);
        bool credit_join = false;
        if (index == slots_.size()) {
            index = find_free();
            if (index == slots_.size()) {
                auto earliest = std::numeric_limits<std::uint64_t>::max();
                for (const auto& slot : slots_) earliest = std::min(earliest, slot.release);
                at = std::max(at, earliest);
                expire(at);
                index = find_free();
            }
            slots_[index] = Slot{line, at, true, key, {}};
        } else {
            credit_join = key && slots_[index].key && *key == *slots_[index].key;
            if (!credit_join) slots_[index].key.reset();
        }
        if (at != last_send_) { last_send_ = at; used_ = 0; }
        ++used_;
        return Ticket{at, index, credit_join};
    }
    void complete(const Ticket& ticket, std::uint64_t response) {
        auto& slot = slots_.at(ticket.slot);
        if (!slot.active || response < ticket.admission)
            throw std::logic_error("invalid store window response");
        slot.release = std::max(slot.release, response);
    }
    void complete(const Ticket& ticket, std::uint64_t response, Origin origin) {
        auto& slot=slots_.at(ticket.slot);
        origin.release=response;
        if (response>=slot.release) slot.origin=origin;
        complete(ticket,response);
    }
    // Does not mutate the window and is only used by explicit diagnostics.
    Observation inspect(std::uint64_t line,std::uint64_t proposed) const {
        Observation observation;
        const auto without_barrier=[&]() {
            auto candidate=std::max(proposed,last_send_);
            if(candidate==last_send_ && used_==ports_) {
                if(candidate==std::numeric_limits<std::uint64_t>::max())
                    throw std::overflow_error("store window send overflow");
                ++candidate;
            }
            const Slot* first=nullptr;
            for(const auto& slot:slots_) {
                if(!slot.active || slot.release<=candidate || slot.line==line)return candidate;
                if(!first || slot.release<first->release)first=&slot;
            }
            return first?first->release:candidate;
        };
        const auto finish=[&](std::uint64_t admission) {
            if(observation.barrier_wait) {
                const auto candidate=without_barrier();
                observation.barrier_marginal_wait=admission>candidate?admission-candidate:0;
            }
            return observation;
        };
        auto at=proposed;
        if (barrier_>at) { observation.barrier_wait=barrier_-at; observation.barrier=barrier_origin_; at=barrier_; }
        if (last_send_>at) { observation.ordered_wait=last_send_-at; at=last_send_; }
        if (at==last_send_ && used_==ports_) {
            if (at==std::numeric_limits<std::uint64_t>::max())
                throw std::overflow_error("store window send overflow");
            ++at; observation.port_wait=1;
        }
        const Slot* earliest=nullptr;
        for (const auto& slot:slots_) {
            if (!slot.active || slot.release<=at) return finish(at);
            if (slot.line==line) return finish(at);
            if (!earliest || slot.release<earliest->release) earliest=&slot;
        }
        if (earliest) {
            observation.capacity_wait=earliest->release-at;
            observation.blocker_release=earliest->release;
            observation.blocker=earliest->origin;
            return finish(earliest->release);
        }
        return finish(at);
    }
    void barrier_until(std::uint64_t cycle) { barrier_ = std::max(barrier_, cycle); }
    void barrier_until(std::uint64_t cycle,BarrierOrigin origin) {
        if(cycle>barrier_)barrier_origin_=origin;
        barrier_until(cycle);
    }
    Origin origin_for_frontier(std::uint64_t cycle) const {
        for(const auto& slot:slots_) if(slot.active && slot.release==cycle)return slot.origin;
        if(barrier_==cycle)return barrier_origin_.parent;
        return {};
    }
  private:
    struct Slot { std::uint64_t line = 0, release = 0; bool active = false; std::optional<CreditKey> key; Origin origin; };
    void expire(std::uint64_t cycle) {
        for (auto& slot : slots_) if (slot.active && slot.release <= cycle) slot.active = false;
    }
    std::size_t find_line(std::uint64_t line) const {
        for (std::size_t i=0; i<slots_.size(); ++i)
            if (slots_[i].active && slots_[i].line == line) return i;
        return slots_.size();
    }
    std::size_t find_free() const {
        for (std::size_t i=0; i<slots_.size(); ++i)
            if (!slots_[i].active) return i;
        return slots_.size();
    }
    std::vector<Slot> slots_;
    std::uint64_t last_send_ = 0, barrier_ = 0;
    BarrierOrigin barrier_origin_;
    std::uint32_t ports_ = 0, used_ = 0;
};
} // namespace fastsim
