#include "fastsim/simulator.hpp"
#include "fastsim/fst_store_requests.hpp"
#include "fastsim/spr_native_coherence.hpp"
#include <algorithm>
#include <set>
#include <map>
#include <tuple>
#include <fstream>
#include <iostream>
#include <sstream>
#include <thread>
#include <chrono>
#include <stdexcept>

using namespace fastsim;
namespace {
void require(bool value, const char* why) { if (!value) throw std::runtime_error(why); }
class Source final : public TraceSource {
    std::vector<TraceRecord> rows_;
    std::size_t cursor_ = 0;
    fst::StoreRequestFacts facts_;
    unsigned host_delay_=0;std::uint32_t extra_=0;std::uint64_t request_flags_=0;std::size_t bad_at_=0;bool boundary_=false;
public:
    explicit Source(std::vector<TraceRecord> rows,unsigned delay=0,std::uint32_t extra=0,std::uint64_t flags=0,std::size_t at=0,bool boundary=false)
        : rows_(std::move(rows)),host_delay_(delay),extra_(extra),request_flags_(flags),bad_at_(at),boundary_(boundary) {}
    bool next(TraceRecord& row) override {
        if (cursor_ == rows_.size()) return false;
        if(host_delay_ && cursor_%2==0)std::this_thread::sleep_for(std::chrono::microseconds(host_delay_));
        row = rows_[cursor_++]; facts_ = {};
        facts_.address=row.address; facts_.size=row.size;
        facts_.byte_enable=(1ull<<row.size)-1;
        facts_.attributes=fst::kStoreFactsComplete |
            (row.is_write()?fst::kStoreOrdinary:(fst::kMemoryRead|fst::kOrdinaryLoad));
        if(cursor_-1==bad_at_) {facts_.attributes|=extra_;facts_.request_flags=request_flags_;if(extra_&fst::kStoreMasked)facts_.byte_enable=1;}
        return true;
    }
    bool has_measurement_boundary() const override {return boundary_;}
    std::string description() const override { return "ordinary-coherence-functional-fixture"; }
    bool complete_dependencies() const override { return true; }
    bool functional_retirement_complete() const override { return true; }
    bool memory_request_roles_complete() const override { return true; }
    std::optional<fst::MemoryRequestRole> current_memory_request_role() const override { return fst::MemoryRequestRole::Data; }
    std::optional<bool> current_ordinary_load_eligible() const override { return true; }
    const fst::StoreRequestFacts* current_store_facts() const override { return &facts_; }
    const fst::StoreRequestFacts* current_memory_facts() const override { return &facts_; }
};
TraceRecord access(bool write, std::uint64_t address=4096) {
    TraceRecord r; r.op_class=write?57:56;
    r.flags=kRetires|kPhysicalAddress|(write?kStore:kLoad); r.address=address; r.size=8; return r;
}
SimulatorConfig config(unsigned cores=2, unsigned chunk=1) {
    SimulatorConfig c; c.core_model="interval_weave"; c.cores=cores;
    c.coherence=false; c.causal_runtime=true; c.causal_native_memory=true;
    c.causal_native_coherence=true;c.cha_xor_hash=false;
    c.causal_l0_transitions_per_cycle=32;
    c.native_coherence.source_bound=true;
    c.native_coherence.source_identity=spr::kNativeCoherenceSourceIdentity;
    c.native_coherence.transitions={32,32,4,32};
    c.native_coherence.llc_request_latency=2;c.native_coherence.llc_response_latency=2;
    c.native_coherence.directory_memory_latency=1;
    c.native_coherence.private_unblock_latency=1;
    c.native_coherence.ordered_private_links=true;
    c.native_coherence.unordered_network_queues=true;
    c.native_coherence.deterministic_unbounded_queues=true;
    c.native_coherence.directories.push_back({0,1ull<<40,0,{}});
    c.store_commit_to_iew_delay=0;
    c.causal_mandatory_queue_latency=1; c.causal_l1_data_access_latency=3;
    c.causal_mandatory_queue_ordered=false;
    c.causal_l1_request_latency=2; c.causal_l1_response_latency=2;
    c.causal_l2_request_latency=2; c.causal_l2_response_latency=2;
    c.causal_llc_to_l2_latency=1;
    c.l1d.replacement=c.l2.replacement=c.llc.replacement=ReplacementPolicy::kLru;
    c.spr_profile.limits={16,16,16,48,256,256,256,112,4,4};
    c.chunk_instructions=chunk; c.interval_target_uops=1;
    c.fetch_to_decode=c.decode_to_rename=c.rename_to_dispatch=0;
    c.dispatch_to_issue=c.issue_to_execute=c.execute_to_commit=c.load_response_to_ready=0;
    return c;
}
SimulationStats run(SimulatorConfig c, std::vector<std::vector<TraceRecord>> rows,unsigned host_delay=0) {
    std::vector<std::unique_ptr<TraceSource>> sources;
    for (auto& r:rows) sources.push_back(std::make_unique<Source>(std::move(r),host_delay));
    Simulator simulator(c,std::move(sources)); return simulator.run();
}
struct Audit {
    std::string kind;std::uint64_t cycle=0,core=0,sequence=0,fragment=0,line=0,a=0,b=0,generation=0;
};
std::vector<Audit> events(const std::string& path) {
    std::ifstream in(path);std::string row;std::getline(in,row);std::vector<Audit> out;
    while(std::getline(in,row)) {
        std::replace(row.begin(),row.end(),',',' ');std::istringstream input(row);Audit e;
        input>>e.kind>>e.cycle>>e.core>>e.sequence>>e.fragment>>e.line>>e.a>>e.b>>e.generation;
        require(bool(input),"complete mechanism audit row");out.push_back(e);
    }
    return out;
}
unsigned count(const std::vector<Audit>& rows,const std::string& kind,std::uint64_t line=64) {
    return static_cast<unsigned>(std::count_if(rows.begin(),rows.end(),[&](const Audit& e){return e.kind==kind && e.line==line;}));
}
bool edge(const std::vector<Audit>& rows,spr::NativeController controller,spr::NativeLineState before,spr::NativeLineState after) {
    return std::any_of(rows.begin(),rows.end(),[&](const Audit& e) {
        return e.kind=="coherence_transition" && e.line==64 && (e.a>>32)==static_cast<unsigned>(controller) &&
            (e.a&0xffffffff)==static_cast<unsigned>(before) && e.b==static_cast<unsigned>(after);
    });
}
std::string canonical(const std::vector<Audit>& rows) {
    std::vector<std::string> result;
    for(const auto& e:rows) {
        if(e.kind=="store_drain_source")continue; // host visits are not modeled service events
        std::ostringstream out;out<<e.kind<<','<<e.cycle<<','<<e.core<<','<<e.sequence<<','<<e.fragment<<','<<e.line<<','<<e.a<<','<<e.b<<','<<e.generation;
        result.push_back(out.str());
    }
    std::sort(result.begin(),result.end());std::ostringstream out;
    for(const auto& row:result)out<<row<<'\n';return out.str();
}
TraceRecord dependent(TraceRecord r) {r.producer_dists[0]=1;r.n_src=1;return r;}
// Supplemental dispatcher boundary tests. Positive qualification above uses
// Simulator::run; this fixture binds the same dispatcher to real cache tags,
// typed credits and OnlineSequencer, with a controlled memory completion edge.
void instruction_protocol_tests() {
 using namespace spr;
 for(unsigned mode=0;mode<4;++mode) {
  auto c=config(1);c.cha_count=1;c.l1i.replacement=ReplacementPolicy::kLru;
  if(mode==2){c.l1i.size_bytes=64;c.l1i.associativity=1;}
  SetAssociativeCache icache(c.l1i),dcache(c.l1d),l2(c.l2),llc(c.llc);
  CacheCounters ic,dc,lc,bc;ProtocolCredits private_credit(c.spr_profile.limits),bank(c.spr_profile.limits);
  OnlineSequencer seq(0,c.spr_profile.limits);std::map<std::uint64_t,std::uint64_t> memory;
  std::vector<NativeProtocolMessage> messages;std::vector<PendingOwner> consumers;
  unsigned mutations=0,memory_requests=0,stale_callbacks=0;std::uint64_t now=0;NativeCoherence* active=nullptr;
  auto array=[&](NativeControllerId id)->SetAssociativeCache& {return id.kind==NativeController::Llc?llc:id.kind==NativeController::L1?l2:id.array==NativeCacheArray::Instruction?icache:dcache;};
  auto count_for=[&](NativeControllerId id)->CacheCounters& {return id.kind==NativeController::Llc?bc:id.kind==NativeController::L1?lc:id.array==NativeCacheArray::Instruction?ic:dc;};
  NativeCoherenceHooks h;
  h.allocation=[&](NativeControllerId id,std::uint64_t line){return array(id).prepare_native_allocation(line);};
  h.reserve=[&](NativeControllerId id,std::uint64_t line,std::uint64_t t){++mutations;auto& a=array(id);require(bool(a.reserve_native(a.prepare_native_allocation(line),count_for(id),t)),"typed I reserve real array");};
  h.install=[&](NativeControllerId id,std::uint64_t line,std::uint64_t t,bool dirty){++mutations;auto& a=array(id);require(a.reserved(line)?a.complete_native(line,t,false,true):a.touch_native(line,t),"typed I install real tag");if(dirty)a.mark_dirty(line);};
  h.update_data=[&](NativeControllerId id,std::uint64_t line,std::uint64_t,bool dirty){++mutations;require(array(id).update_native_data(line,dirty),"typed I parent data");};
  h.finish_data=[&](NativeControllerId id,std::uint64_t line,std::uint64_t t){++mutations;auto& a=array(id);require(a.reserved(line)?a.complete_native(line,t):a.touch_native(line,t),"typed I parent finish");};
  h.invalidate=[&](NativeControllerId id,std::uint64_t line,std::uint64_t){++mutations;require(array(id).invalidate(line),"typed I invalidation selects resident array");};
  h.access=[&](NativeControllerId id,std::uint64_t line,std::uint64_t,NativeProbeOrigin){++mutations;auto& a=array(id);require(bool(a.commit_probe(a.prepare_lookup(line),false,count_for(id))),"typed I lookup");};
  h.eviction=[](std::uint32_t,std::uint64_t,std::uint64_t){};
  h.callback_validity=[&](const SequencerService& service,std::uint64_t t,bool stale){
   const auto result=seq.callback(service,SequencerCallback::Read,t);require(result.callback_accepted && !result.wrote,"real read callback never writes");
   for(const auto& a:result.actions)if(a.kind==SequencerActionKind::Completed)consumers.push_back(a.owner);
   stale_callbacks+=stale;return false;
  };
  h.instruction_memory=[&](std::uint64_t token,PendingOwner,std::uint64_t line,std::uint64_t backing,std::uint64_t ready){require(line==backing,"exact bounded physical primitive");++memory_requests;memory.emplace(ready+20,token);};
  h.sent=[&](const NativeProtocolMessage& m){messages.push_back(m);
   if(mode==3 && m.event==NativeProtocolEvent::Exclusive && m.to==0){auto stale=m;stale.event=NativeProtocolEvent::Stale;stale.ready=now;stale.order+=100000;active->receive(stale);}
  };
  NativeCoherence protocol(c,std::move(h),{&private_credit},{&bank});active=&protocol;
  auto issue=[&](std::uint64_t sequence,std::uint64_t address){
   auto granted=seq.grant(seq.peek({{0,sequence,0,10+sequence},address,SequencerKind::Ifetch}),true,now);
   require(granted.actions.size()==1,"I real ordinary grant");
   NativeInstructionRequest request{granted.actions[0].service,{1,address,11,address,address,false}};
   protocol.request(request,now);return request;
  };
  const auto request=issue(0,4096);
  { auto bad=request;bad.address.routed_address+=64;const auto before=mutations;bool rejected=false;
    try{protocol.request(bad,now);}catch(const std::exception&){rejected=true;}
    require(rejected && mutations==before,"malformed instruction address rejected before storage/action mutation"); }
  if(mode==0) {
   for(unsigned i=1;i<16;++i)require(seq.grant(seq.peek({{0,i,0,10+i},4096,i%2?SequencerKind::Read:SequencerKind::Ifetch}),false,now).status==SequencerStatus::Follower,"I/D same-line followers consume real ordinary slots");
   require(seq.peek({{0,16,0,26},8192,SequencerKind::Ifetch}).status==SequencerStatus::Full,"ordinary16 includes I and D consumers");
   for(unsigned i=0;i<16;++i)require(private_credit.acquire({{0,100+i,0,1},100+i,{ProtocolLevel::L1,true,true}}),"saturate only D fill budget");
  }
  unsigned steps=0;
  while(!protocol.idle()) {
   require(++steps<500,"typed I bounded protocol progress");auto next=protocol.next_cycle();
   if(!memory.empty() && (!next || memory.begin()->first<=*next)){now=memory.begin()->first;const auto token=memory.begin()->second;memory.erase(memory.begin());protocol.memory_response(token,now);}
   else {require(bool(next),"typed I next event");now=*next;}
   protocol.advance(now);if(mode==3 && stale_callbacks)break;
  }
  require(memory_requests==1 && !dcache.has_tag(64) && (mode==3?!icache.has_tag(64):icache.contains(64)),"cold Ifetch allocates I only; stale deallocates I");
  require(seq.outstanding()==0 && consumers.size()==(mode==0?16u:1u),"real mixed consumers release every ordinary slot");
  if(mode==3){require(stale_callbacks==1,"stale validity returns separately from descriptor");continue;}
  if(mode==0)for(unsigned i=0;i<16;++i)require(private_credit.release({{0,100+i,0,1},100+i,{ProtocolLevel::L1,true,true}}),"D fill saturation fixture releases own credits");
  protocol.validate();require(private_credit.fills(ProtocolLevel::L2)==0,"I releases unified L2 fill");
  if(mode==0){issue(20,4096);protocol.advance(now);require(memory_requests==1 && ic.hits==1,"stable Ifetch real I hit");
   const auto before=mutations;bool rejected=false;
   try{protocol.request(SequencerService{{0,30,0,1},4096,100,SequencerKind::Read,now,0},now);protocol.advance(now);}catch(const std::exception&){rejected=true;}
   require(rejected && mutations==before && icache.contains(64) && !dcache.has_tag(64),"opposite I to D transfer rejects without dropping I victim");continue;}
  const auto before=mutations;const auto revision=icache.prepare_native_allocation(64).revision;bool rejected=false;
  try {
   if(mode==1){auto bad=messages.back();bad.ready=now;bad.order+=1000000;auto& origin=std::get<NativeInstructionRequest>(bad.origin);++origin.service.generation;protocol.receive(bad);protocol.advance(now);}
   else {issue(1,8192);protocol.advance(now);}
  }catch(const std::exception&){rejected=true;}
  require(rejected && before==mutations && icache.prepare_native_allocation(64).revision==revision,"stale generation/capacity victim rejects before array mutations");
 }
}
void ingress_boundary_tests() {
    using namespace spr;
    for(unsigned fault=0;fault<9;++fault) {
        auto c=config(3);c.cha_count=1;
        std::vector<std::unique_ptr<PrivateHierarchy>> caches;
        std::vector<std::unique_ptr<SetAssociativeCache>> instruction_caches;
        StoreBuffer optional_store;unsigned terminals=0;
        NativeLineState last_l0_state=NativeLineState::I;
        std::vector<std::unique_ptr<ProtocolCredits>> credits;
        std::vector<std::unique_ptr<OnlineSequencer>> sequencers;
        std::vector<CoreCounters> counters(3);
        std::vector<ProtocolCredits*> references;
        for(unsigned core=0;core<3;++core) {
            caches.push_back(std::make_unique<PrivateHierarchy>(c.l1d,c.l2));
            instruction_caches.push_back(std::make_unique<SetAssociativeCache>(c.l1i));
            credits.push_back(std::make_unique<ProtocolCredits>(c.spr_profile.limits));references.push_back(credits.back().get());
            sequencers.push_back(std::make_unique<OnlineSequencer>(core,c.spr_profile.limits));
        }
        SetAssociativeCache llc(c.llc);CacheCounters llc_count;ProtocolCredits bank(c.spr_profile.limits);
        std::map<std::uint64_t,std::uint64_t> memory;
        std::vector<NativeProtocolMessage> messages;
        unsigned mutations=0,callbacks=0,transitions=0;
        std::uint64_t now=0;NativeCoherence* dispatcher=nullptr;
        bool reorder_ack=false,retained_dirty=false,outgoing_consistent=false;
        std::vector<NativeProtocolEvent> control_order;
        NativeCoherenceHooks h;
        h.allocation=[&](NativeControllerId id,std::uint64_t line) {
            if(id.kind==NativeController::L0 && id.array==NativeCacheArray::Instruction)return instruction_caches[id.index]->prepare_native_allocation(line);
            if(id.kind==NativeController::Llc)return llc.prepare_native_allocation(line);
            return id.kind==NativeController::L0?caches[id.index]->prepare_native_l1(line):caches[id.index]->prepare_native_l2(line);
        };
        h.reserve=[&](NativeControllerId id,std::uint64_t line,std::uint64_t cycle) {
            ++mutations;
            if(id.kind==NativeController::L0 && id.array==NativeCacheArray::Instruction){auto& cache=*instruction_caches[id.index];require(bool(cache.reserve_native(cache.prepare_native_allocation(line),counters[id.index].l1i,cycle)),"ingress I reserve");return;}
            if(id.kind==NativeController::Llc)require(bool(llc.reserve_native(llc.prepare_native_allocation(line),llc_count,cycle)),"ingress LLC reserve");
            else if(id.kind==NativeController::L0)require(bool(caches[id.index]->reserve_native_l1(caches[id.index]->prepare_native_l1(line),counters[id.index],cycle)),"ingress L0 reserve");
            else require(bool(caches[id.index]->reserve_native_l2(caches[id.index]->prepare_native_l2(line),counters[id.index],cycle)),"ingress L1 reserve");
        };
        h.install=[&](NativeControllerId id,std::uint64_t line,std::uint64_t cycle,bool dirty) {
            ++mutations;
            if(id.kind==NativeController::L0 && id.array==NativeCacheArray::Instruction){auto& cache=*instruction_caches[id.index];require(!dirty && (cache.reserved(line)?cache.complete_native(line,cycle):cache.touch_native(line,cycle)),"ingress I install");return;}
            if(id.kind==NativeController::Llc) {
                require(llc.reserved(line)?llc.complete_native(line,cycle):llc.touch_native(line,cycle),"ingress LLC install");
                if(dirty)llc.mark_dirty(line);else llc.clear_dirty(line);
            } else {
                auto& cache=*caches[id.index];
                const bool child=id.kind==NativeController::L0;
                require(child?(cache.native_l1_reserved(line)?cache.complete_native_l1(line,false,cycle):cache.touch_native_l1(line,cycle)):
                              (cache.native_l2_reserved(line)?cache.complete_native_l2(line,cycle):cache.touch_native_l2(line,cycle)),"ingress private install");
                cache.native_data_dirty(child,line,dirty);
            }
        };
        h.update_data=[&](NativeControllerId id,std::uint64_t line,std::uint64_t,bool dirty) {
            ++mutations;require(caches[id.index]->update_native_l2_data(line,dirty),"ingress private data arrival");
        };
        h.finish_data=[&](NativeControllerId id,std::uint64_t line,std::uint64_t cycle) {
            ++mutations;require(caches[id.index]->finish_native_l2_data(line,cycle),"ingress private retained data completion");
            if(reorder_ack && id.index==1)retained_dirty=caches[1]->native_data_is_dirty(false,line);
        };
        h.invalidate=[&](NativeControllerId id,std::uint64_t line,std::uint64_t) {
            ++mutations;bool dirty=false;
            if(id.kind==NativeController::L0 && id.array==NativeCacheArray::Instruction){require(instruction_caches[id.index]->invalidate(line),"ingress I invalidate");return;}
            require(id.kind==NativeController::L0?caches[id.index]->invalidate_l1(line,&dirty):caches[id.index]->invalidate_native_l2(line),"ingress invalidate");
        };
        h.access=[&](NativeControllerId id,std::uint64_t line,std::uint64_t cycle,NativeProbeOrigin origin) {
            if(origin==NativeProbeOrigin::StorePrepare && id.kind==NativeController::L0)return;
            ++mutations;
            if(id.kind==NativeController::L0 && id.array==NativeCacheArray::Instruction){auto& cache=*instruction_caches[id.index];require(bool(cache.commit_probe(cache.prepare_lookup(line),false,counters[id.index].l1i)),"ingress I probe");return;}
            if(id.kind==NativeController::Llc)require(llc.touch_native(line,cycle),"ingress LLC access");
            else if(id.kind==NativeController::L0)caches[id.index]->commit_probe_l1(caches[id.index]->prepare_probe_l1(line),false,counters[id.index]);
            else caches[id.index]->commit_probe_l2(caches[id.index]->prepare_probe_l2(line),counters[id.index].l2);
        };
        h.eviction=[](std::uint32_t,std::uint64_t,std::uint64_t){};
        h.callback=[&](const SequencerService& service,std::uint64_t cycle) {
            const auto result=sequencers[service.owner.core]->callback(service,service.kind==SequencerKind::Write?SequencerCallback::Write:SequencerCallback::Read,cycle);
            require(result.callback_accepted,"ingress ordinary callback identity");++callbacks;
            if(result.wrote)require(caches[service.owner.core]->write_native_l1(service.line/64,cycle),"ingress actual dirty publication");
            return result.wrote;
        };
        h.prepare_terminal=[&](const NativePrepareRequest& request,PrepareTerminal reason,std::uint64_t cycle){
            ++terminals;return optional_store.completePrepare(request.token,reason,cycle);
        };
        h.memory=[&](std::uint64_t token,PendingOwner,std::uint64_t,std::uint64_t ready){memory.emplace(ready+20,token);};
        h.audit=[&](const char* event,std::uint64_t,PendingOwner owner,std::uint64_t,std::uint64_t a,std::uint64_t b) {
            if(std::string(event)=="coherence_transition"){
                ++transitions;if((a>>32)==static_cast<unsigned>(NativeController::L0))last_l0_state=static_cast<NativeLineState>(b);
            }
            if(reorder_ack && std::string(event)=="coherence_service" && owner.core==1 && (a>>48)==1 &&
               ((a&0xffffffff)==static_cast<unsigned>(NativeProtocolEvent::Ack) || (a&0xffffffff)==static_cast<unsigned>(NativeProtocolEvent::Baseline)))
                control_order.push_back(static_cast<NativeProtocolEvent>(a&0xffffffff));
        };
        h.sent=[&](const NativeProtocolMessage& m) {
            messages.push_back(m);
            if(reorder_ack && m.event==NativeProtocolEvent::Ack) {
                // Supplemental legal response-order boundary only: deliver the
                // already generated ACK before the queued baseline. The real
                // modeled-link copy stays queued; stop before its later time.
                auto earlier=m;earlier.ready=now;earlier.order+=1000000;dispatcher->receive(earlier);
            }
            if(reorder_ack && m.event==NativeProtocolEvent::Exclusive && m.to==1)
                outgoing_consistent=m.dirty && caches[1]->native_data_is_dirty(false,m.line);
        };
        NativeCoherence protocol(c,std::move(h),references,{&bank});dispatcher=&protocol;
        auto request=[&](unsigned core,unsigned sequence,SequencerKind kind) {
            SequencerRequest request{{core,sequence,0,1},4096,kind};
            auto result=sequencers[core]->grant(sequencers[core]->peek(request),true,now);
            require(result.actions.size()==1,"ingress new ordinary service");
            if(kind==SequencerKind::Ifetch)protocol.request(NativeInstructionRequest{result.actions.front().service,{1,4096,11,4096,4096,false}},now);
            else protocol.request(result.actions.front().service,now);
        };
        auto drain=[&](bool stop_after_baseline=false) {
            unsigned steps=0;
            while(!protocol.idle()) {
                require(++steps<1000,"ingress bounded progress");
                auto next=protocol.next_cycle();
                if(!memory.empty() && (!next || memory.begin()->first<=*next)) {
                    now=memory.begin()->first;const auto token=memory.begin()->second;memory.erase(memory.begin());protocol.memory_response(token,now);
                } else {require(bool(next),"ingress protocol wake");now=*next;}
                protocol.advance(now);
                if(stop_after_baseline && retained_dirty)return;
            }
            protocol.validate();
        };
        if(fault>=7) {
            // Establish S through real remote sharing, not synthetic line state.
            request(1,0,SequencerKind::Read);drain();
            request(0,0,fault==7?SequencerKind::Ifetch:SequencerKind::Read);drain();
            require(last_l0_state==NativeLineState::S &&
                (fault==7?instruction_caches[0]->contains(64):caches[0]->contains_l1(64)),"prepare fixture reached real resident S");
            StoreFacts facts;facts.source_id=1;facts.bytes=8;facts.kind=StoreKind::Ordinary;
            const auto store=optional_store.allocate(facts,now);require(bool(store),"real prepare SQ owner");
            optional_store.markAddressReady(*store,4096,now);
            const auto candidate=optional_store.peekPrepare(now);require(bool(candidate),"real optional candidate");
            const auto accepted=optional_store.trySendPrepare(*candidate,now,[&](const PrepareCandidate& p,PrepareToken token){
                protocol.prepare({{0,token.id,0,6},token,p.line},now);return true;
            });
            require(accepted.outcome==PrepareOutcome::Accepted && optional_store.outstandingPrepares()==1,"accepted optional ingress retains actual owner");
            if(fault==8){
                drain();require(terminals==1 && optional_store.outstandingPrepares()==0 && last_l0_state==NativeLineState::E && caches[0]->contains_l1(64),
                    "same-array data S prepare still upgrades and returns its real optional credit");continue;
            }
            auto snapshot=[&]{
                std::vector<std::uint64_t> values{mutations,callbacks,transitions,messages.size(),terminals,
                    static_cast<unsigned>(last_l0_state),bank.live_leases(),optional_store.outstandingPrepares(),
                    optional_store.used(),optional_store.counters().prepares_accepted,optional_store.counters().prepares_completed,memory.size()};
                for(unsigned core=0;core<3;++core){
                    values.push_back(caches[core]->prepare_native_l1(64).revision);values.push_back(caches[core]->prepare_native_l2(64).revision);
                    values.push_back(instruction_caches[core]->prepare_native_allocation(64).revision);
                    values.push_back(credits[core]->live_leases());values.push_back(credits[core]->tbes(ProtocolLevel::L1));
                    values.push_back(credits[core]->fills(ProtocolLevel::L1));values.push_back(sequencers[core]->outstanding());
                }
                values.push_back(llc.prepare_native_allocation(64).revision);return values;
            };
            const auto before=snapshot();bool rejected=false;
            require(protocol.next_cycle()==std::optional<std::uint64_t>{now+1},"accepted hint dispatches at native entry edge");
            try{protocol.advance(now+1);}catch(const std::invalid_argument& e){rejected=std::string(e.what()).find("opposite I/D array")!=std::string::npos;}
            require(rejected,"I-resident S optional prepare must reject unsupported opposite-array transfer at dispatch");
            require(snapshot()==before && instruction_caches[0]->contains(64) && !caches[0]->contains_l1(64),
                "rejected prepare preserves real tags, state/actions, credits, sends and accepted optional ownership without fake terminal");
            continue;
        }
        if(fault==6) {
            request(0,0,SequencerKind::Write);drain();
            request(1,0,SequencerKind::Read);drain();
            require(caches[1]->native_data_is_dirty(false,64),"legal owner read leaves actual dirty shared private data");
            reorder_ack=true;request(1,1,SequencerKind::Write);drain(true);
            require(control_order==std::vector<NativeProtocolEvent>{NativeProtocolEvent::Ack,NativeProtocolEvent::Baseline},
                    "supplemental production dispatcher exercises baseline-last legal response order");
            require(retained_dirty && outgoing_consistent && credits[1]->tbes(ProtocolLevel::L2)==0,
                    "baseline-last releases private lease and forwards retained actual dirty metadata");
            continue;
        }
        for(unsigned core=0;core<3;++core)request(core,0,SequencerKind::Read);
        drain();request(0,1,SequencerKind::Write);drain();
        // After the first write a new legal generation transfers ownership.
        request(1,1,SequencerKind::Write);drain();
        const auto event=fault==1?NativeProtocolEvent::Ack:(fault==2?NativeProtocolEvent::ExclusiveUnblock:NativeProtocolEvent::Data);
        auto found=std::find_if(messages.begin(),messages.end(),[&](const NativeProtocolMessage& m){return m.event==event;});
        require(found!=messages.end(),"negative envelope comes from actual prior production send");
        auto duplicate=*found;duplicate.ready=now;duplicate.order+=1000000;
        if(fault==3)duplicate.from=UINT32_MAX;
        if(fault==4)++duplicate.line;
        if(fault==5)duplicate.from=duplicate.to; // invalid response role
        const auto before=std::make_tuple(mutations,callbacks,transitions,messages.size(),bank.live_leases());
        std::vector<std::uint64_t> revisions;
        for(auto& cache:caches){revisions.push_back(cache->prepare_native_l1(64).revision);revisions.push_back(cache->prepare_native_l2(64).revision);}
        revisions.push_back(llc.prepare_native_allocation(64).revision);
        bool rejected=false;
        try{protocol.receive(duplicate);protocol.advance(now);}catch(const std::exception&){rejected=true;}
        require(rejected,"stale/duplicate/invalid production ingress must fail closed");
        require(before==std::make_tuple(mutations,callbacks,transitions,messages.size(),bank.live_leases()),"rejected envelope changes no tag/action/state/credit/outgoing message");
        unsigned pos=0;
        for(unsigned core=0;core<3;++core) {
            require(revisions[pos++]==caches[core]->prepare_native_l1(64).revision && revisions[pos++]==caches[core]->prepare_native_l2(64).revision,"rejected envelope preserves actual cache revisions");
            require(credits[core]->live_leases()==0 && sequencers[core]->outstanding()==0,"rejected envelope preserves real typed/ordinary ownership");
        }
        require(revisions.back()==llc.prepare_native_allocation(64).revision,"rejected envelope preserves LLC revision");
    }
}

void tests() {
    for(unsigned invalid=0;invalid<3;++invalid) {
        auto profile=config();
        if(invalid==0)profile.native_coherence.source_bound=false;
        if(invalid==1)profile.native_coherence.source_identity=std::string(64,'0');
        if(invalid==2)profile.causal_l2_request_latency.reset();
        bool rejected=false;try{profile.validate();}catch(const std::invalid_argument&){rejected=true;}
        require(rejected,"runtime rejects unbound protocol identity or absent action latency");
    }
    {
        std::ofstream out("tmp/coherence-profile-parse.cfg");
        out<<"core.causal_native_coherence=false\nspr.coherence.source_bound=true\nspr.coherence.source_identity="<<spr::kNativeCoherenceSourceIdentity
           <<"\nspr.coherence.l0_transitions_per_cycle=32\nspr.coherence.l1_transitions_per_cycle=32\nspr.coherence.llc_transitions_per_cycle=4\nspr.coherence.directory_transitions_per_cycle=32"
           <<"\nspr.coherence.llc_request_latency=2\nspr.coherence.llc_response_latency=2\nspr.coherence.directory_memory_latency=1\nspr.coherence.private_unblock_latency=1"
           <<"\nspr.coherence.ordered_private_links=true\nspr.coherence.unordered_network_queues=true\nspr.coherence.deterministic_unbounded_queues=true\nspr.coherence.directory_count=2"
           <<"\nspr.coherence.directory.0.start=0\nspr.coherence.directory.0.end=1099511627776\nspr.coherence.directory.0.match=0\nspr.coherence.directory.0.masks=64"
           <<"\nspr.coherence.directory.1.start=0\nspr.coherence.directory.1.end=1099511627776\nspr.coherence.directory.1.match=1\nspr.coherence.directory.1.masks=64\n";
        out.close();const auto parsed=load_simulator_config("tmp/coherence-profile-parse.cfg");
        require(!parsed.causal_native_coherence,"imported facts cannot activate runtime implicitly");
        auto routed=config(1);routed.native_coherence=parsed.native_coherence;routed.cha_count=2;
        require(run(routed,{{access(false),access(false,4160)}}).cores[0].retired_instructions==2,"parsed physical bank/directory masks route both actual cold requests");
    }
    // Removing multicore's unsupported throw is necessary but insufficient:
    // later assertions below bind actual permission and response behavior.
    auto c=config(); c.causal_runtime_audit_path="tmp/coherence-cold-writers.csv";
    const auto result=run(c,{{access(true)},{access(true)}});
    require(result.cores.at(0).retired_instructions==1 && result.cores.at(1).retired_instructions==1,
            "both ordinary cold writers must reach real Simulator retirement");
    auto a=events(c.causal_runtime_audit_path);
    require(count(a,"coherence_memory_fetch")==1 && count(a,"coherence_write")==2,
            "two GETX contenders share one fetch and complete each ordinary write");
    bool local_port=false,remote_port=false,response_port=false;
    for(const auto& x:a)if(x.kind=="coherence_service" && (x.a>>48)==1) {
        const auto event=static_cast<spr::NativeProtocolEvent>(x.a&0xffffffff);
        if(event==spr::NativeProtocolEvent::GetX) {if(((x.a>>32)&0xffff)==x.core)local_port|=x.b==2;else remote_port|=x.b==1;}
        if(event==spr::NativeProtocolEvent::Data)response_port|=x.b==0;
    }
    require(local_port && remote_port && response_port,"native private controller separates child demand, remote forward and response ports");
    require(count(a,"coherence_forward")==1 && count(a,"coherence_owner_data")==1,
            "second GETX must forward to the actual current owner");
    require(result.cores[0].cycles<100 && result.cores[1].cycles<100,
            "detached store protocol drain cannot extend retired endpoints");
    {
        auto resources=config(2);resources.causal_spr_resources=true;resources.spr_profile.generic_fu_preset=spr::kGenericFuPreset;
        resources.spr_profile.iq_entries={128,72,48};resources.spr_profile.load_bank_conflicts=true;
        resources.spr_profile.load_banks=16;resources.spr_profile.load_bank_ports=2;
        resources.spr_profile.cache_load_ports=3;resources.spr_profile.cache_store_ports=2;
        resources.rob_entries=512;resources.lq_entries=240;resources.cache_load_ports=3;resources.cache_store_ports=2;
        resources.memory_units=12;resources.dispatch_width=6;resources.issue_width=12;
        resources.fetch_width=resources.decode_width=resources.rename_width=6;
        resources.causal_runtime_audit_path="tmp/coherence-core-resources.csv";
        const auto actual=run(resources,{{access(true),access(false,8192)},{access(true),access(false,8192)}});
        require(actual.cores[0].retired_instructions==2 && actual.cores[1].retired_instructions==2 &&
                count(events(resources.causal_runtime_audit_path),"coherence_write")==2,
                "ordinary multicore coherence composes with actual SPR core resource admission");
    }
    {
        auto one=config(1);one.causal_runtime_audit_path="tmp/coherence-local-exclusive.csv";
        run(one,{{access(false),dependent(access(true))}});
        const auto e=events(one.causal_runtime_audit_path);
        require(edge(e,spr::NativeController::L0,spr::NativeLineState::IS,spr::NativeLineState::E),"sole cold reader gets E");
        require(edge(e,spr::NativeController::L0,spr::NativeLineState::E,spr::NativeLineState::M),"local E store becomes M");
        require(count(e,"coherence_getx")==0 && count(e,"coherence_upgrade")==0,"E write has no GETX/upgrade");
    }
    {
        auto readers=config(2,7);readers.causal_runtime_audit_path="tmp/coherence-readers.csv";
        run(readers,{{access(false),access(false)},{access(false)}});
        const auto e=events(readers.causal_runtime_audit_path);
        require(edge(e,spr::NativeController::Llc,spr::NativeLineState::ISS,spr::NativeLineState::IS),"concurrent reader changes exclusive pending fetch to shared");
        require(count(e,"coherence_memory_fetch")==1 && count(e,"sequencer_response")==3,"shared physical fetch preserves three ordinary callbacks");
        require(count(e,"sequencer_follower")==1,"same-core follower keeps independent ordinary ownership");
    }
    {
        auto owner=config();owner.causal_runtime_audit_path="tmp/coherence-dirty-read-upgrade.csv";
        run(owner,{{access(true)},{access(false,8192),dependent(access(false,12288)),dependent(access(false)),dependent(access(true))}});
        const auto e=events(owner.causal_runtime_audit_path);
        require(count(e,"coherence_owner_data")==1,"remote read takes dirty owner data once");
        const auto data=std::find_if(e.begin(),e.end(),[](const Audit& x){return x.kind=="coherence_owner_data" && x.line==64;});
        require(data!=e.end() && data->a!=0 && data->b==0,"remote read data descends from actual core0 write callback");
        require(edge(e,spr::NativeController::L0,spr::NativeLineState::M,spr::NativeLineState::I),"owner L0 invalidates on InvElse");
        require(edge(e,spr::NativeController::L1,spr::NativeLineState::MM,spr::NativeLineState::SS),"private owner supplies shared data after child response");
        require(edge(e,spr::NativeController::Llc,spr::NativeLineState::MT_IIB,spr::NativeLineState::MT_SB),"owner data first retains shared unblock obligation");
        require(count(e,"coherence_upgrade")==1 && count(e,"coherence_write")==2,"shared requester must upgrade before its second write");
        std::uint64_t completed=0;bool ack_last=false;
        for(const auto& x:e)if(x.kind=="coherence_transition" && x.core==1 && x.line==64 &&
              (x.a>>32)==1 && (x.a&0xffffffff)==static_cast<unsigned>(spr::NativeLineState::SM) && x.b==static_cast<unsigned>(spr::NativeLineState::M))completed=x.cycle;
        for(const auto& x:e)if(x.kind=="coherence_service" && x.core==1 && x.line==64 && x.cycle==completed &&
              (x.a>>48)==1 && (x.a&0xffffffff)==static_cast<unsigned>(spr::NativeProtocolEvent::Ack))ack_last=true;
        const Audit* physical=nullptr;const Audit* child=nullptr;
        for(const auto& x:e)if(x.kind=="coherence_physical_data" && x.core==1 && x.line==64) {
            if(x.a==1 && x.cycle<=completed)physical=&x;
            if(x.a==0 && x.cycle>completed && !child)child=&x;
        }
        require(completed && ack_last,"dirty shared upgrade completes with ACK last");
        require(physical && physical->b==1 && child && child->b==0,
                "ACK-last upgrade retains parent dirty debt while L0 fill starts locally clean");
        require(physical->cycle<completed,"control completion does not rewrite physical data");

    }
    {
        auto order=config(4,128);order.cha_count=1;order.native_coherence.transitions[2]=1;
        order.noc_one_way_latency=0;order.causal_runtime_audit_path="tmp/coherence-unblock-first.csv";
        std::vector<std::vector<TraceRecord>> inputs(4);
        for(unsigned core=0;core<4;++core)
            inputs[core]={access(true,4096+64*core),access(false,4096+64*core),dependent(access(false,4096+64*((core+1)%4)))};
        run(order,inputs);const auto e=events(order.causal_runtime_audit_path);
        require(std::any_of(e.begin(),e.end(),[](const Audit& x){return x.kind=="coherence_transition" && (x.a>>32)==2 &&
                    (x.a&0xffffffff)==static_cast<unsigned>(spr::NativeLineState::MT_IIB) && x.b==static_cast<unsigned>(spr::NativeLineState::MT_IB);}),
                "simultaneous owner transfers allow shared unblock before budget-delayed owner data");
    }
    {
        auto stale=config(3,128);stale.native_coherence.llc_response_latency=100;
        stale.causal_runtime_audit_path="tmp/coherence-read-invalidated-in-flight.csv";
        run(stale,{{access(false)}, {access(false),dependent(access(false,8192)),dependent(access(true))},
                    {access(false,8192),dependent(access(false))}});
        const auto e=events(stale.causal_runtime_audit_path);
        require(edge(e,spr::NativeController::L1,spr::NativeLineState::IS,spr::NativeLineState::IS_I),"invalidation may precede shared read data");
        require(edge(e,spr::NativeController::L1,spr::NativeLineState::IS_I,spr::NativeLineState::I),"stale read completion returns no shared permission");
        require(count(e,"sequencer_response")==4 && count(e,"coherence_write")==1,"stale read remains one ordinary callback without cancelling the writer");
    }
    {
        auto partial=config(3);partial.causal_l1_response_latency=20;
        partial.causal_runtime_audit_path="tmp/coherence-data-before-final-ack.csv";
        run(partial,{{access(false)},{access(false)},{access(false,8192),dependent(access(true))}});
        const auto e=events(partial.causal_runtime_audit_path);
        std::uint64_t data=0,release=0;bool reserved=false;
        for(const auto& x:e)if(x.core==2 && x.line==64) {
            if(x.kind=="coherence_physical_data" && x.a==1)data=x.cycle;
            if(x.kind=="coherence_data_reserved" && x.a==1)reserved=true;
            if(x.kind=="coherence_release" && x.a==1)release=x.cycle;
        }
        require(data && reserved && release>data,"actual data arrival updates protected private tag while final ACK still owns TBE/fill");
    }
    {
        auto c=config(1);c.l2.size_bytes=64;c.l2.associativity=1;
        PrivateHierarchy cache(c.l1d,c.l2);CoreCounters counters;
        require(bool(cache.reserve_native_l2(cache.prepare_native_l2(64),counters,1)),"reserve real private transient slot");
        require(cache.update_native_l2_data(64,true) && cache.native_l2_reserved(64) && !cache.native_l2_contains(64),
                "data writing action preserves transient tag invisibility");
        require(!cache.prepare_native_l2(128).available,"data-arrived transient slot still blocks a competing victim");
        require(cache.finish_native_l2_data(64,3) && cache.native_l2_contains(64) && cache.native_data_is_dirty(false,64),
                "permission completion exposes retained physical dirty data");
    }
    for(unsigned capacity:{1u,2u}) {
        auto credits=config();credits.spr_profile.limits.l1_data_fills=1;credits.spr_profile.limits.l2_data_fills=1;
        credits.spr_profile.limits.l1_tbes=capacity;credits.spr_profile.limits.l2_tbes=capacity;
        credits.causal_runtime_audit_path="tmp/coherence-upgrade-capacity-"+std::to_string(capacity)+".csv";
        auto younger_miss=access(false,8192);younger_miss.producer_dists[0]=2;younger_miss.n_src=1;
        run(credits,{{access(false)},{access(false),dependent(access(true)),younger_miss}});
        const auto e=events(credits.causal_runtime_audit_path);
        std::uint64_t upgrade=UINT64_MAX,other_acquire=UINT64_MAX,other_release=UINT64_MAX;
        unsigned tbe_only=0;
        for(const auto& x:e)if(x.core==1) {
            if(x.kind=="coherence_upgrade")upgrade=x.cycle;
            if(x.kind=="coherence_acquire" && x.line==128 && x.a==0)other_acquire=x.cycle;
            if(x.kind=="coherence_release" && x.line==128 && x.a==0)other_release=x.cycle;
            if(x.kind=="coherence_acquire" && x.line==64 && x.b==0)++tbe_only;
        }
        require(tbe_only==2,"S upgrade acquires L0/private-L1 TBEs without data fills");
        require(other_acquire<upgrade,"unrelated missing line holds fill before upgrade");
        require(capacity==1?upgrade>=other_release:upgrade<other_release,
                "upgrade stalls on TBE pressure but not on occupied fill pool");
    }
    {
        std::vector<TraceRecord> many;
        for(unsigned i=0;i<12;++i)many.push_back(access(false,4096+64*i));
        auto budgets=config();budgets.native_coherence.transitions={1,1,1,1};budgets.causal_l0_transitions_per_cycle=1;
        budgets.causal_runtime_audit_path="tmp/coherence-budgets.csv";
        run(budgets,{many,many});const auto e=events(budgets.causal_runtime_audit_path);
        require(std::any_of(e.begin(),e.end(),[](const Audit& x){return x.kind=="coherence_budget_defer";}),"busy controllers must defer over-budget transitions");
        std::set<std::uint64_t> first,second;
        for(const auto& x:e)if(x.kind=="coherence_acquire" && x.a==0)(x.core==0?first:second).insert(x.cycle);
        std::vector<std::uint64_t> common;
        std::set_intersection(first.begin(),first.end(),second.begin(),second.end(),std::back_inserter(common));
        require(!common.empty(),"different L0 controllers spend their own same-cycle transition budget");
        require(std::count_if(e.begin(),e.end(),[](const Audit& x){return x.kind=="sequencer_response";})==24,"budget deferral cannot drop or duplicate callbacks");
    }
    {
        std::string reference;
        for(unsigned chunk:{1u,2u,7u,128u}) {
            auto batching=config(2,chunk);batching.causal_runtime_audit_path="tmp/coherence-batch-"+std::to_string(chunk)+".csv";
            run(batching,{{access(true),access(true,8192),access(false,12288)},
                          {access(false,12288),dependent(access(false)),dependent(access(true)),access(false,8192)}});
            const auto actual=canonical(events(batching.causal_runtime_audit_path));
            if(reference.empty())reference=actual;else require(reference==actual,"source batch size cannot alter modeled protocol event identities/times");
        }
    }
    {
        std::string reference;
        for(unsigned delay:{0u,100u}) {
            auto host=config(2,2);host.causal_runtime_audit_path="tmp/coherence-host-delay-"+std::to_string(delay)+".csv";
            run(host,{{access(true),access(false,8192),dependent(access(false))},
                      {access(false,8192),dependent(access(false)),dependent(access(true))}},delay);
            const auto actual=canonical(events(host.causal_runtime_audit_path));
            if(reference.empty())reference=actual;else require(actual==reference,"host producer delays cannot change certified modeled frontier");
        }
    }
    {
        auto three=config(3);three.causal_l1_response_latency=11;
        three.causal_runtime_audit_path="tmp/coherence-three-sharers.csv";
        run(three,{{access(false)},{access(false)},{access(false),dependent(access(true))}});
        const auto e=events(three.causal_runtime_audit_path);
        std::uint64_t last_ack=0,write=0;bool two=false;
        for(const auto& x:e) {
            if(x.kind=="coherence_ack_progress" && x.core==2 && x.b==2) {
                two=true;if(x.a==2)last_ack=x.cycle;
            }
            if(x.kind=="coherence_write" && x.core==2)write=x.cycle;
        }
        require(two && last_ack && write>=last_ack+three.causal_l2_response_latency.value(),
                "three-sharer upgrade waits for two unique ACK senders before local write response");
    }
    {
        auto bank=config(1);bank.spr_profile.limits.llc_tbes=1;bank.causal_l2_request_latency=30;
        bank.causal_runtime_audit_path="tmp/coherence-llc-release-unblock.csv";
        run(bank,{{access(false),access(false,8192)}});const auto e=events(bank.causal_runtime_audit_path);
        std::uint64_t release=0,unblock=0,second=0;
        for(const auto& x:e) {
            if(x.kind=="coherence_release" && x.line==64 && x.a==2)release=x.cycle;
            if(x.kind=="coherence_acquire" && x.line==128 && x.a==2)second=x.cycle;
            if(x.kind=="coherence_transition" && x.line==64 && (x.a>>32)==2 &&
               x.b==static_cast<unsigned>(spr::NativeLineState::MT))unblock=x.cycle;
        }
        require(release && second>=release && second<unblock,"LLC miss lease frees at Mem_Data while first line still awaits unblock");
    }
    {
        auto private_release=config(1);private_release.spr_profile.limits.l2_tbes=1;
        private_release.spr_profile.limits.l2_data_fills=1;private_release.causal_l2_response_latency=30;
        private_release.causal_runtime_audit_path="tmp/coherence-private-release.csv";
        run(private_release,{{access(false),access(false,8192)}});const auto e=events(private_release.causal_runtime_audit_path);
        std::uint64_t parent_release=0,child_release=0,second=0;
        for(const auto& x:e) {
            if(x.kind=="coherence_release" && x.line==64 && x.a==1)parent_release=x.cycle;
            if(x.kind=="coherence_release" && x.line==64 && x.a==0)child_release=x.cycle;
            if(x.kind=="coherence_acquire" && x.line==128 && x.a==1)second=x.cycle;
        }
        require(parent_release && second>=parent_release && second<child_release,"private TBE/fill frees before its L0 response reaches child");
    }
    {
        auto repeated=config(2,7);repeated.causal_runtime_audit_path="tmp/coherence-repeated-generations.csv";
        std::vector<std::vector<TraceRecord>> inputs(2);
        for(unsigned core=0;core<2;++core) {
            inputs[core].push_back(access(false));
            for(unsigned round=0;round<3;++round) {
                inputs[core].push_back(dependent(access(true)));
                inputs[core].push_back(dependent(access(false,8192+core*4096+round*64)));
                inputs[core].push_back(dependent(access(false)));
            }
        }
        const auto result=run(repeated,inputs);const auto e=events(repeated.causal_runtime_audit_path);
        require(result.cores[0].retired_instructions==10 && result.cores[1].retired_instructions==10,
                "repeated legal same-line generations retain all retired owners");
        require(count(e,"coherence_write")==6,"each nonmerged repeated write obtains one actual callback");
        require(edge(e,spr::NativeController::L1,spr::NativeLineState::SM,spr::NativeLineState::SM_IL0),
                "competing upgrades exercise child invalidation while demand TBE remains owned");
    }
    // Whole-Simulator unsupported input failures do not return partial scores.
    for(unsigned mode=0;mode<8;++mode) {
        auto bad=config(1);bad.causal_runtime_audit_path="tmp/coherence-negative-"+std::to_string(mode)+".csv";
        std::vector<TraceRecord> rows{access(false)};
        std::uint32_t extra=0;std::uint64_t flags=0;bool boundary=false;
        if(mode==0)extra=fst::kStoreMasked;
        if(mode==1){extra=fst::kStoreSecure;flags=0x10000000;}
        if(mode==2)extra=fst::kStoreAtomic;
        if(mode==3)rows[0]=access(false,4096+60);
        if(mode==4)boundary=true;
        if(mode==5)bad.spr_profile.early_store_prepare=true;
        if(mode==6)bad.causal_early_store_prepare=true;
        if(mode==7)rows[0].set_kernel_mode(true);
        bool returned=false,rejected=false;
        try {
            std::vector<std::unique_ptr<TraceSource>> sources;
            sources.push_back(std::make_unique<Source>(rows,0,extra,flags,0,boundary));
            Simulator simulator(bad,std::move(sources));simulator.run();returned=true;
        }catch(const std::exception&){rejected=true;}
        require(rejected && !returned,"unsupported functional/config combination returns no score");
    }
    {
        auto late=config(1);late.causal_runtime_audit_path="tmp/coherence-late-unsupported.csv";
        std::vector<TraceRecord> rows(512,access(false));
        bool returned=false,rejected=false;
        try {
            std::vector<std::unique_ptr<TraceSource>> sources;
            sources.push_back(std::make_unique<Source>(rows,0,fst::kStoreSecure,0x10000000,511));
            Simulator simulator(late,std::move(sources));simulator.run();returned=true;
        }catch(const std::exception&){rejected=true;}
        require(rejected && !returned && count(events(late.causal_runtime_audit_path),"sequencer_response")>0,
                "late unsupported input rejects whole run after valid prior work without returning partial score");
    }
    for(unsigned level=0;level<3;++level) {
        auto victim=config(1);victim.cha_count=1;
        auto& target=level==0?victim.l1d:(level==1?victim.l2:victim.llc);
        target.size_bytes=64;target.associativity=1;
        victim.causal_runtime_audit_path="tmp/coherence-victim-"+std::to_string(level)+".csv";
        bool rejected=false;
        try{run(victim,{{access(false),dependent(access(false,8192))}});}catch(const std::exception&){rejected=true;}
        require(rejected,"private/LLC replacement remains unsupported");
        const auto e=events(victim.causal_runtime_audit_path);
        require(std::none_of(e.begin(),e.end(),[&](const Audit& x){return x.kind=="coherence_acquire" && x.line==128 && x.a==level;}),
                "victim refusal precedes rejected controller lease/tag/state mutation");
    }

}
}
int main() { try {
    { auto c=config(1); spr::NativeCoherence n(c,{}, {}, {}); n.request(spr::NativeInstructionRequest{{{0,0,0,10},4096,1,spr::SequencerKind::Ifetch,0,0},{1,4096,1,4096,4096,false}},1); }
    instruction_protocol_tests(); tests(); ingress_boundary_tests(); std::cout<<"native coherence Simulator tests passed\n"; return 0; }
    catch(const std::exception& e) { std::cerr<<e.what()<<'\n'; return 1; } }
