#include "fastsim/bounded_store_timing.hpp"
#include <algorithm>
#include <limits>
#include <queue>
#include <set>
#include <stdexcept>
#include <tuple>
#include <unordered_map>
#include <map>
#include <deque>
namespace fastsim {
namespace {
std::uint64_t add(std::uint64_t a,std::uint64_t b){if(b>std::numeric_limits<std::uint64_t>::max()-a)throw std::overflow_error("bounded response time overflow");return a+b;}
bool store(BoundedUopKind k){return k==BoundedUopKind::Store || k==BoundedUopKind::LocalStore || k==BoundedUopKind::Atomic;}
bool load(BoundedUopKind k){return k==BoundedUopKind::Load || k==BoundedUopKind::Atomic;}
struct Width {
 std::uint64_t cycle=0;std::uint32_t used=0,width=1;
 std::uint64_t ready(std::uint64_t t)const{t=std::max(t,cycle);return t==cycle && used==width?add(t,1):t;}
 void take(std::uint64_t t){if(ready(t)!=t)throw std::logic_error("width reservation is not ready");if(t!=cycle){cycle=t;used=0;}++used;}
};
}
struct BoundedResponseCursor::Impl {
 enum Kind {ReleaseSQ,Complete,Retire,CanWB,Dispatch,Issue};
 struct Event {std::uint64_t at,serial;Kind kind;std::size_t index;};
 struct Later {bool operator()(const Event&a,const Event&b)const{return std::tie(a.at,a.kind,a.serial)>std::tie(b.at,b.kind,b.serial);}};
 struct State {bool dispatched=false,issued=false,done=false,retired=false,response=false,sq_release=false,issue_queued=false;std::size_t remaining=0;};
 BoundedCoreProfile profile;std::vector<BoundedUop> nodes;std::vector<BoundedFrame> frames;
 std::vector<State> states;std::vector<std::vector<std::size_t>> children;
 std::priority_queue<Event,std::vector<Event>,Later> events;
 std::priority_queue<Event,std::vector<Event>,Later> ready;
 std::set<std::size_t> live_stores, pending_loads, fence_waiters;
 std::optional<std::uint64_t> issue_wake;std::uint64_t issue_generation=0;
 BoundedCursorCounters count;std::uint64_t now=0,serial=0,frontend_extra=0;
 std::size_t dispatch_head=0,retire_head=0,live_rob=0,live_iq=0,live_lq=0,live_sq=0;
 bool dispatch_queued=false,retire_queued=false;std::optional<std::size_t> branch_block;
 Width dispatch_width,issue_width,commit_width;
 Impl(BoundedCoreProfile p,std::vector<BoundedUop> n):profile(p),nodes(std::move(n)),frames(nodes.size()),states(nodes.size()),children(nodes.size()) {
  if(!p.rob_entries || !p.iq_entries || !p.lq_entries || !p.sq_entries || !p.dispatch_width || !p.issue_width || !p.commit_width)throw std::invalid_argument("bounded response needs positive resources");
  dispatch_width.width=p.dispatch_width;issue_width.width=p.issue_width;commit_width.width=p.commit_width;
  std::optional<std::size_t> prior_fence;
  for(std::size_t i=0;i<nodes.size();++i){auto& x=nodes[i];
   if(x.kind==BoundedUopKind::Atomic)throw std::invalid_argument("atomic response cursor adapter is not implemented");
   if(prior_fence && (load(x.kind) || x.kind==BoundedUopKind::Fence) && std::find(x.dependencies.begin(),x.dependencies.end(),*prior_fence)==x.dependencies.end())x.dependencies.push_back(*prior_fence);
   if(x.kind==BoundedUopKind::Fence)prior_fence=i;
   if(x.dispatch>x.issue || x.issue>x.completion || x.completion>x.retire || (i && nodes[i-1].sequence>=x.sequence))throw std::invalid_argument("invalid bounded response lower-bound stages/sequence");
   auto ds=x.dependencies;std::sort(ds.begin(),ds.end());if(std::adjacent_find(ds.begin(),ds.end())!=ds.end())throw std::invalid_argument("duplicate response dependency");
   for(auto d:ds){if(d>=i)throw std::invalid_argument("response dependency must precede consumer");children[d].push_back(i);++states[i].remaining;}
  }
  wake_dispatch();
 }
 void push(Kind k,std::size_t i,std::uint64_t at){if(at<now)throw std::logic_error("bounded event precedes cursor clock");events.push({at,serial++,k,i});}
 void wake_dispatch(){if(dispatch_queued || dispatch_head==nodes.size() || branch_block)return;
  const auto& n=nodes[dispatch_head];if(live_rob==profile.rob_entries || live_iq==profile.iq_entries || (load(n.kind)&&live_lq==profile.lq_entries) || (store(n.kind)&&live_sq==profile.sq_entries))return;
  dispatch_queued=true;push(Dispatch,dispatch_head,dispatch_width.ready(std::max(now,add(n.dispatch,frontend_extra))));
 }
 void wake_issue(std::size_t i){auto& s=states[i];if(!s.dispatched || s.issued || s.issue_queued || s.remaining)return;
  if(nodes[i].kind==BoundedUopKind::Fence && older_memory(i)){fence_waiters.insert(i);return;}
  auto extra=frames[i].dispatch-nodes[i].dispatch;
  for(auto d:nodes[i].dependencies)extra=std::max(extra,frames[d].completion-nodes[d].completion);
  s.issue_queued=true;ready.push({std::max(now,add(nodes[i].issue,extra)),serial++,Issue,i});wake_issue_queue();
 }
 void wake_retire(){if(retire_queued || retire_head==nodes.size() || !states[retire_head].done)return;
  const auto i=retire_head;retire_queued=true;push(Retire,i,commit_width.ready(std::max(now,add(frames[i].completion,nodes[i].retire-nodes[i].completion))));
 }
 bool older_memory(std::size_t i)const{return (!live_stores.empty() && *live_stores.begin()<i) || (!pending_loads.empty() && *pending_loads.begin()<i);}
 void wake_fences(){while(!fence_waiters.empty()){const auto i=*fence_waiters.begin();if(older_memory(i))break;fence_waiters.erase(fence_waiters.begin());wake_issue(i);}}
 void wake_issue_queue(){if(ready.empty())return;const auto t=issue_width.ready(std::max(now,ready.top().at));
  if(!issue_wake || t<*issue_wake){issue_wake=t;push(Issue,++issue_generation,t);}}
 std::optional<BoundedCursorEvent> issue_step(const Event& e){
  if(e.index!=issue_generation)return std::nullopt;
  issue_wake.reset();
  if(ready.empty() || ready.top().at>now)throw std::logic_error("issue wake without ready owner");
  auto i=ready.top().index;ready.pop();auto& s=states[i];const auto& n=nodes[i];auto& f=frames[i];
  issue_width.take(now);s.issue_queued=false;s.issued=true;f.issue=now;--live_iq;++count.issued;wake_dispatch();wake_issue_queue();
  if(load(n.kind))return BoundedCursorEvent{BoundedCursorEventKind::Request,i,now};
  push(Complete,i,add(now,n.completion-n.issue));return std::nullopt;
 }
 void release(std::size_t i){auto& s=states[i];if(!s.retired || !s.sq_release || !live_sq)throw std::logic_error("invalid SQ release");
  frames[i].sq_release=now;live_stores.erase(i);--live_sq;++count.sq_released;wake_dispatch();
  wake_fences();
 }
 std::optional<BoundedCursorEvent> step(){if(events.empty())return std::nullopt;auto e=events.top();events.pop();now=e.at;++count.node_steps;
  if(e.kind==Issue)return issue_step(e);
  auto& s=states[e.index];const auto& n=nodes[e.index];auto& f=frames[e.index];
  switch(e.kind){
   case Dispatch:{dispatch_queued=false;
    // Capacity was available when this single next dispatch was queued; only
    // releases can intervene before its timestamp, never a younger allocation.
    if(dispatch_head!=e.index)throw std::logic_error("dispatch cursor changed");
    dispatch_width.take(now);s.dispatched=true;f.dispatch=now;++dispatch_head;++live_rob;++live_iq;
    if(load(n.kind)){++live_lq;pending_loads.insert(e.index);}if(store(n.kind)){++live_sq;live_stores.insert(e.index);}
    count.max_live_rob=std::max(count.max_live_rob,live_rob);count.max_live_iq=std::max(count.max_live_iq,live_iq);count.max_live_lq=std::max(count.max_live_lq,live_lq);count.max_live_sq=std::max(count.max_live_sq,live_sq);++count.dispatched;
    if(n.frontend_barrier)branch_block=e.index;
    wake_issue(e.index);wake_dispatch();break;
   }
   case Issue:throw std::logic_error("issue dispatch unreachable");
   case Complete:{if(s.done)throw std::logic_error("duplicate core completion");s.done=true;f.completion=now;++count.completed;
    if(load(n.kind)){pending_loads.erase(e.index);wake_fences();}
    if(branch_block && *branch_block==e.index){frontend_extra=std::max(frontend_extra,now-n.completion);branch_block.reset();wake_dispatch();}
    for(auto c:children[e.index]){if(!states[c].remaining)throw std::logic_error("dependency underflow");--states[c].remaining;wake_issue(c);}
    wake_retire();break;
   }
   case Retire:{retire_queued=false;if(commit_width.ready(now)!=now){wake_retire();break;}
    commit_width.take(now);if(retire_head!=e.index)throw std::logic_error("retirement cursor changed");s.retired=true;f.retire=now;++retire_head;--live_rob;if(load(n.kind))--live_lq;++count.retired;
    if(store(n.kind))push(CanWB,e.index,add(now,add(profile.commit_to_iew_delay,1)));
    wake_retire();wake_dispatch();return BoundedCursorEvent{BoundedCursorEventKind::Retired,e.index,now};
   }
   case CanWB:return BoundedCursorEvent{BoundedCursorEventKind::StoreCanWB,e.index,now};
   case ReleaseSQ:release(e.index);break;
  }
  return std::nullopt;
 }
};
BoundedResponseCursor::BoundedResponseCursor(BoundedCoreProfile p,std::vector<BoundedUop> n):p_(std::make_unique<Impl>(p,std::move(n))){}
BoundedResponseCursor::~BoundedResponseCursor()=default;
std::optional<std::uint64_t> BoundedResponseCursor::next_cycle()const{if(p_->events.empty())return std::nullopt;return p_->events.top().at;}
std::optional<BoundedCursorEvent> BoundedResponseCursor::step(){return p_->step();}
void BoundedResponseCursor::complete_at(std::size_t i,std::uint64_t at){auto& s=p_->states.at(i);const auto& n=p_->nodes[i];if(!s.issued || !load(n.kind) || s.response || at<p_->frames[i].issue)throw std::invalid_argument("invalid/duplicate scheduled read response");
 const auto response=std::max(at,add(p_->frames[i].issue,n.completion-n.issue));p_->push(Impl::Complete,i,response);s.response=true;}
void BoundedResponseCursor::release_sq_at(std::size_t i,std::uint64_t at){auto& s=p_->states.at(i);const auto& n=p_->nodes[i];if(!s.retired || !store(n.kind) || s.sq_release || at<add(p_->frames[i].retire,add(p_->profile.commit_to_iew_delay,1)))throw std::invalid_argument("invalid/duplicate scheduled SQ response");p_->push(Impl::ReleaseSQ,i,at);s.sq_release=true;}
bool BoundedResponseCursor::finished()const{return p_->count.retired==p_->nodes.size() && p_->live_sq==0;}
const std::vector<BoundedFrame>& BoundedResponseCursor::frames()const{return p_->frames;}
const BoundedCursorCounters& BoundedResponseCursor::counters()const{return p_->count;}
struct BoundedServiceCalendar::Impl {
 enum Kind {Release,Callback,Arrival,Grant};
 struct Event {std::uint64_t at,serial,owner;Kind kind;};
 struct Later {bool operator()(const Event&a,const Event&b)const{return std::tie(a.at,a.kind,a.serial)>std::tie(b.at,b.kind,b.serial);}};
 struct Job {BoundedServiceRequest request;std::uint64_t serial=0,admission=0;std::size_t pool=0;bool released=false;};
 struct Wait {BoundedServicePriority priority;std::uint64_t arrival,serial,owner;};
 struct WaitLater {bool operator()(const Wait&a,const Wait&b)const{return std::tie(a.priority,a.arrival,a.serial)>std::tie(b.priority,b.arrival,b.serial);}};
 std::vector<std::size_t> capacities;Service service;BoundedServiceCounters count;
 std::unordered_map<std::uint64_t,Job> active;
 std::vector<std::priority_queue<Wait,std::vector<Wait>,WaitLater>> waiting;
 std::vector<bool> grant_queued;
 std::priority_queue<Event,std::vector<Event>,Later> events;
 std::uint64_t now=0,serial=0;bool inside_service=false,poisoned=false;
 Impl(std::vector<std::size_t> c,Service f):capacities(std::move(c)),service(std::move(f)),waiting(capacities.size()),grant_queued(capacities.size(),false){
  if(capacities.empty() || capacities.size()>64 || !service || std::any_of(capacities.begin(),capacities.end(),[](auto n){return !n;}))throw std::invalid_argument("invalid bounded service resources");
  count.live.assign(capacities.size(),0);count.max_live=count.live;
 }
 void check()const{if(poisoned)throw std::logic_error("bounded service calendar is poisoned");if(inside_service)throw std::logic_error("service callback cannot mutate its calendar");}
 void push(Kind k,std::uint64_t owner,std::uint64_t at){events.push({at,serial++,owner,k});}
 void wake(std::size_t p){if(!grant_queued[p] && !waiting[p].empty() && count.live[p]<capacities[p]){grant_queued[p]=true;push(Grant,p,now);}}
 void submit(BoundedServiceRequest r){check();if(r.arrival<now || !r.resource_mask || (r.resource_mask&(r.resource_mask-1)) || active.count(r.owner))throw std::invalid_argument("invalid/duplicate bounded service request");
  std::size_t pool=0;auto mask=r.resource_mask;while(mask>1){mask>>=1;++pool;}if(pool>=capacities.size())throw std::invalid_argument("unknown bounded resource pool");
  const auto id=serial;active.emplace(r.owner,Job{r,id,0,pool,false});
  try{push(Arrival,r.owner,r.arrival);}catch(...){active.erase(r.owner);throw;}++count.submitted;
 }
 std::optional<BoundedServiceCompletion> step(){check();if(events.empty())return std::nullopt;const auto e=events.top();events.pop();now=e.at;++count.events;
  if(e.kind==Grant){const auto pool=static_cast<std::size_t>(e.owner);grant_queued[pool]=false;
   while(count.live[pool]<capacities[pool] && !waiting[pool].empty()){
    const auto w=waiting[pool].top();waiting[pool].pop();auto& job=active.at(w.owner);job.admission=now;
    ++count.live[pool];count.max_live[pool]=std::max(count.max_live[pool],count.live[pool]);++count.granted;
    BoundedServiceReply reply;inside_service=true;
    try{reply=service(job.request,now);inside_service=false;
     if(reply.resource_release<now || reply.completion<reply.resource_release)throw std::invalid_argument("invalid bounded service reply");
     push(Release,w.owner,reply.resource_release);push(Callback,w.owner,reply.completion);
    }catch(...){inside_service=false;poisoned=true;throw;}
   }
   return std::nullopt;
  }
  auto& job=active.at(e.owner);const auto pool=job.pool;
  if(e.kind==Arrival){waiting[pool].push({job.request.priority,job.request.arrival,job.serial,e.owner});wake(pool);}
  else if(e.kind==Release){if(job.released || !count.live[pool])throw std::logic_error("duplicate physical release");job.released=true;--count.live[pool];wake(pool);}
  else {if(!job.released)throw std::logic_error("CPU callback before physical release");const BoundedServiceCompletion out{e.owner,job.admission,now};active.erase(e.owner);++count.completed;return out;}
  return std::nullopt;
 }
};
BoundedServiceCalendar::BoundedServiceCalendar(std::vector<std::size_t> p,Service s):p_(std::make_unique<Impl>(std::move(p),std::move(s))){}
BoundedServiceCalendar::~BoundedServiceCalendar()=default;
void BoundedServiceCalendar::submit(BoundedServiceRequest r){p_->submit(r);}
std::optional<std::uint64_t> BoundedServiceCalendar::next_cycle()const{if(p_->events.empty())return std::nullopt;return p_->events.top().at;}
std::optional<BoundedServiceCompletion> BoundedServiceCalendar::step(){return p_->step();}
const BoundedServiceCounters& BoundedServiceCalendar::counters()const{return p_->count;}

struct BoundedStoreTimingPlanner::Impl {
 enum Kind {Permission,Write,CanWB,DemandReady,HintReady,CpuGrant,ServiceGrant,HeadWake};
 struct Event {std::uint64_t at,serial,id;Kind kind;};
 struct Later {bool operator()(const Event&a,const Event&b)const{return std::tie(a.at,a.kind,a.serial)>std::tie(b.at,b.kind,b.serial);}};
 struct Job {BoundedStoreShape shape;bool canwb=false,head_queued=false,demand=false,prepared=false,reacquired=false,hint_live=false;};
 struct Line {bool valid=false;std::optional<std::uint64_t> pending;std::size_t refs=0;};
 struct Tx {std::uint64_t line;std::optional<std::uint64_t> demand;bool retry=false;};
 using Ticket=std::pair<unsigned,std::uint64_t>;
 BoundedStoreProfile profile;BoundedStoreCounters count;Width ports;
 std::uint64_t now=0,serial=0,next_tx=1,last_write=0,last_owner=0,last_retire=0;
 std::size_t optional_live=0;
 bool has_owner=false,last_local=false,cpu_queued=false,service_queued=false;
 std::unordered_map<std::uint64_t,Job> jobs;
 std::unordered_map<std::uint64_t,Line> lines;
 std::unordered_map<std::uint64_t,Tx> transactions;
 std::deque<std::uint64_t> fifo;
 std::set<std::uint64_t> local_barriers,deferred_hints;
 std::map<Ticket,bool> cpu,services;
 std::priority_queue<Event,std::vector<Event>,Later> events;
 Impl(BoundedStoreProfile p):profile(p){if(!p.sq_entries || !p.tbe_entries || !p.cpu_ports)throw std::invalid_argument("bounded store requires finite positive resources");ports.width=p.cpu_ports;}
 void push(Kind k,std::uint64_t id,std::uint64_t at){if(at<now)throw std::logic_error("store event before clock");events.push({at,serial++,id,k});}
 void wake_cpu(){if(!cpu_queued && !cpu.empty()){cpu_queued=true;push(CpuGrant,0,ports.ready(now));}}
 void wake_service(){if(!service_queued && !services.empty()){service_queued=true;push(ServiceGrant,0,now);}}
 void prune(std::uint64_t line){auto it=lines.find(line);if(it!=lines.end() && !it->second.refs && !it->second.pending)lines.erase(it);}
 void hint(std::uint64_t id){auto& j=jobs.at(id);if(j.shape.local || j.demand || j.prepared)return;
  if(!local_barriers.empty() && *local_barriers.begin()<id){deferred_hints.insert(id);return;}
  if(j.hint_live)return;
  if(profile.optional_entries && optional_live>=profile.optional_entries){deferred_hints.insert(id);return;}
  j.hint_live=true;++optional_live;cpu[{1,id}]=true;wake_cpu();
 }
 void release_hint(Job& j){if(j.hint_live){j.hint_live=false;--optional_live;}}
 void wake_hints(){while(!deferred_hints.empty()){
  auto it=deferred_hints.begin();auto id=*it;if(!local_barriers.empty() && *local_barriers.begin()<id)break;
  deferred_hints.erase(it);hint(id);if(deferred_hints.count(id))break;
 }}
 void head(){if(fifo.empty())return;const auto id=fifo.front();auto& j=jobs.at(id);if(!j.canwb || j.head_queued)return;
  j.head_queued=true;const auto at=std::max(now,add(last_write,last_local?0:1));
  if(j.shape.local)push(Write,id,at);else if(at==now){cpu[{0,id}]=true;wake_cpu();}else push(HeadWake,id,at);
 }
 void retire(std::uint64_t id,std::uint64_t at,BoundedStoreShape shape){
  if(at<now || (has_owner && (id<=last_owner || at<last_retire)) || jobs.size()==profile.sq_entries || (!events.empty() && events.top().at<at))throw std::invalid_argument("invalid store retire order/capacity or unprocessed past event");
  const auto eligible=add(at,add(profile.commit_delay,1));now=at;jobs.emplace(id,Job{shape});fifo.push_back(id);
  if(shape.local)local_barriers.insert(id);else {auto [it,inserted]=lines.emplace(shape.line,Line{});if(inserted)it->second.valid=shape.permission_ready;++it->second.refs;}
  push(CanWB,id,eligible);has_owner=true;last_owner=id;last_retire=at;++count.retired;count.max_live_sq=std::max(count.max_live_sq,jobs.size());
 }
 void acquire(std::uint64_t id,bool optional){auto& j=jobs.at(id);auto& line=lines.at(j.shape.line);
  if(transactions.size()==profile.tbe_entries)throw std::logic_error("physical store TBE overbooked");
  const auto tx=next_tx++;transactions.emplace(tx,Tx{j.shape.line,optional?std::nullopt:std::optional<std::uint64_t>{id},false});line.pending=tx;line.valid=false;
  ++count.acquires;if(optional)++count.prepares;count.max_live_tbe=std::max(count.max_live_tbe,transactions.size());push(Permission,tx,add(now,j.shape.acquire_cycles));
 }
 std::optional<BoundedStoreCompletion> step(){if(events.empty())return std::nullopt;const auto e=events.top();events.pop();now=e.at;++count.events;
  if(e.kind==Permission){auto it=transactions.find(e.id);if(it==transactions.end())throw std::logic_error("missing physical callback owner");const auto tx=it->second;transactions.erase(it);
   auto& line=lines.at(tx.line);if(line.pending==e.id)line.pending.reset();line.valid=true;
   if(tx.demand){auto& j=jobs.at(*tx.demand);if(tx.retry){j.reacquired=true;++count.reacquires;services[{0,*tx.demand}]=true;}else push(Write,*tx.demand,now);}
   prune(tx.line);wake_service();return std::nullopt;
  }
  if(e.kind==CpuGrant){cpu_queued=false;while(!cpu.empty() && ports.ready(now)==now){const auto t=cpu.begin()->first;cpu.erase(cpu.begin());auto it=jobs.find(t.second);if(it==jobs.end())continue;auto& j=it->second;ports.take(now);
    if(t.first==0){j.demand=true;push(DemandReady,t.second,add(now,profile.mandatory_queue));}else {j.prepared=true;push(HintReady,t.second,add(now,profile.optional_queue));}
   }wake_cpu();return std::nullopt;}
  if(e.kind==ServiceGrant){service_queued=false;while(!services.empty()){
    const auto t=services.begin()->first;auto it=jobs.find(t.second);if(it==jobs.end()){services.erase(services.begin());continue;}auto& j=it->second;auto& line=lines.at(j.shape.line);
    if(t.first==1){if(line.valid || line.pending){services.erase(services.begin());release_hint(j);wake_hints();continue;}if(transactions.size()==profile.tbe_entries)break;services.erase(services.begin());release_hint(j);acquire(t.second,true);wake_hints();continue;}
    const bool retry=j.prepared && !j.shape.lease_valid && !j.reacquired;
    if(line.pending){auto& tx=transactions.at(*line.pending);if(tx.demand)throw std::logic_error("two mandatory heads share physical transaction");tx.demand=t.second;tx.retry=retry;services.erase(services.begin());++count.takeovers;continue;}
    if(line.valid && !retry){services.erase(services.begin());push(Write,t.second,add(now,j.shape.write_cycles));continue;}
    if(transactions.size()==profile.tbe_entries)break;
    if(retry){j.reacquired=true;++count.reacquires;}services.erase(services.begin());acquire(t.second,false);
   }return std::nullopt;}
  auto it=jobs.find(e.id);if(it==jobs.end())return std::nullopt;auto& j=it->second;
  switch(e.kind){
   case CanWB:j.canwb=true;head();if(!j.head_queued)hint(e.id);break;
   case DemandReady:services[{0,e.id}]=true;wake_service();break;
   case HintReady:services[{1,e.id}]=true;wake_service();break;
   case HeadWake:cpu[{0,e.id}]=true;wake_cpu();break;
   case Write:{if(fifo.empty() || fifo.front()!=e.id || !j.canwb)throw std::logic_error("non-FIFO write completion");const auto shape=j.shape;
    const auto cancelled=cpu.erase({1,e.id})+services.erase({1,e.id});count.superseded+=cancelled!=0;deferred_hints.erase(e.id);
    release_hint(j);fifo.pop_front();local_barriers.erase(e.id);jobs.erase(it);last_write=now;last_local=shape.local;
    if(shape.local)++count.local_completed;else {++count.writes;--lines.at(shape.line).refs;prune(shape.line);}
    head();wake_hints();return BoundedStoreCompletion{e.id,now,shape.local};}
   default:throw std::logic_error("invalid store lifecycle event");
  }
  return std::nullopt;
 }
};
BoundedStoreTimingPlanner::BoundedStoreTimingPlanner(BoundedStoreProfile p):p_(std::make_unique<Impl>(p)){}
BoundedStoreTimingPlanner::~BoundedStoreTimingPlanner()=default;
void BoundedStoreTimingPlanner::retire(std::uint64_t id,std::uint64_t at,BoundedStoreShape s){p_->retire(id,at,s);}
std::optional<std::uint64_t> BoundedStoreTimingPlanner::next_cycle()const{if(p_->events.empty())return std::nullopt;return p_->events.top().at;}
std::optional<BoundedStoreCompletion> BoundedStoreTimingPlanner::step(){return p_->step();}
const BoundedStoreCounters& BoundedStoreTimingPlanner::counters()const{return p_->count;}

BoundedNonbindingStoreResult estimate_nonbinding_store(const BoundedNonbindingStoreInput& x) {
    if(!x.service_cycles || !x.publish_cycles)throw std::invalid_argument("nonbinding store recipes include a positive request queue edge");
    BoundedNonbindingStoreResult r;
    r.eligible=add(x.retire,add(x.commit_delay,1));
    r.prepare=std::max(r.eligible,x.scan_ready);
    r.send=std::max(r.eligible,add(x.prior_response,x.prior_local?0:1));
    if(r.send<x.scan_ready)throw std::invalid_argument("scan barrier must be included in the FIFO predecessor");
    r.permission=add(r.prepare,x.service_cycles);
    if(r.send==r.prepare){r.response=add(r.send,x.service_cycles);return r;}
    if(!x.lease_valid){r.role=BoundedStoreRole::Reacquire;r.response=add(std::max(r.send,r.permission),x.service_cycles);}
    else if(r.send<r.permission){r.role=BoundedStoreRole::Takeover;r.response=r.permission;}
    else {r.role=BoundedStoreRole::Publish;r.response=add(r.send,x.publish_cycles);}
    return r;
}

BoundedStoreLifecycle::BoundedStoreLifecycle(std::size_t cores,std::size_t prepare_credits,
        std::size_t demand_credits,std::uint32_t store_ports,std::size_t ordered_lines,std::size_t store_entries)
    : prepare_credits_(prepare_credits),store_entries_(store_entries),store_ports_(store_ports),cores_(cores) {
    if(!cores || !prepare_credits || !demand_credits || !store_ports || !ordered_lines || !store_entries)
        throw std::invalid_argument("invalid bounded store resources");
    hints_.reserve(cores*store_entries);
    for(auto& core:cores_) {core.demand_ready.assign(demand_credits,0);core.window.resize(ordered_lines);}
}
bool BoundedStoreLifecycle::prepare(BoundedStoreIdentity id,std::uint64_t line,
                                    std::uint64_t issue,std::uint64_t response) {
    if(id.core>=cores_.size() || issue<watermark_ || response<issue)
        throw std::invalid_argument("invalid bounded prepare");
    auto& core=cores_[id.core];
    if(issue<core.prepare_frontier)throw std::invalid_argument("bounded prepare time moved backwards");
    core.prepare_frontier=issue;
    std::size_t live=0,entries=0;
    for(const auto& hint:hints_)if(hint.id.core==id.core) {++entries;if(hint.response>issue)++live;}
    if(live>=prepare_credits_ || entries>=store_entries_) {
        ++stats_.prepare_dropped;return false;
    }
    hints_.push_back({id,line,issue,response,ids_.allocate(),std::nullopt,false,false});++stats_.prepares;return true;
}
BoundedStoreEdges BoundedStoreLifecycle::demand(std::uint32_t core,std::uint64_t issue,std::uint64_t service) {
    if(core>=cores_.size() || issue<watermark_ || !service)
        throw std::invalid_argument("invalid bounded demand");
    auto& c=cores_[core];auto slot=std::min_element(c.demand_ready.begin(),c.demand_ready.end());
    const auto send=std::max(issue,*slot);const auto response=add(send,service);*slot=response;
    return {send,response,response,0};
}
std::optional<std::uint64_t> BoundedStoreLifecycle::generation(BoundedStoreIdentity id)const {
    for(const auto& hint:hints_)if(hint.id.core==id.core && hint.id.sequence==id.sequence && hint.id.ordinal==id.ordinal)
        return hint.generation;
    return std::nullopt;
}
bool BoundedStoreLifecycle::publish_prepare(BoundedStoreIdentity id,std::uint64_t generation,
        const std::function<bool(std::uint32_t,std::uint64_t,std::uint64_t)>& publish) {
    for(auto& hint:hints_) {
        if(hint.id.core!=id.core || hint.id.sequence!=id.sequence || hint.id.ordinal!=id.ordinal ||
           hint.generation!=generation)continue;
        if(hint.published || hint.response>watermark_ ||
           (hint.invalidated && *hint.invalidated<=watermark_))return false;
        if(!publish(hint.id.core,hint.line,hint.generation))return false;
        hint.published=true;return true;
    }
    return false;
}
std::size_t BoundedStoreLifecycle::complete_prepares(std::uint64_t watermark,
        const std::function<bool(BoundedStoreIdentity,std::uint64_t,
                                 std::uint64_t,std::uint64_t)>& publish) {
    if(watermark<watermark_)throw std::invalid_argument("bounded watermark moved backwards");
    struct Completion {BoundedStoreIdentity id;std::uint64_t line,response,generation;};
    std::vector<Completion> due;
    for(const auto& hint:hints_)
        if(!hint.published && hint.response<=watermark &&
           (!hint.invalidated || *hint.invalidated>hint.response))
            due.push_back({hint.id,hint.line,hint.response,hint.generation});
    std::sort(due.begin(),due.end(),[](const Completion& a,const Completion& b) {
        return std::tie(a.response,a.id.core,a.id.sequence,a.id.ordinal,a.generation)<
               std::tie(b.response,b.id.core,b.id.sequence,b.id.ordinal,b.generation);
    });
    std::size_t completed=0;
    for(const auto& item:due) {
        advance(std::max(watermark_,item.response));
        completed+=publish_prepare(item.id,item.generation,
            [&](std::uint32_t,std::uint64_t,std::uint64_t) {
                return publish(item.id,item.line,item.response,item.generation);
            });
    }
    advance(watermark);
    return completed;
}
BoundedStoreEdges BoundedStoreLifecycle::head(BoundedStoreIdentity id,std::uint64_t line,
                                               std::uint64_t eligible,std::uint64_t service,
                                               std::uint64_t publish_service,std::uint64_t reclaim_delay) {
    if(id.core>=cores_.size() || eligible<watermark_ || !service || !publish_service)
        throw std::invalid_argument("invalid bounded head");
    auto& c=cores_[id.core];
    auto demand=std::min_element(c.demand_ready.begin(),c.demand_ready.end());
    auto send=std::max({eligible,*demand,c.port_cycle});
    auto window=std::find_if(c.window.begin(),c.window.end(),
        [line,send](const LineSlot& slot){return slot.line==line && slot.ready>send;});
    if(window==c.window.end()) {
        window=std::min_element(c.window.begin(),c.window.end(),
            [](const LineSlot& a,const LineSlot& b){return a.ready<b.ready;});
        send=std::max(send,window->ready);
    }
    if(send==c.port_cycle && c.port_used==store_ports_)send=add(send,1);
    if(send!=c.port_cycle){c.port_cycle=send;c.port_used=0;}++c.port_used;
    auto response=add(send,service);
    std::uint64_t permission=0;
    for(auto& hint:hints_)if(!hint.consumed &&
        (!hint.invalidated || (send<*hint.invalidated && hint.response<*hint.invalidated)) &&
        hint.line==line && hint.id.core==id.core &&
        hint.id.sequence==id.sequence && hint.id.ordinal==id.ordinal && hint.issue<=send) {
        permission=hint.response;
        if(send<hint.response)response=std::max(send,hint.response);
        else if(hint.published)response=add(send,publish_service);
        else { hint.invalidated=send; permission=0; }
        hint.consumed=true;
        break;
    }
    *demand=response;window->line=line;window->ready=std::max(window->ready,response);++stats_.heads;
    c.release_frontier=std::max(c.release_frontier,add(response,reclaim_delay));
    return {send,response,c.release_frontier,permission};
}
std::optional<std::uint64_t> BoundedStoreLifecycle::next_visible(std::uint64_t line,std::uint64_t at)const {
    std::optional<std::uint64_t> ready;
    for(const auto& hint:hints_)if(!hint.published && (!hint.invalidated || at<*hint.invalidated) && hint.line==line && hint.issue<=at) {
        const auto edge=std::max(at,hint.response);
        ready=ready?std::max(*ready,edge):edge;
    }
    return ready;
}
void BoundedStoreLifecycle::invalidate(std::uint64_t line,std::uint64_t at) {
    if(at<watermark_)throw std::invalid_argument("invalidation before bounded watermark");
    for(auto& hint:hints_)if(hint.line==line && hint.issue<=at && (!hint.invalidated || at<*hint.invalidated)) {
        hint.invalidated=at;++stats_.invalidations;
    }
}
void BoundedStoreLifecycle::advance(std::uint64_t watermark) {
    if(watermark<watermark_)throw std::invalid_argument("bounded watermark moved backwards");
    hints_.erase(std::remove_if(hints_.begin(),hints_.end(),
        [watermark](const Hint& hint){return hint.response<=watermark &&
            ((hint.consumed && hint.published) || (hint.invalidated && *hint.invalidated<=watermark));}),hints_.end());
    watermark_=watermark;
}

}
