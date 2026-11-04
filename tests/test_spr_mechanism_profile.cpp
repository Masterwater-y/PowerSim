#include "fastsim/spr_mechanism_profile.hpp"
#include <cassert>
#include <iostream>
#include <stdexcept>
using namespace fastsim::spr;

template<class F> void rejects(F f) {
    bool rejected = false;
    try { f(); } catch (const std::invalid_argument&) { rejected = true; }
    assert(rejected);
}

int main() {
    TargetProfile profile;
    assert(!profile.enabled && profile.support == SupportStatus::Unsupported);
    profile.validate(); // The disabled historical path needs no new parameters.
    profile.enabled = true;
    rejects([&] { profile.validate(); });
    profile.limits = ResourceLimits{16,16,16,48,256,256,256,112,4,4};
    profile.iq_entries = {128,72,48};
    profile.cache_load_ports = 3;
    profile.cache_store_ports = 2;
    profile.load_banks = 16;
    profile.load_bank_ports = 2;
    profile.store_buffer = true;
    profile.early_store_prepare = true;
    profile.load_bank_conflicts = true;
    rejects([&] { profile.validate(); }); // Parameters alone cannot claim support.
    profile.support = SupportStatus::Approximate;
    profile.validate();
    profile.limits.prepare_requests = 0;
    rejects([&] { profile.validate(); });
    profile.limits.prepare_requests = 16;
    profile.iq_entries[1] = 0;
    rejects([&] { profile.validate(); });
    profile.iq_entries[1] = 72;
    profile.decoupled_frontend = true;
    rejects([&] { profile.validate(); }); // Dormant target subdomain not implemented.
    profile.decoupled_frontend = false;
    profile.load_banks = 0;
    rejects([&] { profile.validate(); });

    using P = Population;
    PopulationLedger ledger;
    fastsim::PendingOwner macro{0,1,0,1}, store{0,2,0,1}, store2{0,3,0,1};
    fastsim::PendingOwner write{0,4,0,1}, hint{0,5,0,1}, request{0,6,0,1};
    fastsim::PendingOwner follower{0,7,0,1}, service{0,8,0,1};
    ledger.add_root(P::RetiredMacro, macro);
    ledger.add(P::OriginalStore, store, P::RetiredMacro, macro);
    ledger.add(P::OriginalStore, store2, P::RetiredMacro, macro);
    ledger.add(P::SyntheticWrite, write, P::OriginalStore, store);
    ledger.relate(P::SyntheticWrite, write, P::OriginalStore, store2);
    ledger.add(P::PrepareHint, hint, P::OriginalStore, store);
    ledger.add(P::OrdinaryRequest, request, P::SyntheticWrite, write);
    ledger.add(P::Follower, follower, P::OrdinaryRequest, request);
    ledger.add(P::PhysicalService, service, P::OrdinaryRequest, request);
    ledger.relate(P::PhysicalService, service, P::Follower, follower);
    ledger.validate();
    assert(ledger.count(P::RetiredMacro) == 1);
    assert(ledger.count(P::OriginalStore) == 2);
    assert(ledger.count(P::SyntheticWrite) == 1);
    assert(ledger.count(P::PrepareHint) == 1);
    assert(ledger.count(P::OrdinaryRequest) == 1);
    assert(ledger.count(P::Follower) == 1);
    assert(ledger.count(P::PhysicalService) == 1);
    // Missing generation, core, or identity is never repaired by an alias/default.
    auto stale = request; stale.generation = 0;
    rejects([&] { ledger.relate(P::PhysicalService, service, P::OrdinaryRequest, stale); });
    rejects([&] { ledger.add(P::Follower, follower, P::OrdinaryRequest, request); });
    rejects([&] { ledger.add_root(P::OriginalStore, store); });
    rejects([&] { ledger.relate(P::PrepareHint, hint, P::PhysicalService, service); });
    assert(ledger.count(P::Follower) == 1);
    std::cout << "SPR profile and independent population ledger tests passed\n";
}
