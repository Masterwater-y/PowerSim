#include "fastsim/simulator.hpp"
#include "fastsim/cache.hpp"
#include "fastsim/core_continuation.hpp"
#include "fastsim/fst_store_requests.hpp"
#include <iostream>
#include <algorithm>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <filesystem>
#include <unistd.h>
#include "fastsim/spr_native_coherence.hpp"
#include <thread>
#include <chrono>
using namespace fastsim;
namespace {
void check(bool v,const char* s){if(!v)throw std::runtime_error(s);}
SimulatorConfig cfg(unsigned chunk=1) {
    SimulatorConfig c;c.core_model="interval_weave";c.cores=1;c.coherence=false;c.causal_runtime=true;
    c.causal_native_memory=true;c.store_commit_to_iew_delay=0;
    c.causal_mandatory_queue_latency=1;c.causal_l1_data_access_latency=3;c.causal_mandatory_queue_ordered=false;c.causal_l1_request_latency=2;
    c.l1d.replacement=c.l2.replacement=c.llc.replacement=ReplacementPolicy::kLru;
    c.causal_l1_response_latency=2;c.causal_l2_request_latency=2;c.causal_l2_response_latency=2;c.causal_llc_to_l2_latency=1;
    c.causal_spr_resources=true;c.spr_profile.generic_fu_preset=spr::kGenericFuPreset;
    c.core_model="interval_weave";c.cores=1;c.coherence=false;c.causal_runtime=true;c.causal_native_memory=true;c.store_commit_to_iew_delay=0;
    c.spr_profile.iq_entries={128,72,48};
    c.spr_profile.load_bank_conflicts=true;c.spr_profile.load_banks=16;c.spr_profile.load_bank_ports=2;c.spr_profile.cache_load_ports=3;c.spr_profile.cache_store_ports=2;
    c.rob_entries=512;c.lq_entries=240;c.cache_load_ports=3;c.cache_store_ports=2;c.memory_units=12;c.dispatch_width=6;c.issue_width=12;
    c.fetch_width=c.decode_width=c.rename_width=6;
    c.spr_profile.limits={16,16,16,48,256,256,256,112,4,4};
    c.chunk_instructions=chunk;c.interval_target_uops=1;
    c.fetch_to_decode=c.decode_to_rename=c.rename_to_dispatch=0;
    c.dispatch_to_issue=c.issue_to_execute=c.execute_to_commit=c.load_response_to_ready=0;
    return c;
}
TraceRecord store(std::uint64_t address){TraceRecord r;r.op_class=57;r.flags=kRetires|kStore|kPhysicalAddress;r.address=address;r.size=8;return r;}

}
namespace {
struct Event {std::string kind;std::uint64_t cycle,core,sequence,fragment,line,issue,completion,generation;};
std::vector<Event> events(const std::string& path) {
    std::ifstream in(path);std::string row;std::vector<Event> result;std::getline(in,row);
    while(std::getline(in,row)) {
        std::istringstream stream(row);std::vector<std::string> fields;std::string field;
        while(std::getline(stream,field,','))fields.push_back(field);
        check(fields.size()==9,"audit schema");
        result.push_back({fields[0],std::stoull(fields[1]),std::stoull(fields[2]),std::stoull(fields[3]),
            std::stoull(fields[4]),std::stoull(fields[5]),std::stoull(fields[6]),std::stoull(fields[7]),std::stoull(fields[8])});
    }
    return result;
}
std::string contents(const std::string& path) {
    // Synchronous refill can interleave same-cycle audit delivery; identity,
    // cycle and every lifecycle relation must remain identical.
    std::ifstream in(path);std::vector<std::string> rows;std::string row;
    while(std::getline(in,row))rows.push_back(row);
    std::sort(rows.begin(),rows.end());std::ostringstream out;
    for(const auto& value:rows)out<<value<<'\n';return out.str();
}
TraceRecord load(std::uint64_t address){auto r=store(address);r.op_class=56;r.flags=kRetires|kLoad|kPhysicalAddress;return r;}
TraceRecord compute(int op){TraceRecord r;r.op_class=op;r.flags=kRetires;return r;}
std::vector<std::string> lifecycle(const std::string& path) {
    std::ifstream in(path);std::vector<std::string> rows;std::string row;
    while(std::getline(in,row))if(row.rfind("core_resource_cost,",0)!=0)rows.push_back(row);
    std::sort(rows.begin(),rows.end());return rows;
}
struct StaticFact {
    TraceRecord record;
    fst::StaticUopFactsRow row;
    std::vector<fst::StaticUopOperand> operands;
    std::optional<fst::StoreRequestFacts> memory_facts;
};
StaticFact integer(unsigned family,unsigned mask,unsigned latency,int op=1,unsigned dependency=0) {
    StaticFact f;f.record=compute(op);f.record.n_dst=1;
    f.row.flags=fst::kStaticExecutionKnown|fst::kStaticOperandRolesKnown;
    f.row.integer_family=family;f.row.candidate_ports=mask;f.row.nominal_latency=latency;f.row.destination_count=1;
    if(dependency) {
        f.record.n_src=1;f.record.producer_dists[0]=dependency;f.record.producer_classes[0]=0;
        f.row.source_count=1;f.operands.push_back({dependency,0,1,0});
    }
    return f;
}
class DelayedSource final : public TraceSource {
    BinaryTraceSource source_; unsigned delay_;
public:
    DelayedSource(const std::string& path,unsigned delay):source_(path),delay_(delay){}
    bool next(TraceRecord& row) override {if(delay_)std::this_thread::sleep_for(std::chrono::microseconds(delay_));return source_.next(row);}
    std::string description() const override {return source_.description();}
    bool complete_dependencies() const override {return source_.complete_dependencies();}
    bool functional_retirement_complete() const override {return source_.functional_retirement_complete();}
    bool memory_request_roles_complete() const override {return source_.memory_request_roles_complete();}
    std::optional<fst::MemoryRequestRole> current_memory_request_role() const override {return source_.current_memory_request_role();}
    std::optional<bool> current_ordinary_load_eligible() const override {return source_.current_ordinary_load_eligible();}
    const fst::StoreRequestFacts* current_store_facts() const override {return source_.current_store_facts();}
    const fst::StoreRequestFacts* current_memory_facts() const override {return source_.current_memory_facts();}
    const fst::StaticUopFactsRow* current_static_uop_facts() const override {return source_.current_static_uop_facts();}
    const std::vector<fst::StaticUopOperand>& current_static_uop_operands() const override {return source_.current_static_uop_operands();}
    std::string static_uop_policy_identity() const override {return source_.static_uop_policy_identity();}
};
SimulationStats run_static(SimulatorConfig c,const std::vector<std::vector<StaticFact>>& streams,unsigned delay=0) {
    const auto directory=std::filesystem::path("tmp")/("f2-coherent-prepare-"+std::to_string(getpid()));
    std::filesystem::create_directories(directory);
    std::vector<std::unique_ptr<TraceSource>> sources;
    unsigned core=0;
    for(const auto& facts:streams) {
        const auto path=(directory/("trace"+std::to_string(core++)+".fst")).string();
        std::array<std::uint8_t,32> policy;policy.fill(0x42);
        {BinaryTraceWriter writer(path,0);writer.enable_complete_dependencies();writer.enable_memory_request_roles();
         writer.enable_functional_retirement();writer.enable_static_uop_facts(policy);writer.enable_store_facts();
         for(const auto& f:facts) {
             fst::StoreRequestFacts memory;memory.address=f.record.address;memory.size=f.record.size;
             memory.byte_enable=f.record.size?((1ull<<f.record.size)-1):0;
             memory.attributes=fst::kStoreFactsComplete|(f.record.is_write()?fst::kStoreOrdinary:(fst::kMemoryRead|fst::kOrdinaryLoad));
             if(f.memory_facts)memory=*f.memory_facts;
             writer.append(f.record,nullptr,{},fst::MemoryRequestRole::Data,0,false,false,f.record.is_memory()?&memory:nullptr,&f.row,&f.operands);
         }
         writer.close();}
        sources.push_back(std::make_unique<DelayedSource>(path,delay));
    }
    Simulator simulator(c,std::move(sources));auto result=simulator.run();
    std::filesystem::remove_all(directory);return result;
}
SimulatorConfig static_cfg(unsigned chunk=16) {
    auto c=cfg(chunk);c.causal_static_integer=true;c.spr_profile.integer_nominal_shape_verified=true;
    c.spr_profile.static_uop_policy_sha256="4242424242424242424242424242424242424242424242424242424242424242";
    return c;
}
StaticFact split_member(bool address,unsigned dependency=0) {
    auto f=integer(0,0,1,address?kSprStoreAddress:kSprStoreData,dependency);
    f.record=address?store(4096):compute(kSprStoreData);
    if(address)f.record.op_class=kSprStoreAddress;
    if(dependency){f.record.n_src=1;f.record.producer_dists[0]=dependency;}
    f.row.flags|=fst::kStaticAddressDataKnown|fst::kStaticStorePairKnown;
    f.row.store_role=address?fst::StaticStoreRole::Address:fst::StaticStoreRole::Data;
    f.row.store_group=1;
    if(dependency){if(address)f.row.address_operands=1;else f.row.data_operands=1;}
    return f;
}
SimulatorConfig early_cfg(const std::string& label,unsigned chunk=1) {
    auto c=static_cfg(chunk);c.spr_profile.early_store_prepare=true;c.causal_early_store_prepare=true;
    c.causal_l0_transitions_per_cycle=32;
    c.cores=2;c.causal_native_coherence=true;c.cha_xor_hash=false;
    c.native_coherence.source_bound=true;c.native_coherence.source_identity=spr::kNativeCoherenceSourceIdentity;
    c.native_coherence.transitions={32,32,4,32};
    c.native_coherence.llc_request_latency=2;c.native_coherence.llc_response_latency=2;
    c.native_coherence.directory_memory_latency=1;c.native_coherence.private_unblock_latency=1;
    c.native_coherence.ordered_private_links=true;c.native_coherence.unordered_network_queues=true;
    c.native_coherence.deterministic_unbounded_queues=true;
    c.native_coherence.directories.push_back({0,1ull<<40,0,{}});
    c.causal_runtime_audit_path="tmp/f2-coherent-"+label+"-"+std::to_string(chunk)+".csv";return c;
}
unsigned count(const std::vector<Event>& es,const std::string& kind) {
    return static_cast<unsigned>(std::count_if(es.begin(),es.end(),[&](const auto& e){return e.kind==kind;}));
}
Event first(const std::vector<Event>& es,const std::string& kind) {
    const auto it=std::find_if(es.begin(),es.end(),[&](const auto& e){return e.kind==kind;});
    check(it!=es.end(),("missing event "+kind).c_str());return *it;
}
StaticFact memory_load(std::uint64_t address,unsigned dependency=0) {
    auto f=integer(0,0,1,56,dependency);f.record=load(address);
    if(dependency){f.record.n_src=1;f.record.producer_dists[0]=dependency;}return f;
}
std::vector<StaticFact> delayed_stores(unsigned n,bool same_line=false,unsigned loads=1) {
    std::vector<StaticFact> result;
    for(unsigned i=0;i<loads;++i)result.push_back(memory_load(8192));
    for(unsigned i=0;i<n;++i) {
        auto sa=split_member(true),sd=split_member(false,static_cast<unsigned>(result.size()+1));
        sa.row.store_group=sd.row.store_group=i+1;sa.record.address=4096+(same_line?0:64*i);
        result.push_back(sa);result.push_back(sd);
    }
    return result;
}
void coherent_takeover() {
    std::vector<std::string> reference;
    for(unsigned chunk:{1u,2u,7u,128u})for(unsigned delay:{0u,10u}) {
        auto c=early_cfg("takeover",chunk);
        const std::vector<StaticFact> stream{integer(0,0,1,7),split_member(true),split_member(false,2)};
        const auto stats=run_static(c,{stream,stream},delay);const auto es=events(c.causal_runtime_audit_path);
        check(count(es,"prepare_accepted")==2 && count(es,"prepare_taken_over")==2,"two cores independently prepare/take over one physical line");
        check(count(es,"prepare_fill")==0 && count(es,"coherence_memory_fetch")==1,"one memory fetch, two coherent physical owners");
        check(count(es,"sequencer_request")==2 && count(es,"store_write_complete")==2,"ordinary callbacks remain independent");
        for(unsigned core=0;core<2;++core) {
            std::uint64_t accepted=UINT64_MAX,data=0,commit=0,completed=0;
            for(const auto& e:es)if(e.core==core) {
                if(e.kind=="prepare_accepted")accepted=e.cycle;
                if(e.kind=="store_data_ready")data=e.cycle;
                if(e.kind=="store_commit")commit=e.cycle;
                if(e.kind=="store_write_complete")completed=e.cycle;
            }
            check(accepted<data && accepted<commit,"prepare precedes functional SD/retirement");
            check(stats.cores[core].cycles<completed,"detached coherent drain does not extend F1 local clock");
            check(stats.cores[core].l1d.accesses==1 && stats.cores[core].l1d.misses==1,"one demand miss at takeover, no hint demand probe");
        }
        auto rows=lifecycle(c.causal_runtime_audit_path);
        rows.erase(std::remove_if(rows.begin(),rows.end(),[](const auto& row){return row.rfind("store_drain_source,",0)==0;}),rows.end());
        if(!reference.empty())check(rows==reference,"full modeled tuples invariant across chunks/host delay");reference=rows;
    }
}
void integration_caps_fill_and_upgrade() {
    {
        auto c=early_cfg("cap16",7);
        const auto stream=delayed_stores(17,false,16);
        const auto result=run_static(c,{stream,stream});const auto es=events(c.causal_runtime_audit_path);
        for(unsigned core=0;core<2;++core) {
            unsigned hints=0,ordinary=0,max_hints=0,max_ordinary=0,accepted=0,dropped=0;
            std::map<std::uint64_t,unsigned> sends;
            for(const auto& e:es)if(e.core==core) {
                if(e.kind=="prepare_accepted"){++accepted;max_hints=std::max(max_hints,++hints);}
                if(e.kind=="prepare_dropped")++dropped;
                if(e.kind=="prepare_fill" || e.kind=="prepare_taken_over" || e.kind=="prepare_ignored"){check(hints>0,"optional terminal has accepted credit");--hints;}
                if(e.kind=="sequencer_request" || e.kind=="sequencer_follower")max_ordinary=std::max(max_ordinary,++ordinary);
                if(e.kind=="sequencer_response"){check(ordinary>0,"ordinary terminal has accepted credit");--ordinary;}
                if(e.kind=="prepare_accepted" || e.kind=="prepare_dropped" || e.kind=="store_write_accepted")check(++sends[e.cycle]<=2,"CPU mandatory/optional share store send ports");
            }
            check(accepted==16 && dropped==1 && max_hints==16 && max_ordinary==16,"per-core independent optional16 and ordinary16 caps");
            check(!hints && !ordinary && result.cores[core].retired_uops==50,"full cap fixture population/credits drain");
        }
    }
    {
        auto c=early_cfg("duplicates");c.cores=1;
        run_static(c,{delayed_stores(6,true)});const auto es=events(c.causal_runtime_audit_path);
        check(count(es,"prepare_accepted")==6 && count(es,"prepare_ignored")==5,"duplicates independently return optional credits");
        check(count(es,"prepare_physical_started")==1,"duplicate creates no second physical acquisition");
    }
    {
        auto c=early_cfg("loadwait");c.cores=1;std::vector<StaticFact> stream;
        for(unsigned i=0;i<40;++i)stream.push_back(integer(0,0,1,7,i?1:0));
        stream.push_back(split_member(true));stream.push_back(split_member(false,2));stream.push_back(memory_load(4112));
        const auto stats=run_static(c,{stream});const auto es=events(c.causal_runtime_audit_path);
        check(count(es,"prepare_fill")==1 && count(es,"prepare_taken_over")==0,"untaken exclusive fill has its own terminal");
        check(count(es,"coherence_park")>=1,"load parks behind SP_IE until real permission/data");
        check(stats.cores[0].l1d.misses==0 && count(es,"coherence_write")==1,"hint fill does not write or create ordinary miss");
    }
    {
        auto c=early_cfg("small");c.cores=1;c.spr_profile.limits.prepare_requests=2;
        c.spr_profile.limits.ordinary_requests=1;c.spr_profile.limits.l1_data_fills=1;c.spr_profile.limits.l1_tbes=2;
        const auto stats=run_static(c,{delayed_stores(3)});const auto es=events(c.causal_runtime_audit_path);
        check(count(es,"prepare_fill_recycle")>0 && count(es,"prepare_dropped")==1,"fill-full optional recycles without leaking optional credit");
        check(stats.cores[0].retired_uops==7,"small-cap full population preserved");
        c=early_cfg("tbe");c.cores=1;c.spr_profile.limits.l1_tbes=1;
        run_static(c,{delayed_stores(3)});const auto stalled=events(c.causal_runtime_audit_path);
        check(count(stalled,"prepare_resource_wait")>0 && count(stalled,"prepare_fill_recycle")==0,"TBE wait is distinct from no-fill recycle");
    }
    {
        auto c=early_cfg("shared-upgrade");
        auto sa=split_member(true,1),sd=split_member(false);
        run_static(c,{{memory_load(4096),sa,sd},{memory_load(4096)}});const auto es=events(c.causal_runtime_audit_path);
        bool shared=false;
        for(const auto& e:es)if(e.kind=="coherence_transition" && (e.issue>>32)==0 &&
            (e.issue&0xffffffff)==static_cast<unsigned>(spr::NativeLineState::S) &&
            e.completion==static_cast<unsigned>(spr::NativeLineState::SP_IE))shared=true;
        check(shared,"real shared read followed by prepare issues coherent upgrade");
    }
    {
        auto c=early_cfg("budget",128);c.native_coherence.transitions={1,1,1,1};c.causal_l0_transitions_per_cycle=1;
        run_static(c,{delayed_stores(4),delayed_stores(4)});
        check(count(events(c.causal_runtime_audit_path),"coherence_budget_defer")>0,"native per-controller budget defers rather than legacy throw");
    }
}

// Supplemental boundary fixture: real production tags, leases, StoreBuffer and
// OnlineSequencer; memory return is controlled only to expose source races.
// Simulator cases above own real DRAM and retirement qualification.
struct ProtocolHarness {
    using K=spr::NativeController;using E=spr::NativeProtocolEvent;using S=spr::NativeLineState;
    SimulatorConfig config;
    std::vector<std::unique_ptr<PrivateHierarchy>> caches;
    SetAssociativeCache llc;
    std::vector<CoreCounters> counters;
    CacheCounters llc_counts;
    std::vector<std::unique_ptr<spr::ProtocolCredits>> credits;
    spr::ProtocolCredits bank;
    std::vector<std::unique_ptr<spr::OnlineSequencer>> sequencers;
    std::vector<std::unique_ptr<spr::StoreBuffer>> stores;
    std::unique_ptr<spr::NativeCoherence> protocol;
    std::vector<spr::NativeProtocolMessage> messages;
    std::vector<Event> audit;
    struct Terminal {spr::NativePrepareRequest request;spr::PrepareTerminal kind;std::uint64_t cycle;};
    struct Callback {spr::SequencerService service;std::uint64_t cycle;bool wrote;};
    std::vector<Terminal> terminals;
    std::vector<Callback> callbacks;
    std::map<std::pair<std::uint64_t,std::uint64_t>,std::uint64_t> memory;
    std::vector<std::uint64_t> sequences;
    std::uint64_t now=0,mutations=0,evictions=0;
    std::function<std::uint64_t(std::uint64_t)> memory_delay=[](std::uint64_t){return 20;};
    explicit ProtocolHarness(SimulatorConfig c):config(std::move(c)),llc(config.llc),counters(config.cores),bank(config.spr_profile.limits),sequences(config.cores) {
        check(config.cha_count==1,"boundary fixture uses one real shared bank");
        std::vector<spr::ProtocolCredits*> pointers;
        for(unsigned core=0;core<config.cores;++core) {
            caches.push_back(std::make_unique<PrivateHierarchy>(config.l1d,config.l2));
            credits.push_back(std::make_unique<spr::ProtocolCredits>(config.spr_profile.limits));pointers.push_back(credits.back().get());
            sequencers.push_back(std::make_unique<spr::OnlineSequencer>(core,config.spr_profile.limits));
            spr::BufferProfile p;p.prepare_outstanding=config.spr_profile.limits.prepare_requests;
            stores.push_back(std::make_unique<spr::StoreBuffer>(p));
        }
        spr::NativeCoherenceHooks h;
        h.allocation=[&](spr::NativeControllerId id,std::uint64_t line) {
            if(id.kind==K::Llc)return llc.prepare_native_allocation(line);
            return id.kind==K::L0?caches[id.index]->prepare_native_l1(line):caches[id.index]->prepare_native_l2(line);
        };
        h.reserve=[&](spr::NativeControllerId id,std::uint64_t line,std::uint64_t cycle) {
            ++mutations;
            if(id.kind==K::Llc)check(bool(llc.reserve_native(llc.prepare_native_allocation(line),llc_counts,cycle)),"real shared reserve");
            else if(id.kind==K::L0)check(bool(caches[id.index]->reserve_native_l1(caches[id.index]->prepare_native_l1(line),counters[id.index],cycle)),"real L0 reserve");
            else check(bool(caches[id.index]->reserve_native_l2(caches[id.index]->prepare_native_l2(line),counters[id.index],cycle)),"real L1 reserve");
        };
        h.install=[&](spr::NativeControllerId id,std::uint64_t line,std::uint64_t cycle,bool dirty) {
            ++mutations;
            if(id.kind==K::Llc) {
                check(llc.reserved(line)?llc.complete_native(line,cycle):llc.touch_native(line,cycle),"real LLC install");
                if(dirty)llc.mark_dirty(line);else llc.clear_dirty(line);
            } else {
                auto& cache=*caches[id.index];const bool child=id.kind==K::L0;
                check(child?(cache.native_l1_reserved(line)?cache.complete_native_l1(line,false,cycle):cache.touch_native_l1(line,cycle)):
                    (cache.native_l2_reserved(line)?cache.complete_native_l2(line,cycle):cache.touch_native_l2(line,cycle)),"real private install");
                cache.native_data_dirty(child,line,dirty);
            }
        };
        h.update_data=[&](spr::NativeControllerId id,std::uint64_t line,std::uint64_t,bool dirty) {
            ++mutations;check(caches[id.index]->update_native_l2_data(line,dirty),"real parent data arrival");
        };
        h.finish_data=[&](spr::NativeControllerId id,std::uint64_t line,std::uint64_t cycle) {
            ++mutations;check(caches[id.index]->finish_native_l2_data(line,cycle),"real parent permission finish");
        };
        h.invalidate=[&](spr::NativeControllerId id,std::uint64_t line,std::uint64_t) {
            ++mutations;bool dirty=false;
            check(id.kind==K::L0?caches[id.index]->invalidate_l1(line,&dirty):caches[id.index]->invalidate_native_l2(line),"real private invalidation");
        };
        h.access=[&](spr::NativeControllerId id,std::uint64_t line,std::uint64_t cycle,spr::NativeProbeOrigin origin) {
            if(id.kind==K::Llc)check(llc.touch_native(line,cycle),"real shared lookup");
            else if(id.kind==K::L0) {
                if(origin==spr::NativeProbeOrigin::StorePrepare)return;
                if(origin==spr::NativeProbeOrigin::Takeover){++counters[id.index].l1d.accesses;++counters[id.index].l1d.misses;return;}
                check(bool(caches[id.index]->commit_probe_l1(caches[id.index]->prepare_probe_l1(line),false,counters[id.index])),"real child lookup");
            } else check(bool(caches[id.index]->commit_probe_l2(caches[id.index]->prepare_probe_l2(line),counters[id.index].l2)),"real parent lookup");
        };
        h.eviction=[&](std::uint32_t core,std::uint64_t line,std::uint64_t cycle){++evictions;stores[core]->invalidateLine(line*64,cycle);};
        h.prepare_terminal=[&](const spr::NativePrepareRequest& request,spr::PrepareTerminal reason,std::uint64_t cycle) {
            const bool accepted=stores[request.owner.core]->completePrepare(request.token,reason,cycle);
            if(accepted)terminals.push_back({request,reason,cycle});return accepted;
        };
        h.callback=[&](const spr::SequencerService& service,std::uint64_t cycle) {
            auto& seq=*sequencers[service.owner.core];
            const auto result=seq.callback(service,service.kind==spr::SequencerKind::Write?spr::SequencerCallback::Write:spr::SequencerCallback::Read,cycle);
            check(result.callback_accepted,"real sequencer callback accepted");callbacks.push_back({service,cycle,result.wrote});
            if(result.wrote)check(caches[service.owner.core]->write_native_l1(service.line/64,cycle),"write only on real wrote callback");
            return result.wrote;
        };
        h.memory=[&](std::uint64_t token,PendingOwner,std::uint64_t line,std::uint64_t ready){memory.emplace(std::make_pair(ready+memory_delay(line),token),token);};
        h.sent=[&](const spr::NativeProtocolMessage& message){messages.push_back(message);};
        h.audit=[&](const char* event,std::uint64_t cycle,PendingOwner owner,std::uint64_t line,std::uint64_t a,std::uint64_t b) {
            audit.push_back({event,cycle,owner.core,owner.sequence,owner.fragment,line,a,b,owner.generation});
        };
        protocol=std::make_unique<spr::NativeCoherence>(config,std::move(h),pointers,std::vector<spr::ProtocolCredits*>{&bank});
    }
    spr::NativePrepareRequest hint(unsigned core,std::uint64_t address,std::uint64_t send) {
        spr::StoreFacts facts;facts.source_id=++sequences[core];facts.bytes=8;facts.kind=spr::StoreKind::Ordinary;
        auto& buffer=*stores[core];const auto store=buffer.allocate(facts,send);check(bool(store),"real optional source store allocation");
        buffer.markAddressReady(*store,address,send);spr::NativePrepareRequest request;
        const auto candidate=buffer.peekPrepare(send);check(bool(candidate),"real optional CPU port available");
        const auto result=buffer.trySendPrepare(*candidate,send,[&](const spr::PrepareCandidate& p,spr::PrepareToken token) {
            request={{core,token.id,0,6},token,p.line};protocol->prepare(request,send);return true;
        });
        check(result.outcome==spr::PrepareOutcome::Accepted,"real optional credit accepted");return request;
    }
    spr::SequencerService demand(unsigned core,std::uint64_t address,bool write,std::uint64_t ready,bool ordered=true) {
        spr::SequencerRequest request;request.owner={core,++sequences[core],0,2};request.address=address;
        request.kind=write?spr::SequencerKind::Write:spr::SequencerKind::Read;request.ordered_store=write && ordered;
        auto& seq=*sequencers[core];const auto result=seq.grant(seq.peek(request),true,now);
        check(result.status==spr::SequencerStatus::NewService,"fixture ordinary owner has independent service");
        const auto service=result.actions.front().service;protocol->request(service,ready);return service;
    }
    void step() {
        auto next=protocol->next_cycle();
        if(!memory.empty() && (!next || memory.begin()->first.first<*next))next=memory.begin()->first.first;
        check(bool(next),"boundary fixture has next real work");now=*next;
        while(!memory.empty() && memory.begin()->first.first==now) {
            protocol->memory_response(memory.begin()->second,now);memory.erase(memory.begin());
        }
        protocol->advance(now);
    }
    void drain(bool reissue=true) {
        for(unsigned iterations=0;iterations<10000;++iterations) {
            if(reissue)for(auto& seq:sequencers)while(const auto service=seq->peek_service()) {
                check(seq->grant_service(*service,true,now),"real ordered reissue granted");protocol->request(*service,now+1);
            }
            if(protocol->idle() && memory.empty()) {
                protocol->validate();for(auto& seq:sequencers)seq->validate();return;
            }
            step();
        }
        throw std::runtime_error("bounded fixture did not drain");
    }
    std::uint64_t response_cycle(unsigned core=0) {
        for(unsigned n=0;n<1000;++n) {
            for(const auto& m:messages)if(m.event==E::Exclusive && m.to==core && m.ready>now)return m.ready;
            step();
        }
        throw std::runtime_error("missing queued exclusive response");
    }
};
SimulatorConfig boundary_config() {auto c=early_cfg("boundary");c.cha_count=1;return c;}
void boundary_arbitration_and_credits() {
    using E=spr::NativeProtocolEvent;using K=spr::NativeController;using S=spr::NativeLineState;
    {
        ProtocolHarness h(boundary_config());const auto original=h.hint(0,4096,0);
        check(h.protocol->next_cycle()==1,"prepare ingress owns exactly CPU-send+1");
        const auto at=h.response_cycle();h.hint(0,4096,at-1);h.demand(0,4096,true,at);h.drain();
        std::vector<unsigned> ports;
        for(const auto& e:h.audit)if(e.kind=="coherence_service" && e.cycle==at && (e.issue>>48)==0 && e.core==0)ports.push_back(e.completion);
        check(ports==std::vector<unsigned>{0,1,2},"equal cycle optional/incoming/mandatory declaration order");
        check(h.terminals.size()==2 && h.terminals[0].kind==spr::PrepareTerminal::Ignored && h.terminals[1].kind==spr::PrepareTerminal::Fill,"optional ignored before physical fill terminal");
        for(const auto& m:h.messages)if(m.token==1) {
            const auto* p=std::get_if<spr::NativePrepareRequest>(&m.origin);
            check(p && p->owner==original.owner && p->token.id==original.token.id,"every physical hop retains immutable prepare origin");
        }
    }
    {
        auto c=boundary_config();c.spr_profile.limits.l1_tbes=1;ProtocolHarness h(c);
        h.hint(0,4096,0);h.step();h.hint(0,8192,1);h.hint(0,4096,1);h.step();
        check(h.terminals.empty() && h.stores[0]->outstandingPrepares()==3,"resource-stalled optional head prevents younger duplicate bypass");
        h.drain();check(h.terminals.size()==3,"all stalled/duplicate optional credits conserved");
        std::uint64_t b_start=0,duplicate=0;
        for(const auto& e:h.audit)if(e.kind=="prepare_physical_started" && e.line==128)b_start=e.cycle;
        for(const auto& t:h.terminals)if(t.kind==spr::PrepareTerminal::Ignored)duplicate=t.cycle;
        check(b_start && b_start==duplicate,"incoming completion restarts optional first and admits held head before younger duplicate");
    }
    {
        auto c=boundary_config();c.spr_profile.limits.l1_data_fills=1;ProtocolHarness h(c);
        h.hint(0,4096,0);h.step();h.hint(0,8192,1);h.hint(0,4096,1);h.step();
        check(h.now==2 && h.terminals.empty(),"no-fill recycle falls through without revisiting younger optional head");
        h.step();check(h.now==3 && h.terminals.size()==1 && h.terminals[0].kind==spr::PrepareTerminal::Ignored,"younger optional waits until next source cycle absent lower success");
        h.drain();
    }
    {
        auto c=boundary_config();c.spr_profile.limits.l1_data_fills=1;ProtocolHarness h(c);
        h.hint(0,4096,0);h.step();h.hint(0,8192,1);h.hint(0,4096,1);h.demand(0,4096,false,2);h.step();
        check(h.now==2 && h.terminals.size()==1 && h.terminals[0].kind==spr::PrepareTerminal::Ignored,"accepted lower-port park restarts first port after recycle");
        h.drain();
    }
    {
        ProtocolHarness h(boundary_config());h.hint(0,4096,0);h.step();h.demand(0,4096,true,2);h.step();
        check(h.terminals.size()==1 && h.terminals[0].kind==spr::PrepareTerminal::TakenOver,"store takeover has exactly one optional terminal");
        check(h.stores[0]->outstandingPrepares()==0 && h.credits[0]->tbes(spr::ProtocolLevel::L1)==1 && h.credits[0]->fills(spr::ProtocolLevel::L1)==1 && h.sequencers[0]->outstanding()==1,"takeover returns only optional credit, retaining physical and ordinary leases");
        h.drain();check(h.terminals.size()==1 && h.callbacks.size()==1 && h.callbacks[0].wrote,"physical completion releases leases and real ordinary callback once");
    }
}

void boundary_shared_races_and_dirty_fifo() {
    using K=spr::NativeController;using E=spr::NativeProtocolEvent;using S=spr::NativeLineState;
    for(bool takeover:{false,true}) {
        ProtocolHarness h(boundary_config());h.demand(0,4096,false,0);h.demand(1,4096,false,0);h.drain();
        const auto start=h.now+1;h.hint(0,4096,start);h.hint(1,4096,start);
        if(takeover){h.demand(0,4096,true,start+2);h.demand(1,4096,true,start+2);}
        h.drain();
        bool special=false,parent_race=false,child_im_inv=false;
        for(const auto& e:h.audit) {
            special|=e.kind=="prepare_invalidation_ack";
            parent_race|=e.kind=="coherence_transition" && (e.issue>>32)==1 && (e.issue&0xffffffff)==static_cast<unsigned>(S::SM) && e.completion==static_cast<unsigned>(S::SM_IL0);
            child_im_inv|=e.kind=="coherence_child_invalidate" && e.cycle>start;
        }
        check(parent_race,"real competing upgrades preserve SM->SM_IL0 parent race");
        check(takeover?(!special && child_im_inv):special,"SP_IE invalidation ACK-only before takeover; ordinary invalidation after takeover");
        check(h.terminals.size()==2,"racing upgrade gives each accepted hint one terminal");
        check(h.evictions==(takeover?2u:1u),"untaken SP_IE ACK emits no CPU eviction; IM uses ordinary eviction");
        for(const auto& terminal:h.terminals)check(terminal.kind==(takeover?spr::PrepareTerminal::TakenOver:spr::PrepareTerminal::Fill),"remote invalidation never fabricates optional cancellation");
        const auto writes=std::count_if(h.audit.begin(),h.audit.end(),[](const auto& e){return e.kind=="coherence_write";});
        check(writes==(takeover?2:0),"untaken remote prepare never invents write");
    }
    {
        auto c=boundary_config();c.spr_profile.limits.l1_data_fills=1;ProtocolHarness h(c);
        h.demand(0,4096,false,0);h.demand(1,4096,false,0);h.drain();
        h.memory_delay=[](std::uint64_t){return 200;};const auto start=h.now+1;
        h.hint(0,8192,start);h.hint(0,4096,start);h.demand(0,4096,true,start+2);
        while(h.terminals.empty())h.step();
        check(h.terminals.front().kind==spr::PrepareTerminal::TakenOver && h.terminals.front().request.line_address==4096,"S prepare takes over despite I prepare occupying last fill credit");
        check(h.credits[0]->fills(spr::ProtocolLevel::L1)==1 && h.credits[0]->tbes(spr::ProtocolLevel::L1)==2,"S upgrade holds TBE only, I holds TBE+fill");
        check(h.counters[0].l1d.accesses==2 && h.counters[0].l1d.misses==2,"S-origin takeover profiles demand miss despite real tag hit");
        h.drain();
    }
    {
        ProtocolHarness h(boundary_config());h.demand(0,4096,true,0);h.drain();
        const auto start=h.now+1;h.hint(1,4096,start);h.drain();
        check(h.caches[1]->native_data_is_dirty(false,64) && !h.caches[1]->native_data_is_dirty(true,64),"remote dirty debt stays in parent, untaken child fill remains clean");
        bool received=false;
        for(const auto& m:h.messages)if(m.event==E::Exclusive && m.to==1 && m.dirty && m.data)received=true;
        check(received && count(h.audit,"coherence_write")==1,"actual received dirty data retains writer lineage without a new write");
    }
    {
        ProtocolHarness h(boundary_config());h.demand(0,4096,true,0);h.drain();
        h.memory_delay=[](std::uint64_t){return 200;};const auto start=h.now+1;
        h.hint(1,4096,start);h.demand(1,8192,true,start+1);const auto taken=h.demand(1,4096,true,start+2);
        bool parked=false;
        for(unsigned n=0;n<1000 && !parked;++n) {
            h.step();for(const auto& callback:h.callbacks)if(callback.service.owner==taken.owner) {
                check(!callback.wrote,"later ordered store callback really returns wrote=false");parked=true;
            }
        }
        check(parked && h.caches[1]->native_data_is_dirty(false,64) && !h.caches[1]->native_data_is_dirty(true,64),"takeover callback parked behind FIFO keeps parent debt but creates no local dirty");
        check(h.terminals.size()==1 && h.terminals[0].kind==spr::PrepareTerminal::TakenOver && count(h.audit,"coherence_write")==1,"parked callback does not create optional terminal or new version");
        check(h.sequencers[1]->outstanding()==2,"parked callback retains both real ordinary credits");
        const auto remote=h.demand(0,4096,false,h.now+1);bool remote_done=false;
        for(unsigned n=0;n<1000 && !remote_done;++n) {
            h.step();for(const auto& callback:h.callbacks)remote_done|=callback.service.owner==remote.owner;
        }
        check(remote_done && h.caches[1]->native_data_is_dirty(false,64),"clean child invalidation after wrote=false must retain parent dirty debt");
        bool dirty_forward=false;
        for(const auto& message:h.messages)if(message.event==E::Data && message.from==3 && message.to==2 && message.dirty && message.data)dirty_forward=true;
        check(dirty_forward,"later remote transfer preserves authoritative parent dirty data");
        h.drain();
        check(h.sequencers[1]->outstanding()==0 && h.caches[1]->native_data_is_dirty(true,64),"ordered FIFO wake reissues and eventually performs actual write");
        std::vector<std::uint64_t> written;
        for(const auto& cb:h.callbacks)if(cb.service.owner.core==1 && cb.wrote)written.push_back(cb.service.line);
        check(written==std::vector<std::uint64_t>{8192,4096},"actual writes follow original ordered FIFO");
        check(count(h.audit,"coherence_write")==3,"only three real writes advance data lineage");
    }
}
void boundary_stale_and_unsupported() {
    using E=spr::NativeProtocolEvent;
    for(unsigned fault=0;fault<5;++fault) {
        ProtocolHarness h(boundary_config());const auto request=h.hint(0,4096,0);h.demand(0,4096,true,2);h.drain();
        const auto before=std::make_tuple(h.mutations,h.callbacks.size(),h.terminals.size(),h.messages.size(),h.audit.size(),h.credits[0]->live_leases(),h.bank.live_leases());
        bool rejected=false;
        try {
            if(fault==0)h.protocol->prepare(request,h.now);
            else if(fault==1){auto invalid=request;invalid.line_address+=1;invalid.token.id+=1;h.protocol->prepare(invalid,h.now);}
            else {
                auto it=std::find_if(h.messages.begin(),h.messages.end(),[](const auto& m){return m.event==E::Exclusive && m.to==0;});
                check(it!=h.messages.end(),"real queued prepare response available");auto m=*it;m.ready=h.now;m.order+=100000;
                if(fault==3)++std::get<spr::NativePrepareRequest>(m.origin).token.generation;
                if(fault==4)m.origin=spr::SequencerService{request.owner,request.line_address,request.token.generation,spr::SequencerKind::Write,0,0};
                h.protocol->receive(m);h.protocol->advance(h.now);
            }
        }catch(const std::logic_error&){rejected=true;}
        check(rejected,"duplicate/stale prepare response or malformed request rejects");
        check(before==std::make_tuple(h.mutations,h.callbacks.size(),h.terminals.size(),h.messages.size(),h.audit.size(),h.credits[0]->live_leases(),h.bank.live_leases()),"rejected prepare changes no physical tag/action/terminal/lease");
    }
    {
        auto c=boundary_config();c.l1d.size_bytes=64;c.l1d.associativity=1;ProtocolHarness h(c);
        h.hint(0,4096,0);h.drain();const auto before=std::make_tuple(h.mutations,h.messages.size(),h.audit.size());
        h.hint(0,8192,h.now+1);bool rejected=false;
        try{h.drain();}catch(const std::invalid_argument& e){rejected=std::string(e.what()).find("replacement unsupported before")!=std::string::npos;}
        check(rejected && before==std::make_tuple(h.mutations,h.messages.size(),h.audit.size()),"unsupported replacement rejects before protocol/tag transition");
    }
}

void active_prepare_generation_rejection() {
    using E=spr::NativeProtocolEvent;
    {
        ProtocolHarness h(boundary_config());auto request=h.hint(0,4096,0);++request.token.generation;
        bool rejected=false;try{h.protocol->prepare(request,0);}catch(const std::logic_error&){rejected=true;}
        check(rejected,"live token cannot be reincarnated before enqueue");
        h.drain();
        check(h.terminals.size()==1 && h.stores[0]->outstandingPrepares()==0,"rejected reincarnation leaves no queued duplicate or lost credit");
    }
    for(bool child_ack:{false,true}) {
        ProtocolHarness h(boundary_config());
        if(child_ack) {
            h.demand(0,4096,false,0);h.demand(1,4096,false,0);h.drain();
            h.hint(0,4096,h.now+1);h.hint(1,4096,h.now+1);
        } else h.hint(0,4096,0);
        spr::NativeProtocolMessage response;bool found=false;
        for(unsigned n=0;n<1000 && !found;++n) {
            h.step();
            for(const auto& m:h.messages)if(m.event==(child_ack?E::ChildAck:E::Exclusive) &&
               (child_ack || m.to==0) && m.ready>h.now && std::holds_alternative<spr::NativePrepareRequest>(m.origin)) {response=m;found=true;break;}
        }
        check(found,"inflight response with real prepare origin");
        response.ready=h.now;response.order+=1000000;++std::get<spr::NativePrepareRequest>(response.origin).token.generation;
        const auto before=std::make_tuple(h.mutations,h.audit.size(),h.messages.size(),h.terminals.size(),h.credits[0]->live_leases(),h.credits[1]->live_leases());
        bool rejected=false;try{h.protocol->receive(response);h.protocol->advance(h.now);}catch(const std::logic_error&){rejected=true;}
        check(rejected,"active prepare/child response generation rejects before transition");
        check(before==std::make_tuple(h.mutations,h.audit.size(),h.messages.size(),h.terminals.size(),h.credits[0]->live_leases(),h.credits[1]->live_leases()),"active stale response does not mutate data/permissions/credits");
    }
}

}
int main(){try {coherent_takeover();integration_caps_fill_and_upgrade();boundary_arbitration_and_credits();boundary_shared_races_and_dirty_fifo();boundary_stale_and_unsupported();active_prepare_generation_rejection();std::cout<<"coherent prepare runtime tests passed\n";}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
