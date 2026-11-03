#include "fastsim/store_request_engine.hpp"
#include <algorithm>
#include <limits>
#include <stdexcept>

namespace fastsim {
StoreRequestEngine::StoreRequestEngine(StoreRequestProfile p,EventSink sink)
    : profile_(p),sink_(std::move(sink)) {
    if(!p.sq_entries || !p.store_ports || !p.tbe_entries || !p.optional_queue_latency || !p.mandatory_queue_latency)
        throw std::invalid_argument("store request capacities/queue latency must be positive");
    add(p.commit_to_iew_delay,1);
}
std::uint64_t StoreRequestEngine::add(std::uint64_t a,std::uint64_t b) {
    if(b>std::numeric_limits<std::uint64_t>::max()-a)
        throw std::overflow_error("store request exact time overflow");
    return a+b;
}
void StoreRequestEngine::emit(StoreEngineEventKind kind,const StoreDescriptor& d,
                            std::optional<StoreRequestId> request,
                            std::optional<StoreRequestId> transaction) {
    emit_event({kind,now_,d.owner,d.line,request,transaction});
}
void StoreRequestEngine::check_mutation() const {
    if(in_sink_)throw std::logic_error("store event sink cannot reenter mutation");
    if(sink_failed_)throw std::logic_error("store event sink failed; engine is poisoned");
}
bool StoreRequestEngine::admitted(const AdmissionGate& gate,std::uint64_t address) {
    if(!gate)return true;
    in_sink_=true;
    try {const auto result=gate(address);in_sink_=false;return result;}
    catch(...) {in_sink_=false;sink_failed_=true;throw;}
}
void StoreRequestEngine::emit_event(const StoreEngineEvent& event) {
    if(!sink_)return;
    in_sink_=true;
    try {sink_(event);in_sink_=false;}
    catch(...) {in_sink_=false;sink_failed_=true;throw;}
}
void StoreRequestEngine::advance_to(std::uint64_t cycle) {
    check_mutation();
    if(cycle<now_)throw std::logic_error("backwards store request clock");
    if(cycle!=now_)ports_used_=0;
    now_=cycle;
}
StoreRequestEngine::Store& StoreRequestEngine::store(PendingOwner owner) {
    auto it=std::find_if(stores_.begin(),stores_.end(),[&](const auto& s){return s.description.owner==owner;});
    if(it==stores_.end())throw std::invalid_argument("stale or missing SQ owner");
    return *it;
}
StoreRequestEngine::Line& StoreRequestEngine::line(std::uint64_t address) {
    auto it=std::find_if(lines_.begin(),lines_.end(),[&](const auto& l){return l.line==address;});
    if(it==lines_.end())throw std::invalid_argument("untracked store permission line");
    return *it;
}
bool StoreRequestEngine::allocate(StoreDescriptor d) {
    check_mutation();
    if(d.owner.core!=profile_.core || d.owner.fragment ||
       (d.kind!=StoreDescriptorKind::Ordinary && d.kind!=StoreDescriptorKind::NoAccess))
        throw std::invalid_argument("store reference engine requires local ordinary single-fragment store");
    if(d.kind==StoreDescriptorKind::NoAccess)d.prepare_eligible=false;
    if(last_sequence_ && d.owner.sequence<=*last_sequence_)
        throw std::invalid_argument("store source sequence must increase");
    if(stores_.size()==profile_.sq_entries)return false;
    stores_.push_back(Store{d,false,false,false,std::nullopt});
    try {
        if(d.kind!=StoreDescriptorKind::NoAccess &&
           std::none_of(lines_.begin(),lines_.end(),[&](const auto& l){return l.line==d.line;}))
            lines_.push_back(Line{d.line,0,false,std::nullopt});
    } catch(...) {stores_.pop_back();throw;}
    last_sequence_=d.owner.sequence;++counters_.sq_allocated;
    emit(StoreEngineEventKind::SQAllocated,d);return true;
}
void StoreRequestEngine::address_ready(PendingOwner id) {
    check_mutation();
    auto& s=store(id);
    if(s.address)throw std::logic_error("address readiness already published");
    s.address=true;emit(StoreEngineEventKind::AddressReady,s.description);
}
void StoreRequestEngine::retire(PendingOwner id) {
    check_mutation();
    auto& s=store(id);
    if(!s.address || s.retirement)throw std::logic_error("invalid store retirement event");
    for(const auto& prior:stores_) {
        if(prior.description.owner==id)break;
        if(!prior.retirement)throw std::logic_error("out-of-order store retirement");
    }
    add(now_,add(profile_.commit_to_iew_delay,1));
    s.retirement=now_;emit(StoreEngineEventKind::Retired,s.description);
}
void StoreRequestEngine::can_writeback(PendingOwner id) {
    check_mutation();
    auto& s=store(id);
    if(!s.retirement || s.can_wb || now_<add(*s.retirement,add(profile_.commit_to_iew_delay,1)))
        throw std::logic_error("invalid store canWB event");
    for(const auto& prior:stores_) {
        if(prior.description.owner==id)break;
        if(!prior.can_wb)throw std::logic_error("canWB skipped older store");
    }
    s.can_wb=true;emit(StoreEngineEventKind::CanWB,s.description);
}
bool StoreRequestEngine::ready(const Store& s) const {return s.address && s.can_wb;}
void StoreRequestEngine::require_ids(std::uint64_t count) const {
    if(count>std::numeric_limits<std::uint64_t>::max()-generation_)
        throw std::overflow_error("store request identity exhausted");
}
StoreRequestId StoreRequestEngine::next_id(){require_ids(1);return {profile_.core,++generation_};}
StoreRequestId StoreRequestEngine::start_transaction(const StoreDescriptor& d) {
    auto id=next_id();auto& l=line(d.line);
    transactions_.push_back({id,d,l.version});l.pending=id;++counters_.permission_started;
    return id;
}
bool StoreRequestEngine::enqueue_prepare(PendingOwner id) {
    check_mutation();
    auto& s=store(id);
    if(!ready(s) || !s.description.prepare_eligible || s.prepare_issued)return false;
    // Ordinary mandatory requests have first use of this cycle's store port.
    if(!demand_ && ready(stores_.front()))return false;
    if(demand_ && stores_.front().description.owner==id)return false;
    for(const auto& prior:stores_) {
        if(!ready(prior) || !prior.description.prepare_eligible)return false;
        if(prior.description.owner==id)break;
    }
    if(ports_used_==profile_.store_ports ||
       (profile_.optional_queue_entries && optional_.size()==profile_.optional_queue_entries))return false;
    auto service_floor=add(now_,profile_.optional_queue_latency);
    require_ids(1);const auto id_hint=next_id();
    optional_.push_back({s.description,service_floor,id_hint});s.prepare_issued=true;++ports_used_;
    ++counters_.prepares_enqueued;emit(StoreEngineEventKind::PrepareEnqueued,s.description,id_hint);
    return true;
}
std::optional<StoreEngineAction> StoreRequestEngine::start_prepare(const AdmissionGate& gate) {
    check_mutation();
    if(optional_.empty() || optional_.front().ready>now_)return std::nullopt;
    if(demand_ && !demand_->started && demand_->ready<=now_) {
        const auto& head_line=line(stores_.front().description.line);
        if(head_line.pending || ((head_line.valid || transactions_.size()<profile_.tbe_entries) &&
           admitted(gate,head_line.line)))
            return std::nullopt; // ready mandatory service precedes optional
    }
    const auto queued=optional_.front();const auto d=queued.description;auto& l=line(d.line);
    if(l.valid || l.pending) {
        const auto parent=l.pending;
        optional_.pop_front();++counters_.prepares_dequeued;++counters_.prepares_skipped;
        emit(StoreEngineEventKind::PrepareSkipped,d,queued.id,parent);prune_line(d.line);
        return StoreEngineAction{StoreEngineActionKind::PrepareSkipped,d.owner,
                                queued.id,parent,d.line};
    }
    if(transactions_.size()==profile_.tbe_entries || !admitted(gate,d.line))return std::nullopt;
    require_ids(1);auto transaction=start_transaction(d);
    optional_.pop_front();++counters_.prepares_dequeued;
    emit(StoreEngineEventKind::PrepareStarted,d,queued.id,transaction);
    return StoreEngineAction{StoreEngineActionKind::PrepareAcquire,d.owner,queued.id,transaction,d.line};
}
std::optional<StoreEngineAction> StoreRequestEngine::send_head() {
    check_mutation();
    if(stores_.empty() || demand_ || !ready(stores_.front()))
        return std::nullopt;
    if(last_write_response_ && now_<add(*last_write_response_,last_response_local_?0:1))return std::nullopt;
    if(stores_.front().description.kind==StoreDescriptorKind::NoAccess) {
        require_ids(1);const auto id=next_id();const auto d=stores_.front().description;
        emit(StoreEngineEventKind::LocalCompleted,d,id);
        stores_.pop_front();last_write_response_=now_;last_response_local_=true;
        ++counters_.sq_released;++counters_.local_completed;
        emit(StoreEngineEventKind::SQReleased,d,id);prune_line(d.line);
        return StoreEngineAction{StoreEngineActionKind::LocalComplete,d.owner,id,std::nullopt,d.line};
    }
    if(ports_used_==profile_.store_ports)return std::nullopt;
    const auto service_ready=add(now_,profile_.mandatory_queue_latency);
    require_ids(1);auto id=next_id();
    const auto d=stores_.front().description;
    demand_=Demand{id,std::nullopt,service_ready,false};++ports_used_;++counters_.demands_accepted;
    emit(StoreEngineEventKind::DemandAccepted,d,id);
    return StoreEngineAction{StoreEngineActionKind::DemandQueued,d.owner,id,std::nullopt,d.line};
}
std::optional<StoreEngineAction> StoreRequestEngine::start_head(const AdmissionGate& gate) {
    check_mutation();
    if(!demand_ || demand_->started || demand_->ready>now_)return std::nullopt;
    const auto d=stores_.front().description;auto& l=line(d.line);
    if(!l.pending && !admitted(gate,d.line))return std::nullopt;
    const bool acquire=!l.valid && !l.pending;
    if(acquire && transactions_.size()==profile_.tbe_entries)return std::nullopt;
    if(acquire)require_ids(1);
    auto transaction=acquire?std::optional<StoreRequestId>(start_transaction(d)):
                     l.valid?std::nullopt:l.pending;
    const auto kind=acquire?StoreEngineActionKind::DemandAcquire:
                    transaction?StoreEngineActionKind::DemandTakeover:StoreEngineActionKind::DemandHit;
    demand_->waiting=transaction;demand_->started=true;++counters_.demands_started;
    emit(StoreEngineEventKind::DemandStarted,d,demand_->id,transaction);
    if(kind==StoreEngineActionKind::DemandTakeover) {
        ++counters_.takeovers;emit(StoreEngineEventKind::DemandTakeover,d,demand_->id,transaction);
    }
    return StoreEngineAction{kind,d.owner,demand_->id,transaction,d.line};
}
void StoreRequestEngine::permission_response(StoreRequestId id) {
    check_mutation();
    auto it=std::find_if(transactions_.begin(),transactions_.end(),[&](const auto& t){return t.id==id;});
    if(it==transactions_.end())throw std::invalid_argument("stale or duplicate permission callback");
    const auto t=*it;auto& l=line(t.description.line);
    const bool valid=l.pending==id && l.version==t.version;
    if(l.pending==id)l.pending.reset();
    if(valid)l.valid=true;
    transactions_.erase(it);++counters_.permission_returned;
    if(!valid)++counters_.stale_returns;
    if(demand_ && demand_->waiting==id)demand_->waiting.reset();
    emit(valid?StoreEngineEventKind::PermissionReturned:StoreEngineEventKind::PermissionStale,t.description,std::nullopt,id);
    prune_line(t.description.line);
}
void StoreRequestEngine::write_response(StoreRequestId id) {
    check_mutation();
    if(!demand_ || demand_->id!=id || stores_.empty())
        throw std::invalid_argument("stale or duplicate mandatory callback");
    const auto d=stores_.front().description;
    if(!demand_->started || demand_->waiting || !line(d.line).valid)
        throw std::logic_error("mandatory response precedes valid write permission");
    emit(StoreEngineEventKind::WriteResponse,d,id);
    demand_.reset();stores_.pop_front();last_write_response_=now_;last_response_local_=false;++counters_.sq_released;
    emit(StoreEngineEventKind::SQReleased,d,id);prune_line(d.line);
}
void StoreRequestEngine::observe_permission(std::uint64_t address) {
    check_mutation();
    auto it=std::find_if(lines_.begin(),lines_.end(),[&](const auto& l){return l.line==address;});
    if(it==lines_.end())return;
    const auto version=add(it->version,1);
    it->version=version;it->valid=true;
    emit_event({StoreEngineEventKind::PermissionObserved,now_,std::nullopt,address,std::nullopt,std::nullopt});
}
void StoreRequestEngine::invalidate(std::uint64_t address) {
    check_mutation();
    auto it=std::find_if(lines_.begin(),lines_.end(),[&](const auto& l){return l.line==address;});
    if(it==lines_.end())return;
    const auto version=add(it->version,1);
    it->version=version;it->valid=false;
    emit_event({StoreEngineEventKind::Invalidated,now_,std::nullopt,address,std::nullopt,std::nullopt});
}
std::optional<StoreEngineAction> StoreRequestEngine::retry_head(const AdmissionGate& gate) {
    check_mutation();
    if(!demand_ || !demand_->started || demand_->waiting || stores_.empty())return std::nullopt;
    const auto d=stores_.front().description;auto& l=line(d.line);
    if(l.valid)return std::nullopt;
    if(!l.pending && (transactions_.size()==profile_.tbe_entries || !admitted(gate,d.line)))return std::nullopt;
    if(!l.pending)require_ids(1);
    auto transaction=l.pending?*l.pending:start_transaction(d);
    demand_->waiting=transaction;++counters_.protocol_retries;
    emit(StoreEngineEventKind::ProtocolRetry,d,demand_->id,transaction);
    return StoreEngineAction{StoreEngineActionKind::ProtocolRetry,d.owner,demand_->id,transaction,d.line};
}
void StoreRequestEngine::prune_line(std::uint64_t address) {
    if(std::any_of(stores_.begin(),stores_.end(),[&](const auto& s){return s.description.kind!=StoreDescriptorKind::NoAccess && s.description.line==address;}) ||
       std::any_of(optional_.begin(),optional_.end(),[&](const auto& q){return q.description.line==address;}) ||
       std::any_of(transactions_.begin(),transactions_.end(),[&](const auto& t){return t.description.line==address;}))return;
    lines_.erase(std::remove_if(lines_.begin(),lines_.end(),[&](const auto& l){return l.line==address;}),lines_.end());
}
} // namespace fastsim
