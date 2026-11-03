#include "fastsim/bounded_store_timing.hpp"
#include "fastsim/store_demand_observer.hpp"
#include <iostream>
#include <stdexcept>
#include <vector>
#include <chrono>
#include <cstdlib>
using namespace fastsim;
void require(bool b,const char* s){if(!b)throw std::runtime_error(s);}
BoundedUop node(std::uint64_t seq,BoundedUopKind k,std::uint64_t issue=0){
 BoundedUop n;n.sequence=seq;n.kind=k;n.issue=issue;n.completion=issue+1;n.retire=issue+1;return n;
}
void test_cursor() {
 BoundedCoreProfile p;p.rob_entries=8;p.iq_entries=8;p.lq_entries=8;p.sq_entries=2;p.dispatch_width=8;p.issue_width=8;p.commit_width=8;
 std::vector<BoundedUop> ns{node(0,BoundedUopKind::Load,10),node(1,BoundedUopKind::Load,1),node(2,BoundedUopKind::Alu,11)};
 ns[2].dependencies.push_back(0);
 BoundedResponseCursor c(p,ns);std::vector<std::uint64_t> issued;
 while(c.next_cycle()) {auto e=c.step();if(!e)continue;if(e->kind==BoundedCursorEventKind::Request) {issued.push_back(e->uop);c.complete_at(e->uop,e->cycle+(e->uop==0?100:2));}}
 require(c.finished(),"cursor must complete all nodes");
 require(issued==std::vector<std::uint64_t>({1,0}),"independent younger read must issue before an older delayed read");
 require(c.frames()[2].issue>=c.frames()[0].completion,"dependent ALU waits for the real read completion");
 require(c.counters().node_steps<=ns.size()*12,"bounded work, no cycle polling");
}
void test_future_store() {
 BoundedCoreProfile p;p.rob_entries=8;p.iq_entries=8;p.lq_entries=8;p.sq_entries=2;p.dispatch_width=8;p.issue_width=8;p.commit_width=8;
 auto s=node(0,BoundedUopKind::Store);s.retire=100;
 BoundedResponseCursor c(p,{s,node(1,BoundedUopKind::Load,1)});std::uint64_t load_issue=999,eligible=0;
 while(c.next_cycle()) {auto e=c.step();if(!e)continue;
  if(e->kind==BoundedCursorEventKind::Request){load_issue=e->cycle;c.complete_at(e->uop,e->cycle+2);}
  if(e->kind==BoundedCursorEventKind::StoreCanWB){eligible=e->cycle;c.release_sq_at(e->uop,e->cycle+100);}
 }
 require(c.finished() && load_issue==1 && eligible>=102,"future store cannot reserve a resource before canWB or hold an earlier independent read");
}
void test_sq_local_chain() {
 BoundedCoreProfile p;p.rob_entries=16;p.iq_entries=16;p.sq_entries=1;p.lq_entries=4;
 std::vector<BoundedUop> ns;for(unsigned i=0;i<4096;++i)ns.push_back(node(i,BoundedUopKind::LocalStore));
 BoundedResponseCursor c(p,ns);
 while(c.next_cycle()){auto e=c.step();if(e && e->kind==BoundedCursorEventKind::StoreCanWB)c.release_sq_at(e->uop,e->cycle);}
 require(c.finished() && c.counters().max_live_sq==1,"local SQ capacity must be conserved");
 require(c.counters().node_steps<=ns.size()*14,"long chain must advance without fixed-point passes or tick polling");
}
void test_prototype_benchmark() {
 if(!std::getenv("FASTSIM_BOUNDED_PROTOTYPE_BENCH"))return;
 BoundedCoreProfile p;std::vector<BoundedUop> ns;
 for(unsigned i=0;i<4096;++i){auto n=node(i,BoundedUopKind::Alu,i/8);n.dispatch=i/8;ns.push_back(n);}
 const auto start=std::chrono::steady_clock::now();std::uint64_t steps=0;
 for(unsigned i=0;i<128;++i){BoundedResponseCursor c(p,ns);while(c.next_cycle())c.step();require(c.finished(),"benchmark completed");steps+=c.counters().node_steps;}
 const auto sec=std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count();
 std::cout<<"prototype_alu_uops="<<4096*128<<" node_steps="<<steps<<" host_seconds="<<sec<<" fixture_uops_per_second="<<(4096.0*128/sec)<<"\n";
}
void test_fence_and_ready_work() {
 BoundedCoreProfile p;p.rob_entries=64;p.iq_entries=64;p.lq_entries=32;p.sq_entries=8;p.dispatch_width=8;p.issue_width=1;p.commit_width=8;
 BoundedResponseCursor c(p,{node(0,BoundedUopKind::Store),node(1,BoundedUopKind::Fence),node(2,BoundedUopKind::Store),node(3,BoundedUopKind::Alu)});
 while(c.next_cycle()){auto e=c.step();if(e && e->kind==BoundedCursorEventKind::StoreCanWB)c.release_sq_at(e->uop,e->cycle+100);}
 require(c.finished(),"fence must not wait on a younger unretired store");
 require(c.frames()[3].issue<c.frames()[1].completion,"fence preserves unrelated ALU parallelism");
 std::vector<BoundedUop> ns;for(unsigned i=0;i<5000;++i)ns.push_back(node(i,BoundedUopKind::Alu));
 BoundedResponseCursor a(p,ns);while(a.next_cycle())a.step();
 require(a.finished() && a.counters().node_steps<=ns.size()*9,"issue-width pressure must not poll all ready UOPs each tick");
}
void test_service_calendar() {
 std::vector<std::uint64_t> starts;
 BoundedServiceCalendar c({1},[&](const BoundedServiceRequest& r,std::uint64_t at){starts.push_back(at);const auto done=at+(r.owner==0?100:2);return BoundedServiceReply{done,done};});
 c.submit({0,100,1,BoundedServicePriority::Mandatory});
 c.submit({1,1,1,BoundedServicePriority::Load});
 while(c.next_cycle() && *c.next_cycle()<100)c.step();
 require(starts==std::vector<std::uint64_t>({1}),"future store must not occupy a past calendar slot");
 while(c.next_cycle())c.step();require(starts==std::vector<std::uint64_t>({1,100}),"future store starts only at its own admission");
 BoundedServiceCalendar q({1},[](const BoundedServiceRequest&,std::uint64_t t){return BoundedServiceReply{t+100,t+100};});
 for(unsigned k=0;k<256;++k)q.submit({k,0,1,BoundedServicePriority::Optional});
 std::uint64_t completed=0;while(q.next_cycle()){auto event=q.step();if(event)++completed;}
 require(completed==256 && q.counters().max_live[0]==1 && q.counters().granted==256,"finite TBE queue conserves each physical service");
 require(q.counters().events<=256*4,"finite queue must wake waiters without target tick polling");
 bool duplicate=false;BoundedServiceCalendar d({2},[](const BoundedServiceRequest&,std::uint64_t t){return BoundedServiceReply{t+1,t+1};});
 d.submit({1,0,1,BoundedServicePriority::Load});try{d.submit({1,0,1,BoundedServicePriority::Load});}catch(const std::invalid_argument&){duplicate=true;}require(duplicate,"active physical ID cannot be submitted twice");
}
void test_analytic_store_planner() {
 BoundedStoreProfile p;p.tbe_entries=2;p.sq_entries=8;
 BoundedStoreTimingPlanner a(p);a.retire(0,10,{10,100,0,false,false});a.retire(1,11,{20,100,0,false,false});
 std::vector<std::uint64_t> done;while(a.next_cycle()){auto c=a.step();if(c)done.push_back(c->cycle);}
 require(done==std::vector<std::uint64_t>({113,115}),"two-TBE cold-store overlap must preserve queue/FIFO edges");
 p.tbe_entries=1;BoundedStoreTimingPlanner b(p);b.retire(0,10,{10,100,0,false,false});b.retire(1,11,{20,100,0,false,false});
 done.clear();while(b.next_cycle()){auto c=b.step();if(c)done.push_back(c->cycle);}
 require(done==std::vector<std::uint64_t>({113,213}),"one TBE limits physical services without tick polling");
 require(b.counters().max_live_tbe==1 && b.counters().acquires==2,"physical acquire resource conservation");
 BoundedStoreTimingPlanner same(p);same.retire(0,10,{10,100,0,false,false});same.retire(1,11,{10,100,0,false,false});
 while(same.next_cycle())same.step();require(same.counters().acquires==1,"same-line parents do not allocate twice");
}
void test_cursor_planner_bridge() {
 BoundedCoreProfile p;p.rob_entries=64;p.iq_entries=16;p.lq_entries=8;p.sq_entries=2;
 BoundedStoreProfile sp;sp.sq_entries=2;sp.tbe_entries=2;
 std::vector<BoundedUop> ns;
 for(unsigned i=0;i<256;++i){for(unsigned k=0;k<4;++k){auto n=node(i*4+k,k==1?BoundedUopKind::LocalStore:BoundedUopKind::Store,i/8);n.dispatch=i/8;ns.push_back(n);}}
 BoundedResponseCursor c(p,ns);BoundedStoreTimingPlanner planner(sp);std::uint64_t loops=0;
 while(c.next_cycle() || planner.next_cycle()){
  require(++loops<=ns.size()*40,"coupled cursor/planner must have bounded work");
  const auto cc=c.next_cycle(),pc=planner.next_cycle();
  if(pc && (!cc || *pc<=*cc)){auto done=planner.step();if(done)c.release_sq_at(done->owner,done->cycle);}
  else {auto e=c.step();if(e && e->kind==BoundedCursorEventKind::Retired){const bool local=ns[e->uop].kind==BoundedUopKind::LocalStore;planner.retire(e->uop,e->cycle,{e->uop/4,100,0,false,local});}}
 }
 require(c.finished() && c.counters().sq_released==ns.size(),"coupled long SQ2 chain releases every owner");
 require(planner.counters().local_completed==256 && planner.counters().writes==768,"local stores never create ordinary cache writes");
 require(planner.counters().max_live_tbe<=2 && c.counters().max_live_sq==2,"coupled finite resources conserved");
}
void test_finite_hint_queue() {
 BoundedStoreProfile p;p.sq_entries=8;p.tbe_entries=8;p.optional_entries=1;p.optional_queue=50;
 BoundedStoreTimingPlanner planner(p);
 for(unsigned i=0;i<8;++i)planner.retire(i,0,{i,1000,0,false,false,true});
 while(planner.next_cycle() && *planner.next_cycle()<100)planner.step();
 require(planner.counters().prepares==1,"optional admission capacity includes queue transit before protocol service");
 while(planner.next_cycle())planner.step();
 require(planner.counters().writes==8,"finite optional queue eventually drains all writes");
}
void test_nonbinding_formula() {
 auto cold=estimate_nonbinding_store({10,0,0,101,1,1,false,true});
 require(cold.eligible==12 && cold.response==113 && cold.role==BoundedStoreRole::OriginalDemand,"cold head pays one complete recipe");
 auto overlap=estimate_nonbinding_store({11,113,0,101,1,1,false,true});
 require(overlap.response==115 && overlap.role==BoundedStoreRole::Publish,"completed prepare uses local publish recipe");
 auto takeover=estimate_nonbinding_store({11,50,0,101,1,1,false,true});
 require(takeover.response==114 && takeover.role==BoundedStoreRole::Takeover,"in-flight prepare is taken over once");
 auto lost=estimate_nonbinding_store({11,50,0,101,1,1,false,false});
 require(lost.response==215 && lost.role==BoundedStoreRole::Reacquire,"lost forecast still prepares then reacquires once after old callback");
 auto local=estimate_nonbinding_store({11,50,50,101,1,1,true,true});
 require(local.send==50 && local.prepare==50 && local.response==151,"local predecessor has no ordinary-store handoff bubble");
 bool rejected=false;try{estimate_nonbinding_store({0,0,0,0,1,1,false,true});}catch(const std::invalid_argument&){rejected=true;}
 require(rejected,"empty recipe rejected");
}
void test_demand_observation() {
 StoreDemandObservation o;
 for(const auto& x:std::vector<BoundedNonbindingStoreInput>{
     {10,0,0,101,1,1,false,true}, {11,50,0,101,1,1,false,true},
     {11,113,0,101,1,1,false,true}, {11,50,0,101,1,1,false,false}}) {
  const auto result=estimate_nonbinding_store(x);
  o.sample(result,2,x.service_cycles,20,12,50);
 }
 require(o.demand.samples==4 && o.demand.sum==329,"demand records remaining time, not four complete recipes");
 require(o.role[0].sum==101 && o.role[1].sum==63 && o.role[2].sum==1 && o.role[3].sum==164,
         "four CPU roles retain distinct demand waiting times");
 require(o.service_cycles[2]==404 && o.shared_queue_cycles[2]==80 && o.dram_service_cycles[2]==200,
         "full canonical service accounting stays separate from remaining demand");
 StoreLatencyHistogram serial,left,right;
 for(std::uint64_t value:{0ull,1ull,9ull,10ull,19ull,20ull,255ull,256ull,4096ull}) {
  serial.sample(value);(value<100?left:right).sample(value);
 }
 left.merge(right);
 require(left.buckets==serial.buckets && left.bucket_size==serial.bucket_size && left.sum==serial.sum && left.samples==serial.samples,
         "per-core and per-epoch histogram merge matches a single gem5-style positive histogram");
 require(o.duration[0].samples==1 && o.duration[1].samples==3,"duration threshold is descriptive, never a hit classification");
}
void test_bounded_store_components(){test_demand_observation();test_finite_hint_queue();test_nonbinding_formula();test_cursor_planner_bridge();test_analytic_store_planner();test_service_calendar();test_fence_and_ready_work();test_cursor();test_future_store();test_sq_local_chain();test_prototype_benchmark();}
#ifdef FASTSIM_BOUNDED_COMPONENT_STANDALONE
int main(){try{test_bounded_store_components();std::cout<<"bounded response cursor tests passed\n";}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}

#endif
