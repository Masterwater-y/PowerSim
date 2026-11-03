#include "fastsim/store_request_engine.hpp"
#include <iostream>
#include <limits>
#include <stdexcept>
#include <vector>

namespace {
using namespace fastsim;
void require(bool yes, const char* text) { if (!yes) throw std::runtime_error(text); }
template<class F> void rejects(F f, const char* text) {
    bool rejected=false;
    try { f(); } catch (const std::logic_error&) { rejected=true; }
    catch (const std::overflow_error&) { rejected=true; }
    require(rejected,text);
}
PendingOwner key(std::uint64_t sequence) { return {0,sequence,0,1}; }
void ready(StoreRequestEngine& e, PendingOwner id) {
    e.address_ready(id); e.retire(id);
}
StoreRequestProfile profile() {
    StoreRequestProfile p; p.sq_entries=4; p.store_ports=4;
    p.optional_queue_entries=2; p.tbe_entries=2;
    return p;
}

// Removing separate prepare/mandatory owners or releasing SQ on permission
// return must break this two-cold-line overlap and takeover witness.
void parallel_prepare_and_takeover() {
    std::vector<StoreEngineEvent> events;
    StoreRequestEngine e(profile(),[&](const auto& event){events.push_back(event);});
    require(e.allocate({key(1),10}) && e.allocate({key(2),20}),"allocate two SQ entries");
    ready(e,key(1)); ready(e,key(2));
    e.advance_to(1);
    rejects([&]{e.can_writeback(key(1));},"canWB must follow commit propagation");
    e.advance_to(2); e.can_writeback(key(1)); e.can_writeback(key(2));
    require(!e.enqueue_prepare(key(2)),"ready head demand precedes optional scan");
    auto first=e.send_head();
    require(first && first->kind==StoreEngineActionKind::DemandQueued,"CPU demand enqueue precedes protocol service");
    require(e.enqueue_prepare(key(2)),"younger canWB hint accepted without waiting for first response");
    require(!e.start_prepare(),"optional enqueue is not service start");
    e.advance_to(3);first=e.start_head();auto second=e.start_prepare();
    require(second && second->kind==StoreEngineActionKind::PrepareAcquire,
            "independent second line starts a real permission transaction");
    require(e.transactions_inflight()==2 && e.sq_size()==2,"two physical acquisitions overlap; SQ remains owned");
    e.advance_to(10); e.permission_response(*first->transaction);
    require(e.transactions_inflight()==1 && e.sq_size()==2,"permission return does not free SQ");
    e.write_response(first->request);
    require(e.sq_size()==1,"only real write response releases first SQ entry");
    require(!e.send_head(),"next ordinary store waits until cycle after previous write response");
    e.advance_to(11);require(bool(e.send_head()),"second CPU demand enqueued");
    e.advance_to(12);auto joined=e.start_head();
    require(joined && joined->kind==StoreEngineActionKind::DemandTakeover &&
            joined->transaction==second->transaction,"head takes over same pending prepare");
    require(e.transactions_inflight()==1,"takeover cannot allocate a duplicate TBE");
    rejects([&]{e.write_response(joined->request);},"mandatory cannot finish before permission return");
    e.advance_to(20); e.permission_response(*second->transaction);
    require(e.sq_size()==1 && e.transactions_inflight()==0,"permission completion only releases TBE");
    e.write_response(joined->request);
    require(e.sq_size()==0 && e.tracked_lines()==0,"finished ownership/history is reclaimed");
    const auto& c=e.counters();
    require(c.sq_allocated==2 && c.sq_released==2 && c.permission_started==2 &&
            c.permission_returned==2 && c.demands_accepted==2 && c.takeovers==1,
            "independent resource and request populations conserve");
    require(!events.empty() && events.back().kind==StoreEngineEventKind::SQReleased,
            "streaming sink observes actual final release");
    rejects([&]{e.permission_response(*second->transaction);},"duplicate response cannot free a reused resource");
}

void finite_resources_and_retry() {
    auto p=profile(); p.tbe_entries=1; p.optional_queue_entries=1; p.sq_entries=3;
    StoreRequestEngine e(p);
    for (unsigned i=1;i<=3;++i) {require(e.allocate({key(i),100+i}),"allocate SQ");ready(e,key(i));}
    require(!e.allocate({key(4),104}),"SQ capacity is finite");
    e.advance_to(2);
    for(unsigned i=1;i<=3;++i)e.can_writeback(key(i));
    auto head=e.send_head(); require(bool(head),"head admitted");
    require(e.enqueue_prepare(key(2)),"optional path bypasses occupied ordinary request slot");
    require(!e.enqueue_prepare(key(3)),"full optional queue rejects atomically");
    e.advance_to(3);head=e.start_head();
    require(!e.start_prepare() && e.optional_queue_size()==1,"TBE-full cannot dequeue or start optional service");
    e.advance_to(8);e.permission_response(*head->transaction);
    auto prepared=e.start_prepare();require(bool(prepared),"TBE credit wakes queued hint");
    require(e.enqueue_prepare(key(3)),"queue rejection can retry after dequeue");
    require(e.counters().prepares_enqueued==2 && e.counters().permission_started==2,
            "rejections did not create ghost accepted requests");
}

// An accepted hint has its own identity even when it allocates no physical TBE.
void same_line_hint_identity() {
    std::vector<StoreEngineEvent> events;
    StoreRequestEngine e(profile(),[&](const auto& event){events.push_back(event);});
    e.allocate({key(1),10});e.allocate({key(2),10});ready(e,key(1));ready(e,key(2));
    e.advance_to(2);e.can_writeback(key(1));e.can_writeback(key(2));
    auto head=e.send_head();require(bool(head),"cold head");e.enqueue_prepare(key(2));
    e.advance_to(3);head=e.start_head();auto skip=e.start_prepare();
    require(skip && skip->kind==StoreEngineActionKind::PrepareSkipped &&
            skip->transaction==head->transaction && skip->request.generation!=0 &&
            skip->request!=*head->transaction,"skipped enqueue has a real distinct identity, not sentinel zero");
    bool bound=false;
    for(const auto& event:events)if(event.kind==StoreEngineEventKind::PrepareEnqueued)
        bound=event.request==skip->request;
    require(bound,"enqueue and skip keep the same accepted-hint identity");
    require(e.transactions_inflight()==1,"same line hint cannot allocate a duplicate TBE");
    e.advance_to(5);e.permission_response(*head->transaction);e.write_response(head->request);
    e.advance_to(6);e.send_head();e.advance_to(7);auto second=e.start_head();
    require(second && second->kind==StoreEngineActionKind::DemandHit && !second->transaction,
            "next same-line store uses established permission");
    e.write_response(second->request);
    require(e.counters().permission_started==1 && e.counters().sq_released==2,"one physical fill, two stores");
}

void completed_prepare_can_lose_permission() {
    StoreRequestEngine e(profile());
    e.allocate({key(1),10});e.allocate({key(2),20});ready(e,key(1));ready(e,key(2));
    e.advance_to(2);e.can_writeback(key(1));e.can_writeback(key(2));
    auto head=e.send_head();e.enqueue_prepare(key(2));
    e.advance_to(3);head=e.start_head();auto prepare=e.start_prepare();
    e.advance_to(4);e.permission_response(*prepare->transaction);
    e.advance_to(5);e.invalidate(20);
    e.advance_to(10);e.permission_response(*head->transaction);e.write_response(head->request);
    e.advance_to(11);e.send_head();e.advance_to(12);auto reacquire=e.start_head();
    require(reacquire && reacquire->kind==StoreEngineActionKind::DemandAcquire &&
            reacquire->transaction!=prepare->transaction,"invalidated prepared permission must be reacquired at FIFO head");
    rejects([&]{e.permission_response(*prepare->transaction);},"old completed callback cannot free new TBE");
    require(e.transactions_inflight()==1,"stale callback leaves current owner intact");
    e.advance_to(20);e.permission_response(*reacquire->transaction);e.write_response(reacquire->request);
    require(e.counters().permission_started==3 && e.counters().permission_returned==3 && e.sq_size()==0,
            "eviction is modeled as real extra acquisition, not prepare prohibition");
}

void invalidated_inflight_takeover_keeps_mandatory_owner() {
    StoreRequestEngine e(profile());
    e.allocate({key(1),10});e.allocate({key(2),20});ready(e,key(1));ready(e,key(2));
    e.advance_to(2);e.can_writeback(key(1));e.can_writeback(key(2));
    auto first=e.send_head();e.enqueue_prepare(key(2));
    e.advance_to(3);first=e.start_head();auto prepare=e.start_prepare();
    e.advance_to(4);e.invalidate(20);
    e.advance_to(5);e.permission_response(*first->transaction);e.write_response(first->request);
    e.advance_to(6);e.send_head();e.advance_to(7);auto head=e.start_head();
    require(head && head->kind==StoreEngineActionKind::DemandTakeover,"in-flight transaction keeps physical ownership");
    e.advance_to(7);e.permission_response(*prepare->transaction);
    require(e.counters().stale_returns==1 && e.transactions_inflight()==0 && e.sq_size()==1,
            "stale return releases its own TBE but cannot grant permission or free SQ");
    rejects([&]{e.write_response(head->request);},"stale fill cannot authorize write");
    auto retry=e.retry_head();
    require(retry && retry->kind==StoreEngineActionKind::ProtocolRetry &&
            retry->request==head->request && retry->transaction!=prepare->transaction,
            "protocol retry preserves original mandatory owner and mints new physical generation");
    rejects([&]{e.permission_response(*prepare->transaction);},"duplicate stale callback cannot release retry resources");
    require(e.transactions_inflight()==1 && e.counters().demands_accepted==2,"retry is not another CPU demand or SQ allocation");
    e.advance_to(8);e.permission_response(*retry->transaction);e.write_response(head->request);
    require(e.counters().permission_started==3 && e.counters().permission_returned==3 &&
            e.counters().sq_allocated==2 && e.counters().sq_released==2,"retry resources conserve");
}

void ports_clock_and_input_contract() {
    auto p=profile();p.store_ports=1;
    StoreRequestEngine e(p);
    auto foreign=key(1);foreign.core=1;
    rejects([&]{e.allocate({foreign,10});},"wrong core rejected");
    auto fragment=key(1);fragment.fragment=1;
    rejects([&]{e.allocate({fragment,10});},"unimplemented split member rejected");
    for(auto kind:{StoreDescriptorKind::Atomic,StoreDescriptorKind::Split})
        rejects([&]{e.allocate({key(1),10,true,kind});},"special store cannot masquerade as ordinary");
    e.allocate({key(1),10});e.allocate({key(2),20});
    e.address_ready(key(2));
    rejects([&]{e.retire(key(2));},"store retirement follows source order");
    e.address_ready(key(1));e.retire(key(1));e.retire(key(2));
    e.advance_to(2);
    rejects([&]{e.can_writeback(key(2));},"canWB scan cannot skip older unready entry");
    e.can_writeback(key(1));e.can_writeback(key(2));
    auto head=e.send_head();require(bool(head),"head uses single store port");
    require(!e.enqueue_prepare(key(2)),"prepare cannot exceed shared store-port bandwidth");
    e.advance_to(2);require(!e.enqueue_prepare(key(2)),"same-cycle advance cannot replenish port");
    rejects([&]{e.advance_to(1);},"backwards clock rejected");
    e.advance_to(3);require(e.enqueue_prepare(key(2)),"next cycle port replenished");
    require(!e.enqueue_prepare(key(2)),"accepted one-shot hint cannot be issued twice");
    rejects([&]{e.allocate({key(1),30});},"duplicate/older sequence rejected without growing history");
    require(e.counters().sq_allocated==2 && e.counters().prepares_enqueued==1,
            "rejected actions do not leave accepted populations");
}

void queued_hint_outlives_sq_and_tracks_new_service() {
    auto p=profile();p.optional_queue_latency=50;p.tbe_entries=1;
    StoreRequestEngine e(p);
    e.allocate({key(1),10});e.allocate({key(2),10});ready(e,key(1));ready(e,key(2));
    e.advance_to(2);e.can_writeback(key(1));e.can_writeback(key(2));
    auto head=e.send_head();e.enqueue_prepare(key(2));
    e.advance_to(3);head=e.start_head();
    e.advance_to(4);e.permission_response(*head->transaction);e.write_response(head->request);
    e.advance_to(5);e.send_head();e.advance_to(6);auto second=e.start_head();e.write_response(second->request);
    require(e.sq_size()==0 && e.optional_queue_size()==1 && e.tracked_lines()==1,
            "queued hint owns its identity/line after source SQ reclamation");
    e.advance_to(7);e.invalidate(10);
    e.advance_to(52);auto hint=e.start_prepare();
    require(hint && hint->kind==StoreEngineActionKind::PrepareAcquire,"late accepted hint is not silently deleted with SQ");
    e.advance_to(53);e.allocate({key(3),30});ready(e,key(3));
    e.advance_to(55);e.can_writeback(key(3));auto queued=e.send_head();
    require(queued && queued->kind==StoreEngineActionKind::DemandQueued,
            "CPU mandatory enqueue remains separate from occupied TBE");
    e.advance_to(56);require(!e.start_head(),"protocol service waits on actual transaction credit");
    require(e.sq_size()==1 && e.counters().demands_accepted==3,"queued mandatory keeps SQ/CPU owner while TBE blocked");
    e.advance_to(60);e.permission_response(*hint->transaction);auto last=e.start_head();
    require(last && last->kind==StoreEngineActionKind::DemandAcquire,"released TBE starts pending mandatory service");
    e.advance_to(70);e.permission_response(*last->transaction);e.write_response(last->request);
    require(e.tracked_lines()==0 && e.transactions_inflight()==0,"late service reclaims its own final state");
}

void newer_permission_survives_old_callback() {
    StoreRequestEngine e(profile());e.allocate({key(1),10});ready(e,key(1));
    e.advance_to(2);e.can_writeback(key(1));auto head=e.send_head();
    e.advance_to(3);head=e.start_head();e.observe_permission(10);
    e.advance_to(4);e.permission_response(*head->transaction);
    require(e.counters().stale_returns==1,"old transaction cannot claim newer permission generation");
    e.write_response(head->request);
    require(e.sq_size()==0,"stale response did not invalidate independently granted newer permission");
    e.invalidate(999);e.observe_permission(999);
    require(e.tracked_lines()==0,"unreferenced backend broadcasts do not create infinite history");
}

void noaccess_is_a_local_fifo_barrier() {
    auto p=profile();p.sq_entries=4;p.store_ports=1;StoreRequestEngine e(p);
    e.allocate({key(1),0,false,StoreDescriptorKind::NoAccess});
    e.allocate({key(2),0,false,StoreDescriptorKind::NoAccess});e.allocate({key(3),30});
    ready(e,key(1));ready(e,key(2));ready(e,key(3));e.advance_to(2);
    e.can_writeback(key(1));e.can_writeback(key(2));e.can_writeback(key(3));
    require(!e.enqueue_prepare(key(3)),"local SQ item stops the prepare scan");
    require(e.send_head()->kind==StoreEngineActionKind::LocalComplete &&
            e.send_head()->kind==StoreEngineActionKind::LocalComplete,
            "consecutive local SQ entries complete at the same canWB tick");
    require(e.send_head()->kind==StoreEngineActionKind::DemandQueued,
            "local completion consumes no cache port or mandatory owner");
    require(e.counters().local_completed==2 && e.counters().demands_accepted==1 &&
            e.transactions_inflight()==0 && e.sq_size()==1 && e.tracked_lines()==1,"local completions conserve independent resources");
}

void backend_admission_precedes_transaction_allocation() {
    StoreRequestEngine e(profile());e.allocate({key(1),10});e.allocate({key(2),20});
    ready(e,key(1));ready(e,key(2));e.advance_to(2);e.can_writeback(key(1));e.can_writeback(key(2));
    require(bool(e.send_head()),"CPU queue accepts head before backend admission");
    require(e.enqueue_prepare(key(2)),"optional CPU queue accepts follower");e.advance_to(3);
    auto deny=[](std::uint64_t){return false;};
    require(!e.start_head(deny) && !e.start_prepare(deny) && e.transactions_inflight()==0,
            "backend rejection precedes any TBE allocation or service side effect");
    bool reentry_rejected=false;
    auto follower=e.start_prepare([&](std::uint64_t line){
        try {e.invalidate(line);} catch(const std::logic_error&) {reentry_rejected=true;}
        return line==20;
    });
    require(reentry_rejected,"backend admission observer cannot reenter the engine");
    require(follower && e.transactions_inflight()==1,"blocked head line does not reject independent optional service");
    auto head=e.start_head();require(head && e.transactions_inflight()==2,"later admission starts exactly once");
}

void event_sink_is_observational_and_failure_stops_mutation() {
    StoreRequestEngine* current=nullptr;
    bool reentry_rejected=false;
    StoreRequestEngine e(profile(),[&](const StoreEngineEvent& event) {
        if(event.kind==StoreEngineEventKind::DemandStarted) {
            require(current->sq_size()==1,"sink may inspect live state");
            try {current->invalidate(event.line);}
            catch(const std::logic_error&) {reentry_rejected=true;}
        }
    });
    current=&e;e.allocate({key(1),10});ready(e,key(1));
    e.advance_to(2);e.can_writeback(key(1));e.send_head();e.advance_to(3);
    auto head=e.start_head();
    require(reentry_rejected,"observational sink cannot reenter a state mutation");
    e.permission_response(*head->transaction);e.write_response(head->request);
    require(e.sq_size()==0,"rejected sink reentry preserves normal completion");

    bool fail=true;
    StoreRequestEngine broken(profile(),[&](const StoreEngineEvent&) {
        if(fail)throw std::runtime_error("sink failed");
    });
    bool propagated=false;
    try {broken.allocate({key(1),10});}
    catch(const std::runtime_error&) {propagated=true;}
    require(propagated,"sink exception is reported to caller");fail=false;
    require(broken.sq_size()==1 && broken.counters().sq_allocated==1,
            "getter remains available after sink failure");
    rejects([&]{broken.advance_to(1);},"sink exception poisons further mutations");
    rejects([&]{broken.address_ready(key(1));},"poison applies to store events too");
}

void drain_has_no_completed_history_and_overflow_is_atomic() {
    StoreRequestEngine e(profile());
    for(unsigned i=0;i<200;++i) {
        e.advance_to(10u*i);const auto id=key(i);e.allocate({id,1000u+i});ready(e,id);
        e.advance_to(10u*i+2);e.can_writeback(id);e.observe_permission(1000u+i);
        require(bool(e.send_head()),"resident demand is queued");e.advance_to(10u*i+3);
        auto head=e.start_head();require(head && head->kind==StoreEngineActionKind::DemandHit,"resident backend permission");
        e.write_response(head->request);
        require(e.tracked_lines()==0 && e.transactions_inflight()==0 && e.sq_size()==0,
                "retired request metadata must not accumulate with trace length");
    }
    require(e.counters().sq_allocated==200 && e.counters().sq_released==200,"long stream keeps exact counters");
    rejects([&]{e.address_ready(key(0));},"old SQ callback cannot bind to reclaimed entry");
    StoreRequestEngine overflow(profile());overflow.allocate({key(1),10});overflow.address_ready(key(1));
    overflow.advance_to(std::numeric_limits<std::uint64_t>::max()-1);
    rejects([&]{overflow.retire(key(1));},"unrepresentable canWB edge rejects before publishing retire");
    rejects([&]{overflow.can_writeback(key(1));},"failed retire leaves entry unretired");
    auto p=profile();p.optional_queue_latency=std::numeric_limits<std::uint64_t>::max();
    StoreRequestEngine q(p);q.allocate({key(1),10});q.allocate({key(2),20});ready(q,key(1));ready(q,key(2));
    q.advance_to(2);q.can_writeback(key(1));q.can_writeback(key(2));q.send_head();
    rejects([&]{q.enqueue_prepare(key(2));},"queue-ready time overflow rejects atomically");
    require(q.optional_queue_size()==0 && q.counters().prepares_enqueued==0,"overflow creates no queued hint");
}
}

void test_store_request_engine() {
    parallel_prepare_and_takeover(); finite_resources_and_retry();same_line_hint_identity();
    completed_prepare_can_lose_permission();invalidated_inflight_takeover_keeps_mandatory_owner();
    ports_clock_and_input_contract();queued_hint_outlives_sq_and_tracks_new_service();
    newer_permission_survives_old_callback();drain_has_no_completed_history_and_overflow_is_atomic();
    event_sink_is_observational_and_failure_stops_mutation();
    backend_admission_precedes_transaction_allocation();
    noaccess_is_a_local_fifo_barrier();
}
#ifdef FASTSIM_STORE_REQUEST_ENGINE_STANDALONE
int main() {
    try {test_store_request_engine();std::cout<<"store request engine tests passed\n";}
    catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}
}
#endif
