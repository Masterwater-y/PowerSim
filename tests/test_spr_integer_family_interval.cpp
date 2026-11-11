// Standalone regression; link against the updated FastSim library.
#include "fastsim/interval_core.hpp"
#include "fastsim/trace.hpp"
#include <cassert>
#include <string>
using namespace fastsim;

class FamilyTrace final : public TraceSource {
 public:
    fst::StaticUopFactsRow facts;
    fst::CoreFunctionalRow core;
    std::string policy=std::string(64,'a');
    mutable unsigned policy_reads=0, row_reads=0;
    bool present=true,core_present=true;
    FamilyTrace() {
        facts.flags=fst::kStaticExecutionKnown;
        facts.integer_family=1;facts.candidate_ports=0x863;facts.nominal_latency=1;
        core.known_flags=fst::kCoreStaticKnown|fst::kCoreFusionKnown;
    }
    bool next(TraceRecord&) override{return false;}
    std::string description() const override{return "family-test";}
    std::string static_uop_policy_identity() const override{++policy_reads;return policy;}
    const fst::StaticUopFactsRow* current_static_uop_facts()const override{
        ++row_reads;return present?&facts:nullptr;
    }
    const fst::CoreFunctionalRow* current_core_functional_facts()const override{
        return core_present?&core:nullptr;
    }
};

int main(){
    SimulatorConfig config;
    config.spr_shared_fu=true;
    config.spr_shared_fu_gap_aware=true;
    config.spr_integer_family_enabled=true;
    config.spr_integer_family_policy_sha256=std::string(64,'a');
    config.spr_integer_family_resources_verified=true;
    config.integer_alu_latency=9; // managed row supplies target nominal latency 1
    config.validate();
    auto missing=config;missing.spr_integer_family_resources_verified=false;
    bool rejected=false;try{missing.validate();}catch(const std::invalid_argument&){rejected=true;}assert(rejected);
    missing=config;missing.spr_integer_family_policy_sha256="invalid";
    rejected=false;try{missing.validate();}catch(const std::invalid_argument&){rejected=true;}assert(rejected);
    FamilyTrace source;
    TraceRecord record;record.pc=0x1000;record.flags=kRetires;record.op_class=1;
    IntervalCoreModel core(config);
    auto managed=core.schedule(record,false,false,0,false,&source);
    assert(managed.spr_integer_route==IntervalTiming::IntegerRoute::Managed);
    assert(managed.completion_cycle==managed.issue_cycle+config.issue_to_execute+1);
    core.schedule(record,false,false,0,false,&source);
    assert(source.policy_reads==1); // source identity is cached, not per UOP
    source.facts.integer_family=0;source.facts.candidate_ports=0;
    assert(core.schedule(record,false,false,0,false,&source).spr_integer_route==IntervalTiming::IntegerRoute::Generic);
    source.facts.zero_idiom=1;
    assert(core.schedule(record,false,false,0,false,&source).spr_integer_route==IntervalTiming::IntegerRoute::StaticZeroFallback);
    source.facts.zero_idiom=0;source.facts.integer_family=1;source.facts.candidate_ports=0x863;
    source.core.static_flags=fst::kCoreFullGprMove;
    assert(core.schedule(record,false,false,0,false,&source).spr_integer_route==IntervalTiming::IntegerRoute::MoveFallback);
    source.core.static_flags=0;source.core.fusion_producer_family=1;
    assert(core.schedule(record,false,false,0,false,&source).spr_integer_route==IntervalTiming::IntegerRoute::FusionFallback);
    source.core.fusion_producer_family=0;source.core_present=false;
    assert(core.schedule(record,false,false,0,false,&source).spr_integer_route==IntervalTiming::IntegerRoute::Unknown);
    const auto counts=core.committed_pipeline_audit();
    assert(counts.spr_integer_managed==2 && counts.spr_integer_generic==1 &&
           counts.spr_integer_unknown==1 && counts.spr_integer_zero_fallback==1 &&
           counts.spr_integer_move_fallback==1 && counts.spr_integer_fusion_fallback==1);
    core.reset_measurement_audit();
    assert(core.committed_pipeline_audit().spr_integer_managed==0);
    FamilyTrace wrong;wrong.policy=std::string(64,'b');
    assert(core.schedule(record,false,false,0,false,&wrong).spr_integer_route==IntervalTiming::IntegerRoute::Unknown);
    auto disabled=config;disabled.spr_integer_family_enabled=false;
    IntervalCoreModel old(disabled);FamilyTrace untouched;
    assert(old.schedule(record,false,false,0,false,&untouched).spr_integer_route==IntervalTiming::IntegerRoute::Disabled);
    assert(untouched.policy_reads==0 && untouched.row_reads==0);
    IntervalCoreModel old1(disabled),old2(disabled);FamilyTrace ignored;
    for(unsigned i=0;i<32;++i) {
        auto x=record;x.op_class=(i%3==0)?2:1;
        const auto with=old1.schedule(x,false,false,0,false,&ignored);
        const auto without=old2.schedule(x,false,false,0,false,nullptr);
        assert(with.issue_cycle==without.issue_cycle && with.completion_cycle==without.completion_cycle && with.retire_cycle==without.retire_cycle);
    }
    assert(ignored.policy_reads==0 && ignored.row_reads==0);

    // Managed and generic routes must consume the same physical port ledger.
    auto shared=config;
    shared.fetch_width=shared.decode_width=shared.rename_width=shared.dispatch_width=shared.issue_width=8;
    IntervalCoreModel mixed(shared);FamilyTrace traffic;
    auto first=mixed.schedule(record,false,false,0,false,&traffic);
    auto second=mixed.schedule(record,false,false,0,false,&traffic);
    traffic.facts.integer_family=0;traffic.facts.candidate_ports=0;
    TraceRecord p0=record;p0.op_class=9;
    auto restricted=mixed.schedule(p0,false,false,0,false,&traffic);
    assert(first.issue_cycle==second.issue_cycle && restricted.issue_cycle>first.issue_cycle);
}
