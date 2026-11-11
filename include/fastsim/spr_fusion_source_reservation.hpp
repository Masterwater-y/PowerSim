#pragma once

#include "fastsim/spr_frontend.hpp"
#include "fastsim/spr_macro_fusion.hpp"
#include <cstdint>
#include <limits>
#include <optional>

namespace fastsim {
// A single bounded source-bandwidth reservation, not a backend allocation
// group. All fields are model-owned or functional facts. The caller must
// supply the actual proposed source service cycle after byte/source/rename
// frontiers and must commit the offer atomically with its source service.
// IDs are local to one frontend lifetime; call clear on an identity reset.
// A full source width can admit the branch only because its immediately
// preceding producer already reserved a slot in THIS source service cycle.
// IDQ and rename still each charge one slot and both UOPs still retire.
class SprFusionSourceReservation {
 public:
    enum class Decision { Eligible, Ineligible, Unknown, Blocked };
    struct Producer {
        std::uint64_t sequence=0,batch_id=0;
        std::optional<std::uint64_t> service_cycle;
        SprFrontendSource source=SprFrontendSource::Mite;
        bool source_slot_charged=false;
    };
    struct Offer {
        std::uint64_t sequence=0,batch_id=0;
        std::optional<std::uint64_t> service_cycle;
        SprFrontendSource source=SprFrontendSource::Mite;
        std::uint32_t source_slots_used=0,source_width=0,ops_used=0,ops_limit=0;
        bool idq_available=false;
    };
    struct Result {
        Decision decision=Decision::Unknown;
        std::uint32_t source_slots=1,idq_slots=1,rename_slots=1,uops=1;
    };
    struct Counters {
        std::uint64_t eligible=0,fused=0,ineligible=0,unknown=0,blocked=0;
    };
    void note_producer(const Producer& producer) { producer_=producer; used_=false; }
    void clear() { producer_.reset();used_=false; }
    void reset_counters() { counters_={}; }
    const Counters& counters()const {return counters_;}

    Result preview(SprFusionPair facts,const Offer& offer) const {
        if(!producer_ || !producer_->batch_id || !offer.batch_id ||
           !producer_->service_cycle || !offer.service_cycle ||
           !producer_->source_slot_charged || !offer.source_width ||
           !offer.source_slots_used || offer.source_slots_used>offer.source_width ||
           !offer.ops_limit || !offer.ops_used || offer.ops_used>offer.ops_limit)
            return {};
        if(used_ || producer_->sequence==std::numeric_limits<std::uint64_t>::max() ||
           offer.sequence!=producer_->sequence+1 ||
           offer.batch_id!=producer_->batch_id || offer.source!=producer_->source ||
           offer.source==SprFrontendSource::Microcode ||
           *offer.service_cycle!=*producer_->service_cycle)
            return {Decision::Ineligible};
        facts.same_delivery_batch=true; // model-owned batch AND service evidence
        const auto qualification=spr_fusion_pair(facts);
        if(qualification==SprFusionDecision::Unknown)return {};
        if(qualification==SprFusionDecision::Ineligible)return {Decision::Ineligible};
        if(!offer.idq_available || offer.ops_used==offer.ops_limit)
            return {Decision::Blocked};
        return {Decision::Eligible,0,1,1,1};
    }
    // Call only when all external service and credit checks succeed and the
    // caller will immediately consume the returned source reservation.
    Result reserve(const SprFusionPair& facts,const Offer& offer) {
        const auto result=preview(facts,offer);
        switch(result.decision) {
          case Decision::Eligible: ++counters_.eligible;++counters_.fused;used_=true;break;
          case Decision::Ineligible: ++counters_.ineligible;break;
          case Decision::Unknown: ++counters_.unknown;break;
          // Static and same-cycle qualification succeeded, but a separate
          // IDQ or operation-width constraint prevented model fusion.
          case Decision::Blocked: ++counters_.eligible;++counters_.blocked;break;
        }
        return result;
    }
 private:
    std::optional<Producer> producer_;
    bool used_=false;
    Counters counters_{};
};
}
