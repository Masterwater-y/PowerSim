#include "fastsim/parent_service.hpp"
#include "fastsim/legacy_prepare_timing.hpp"
#include <iostream>
#include <stdexcept>

using P=fastsim::ParentServicePool;
static void require(bool ok,const char* message) {
  if (!ok) throw std::runtime_error(message);
}
int main() {
  try {
    // Selected 10..110 contains 20 queued cycles. Admission moved to 40:
    // retain completion 110 and require 80 service cycles after admission.
    const auto moved=P::estimate_read_data(40,10,110,20);
    require(moved && *moved==120,"queued interval was charged twice or service floor lost");
    const auto queued=P::estimate_read_data(20,10,110,50);
    require(queued && *queued==110,"frozen selected completion floor was discarded");
    const auto local=P::estimate_read_data(40,10,23,0);
    require(local && *local==53,"private service duration was not rebased once");
    require(!P::estimate_read_data(5,10,110,20),"future source was estimated");
    require(!P::estimate_read_data(40,10,10,0),"empty selected service was estimated");
    require(!P::estimate_read_data(40,10,23,14),"invalid queue decomposition was estimated");
    require(!P::estimate_read_data(UINT64_MAX-1,0,10,0),"estimated data overflowed");
    P p;p.configure(4);p.begin_checkpoint(1,1);
    const auto id=p.create(7,1,40,120,P::Time::kEstimated,true);
    p.set_service(id,P::Action::kFetchShared,P::Role::kLoad,120,0,120,P::Time::kEstimated);
    p.set_lower_generation(id,9);
    require(p.estimated_owner(7,50,9,false)==id,"matching fill generation lost owner");
    require(p.estimated_owner(7,50,0,true)==id,"private L2 continuation lost owner");
    require(!p.estimated_owner(7,50,10,false),"different generation merged");
    require(!p.estimated_owner(7,39,9,false),"future owner was attached");
    require(!p.estimated_owner(7,120,9,false),"completed owner was attached");
    require(p.decide(7,50).outcome==P::Outcome::kFollower && p.read_gate(id,50)==120,
            "estimated data did not constrain an ordinary follower");
    auto store=p;store.takeover(id,140,2,130);
    require(!store.estimated_owner(7,50,9,true),"store consumer reused a read-only estimated owner");
    auto proposal=p;proposal.invalidate(7);
    require(p.estimated_owner(7,50,9,false)==id && !proposal.indexed_id(7),
            "rejected snapshot destroyed incoming owner");
    p.expire_before(120);
    require(!p.indexed_id(7),"estimated owner did not expire at its data edge");
    const auto unknown=p.create(7,2,130,150,P::Time::kUnknown,true);
    require(!p.estimated_owner(7,135,9,true) &&
                p.decide(7,135).outcome==P::Outcome::kUncertified,
            "unsupported UNKNOWN became an estimated owner");
    (void)unknown;
    // A read-data estimate must not certify store permission before the
    // unchanged owner callback. Test an index still present and the expiry
    // interval where read data is ready but the callback remains in flight.
    for (bool expired : {false,true}) {
      P pending;pending.configure(8);
      const auto load=pending.create(7,1,40,150,P::Time::kEstimated,true);
      pending.set_service(load,P::Action::kFetchShared,P::Role::kLoad,
                          120,0,120,P::Time::kEstimated);
      if (expired) pending.expire_before(120);
      fastsim::LegacyPrepareTiming timing(4,16);
      fastsim::UnifiedParentCounters counters;
      const auto result=timing.store_unified({2,0,7},130,140,170,40,2,
          {P::State::kExclusive,P::Action::kNone,false},true,true,true,pending,counters);
      require(result.permission>=150 && counters.prepare_no_transaction==0,
              expired ? "estimated expiry granted store permission before callback"
                      : "estimated read completion granted store permission before callback");
    }
    std::cout<<"estimated parent time, identity and snapshot tests passed\n";
  } catch(const std::exception& e) {std::cerr<<e.what()<<'\n';return 1;}
}
