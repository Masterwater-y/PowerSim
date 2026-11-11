#pragma once
#include "fastsim/spr_port_calendar.hpp"
#include <array>
#include <cstdint>

namespace fastsim {
// Managed ExecutionPorts arbitration. Callers supply the target's static
// family/candidate mask and model-derived resource/forwarding eligibility;
// no OoO grant or timestamp is an input. `probe` never mutates the cursor,
// including on failure. Generic FUPool users must reserve the same calendar.
// Calls may use predicted time gaps but cursor updates follow caller order,
// which is explicitly not claimed to reproduce target issue order.
class SprIntegerFamilyArbiter {
 public:
    static constexpr int kBusy=-1,kUnsupported=-2;
    // External-calendar variant for integration with an existing physical
    // port ledger. The caller computes availability on that shared ledger.
    int probe_available(unsigned family,unsigned candidates,unsigned available) const {
        if(!valid(family,candidates))return kUnsupported;
        const auto start=cursors_[family][candidates];
        for(unsigned i=0;i<12;++i) {
            const auto port=(start+i)%12;
            if(candidates & available & (1u<<port))return static_cast<int>(port);
        }
        return kBusy;
    }
    bool commit_cursor(unsigned family,unsigned candidates,unsigned port) {
        if(!valid(family,candidates) || port>=12 || !(candidates & (1u<<port)))return false;
        cursors_[family][candidates]=static_cast<std::uint8_t>((port+1)%12);
        return true;
    }
    int probe(unsigned family,unsigned candidates,std::uint64_t cycle,
              unsigned eligible) const {
        if(!valid(family,candidates))return kUnsupported;
        unsigned available=0;
        for(unsigned port=0;port<12;++port)
            if((eligible & (1u<<port)) && ports_[port].available(cycle,1))available|=1u<<port;
        return probe_available(family,candidates,available);
    }
    bool commit(unsigned family,unsigned candidates,std::uint64_t cycle,
                unsigned port,unsigned eligible) {
        if(!valid(family,candidates) || port>=12 ||
           !(candidates & eligible & (1u<<port)) || !ports_[port].available(cycle,1))
            return false;
        ports_[port].reserve(cycle,1);
        return commit_cursor(family,candidates,port);
    }
    // Generic queue arbitration remains a separate owner; only its physical
    // port grant is shared. No cursor in a managed family is advanced here.
    bool reserve_generic(unsigned port,std::uint64_t cycle) {
        if(port>=12 || !ports_[port].available(cycle,1))return false;
        ports_[port].reserve(cycle,1);return true;
    }
    // Only use a certified lower bound for every later requested cycle.
    void prune(std::uint64_t lower_bound) {
        for(auto& p:ports_)p.prune(lower_bound);
    }
 private:
    static bool valid(unsigned family,unsigned candidates) {
        return family>=1 && family<=5 && candidates && !(candidates & ~0xfffu);
    }
    std::array<SprPortCalendar,12> ports_{};
    std::array<std::array<std::uint8_t,4096>,6> cursors_{};
};
} // namespace fastsim
