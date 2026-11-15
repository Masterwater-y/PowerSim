#include <cassert>
#include <cstring>
#include <iostream>
#include "mem/taotrace_response.hh"
using namespace gem5;
using R=TaoTraceNativeAccessRegistry;
using E=TaoTraceRubyResponseExtension;
#include "projection.hh"
#include "issue_fixture.hh"
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
template<class T> auto aliasCategory(const T &f,int) -> decltype(f.spr_failed_alias_category) {return f.spr_failed_alias_category;}
template<class T> unsigned aliasCategory(const T &,long) {return 255;}
template<class T> auto prepareStatus(const T &f,int) -> decltype(f.spr_failed_prepare_status) {return f.spr_failed_prepare_status;}
template<class T> unsigned prepareStatus(const T &,long) {return 255;}
template<class T> auto prepareInvalidation(const T &f,int) -> decltype(f.spr_failed_prepare_invalidation) {return f.spr_failed_prepare_invalidation;}
template<class T> unsigned prepareInvalidation(const T &,long) {return 0;}
template<class T> auto prepareBegin(int core,uint64_t line,bool fill,int) -> decltype(T::sprPrepareBegin(core,line,fill)) {return T::sprPrepareBegin(core,line,fill);}
template<class T> uint64_t prepareBegin(int,uint64_t,bool,long) {return 0;}
template<class T> auto prepareFollow(const RequestPtr &q,int core,uint64_t line,int) -> decltype(T::sprPrepareFollow(q,core,line),void()) {T::sprPrepareFollow(q,core,line);}
template<class T> void prepareFollow(const RequestPtr &,int,uint64_t,long) {}
template<class T> auto prepareData(uint64_t id,int) -> decltype(T::sprPrepareData(id),void()) {T::sprPrepareData(id);}
template<class T> void prepareData(uint64_t,long) {}
template<class T> auto prepareClose(uint64_t id,int) -> decltype(T::sprPrepareClose(id),void()) {T::sprPrepareClose(id);}
template<class T> void prepareClose(uint64_t,long) {}
template<class T> auto prepareInvalidate(int core,uint64_t line,int) -> decltype(T::sprPrepareInvalidate(core,line),void()) {T::sprPrepareInvalidate(core,line);}
template<class T> void prepareInvalidate(int,uint64_t,long) {}
template<class T> auto prepareLive(int) -> decltype(T::sprPrepareLive()) {return T::sprPrepareLive();}
template<class T> unsigned prepareLive(long) {return 999999;}
template<class T> auto rmwAlias(const RequestPtr &q,unsigned outcome,int) -> decltype(T::sprAliasRmwAdmission(q,outcome),void()) {T::sprAliasRmwAdmission(q,outcome);}
template<class T> void rmwAlias(const RequestPtr &,unsigned,long) {}
template<class T> auto borrowService(uint64_t id,int) -> decltype(T::sprBorrowService(id)) {return T::sprBorrowService(id);}
template<class T> std::shared_ptr<spr_retired::Result> borrowService(uint64_t,long) {return {};}
template<class T> auto rebindFollower(const RequestPtr &q,const std::shared_ptr<spr_retired::Result> &r,bool data,int) -> decltype(T::sprRebindPrepareFollower(q,r,data),void()) {T::sprRebindPrepareFollower(q,r,data);}
template<class T> void rebindFollower(const RequestPtr &,const std::shared_ptr<spr_retired::Result> &,bool,long) {}
template<class T> auto prepareLeader(uint64_t id,uint64_t generation,int) -> decltype(T::sprMarkPrepareLeader(id,generation),void()) {T::sprMarkPrepareLeader(id,generation);}
template<class T> void prepareLeader(uint64_t,uint64_t,long) {}
void a(const RequestPtr&q){accept<R>(q,0);} void b(const RequestPtr&q){respond<R>(q,0);}
RequestPtr req(uint64_t seq=49188){auto q=std::make_shared<Request>();q->sequence=seq;return q;}
R::Snapshot s(uint64_t seq=49188){return R::snapshot(1,seq);}
void import(const RequestPtr&q){auto e=q->getExtension<E>();assert(e);R::noteObservedLifecycle(1,q->sequence,*e);}
void pure(unsigned count=1){auto f=s();assert(f.resolved());assert(f.terminalNoRuby);assert(f.terminalReasonMask==(1u<<9));assert(accepted(f,0)==count&&completed(f,0)==count);assert(f.admissions==0&&f.responses==0&&f.hierarchyRequests==0);assert(f.externalHits==0&&f.externalMisses==0&&f.hierarchy.accesses[0]==0);}
int main(int argc,char**argv){assert(argc>=2);const std::string name=argv[1];R::prepareContext(1);auto q=req();
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
 else if(name=="late_prefetch_oracle") {R::enableContext(1);R::noteAdmission(q,false);R::noteHierarchyRequest(q);R::noteLatePrefetch(1,49188,0);R::noteHierarchy(1,49188,0,TaoTraceNativeCacheOutcome::TagMiss);R::noteResponse(q,false,true,2);R::noteIssuanceClosed(q);auto f=s();assert(f.resolved());assert(f.hierarchy.latePrefetches[0]==1);auto projected=perfRetirementNativeFacts(f,true);assert(projected.l1_late_prefetches==1);assert(retirement::classifyRetiredCache(projected)==(retirement::L2Unknown|retirement::L3Unknown));auto before=f.hierarchy;before.merge(f.hierarchy);assert(before.latePrefetches[0]==2);R::noteLatePrefetch(1,49188,9);assert(s().hierarchy.latePrefetches[0]==1);}
 else if(name=="prefetch_parent_witness") {
   R::enableContext(1);R::noteAdmission(q,false);R::noteHierarchyRequest(q);
   R::noteHierarchyAttempt(q,51);
   R::noteLatePrefetch(1,49188,1);R::noteHierarchy(1,49188,0,TaoTraceNativeCacheOutcome::TagMiss);
   prefetch_lineage::Oracle oracle(2);auto id=oracle.begin(0,0x1000);
   assert(oracle.lookup(id,0,0x1000,1));assert(oracle.attach(id,0,0x1000,1,49188,51));
   assert(oracle.response(id,0,0x1000,1,4,2));
   auto witness=oracle.finish(id,0,0x1000);assert(witness);
   R::noteL2PrefetchParentLlc(witness->context,witness->sequence,witness->attempt,witness->llc_outcome,
                              witness->response_sender_kind,witness->response_sender_id,witness->response_kind);
   R::noteHierarchyCompletion(q,51);
   R::noteResponse(q,true,false,2);R::noteIssuanceClosed(q);auto f=s();
   assert(f.resolved()&&f.hierarchy.l2PrefetchParentLlc[1]==1);
   auto projected=perfRetirementNativeFacts(f,true);assert(projected.l2_prefetch_parent_llc[1]==1);
   assert(projected.l2_prefetch_parent_sender[1]==1 && projected.l2_prefetch_parent_sender_id==4);
   assert(projected.l2_prefetch_parent_response[2]==1);
   assert(retirement::classifyRetiredCache(projected)==(retirement::L2Unknown|retirement::L3Unknown));
   auto copy=f.hierarchy;copy.merge(f.hierarchy);assert(copy.l2PrefetchParentLlc[1]==2);
 }
 else if(name=="prefetch_parent_attempt_guard") {
   // No inferred whole-UOP parent outcome if the issuance set has two
   // fragments/attempts, identity is missing, or the IDs disagree.
   for (int mode=0;mode<12;++mode) {
     R::prepareContext(1);R::enableContext(1);auto rq=req();
     R::noteAdmission(rq,false);R::noteHierarchyRequest(rq);
     R::noteHierarchyAttempt(rq,mode==0?0:51);
     R::noteLatePrefetch(1,49188,1);
     R::noteHierarchy(1,49188,0,TaoTraceNativeCacheOutcome::TagMiss);
     R::noteL2PrefetchParentLlc(1,49188,mode==1?52:51,1,1,4,2);
     if(mode==2) { R::noteAdmission(rq,false);R::noteHierarchyRequest(rq);
                    R::noteHierarchyAttempt(rq,52);
                    R::noteHierarchy(1,49188,0,TaoTraceNativeCacheOutcome::TagMiss); }
     if(mode==3) R::noteL2PrefetchParentLlc(1,49188,51,0,1,4,2);
     if(mode==4) R::noteHierarchyAttempt(rq,52); // retry, no unique attempt
     if(mode!=6) R::noteHierarchyCompletion(rq,mode==7?52:(mode==9?0:51));
     if(mode==8) R::noteHierarchyCompletion(rq,51);
     if(mode==10) R::noteTerminal(rq,TaoTraceNativeTerminalReason::StoreForward);
     if(mode==11) { a(rq);b(rq); }
     R::noteResponse(rq,true,mode==5,2);
     if(mode==2) R::noteResponse(rq,true,false,2);
     R::noteIssuanceClosed(rq);
     auto f=s();assert(f.resolved());auto projected=perfRetirementNativeFacts(f,true);
     for(auto n:projected.l2_prefetch_parent_llc) assert(n==0);
     for(auto n:projected.l2_prefetch_parent_sender) assert(n==0);
     assert(projected.l2_prefetch_parent_sender_id==-1);
     assert(projected.l2_late_prefetches==1);
     if(mode!=10) assert(retirement::classifyRetiredCache(projected)&retirement::L3Unknown);
   }
 }
 else if(name=="prefetch_parent_issue_before_begin") {
   // No functional pretracking: the existing native observer can return
   // attempt=0 at issue, even if the demand later meets a tracked prefetch.
   R::disableContext(1);R::noteAdmission(q,false);R::noteHierarchyRequest(q);
   R::noteHierarchyAttempt(q,0);R::enableContext(1);
   prefetch_lineage::Oracle oracle(2);auto id=oracle.begin(0,0x1000);
   assert(oracle.lookup(id,0,0x1000,1));
   R::noteLatePrefetch(1,49188,1);
   assert(!oracle.attach(id,0,0x1000,1,49188,0));
   assert(!oracle.finish(id,0,0x1000));
   R::noteHierarchy(1,49188,0,TaoTraceNativeCacheOutcome::TagMiss);
   R::noteHierarchyCompletion(q,0);
   R::noteResponse(q,true,false,2);R::noteIssuanceClosed(q);import(q);
   auto f=s();assert(f.resolved());auto projected=perfRetirementNativeFacts(f,true);
   assert(projected.l2_late_prefetches==1);
   for(auto n:projected.l2_prefetch_parent_llc) assert(n==0);
   assert(retirement::classifyRetiredCache(projected)&retirement::L3Unknown);
 }
 else if(name=="prefetch_parent_extension_fails_closed") {
   // Production also instantiates the projection for the Request extension.
   // It lacks attempt/witness uniqueness facts and must never qualify them.
   TaoTraceRubyResponseExtension ext;
   ext.admissions=ext.responses=ext.hierarchyRequests=1;
   ext.externalMisses=1;ext.issuanceClosed=true;
   ext.hierarchy.accesses[0]=ext.hierarchy.tagMisses[0]=1;
   ext.hierarchy.l2PrefetchParentLlc[1]=1;
   assert(ext.resolved());
   auto projected=perfRetirementNativeFacts(ext,true);
   for(auto n:projected.l2_prefetch_parent_llc) assert(n==0);
   assert(retirement::classifyRetiredCache(projected)&retirement::L3Unknown);
 }
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
 else if (name == "membership_attempts") {
    using namespace retired_membership;
    R::enableContext(1);
    for (unsigned id=1;id<=2;++id) {
        auto fragment=req();
        R::noteAdmission(fragment,false);
        R::noteHierarchyRequest(fragment);
        R::noteHierarchyAttempt(fragment,id);
        const auto outcome=id==1 ? TaoTraceNativeCacheOutcome::Hit :
                                   TaoTraceNativeCacheOutcome::TagMiss;
        R::noteHierarchy(1,49188,0,outcome);
        R::noteAttemptHierarchy(1,49188,id,0,outcome);
        if (id==2) {
            R::noteHierarchy(1,49188,1,TaoTraceNativeCacheOutcome::Hit);
            R::noteAttemptHierarchy(1,49188,id,1,TaoTraceNativeCacheOutcome::Hit);
        }
        R::noteHierarchyCompletion(fragment,id);
        R::noteResponse(fragment,id==2,false,2);
    }
    R::noteIssuanceClosed(q);
    auto facts=perfRetirementNativeFacts(s(),s().resolved());
    assert(facts.membership_fragments.size()==2);
    CaseInput input;input.fragments=facts.membership_fragments;
    auto result=evaluate(input);
    assert(result.labels[L1Hit].membership==Membership::Member);
    assert(result.labels[L1Miss].membership==Membership::Member);
    assert(result.labels[L2Hit].membership==Membership::Member);
    assert(result.labels[L3Miss].membership==Membership::Nonmember);
 }
 else if (name == "membership_coalesced") {
    R::enableContext(1);
    R::noteAdmission(q,true);
    R::noteHierarchyCompletion(q,0,true,false);
    R::noteResponse(q,true,true,2);
    R::noteIssuanceClosed(q);
    auto facts=perfRetirementNativeFacts(s(),s().resolved());
    assert(facts.membership_fragments.size()==1);
    retired_membership::CaseInput input;input.fragments=facts.membership_fragments;
    auto result=retired_membership::evaluate(input);
    assert(result.labels[retired_membership::FbHit].membership==
        retired_membership::Membership::Member);
 }
 else if (name == "membership_overflow") {
    R::enableContext(1);
    for (unsigned id=1;id<=17;++id) R::noteHierarchyAttempt(q,id);
    assert(s().attemptOverflow && s().attempts.size()==16);
    auto facts=perfRetirementNativeFacts(s(),true);
    assert(facts.membership_fragments.empty());
 }
 else if (name == "spr_v2_slicc_hit_order") {
    assert(argc==3); R::enableContext(1); R::noteAdmission(q,false);
    R::noteHierarchyAttempt(q,9001);
    for (const char action : std::string(argv[2])) {
        if (action=='o' || action=='p') R::sprLookup(9001,0,0);
        if (action=='h') R::noteHierarchyCompletion(q,9001);
    }
    const auto result=s().sprMiss;
    assert(result.seen && result.miss_mask==0 && result.unknown()==0);
 }
 else if (name == "spr_v2_reissue_recycles") {
    R::enableContext(1); perf_cache_native::observer().prepare(0);
    IssueFixture fixture; FixturePacket packet{q};
    for (unsigned i=0;i<70000;++i) {
        fixture.issue(&packet);
        const auto current=fixture.issue(&packet);
        R::sprLookup(current,0,0);
        R::noteHierarchyCompletion(q,current);
        perf_cache_native::observer().finish(uint64_t(reinterpret_cast<uintptr_t>(&packet)));
        R::erase(1,49188);
    }
    const auto &d=spr_retired::lifecycleDiagnostics();
    assert(d.replaced==70000 && d.completed==70000 && d.live==0 && d.peak<=2);
 }
 else if (name == "spr_v2_replaced_live_service") {
    R::enableContext(1); perf_cache_native::observer().prepare(0);
    IssueFixture fixture; FixturePacket packet{q};
    const auto old=fixture.issue(&packet);
    R::sprLookup(old,0,1); R::sprLookup(old,1,1); R::sprRetain(old);
    auto follower=req(60021); R::noteAdmission(follower,false);
    R::noteHierarchyAttempt(follower,999999); R::sprLookup(999999,0,1);
    R::sprLookup(999999,1,3); R::sprFollow(999999,old);
    const auto current=fixture.issue(&packet);
    R::sprLookup(old,2,1);
    R::noteHierarchyCompletion(follower,999999);
    assert(s(60021).sprMiss.miss_mask==7 && s(60021).sprMiss.unknown()==0);
    R::sprRelease(old);
    R::sprLookup(current,0,0);
    R::noteHierarchyCompletion(q,current);
    assert((s().sprMiss.miss_mask&3)==3);
    assert(s().sprMiss.estimated_mask&3);
    perf_cache_native::observer().finish(uint64_t(reinterpret_cast<uintptr_t>(&packet)));
    assert(spr_retired::lifecycleDiagnostics().live==0);
 }
 else if (name == "spr_prepare_slicc_order") {
    assert(argc==5);R::enableContext(1);uint64_t id=0;
    const bool fill=std::string(argv[2])=="fill";
    for (char action:std::string(argv[3]))
        if (action=='b') id=prepareBegin<R>(0,0x1000,false,0);
        else if (action=='f') id=prepareBegin<R>(0,0x1000,true,0);
    auto load=req(61900);R::noteAdmission(load,true);prepareFollow<R>(load,0,0x1000,0);
    bool completed=false;
    for (char action:std::string(argv[4])) {
        if (action=='d') prepareData<R>(id,0);
        else if (action=='h') {R::noteHierarchyCompletion(load,0,true,false);completed=true;}
        else if (action=='c') prepareClose<R>(id,0);
        else if (action=='i') prepareInvalidate<R>(0,0x1000,0);
    }
    if (!completed) R::noteHierarchyCompletion(load,0,true,false);
    auto result=s(61900).sprMiss;
    assert(result.seen && result.unknown()==0 && result.miss_mask==(fill?1:0));
    assert(result.estimated_mask==7 && prepareLive<R>(0)==0);
 }
 else if (name == "spr_prepare_service") {
    R::enableContext(1);
    for (unsigned kind=0;kind<2;++kind) {
        auto load=req(61000+kind); const auto id=prepareBegin<R>(0,0x1000,kind==1,0);
        R::noteAdmission(load,true); prepareFollow<R>(load,0,0x1000,0);
        prepareData<R>(id,0); prepareClose<R>(id,0);
        R::noteHierarchyCompletion(load,0,true,false);
        auto result=s(61000+kind).sprMiss;
        assert(result.seen && result.unknown()==0 && result.miss_mask==(kind?1:0));
        assert(result.estimated_mask==7);
    }
    auto early=req(61002); const auto first=prepareBegin<R>(0,0x2000,true,0);
    R::noteAdmission(early,true); prepareFollow<R>(early,0,0x2000,0);
    R::noteHierarchyCompletion(early,0,true,false); assert(s(61002).sprMiss.unknown()==7);
    prepareClose<R>(first,0);
    auto invalid=req(61003); const auto second=prepareBegin<R>(0,0x3000,true,0);
    R::noteAdmission(invalid,true); prepareFollow<R>(invalid,0,0x3000,0);
    prepareData<R>(second,0); prepareClose<R>(second,0); prepareInvalidate<R>(0,0x3000,0);
    R::noteHierarchyCompletion(invalid,0,true,false); assert(s(61003).sprMiss.unknown()==7);
    assert(prepareStatus(perfRetirementNativeFacts(s(61003),true),0)==19);
    assert(prepareStatus(perfRetirementNativeFacts(s(61003),true),0)&16);
    assert(prepareInvalidation(perfRetirementNativeFacts(s(61003),true),0)==1);
    auto before=req(61009); const auto pre=prepareBegin<R>(0,0x3100,true,0);
    R::noteAdmission(before,true);prepareFollow<R>(before,0,0x3100,0);
    prepareInvalidate<R>(0,0x3100,0);prepareData<R>(pre,0);prepareClose<R>(pre,0);
    R::noteHierarchyCompletion(before,0,true,false);
    assert(s(61009).sprMiss.unknown()==0 && s(61009).sprMiss.miss_mask==1);
    auto preinvalid=req(61010);const auto resident=prepareBegin<R>(0,0x3200,false,0);
    R::sprPrepareInvalidate(0,0x3200,1);R::noteAdmission(preinvalid,true);
    prepareFollow<R>(preinvalid,0,0x3200,0);prepareData<R>(resident,0);prepareClose<R>(resident,0);
    R::noteHierarchyCompletion(preinvalid,0,true,false);
    assert(s(61010).sprMiss.unknown()==0 && s(61010).sprMiss.miss_mask==1);
    assert(s(61010).sprMiss.estimated_mask==7);
    auto preinvalidNoData=req(61012);const auto noData=prepareBegin<R>(0,0x3400,false,0);
    R::sprPrepareInvalidate(0,0x3400,1);R::noteAdmission(preinvalidNoData,true);
    prepareFollow<R>(preinvalidNoData,0,0x3400,0);prepareClose<R>(noData,0);
    R::noteHierarchyCompletion(preinvalidNoData,0,true,false);
    assert(s(61012).sprMiss.unknown()==7);
    auto eviction=req(61013);const auto evicted=prepareBegin<R>(0,0x3500,false,0);
    R::sprPrepareInvalidate(0,0x3500,2);R::noteAdmission(eviction,true);
    prepareFollow<R>(eviction,0,0x3500,0);prepareData<R>(evicted,0);prepareClose<R>(evicted,0);
    R::noteHierarchyCompletion(eviction,0,true,false);
    assert(s(61013).sprMiss.unknown()==7);
    auto prefill=req(61011);const auto nonresident=prepareBegin<R>(0,0x3300,true,0);
    prepareInvalidate<R>(0,0x3300,0);R::noteAdmission(prefill,true);
    prepareFollow<R>(prefill,0,0x3300,0);prepareData<R>(nonresident,0);prepareClose<R>(nonresident,0);
    R::noteHierarchyCompletion(prefill,0,true,false);
    assert(s(61011).sprMiss.unknown()==0 && s(61011).sprMiss.miss_mask==1);
    auto old=req(61004); const auto third=prepareBegin<R>(0,0x4000,true,0);
    R::noteAdmission(old,true); prepareFollow<R>(old,0,0x4000,0);
    prepareData<R>(third,0); prepareClose<R>(third,0);
    const auto fourth=prepareBegin<R>(0,0x4000,false,0);
    R::noteHierarchyCompletion(old,0,true,false); assert(s(61004).sprMiss.unknown()==7);
    prepareClose<R>(fourth,0);
    auto left=req(61005), right=req(61006);
    const auto shared=prepareBegin<R>(0,0x5000,true,0);
    R::noteAdmission(left,true);R::noteAdmission(right,true);
    prepareFollow<R>(left,0,0x5000,0);prepareFollow<R>(right,0,0x5000,0);
    prepareData<R>(shared,0);prepareClose<R>(shared,0);
    R::noteHierarchyCompletion(left,0,true,false);
    assert(s(61005).sprMiss.miss_mask==1 && prepareLive<R>(0)==1);
    R::noteHierarchyCompletion(right,0,true,false);
    assert(s(61006).sprMiss.miss_mask==1 && prepareLive<R>(0)==0);
    auto stale=req(61008); const auto staleId=prepareBegin<R>(0,0x7000,true,0);
    R::noteAdmission(stale,true);prepareFollow<R>(stale,0,0x7000,0);
    prepareData<R>(staleId,0);prepareClose<R>(staleId,0);
    R::noteStaleServiced(stale);R::noteHierarchyCompletion(stale,0,true,false);
    assert(s(61008).sprMiss.miss_mask==1 && (s(61008).sprMiss.path_mask&(1u<<12)));
    auto staleAlias=req(61007);R::noteAdmission(staleAlias,true);
    R::sprAliasAdmission(staleAlias,0);prepareFollow<R>(staleAlias,0,0x6000,0);
    R::noteHierarchyCompletion(staleAlias,0,true,false);
    assert(s(61007).sprMiss.unknown()==7);
    for (unsigned n=0;n<70000;++n) {
        auto abandoned=req(70000+n); const auto id=prepareBegin<R>(0,0x100000+64*n,true,0);
        prepareFollow<R>(abandoned,0,0x100000+64*n,0); prepareClose<R>(id,0);
    }
    assert(prepareLive<R>(0)==0);
 }
 else if (name == "spr_prepare_leader_origin") {
    R::enableContext(1);
    for(unsigned kind=0;kind<4;++kind) {
        const auto line=0x12000+64*kind,id=910000+kind;
        const auto generation=prepareBegin<R>(0,line,kind==1,0);
        if(kind==2) prepareInvalidate<R>(0,line,0);
        auto leader=req(65000+kind);R::noteAdmission(leader,false);R::noteHierarchyAttempt(leader,id);
        prepareLeader<R>(id,kind==3?0:generation,0);R::sprLookup(id,0,3);
        auto actual=borrowService<R>(id,0);assert(actual);
        const auto result=spr_retired::convert(*actual);
        if(kind>=2) assert(result.known_mask==0);
        else {assert(result.known_mask==7 && result.miss_mask==(kind==1?1:0));
              assert(result.estimated_mask==7);}
        R::noteHierarchyCompletion(leader,id);prepareClose<R>(generation,0);
    }
 }
 else if (name == "spr_prepare_rebind") {
    R::enableContext(1);
    for(unsigned kind=0;kind<5;++kind) {
        const uint64_t seq=63000+kind, id=900000+kind, line=0x8000+64*kind;
        auto follower=req(seq);const auto service=prepareBegin<R>(0,line,true,0);
        R::noteAdmission(follower,true);prepareFollow<R>(follower,0,line,0);
        prepareData<R>(service,0);prepareClose<R>(service,0);prepareInvalidate<R>(0,line,0);
        auto leader=req(64000+kind);R::noteAdmission(leader,false);R::noteHierarchyAttempt(leader,id);
        if(kind!=3) {R::sprLookup(id,0,kind>=2?0:1);R::sprLookup(id,1,1);R::sprLookup(id,2,1);}
        auto actual=borrowService<R>(id,0);
        if(kind==4) actual->sticky_miss=1; // A previous attempt is not this follower's lookup.
        R::noteHierarchyCompletion(leader,id);
        rebindFollower<R>(follower,actual,kind==1,0);
        R::noteHierarchyCompletion(follower,0,true,false);
        const auto result=s(seq).sprMiss;
        if(kind==0 || kind==3) assert(result.unknown()==7);
        else {assert(result.unknown()==0 && result.miss_mask==(kind==1?1:0));
              assert(result.estimated_mask==7);}
    }
 }
 else if (name == "spr_rmw_alias") {
    R::enableContext(1);
    for (unsigned outcome=0;outcome<3;++outcome) {
        auto load=req(62000+outcome);R::noteAdmission(load,true);
        rmwAlias<R>(load,outcome,0);R::noteHierarchyCompletion(load,0,true,false);
        auto result=s(62000+outcome).sprMiss;
        assert(result.seen && result.unknown()==0 && result.miss_mask==(outcome?1:0));
        assert(result.estimated_mask==7);
    }
 }
 else if (name == "spr_v2_alias_diagnostic") {
    R::enableContext(1); R::noteAdmission(q,true);
    R::sprAliasUnclassified(q,6); R::noteHierarchyCompletion(q,0,true,false);
    const auto projected=perfRetirementNativeFacts(s(),true);
    assert(projected.sprMiss.unknown()==7 && aliasCategory(projected,0)==6);
 }
 else if (name == "spr_v2_extension_projection") {
    TaoTraceRubyResponseExtension extension;
    auto result=spr_retired::Result{};
    result.service=spr_retired::Service::Forwarded;
    extension.sprMiss.add(spr_retired::convert(result));
    auto projected=perfRetirementNativeFacts(extension,true);
    assert(projected.sprMiss.seen && projected.sprMiss.unknown()==0);
    assert(projected.sprMiss.miss_mask==0 && projected.sprMiss.estimated_mask==7);
 }
 else if (name == "spr_v2_live_service") {
    R::enableContext(1);
    auto leader=req(60000), follower=req(60001), sibling=req(60002);
    for(auto pair : {std::make_pair(leader,1001u),std::make_pair(follower,1002u),
                    std::make_pair(sibling,1003u)}) {
        R::noteAdmission(pair.first,false);
        R::noteHierarchyAttempt(pair.first,pair.second);
        R::sprLookup(pair.second,0,1);
    }
    R::sprLookup(1001,1,1);
    R::sprRetain(1001);
    R::sprLookup(1002,1,3);R::sprFollow(1002,1001);
    R::sprLookup(1003,1,3);R::sprFollow(1003,1001);
    // Followers attach before the actual shared LLC lookup completes.
    R::sprLookup(1001,2,1);
    R::noteHierarchyCompletion(leader,1001);
    R::noteStaleServiced(follower);
    R::noteHierarchyCompletion(follower,1002);
    R::sprRelease(1001); // Sibling holds the live service result independently.
    R::noteHierarchyCompletion(sibling,1003);
    for(auto sequence : {60000u,60001u,60002u}) {
        auto f=s(sequence);
        assert(f.sprMiss.miss_mask==7 && f.sprMiss.unknown()==0);
        assert(!f.sprIncompleteCompletions);
    }
    assert(s(60001).sprMiss.estimated_mask==7);
    assert(s(60001).sprMiss.path_mask & (1u<<14));
    auto pf=req(60003);
    R::noteAdmission(pf,false);R::noteHierarchyAttempt(pf,1004);
    R::sprLookup(1004,0,1);R::sprLookup(1004,1,3);
    R::sprPrefetchBegin(2001);R::sprPrefetchFollow(1004,2001);
    R::sprPrefetchLookup(2001,0);R::sprPrefetchFinish(2001);
    R::noteHierarchyCompletion(pf,1004);
    assert(s(60003).sprMiss.miss_mask==3 && s(60003).sprMiss.unknown()==0);
    auto fb=req(60004);R::noteAdmission(fb,true);
    R::sprAliasAdmission(fb,1);
    R::noteHierarchyCompletion(fb,0,true,false);
    assert(s(60004).sprMiss.miss_mask==1 && s(60004).sprMiss.estimated_mask==7);
    auto l1pf=req(60010);R::noteAdmission(l1pf,true);
    R::sprAliasAdmission(l1pf,2);
    R::noteHierarchyCompletion(l1pf,0,true,false);
    assert(s(60010).sprMiss.miss_mask==1 && s(60010).sprMiss.estimated_mask==7);
    assert(s(60010).sprMiss.path_mask & (1u<<4));
    auto unobservedAlias=req(60011);R::noteAdmission(unobservedAlias,true);
    R::noteHierarchyCompletion(unobservedAlias,0,true,true);
    assert(s(60011).sprMiss.unknown()==7 && s(60011).sprIncompleteCompletions==1);
    auto missing=req(60005);R::noteAdmission(missing,false);
    R::noteHierarchyAttempt(missing,1005);R::sprLookup(1005,0,1);
    R::noteHierarchyCompletion(missing,1005);
    auto failure=s(60005);
    assert(failure.sprMiss.miss_mask==1 && failure.sprMiss.unknown()==6);
    assert(failure.sprIncompleteCompletions==1 && failure.sprMissingLevels==6);
    assert(failure.sprFailedRequest==1005 && failure.sprFailedAddress==0x1000);
    R::noteHierarchyCompletion(missing,1005); // Duplicate callback is idempotent.
    assert(s(60005).sprIncompleteCompletions==1);
    auto forwarded=req(60006);
    R::noteAdmission(forwarded,false);R::noteHierarchyAttempt(forwarded,1006);
    R::sprLookup(1006,0,1);R::sprLookup(1006,1,1);R::sprLookup(1006,2,1);
    R::noteTerminal(forwarded,gem5::TaoTraceNativeTerminalReason::StoreForward);
    R::noteHierarchyCompletion(forwarded,1006); // Discarded request closes only.
    assert(s(60006).sprMiss.miss_mask==0 && s(60006).sprMiss.unknown()==0);
    // The primary macro result does not depend on diagnostic attempt arrays.
    retirement::Ledger ledger;
    retirement::Member member;member.context=1;member.sequence=60001;
    member.read=member.measured=true;member.owner=7;ledger.commit(member);
    auto facts=perfRetirementNativeFacts(s(60001),true);
    facts.membership_fragments.clear();
    ledger.resolveNative(1,60001,facts);
    auto counts=ledger.snapshot({7}).workload_task;
    for(const auto &event:counts.spr_miss)
        assert(event.count==1 && event.unknown==0 && event.estimated_count==1);
    assert(counts.spr_paths[8]==1 && counts.spr_paths[12]==1 && counts.spr_paths[14]==1);
    retirement::Ledger hints;
    retirement::Member hint;hint.context=1;hint.sequence=60007;hint.owner=7;
    hint.software_prefetch=hint.measured=true;
    hints.commit(hint);
    auto hintCounts=hints.snapshot({7}).workload_task;
    assert(hintCounts.perf_retired_loads==1 && hintCounts.spr_paths[6]==1);
    for(const auto &event:hintCounts.spr_miss)
        assert(event.count==0 && event.unknown==0 && event.estimated_nonmember==1);
    retirement::Ledger multi;
    retirement::Member first=member;first.sequence=60008;first.last=false;
    retirement::Member last=member;last.sequence=60009;last.first=false;
    retirement::NativeFacts a,b;a.resolved=b.resolved=true;
    a.sprMiss.add(spr_retired::convert(spr_retired::ordinary(1)));
    b.sprMiss.add(spr_retired::convert(spr_retired::ordinary(7)));
    multi.commit(first);multi.resolveNative(1,60008,a);
    multi.commit(last);multi.resolveNative(1,60009,b);
    auto multiCounts=multi.snapshot({7}).workload_task;
    assert(multiCounts.perf_retired_loads==1);
    assert(multiCounts.spr_miss[0].exact_count==1);
    assert(multiCounts.spr_miss[1].estimated_count==1);
    assert(multiCounts.spr_miss[2].exact_count==1);
 }
 else return 2;
 std::cout<<name<<" passed\n";
}
