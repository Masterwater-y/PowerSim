#include <cassert>
#include <cstring>
#include <iostream>
#include "mem/taotrace_response.hh"
using namespace gem5;
using R=TaoTraceNativeAccessRegistry;
using E=TaoTraceRubyResponseExtension;
#include "projection.hh"
#include "lsq_terminal.hh"
#include "hooks.hh"
#include "serializers.hh"
// Missing old hooks are deliberately no-ops, matching old RubyPort behavior.
// This compatibility harness yields a behavioral RED, not a compile error.
template<class T> auto accept(const RequestPtr &q,int) -> decltype(T::noteBypassAccepted(q),void()) {T::noteBypassAccepted(q);}
template<class T> void accept(const RequestPtr &,long) {}
template<class T> auto respond(const RequestPtr &q,int) -> decltype(T::noteBypassResponse(q),void()) {T::noteBypassResponse(q);}
template<class T> void respond(const RequestPtr &,long) {}
template<class T> auto accepted(const T &s,int) -> decltype(s.bypassAccepted) {return s.bypassAccepted;}
template<class T> unsigned accepted(const T &,long) {return 0;}
template<class T> auto completed(const T &s,int) -> decltype(s.bypassResponses) {return s.bypassResponses;}
template<class T> unsigned completed(const T &,long) {return 0;}
void a(const RequestPtr&q){accept<R>(q,0);} void b(const RequestPtr&q){respond<R>(q,0);}
RequestPtr req(uint64_t seq=49188){auto q=std::make_shared<Request>();q->sequence=seq;return q;}
R::Snapshot s(uint64_t seq=49188){return R::snapshot(1,seq);}
void import(const RequestPtr&q){auto e=q->getExtension<E>();assert(e);R::noteObservedLifecycle(1,q->sequence,*e);}
void pure(unsigned count=1){auto f=s();assert(f.resolved());assert(f.terminalNoRuby);assert(f.terminalReasonMask==(1u<<9));assert(accepted(f,0)==count&&completed(f,0)==count);assert(f.admissions==0&&f.responses==0&&f.hierarchyRequests==0);assert(f.externalHits==0&&f.externalMisses==0&&f.hierarchy.accesses[0]==0);}
int main(int argc,char**argv){assert(argc==2);const std::string name=argv[1];R::prepareContext(1);auto q=req();
 if(name=="preboundary") {a(q);R::noteIssuanceClosed(q);b(q);R::enableContext(1);import(q);R::noteCommitted(1,49188);pure();import(q);pure();}
 else if(name=="empty") {R::enableContext(1);R::noteIssuanceClosed(q);import(q);R::noteCommitted(1,49188);assert(!s().resolved());}
 else if(name=="crossboundary") {a(q);R::noteIssuanceClosed(q);R::enableContext(1);R::noteCommitted(1,49188);assert(!s().resolved());b(q);import(q);pure();}
 else if(name=="late_store") {R::enableContext(1);R::noteCommitted(1,49188);R::disableContext(1);a(q);R::noteIssuanceClosed(q);assert(!s().resolved());b(q);pure();auto late=req(49189);a(late);b(late);R::noteIssuanceClosed(late);assert(!s(49189).found);}
 else if(name=="split") {auto f=req(),g=req();a(f);b(f);assert(!s().resolved());a(g);R::noteIssuanceClosed(q);assert(!s().resolved());b(g);pure(2);auto aggregate=std::make_shared<E>();aggregate->merge(*q->getExtension<E>());aggregate->merge(*f->getExtension<E>());aggregate->merge(*g->getExtension<E>());q->setExtension(aggregate);R::enableContext(1);import(q);pure(2);}
 else if(name=="mixed") {for(bool pioFirst:{true,false}) {R::prepareContext(1);auto ram=req(),pio=req();if(pioFirst){a(pio);b(pio);}else {R::noteAdmission(ram,false);R::noteResponse(ram,false,false,2);}assert(!s().resolved());if(pioFirst){R::noteAdmission(ram,false);R::noteResponse(ram,false,false,2);}else{a(pio);b(pio);}assert(!s().resolved());R::noteIssuanceClosed(q);auto f=s();assert(f.resolved()&&!f.terminalNoRuby);assert(f.admissions==1&&f.responses==1&&f.externalHits==1);assert(accepted(f,0)==1&&completed(f,0)==1);}}
 else if(name=="ram") {R::noteAdmission(q,false);R::noteHierarchyRequest(q);R::noteHierarchy(1,49188,0,TaoTraceNativeCacheOutcome::Hit);fixtureTick=200;R::noteResponse(q,false,false,2);R::noteIssuanceClosed(q);R::enableContext(1);import(q);import(q);auto f=s();assert(f.resolved()&&!f.terminalNoRuby);assert(f.admissions==1&&f.responses==1&&f.hierarchy.accesses[0]==1);assert(f.firstAdmissionTick==100&&f.lastResponseTick==200);}
 else if(name=="identity") {R::enableContext(1);b(q);R::noteIssuanceClosed(q);assert(!s().resolved());R::erase(1,49188);auto missing=req();missing->sequenceValid=false;a(missing);b(missing);assert(!s().found);a(q);a(q);b(q);b(q);R::noteIssuanceClosed(q);assert(!s().resolved());}
 else if(name=="terminal") {R::enableContext(1);for(unsigned bit=0;bit<9;++bit){R::noteTerminal(q,static_cast<TaoTraceNativeTerminalReason>(1u<<bit));assert(s().resolved());R::erase(1,49188);} }
 else if(name=="clone") {a(q);R::noteIssuanceClosed(q);b(q);auto copy=std::make_shared<Request>(*q);R::erase(1,49188);R::enableContext(1);import(copy);pure();}
 else if(name=="projection") {struct Mixed : R::Snapshot {unsigned bypassAccepted=1;}; Mixed f;f.admissions=f.responses=f.hierarchyRequests=f.externalHits=1;f.hierarchy.accesses[0]=f.hierarchy.hits[0]=1;auto projected=perfRetirementNativeFacts(f,true);assert(retirement::classifyIndependent(projected)&retirement::Unknown);assert(retirement::classifyRetiredCache(projected)==(retirement::L2Unknown|retirement::L3Unknown));}
 else if(name=="forward_after_issue") {R::enableContext(1);R::noteAdmission(q,false);TerminalRequest wrapper{{q}};wrapper.noteNativeTerminal(TaoTraceNativeTerminalReason::StoreForward);assert(!s().resolved());R::noteResponse(q,false,false,2);assert(s().resolved());}
 else if(name=="prefix_import") {R::disableContext(1);a(q);R::noteIssuanceClosed(q);R::enableContext(1);b(q);assert(!s().resolved());import(q);pure();import(q);pure();}
 else if(name=="split_ram") {auto f=req(),g=req();R::noteAdmission(f,false);R::noteResponse(f,true,false,2);R::noteAdmission(g,true);R::noteResponse(g,false,true,2);R::noteIssuanceClosed(q);auto aggregate=std::make_shared<E>();aggregate->merge(*q->getExtension<E>());aggregate->merge(*f->getExtension<E>());aggregate->merge(*g->getExtension<E>());q->setExtension(aggregate);R::enableContext(1);import(q);auto x=s();assert(x.resolved()&&x.admissions==2&&x.responses==2&&x.coalesced==1&&x.aliasedAdmissions==1);}
 else if(name=="cleanup") {a(q);R::noteIssuanceClosed(q);R::noteSquashed(1,49188);b(q);assert(!s().found);auto warm=req(49189);a(warm);b(warm);R::noteIssuanceClosed(warm);R::erase(1,49189);R::enableContext(1);assert(!s(49189).found);R::disableContext(1);R::enableRetirementContext(1);auto store=req(49190);R::releaseRetired(1,49190,true);a(store);R::noteIssuanceClosed(store);b(store);assert(!s(49190).found);}
 else if(name=="prefetch") {R::enableContext(1);auto shared=std::make_shared<E>();q->setExtension(shared);auto downstream=std::make_shared<Request>(*q);downstream->setExtension(shared);a(downstream);R::noteIssuanceClosed(q);R::noteCommitted(1,49188);assert(!s().resolved());b(downstream);pure();}
 else if(name=="bad_import") {a(q);b(q);R::noteIssuanceClosed(q);R::enableContext(1);R::noteObservedLifecycle(1,49189,*q->getExtension<E>());assert(!s(49189).found);q->sequence=49189;b(q);assert(!s(49189).resolved());}
 else if(name=="hooks") {AcceptFixture upstream;ResponseFixture downstream;Packet packet{q};assert(upstream.route(&packet));assert(upstream.owner.memRequestPort.scheduled==1&&upstream.owner.memRequestPort.scheduledAt==110);R::noteIssuanceClosed(q);assert(!s().resolved());R::enableContext(1);fixtureTick=200;assert(downstream.receive(&packet));assert(upstream.deliveries==1&&upstream.deliveredAt==210&&!packet.state);import(q);R::noteCommitted(1,49188);pure();}
 else if(name=="serialize") {a(q);R::noteIssuanceClosed(q);b(q);serializeCommit(s());serializeResolution(s());serializePopulation();serializeSummary();return 0;}
 else if(name=="late_accept") {R::enableContext(1);R::noteIssuanceClosed(q);a(q);b(q);assert(!s().resolved());}
 else if(name=="mixed_reason") {auto skipped=req(),ram=req(),pio=req();R::enableContext(1);R::noteTerminal(skipped,TaoTraceNativeTerminalReason::PrefetchSkipped);a(pio);R::noteAdmission(ram,false);R::noteResponse(ram,false,false,2);b(pio);R::noteIssuanceClosed(q);assert(s().resolved()&&!s().terminalNoRuby);assert(!(s().terminalReasonMask&512));}

 else if (name == "partial_squash_before_response") {
    R::enableContext(1);
    a(q); // Fragment B has not been accepted; no main issuance closure.
    R::noteSquashed(1, 49188);
    assert(s().found && s().squashed && !s().resolved());
    assert(!s().issuanceClosed && !s().terminalNoRuby);
    b(q);
    assert(!s().found);
    assert(!q->getExtension<E>()->issuanceClosed);
    assert(!q->getExtension<E>()->terminalNoRuby);
 }
 else if (name == "partial_response_before_squash") {
    R::enableContext(1);
    a(q);
    b(q);
    assert(s().found && !s().resolved() && !s().terminalNoRuby);
    R::noteSquashed(1, 49188);
    assert(!s().found);
 }
 else if (name == "partial_mixed_squash") {
    for (bool device_first : {false, true}) {
        for (unsigned squash_after = 0; squash_after <= 2; ++squash_after) {
            R::prepareContext(1);
            R::enableContext(1);
            auto device = req(), ram = req();
            a(device);
            R::noteAdmission(ram, false);
            // A third fragment remains unissued forever after abandonment.
            for (unsigned stage = 0; stage <= 2; ++stage) {
                if (stage == squash_after) R::noteSquashed(1, 49188);
                if (stage == 2) break;
                assert(s().found && !s().resolved());
                if ((stage == 0) == device_first) b(device);
                else R::noteResponse(ram, false, false, 2);
                if (stage == 0) assert(s().found && !s().resolved());
            }
            assert(!s().found);
        }
    }
 }
 else if (name == "partial_outstanding") {
    R::enableContext(1);
    auto second_device = req(), ram = req();
    a(q);
    a(second_device);
    R::noteAdmission(ram, false);
    R::noteSquashed(1, 49188);
    b(q);
    assert(s().found && s().bypassAccepted == 2 && s().bypassResponses == 1);
    R::noteResponse(ram, false, false, 2);
    assert(s().found && !s().resolved());
    b(second_device);
    assert(!s().found);
 }
 else if (name == "retained_open") {
    R::enableContext(1);
    a(q);
    R::noteCommitted(1, 49188);
    R::disableContext(1);
    b(q);
    assert(s().found && s().committed && !s().squashed);
    assert(!s().resolved() && !s().terminalNoRuby);
    R::noteIssuanceClosed(q);
    pure(); // Retained completion still requires the real main closure.
 }
 else if (name == "partial_ruby_squash") {
    for (bool response_first : {false, true}) {
        R::prepareContext(1);
        R::enableContext(1);
        auto ram = req();
        R::noteAdmission(ram, false);
        if (response_first) R::noteResponse(ram, false, false, 2);
        R::noteSquashed(1, 49188);
        if (!response_first) {
            assert(s().found && !s().resolved());
            R::noteResponse(ram, false, false, 2);
        }
        assert(!s().found);
    }
 }
 else if (name == "partial_late_hierarchy") {
    R::enableContext(1);
    R::noteAdmission(q, false);
    R::noteHierarchyRequest(q);
    R::noteSquashed(1, 49188);
    R::noteResponse(q, false, false, 2);
    // Sequencer publishes its response before the SLICC L1 action. Keep the
    // identity until that already-admitted callback finishes, then reclaim.
    assert(s().found && !s().resolved());
    R::noteHierarchy(1, 49188, 0, TaoTraceNativeCacheOutcome::Hit);
    assert(!s().found);
 }

 else if (name == "partial_disabled_squash") {
    R::enableContext(1);
    a(q);
    R::noteSquashed(1, 49188);
    R::disableContext(1);
    assert(s().found);
    b(q);
    assert(!s().found);
    R::noteSquashed(1, 49188); // The second existing squash observer is inert.
    assert(!s().found);
 }
 else if (name == "partial_aliased_reissue") {
    R::enableContext(1);
    auto ram = req();
    a(q);
    R::noteAdmission(ram, true);
    R::noteSquashed(1, 49188);
    b(q);
    assert(s().found && s().responses == 0 && s().admissions == 1);
    // An already-accepted aliased Ruby fragment may issue its hierarchy
    // request after squash. This is not another upstream admission.
    R::noteHierarchyRequest(ram);
    R::noteResponse(ram, false, false, 2);
    assert(s().found && !s().resolved());
    R::noteHierarchy(1, 49188, 0, TaoTraceNativeCacheOutcome::Hit);
    assert(!s().found);
 }
 else if (name == "abandoned_recycling") {
    R::enableContext(1);
    for (uint64_t sequence = 50000; sequence < 55000; ++sequence) {
        auto fragment = req(sequence);
        a(fragment);
        if (sequence % 2) b(fragment);
        R::noteSquashed(1, sequence);
        if (!(sequence % 2)) b(fragment);
        assert(!s(sequence).found);
        R::noteSquashed(1, sequence);
        assert(!s(sequence).found);
    }
 }
 else return 2;
 std::cout<<name<<" passed\n";
}
