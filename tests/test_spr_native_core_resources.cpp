#include "fastsim/simulator.hpp"
#include "fastsim/cache.hpp"
#include "fastsim/core_continuation.hpp"
#include "fastsim/fst_store_requests.hpp"
#include <iostream>
#include <algorithm>
#include <fstream>
#include <sstream>
#include <stdexcept>
using namespace fastsim;
namespace {
void check(bool v,const char* s){if(!v)throw std::runtime_error(s);}
class Source final:public TraceSource {
    std::vector<TraceRecord> records_;std::size_t cursor_=0;fst::StoreRequestFacts facts_;
public:
    explicit Source(std::vector<TraceRecord> r):records_(std::move(r)){}
    bool next(TraceRecord& r) override {
        if(cursor_==records_.size())return false;
        r=records_[cursor_++];
        facts_={};facts_.address=r.address;facts_.size=r.size;
        facts_.byte_enable=r.size==64?~0ull:((1ull<<r.size)-1);
        facts_.attributes=fst::kStoreFactsComplete|(r.is_write()?fst::kStoreOrdinary:(fst::kMemoryRead|fst::kOrdinaryLoad));
        if(r.is_write() && r.address==0) {facts_.attributes=fst::kStoreFactsComplete|fst::kStoreNoAccess;facts_.request_flags=0x80000;}
        return true;
    }
    std::string description()const override{return "constructed-native-memory-functional-fixture";}
    bool complete_dependencies()const override{return true;}
    bool functional_retirement_complete()const override{return true;}
    bool memory_request_roles_complete()const override{return true;}
    std::optional<fst::MemoryRequestRole> current_memory_request_role()const override{return fst::MemoryRequestRole::Data;}
    std::optional<bool> current_ordinary_load_eligible()const override{return true;}
    const fst::StoreRequestFacts* current_store_facts()const override{return &facts_;}
    const fst::StoreRequestFacts* current_memory_facts()const override{return &facts_;}
};
SimulatorConfig cfg(unsigned chunk=1) {
    SimulatorConfig c;c.core_model="interval_weave";c.cores=1;c.coherence=false;c.causal_runtime=true;
    c.causal_native_memory=true;
    c.causal_mandatory_queue_latency=1;c.causal_l1_data_access_latency=3;c.causal_mandatory_queue_ordered=false;c.causal_l1_request_latency=2;
    c.l1d.replacement=c.l2.replacement=c.llc.replacement=ReplacementPolicy::kLru;
    c.causal_l1_response_latency=2;c.causal_l2_request_latency=2;c.causal_l2_response_latency=2;c.causal_llc_to_l2_latency=1;
    c.causal_spr_resources=true;
    c.core_model="interval_weave";c.cores=1;c.coherence=false;c.causal_runtime=true;c.causal_native_memory=true;
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
SimulationStats run(SimulatorConfig c,std::vector<TraceRecord> r){std::vector<std::unique_ptr<TraceSource>> s;s.push_back(std::make_unique<Source>(std::move(r)));Simulator sim(c,std::move(s));return sim.run();}
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
void standalone() {
    // The model-produced allocation identity shares one slot; members retain
    // distinct issue/completion lifetimes, and full native fusion is not input.
    auto c=cfg();c.spr_profile.iq_entries={1,1,1};c.dispatch_to_issue=2;
    spr::FunctionalCoreUop a;a.sequence=0;a.canonical_op=7;a.iq_op_class="Execution";
    a.candidate_ports=3;a.static_resources_complete=true;a.model_allocation_group=44;
    auto b=a;b.sequence=1;
    spr::CoreContinuation grouped(c,0);grouped.append({a,b},0,true);
    auto p=grouped.advance_until_blocked(0);
    check(grouped.counters().dispatched==2 && grouped.counters().max_iq_used[0]==1,"model allocation group charges one execution slot");
    p=grouped.advance_until_blocked(4);check(p.finished,"all grouped members release and retire");
    // Store-data domain can be exercised as a standalone resource member;
    // actual split-store source pairing remains rejected by Simulator.
    a.model_allocation_group.reset();a.canonical_op=kSprStoreData;a.iq_op_class="X86StoreData";a.candidate_ports=(1<<4)|(1<<9);
    spr::CoreContinuation sd(c,0);sd.append({a},0,true);sd.advance_until_blocked(0);
    check(sd.counters().max_iq_used[2]==1 && sd.counters().max_iq_used[0]==0,"store-data has independent IQ domain");
    check(sd.advance_until_blocked(4).finished,"store-data issue releases its reservation");
    // Two physical ports held for a long span. Compare event jumps with the
    // identical machine called once each cycle; host reentry adds no attempts.
    auto run_core=[&](bool slow,bool reinvoke) {
        auto config=cfg();config.dispatch_width=6;config.issue_width=12;
        spr::CoreContinuation core(config,0);
        std::vector<spr::FunctionalCoreUop> facts;
        for(unsigned i=0;i<5;++i){auto u=a;u.sequence=i;u.canonical_op=7;u.iq_op_class="Execution";u.candidate_ports=3;u.fu_occupancy_cycles=101;u.execution_latency=101;facts.push_back(u);}
        core.append(std::move(facts),0,true);
        std::vector<std::tuple<std::uint64_t,std::uint64_t,std::uint64_t>> grants;
        auto step=[&](unsigned cycle){auto pub=core.advance_until_blocked(cycle);for(auto e:pub.resources)if(std::string(e.kind)=="fu_grant")grants.emplace_back(e.sequence,e.cycle,e.value);return pub;};
        auto pub=step(0);if(reinvoke)for(unsigned n=0;n<10;++n)step(0);
        if(slow){for(unsigned cycle=1;cycle<400 && !pub.finished;++cycle)pub=step(cycle);}
        else {while(!pub.finished){check(pub.next_internal_cycle.has_value(),"sparse core publishes next event");pub=step(*pub.next_internal_cycle);}}
        check(grants.size()==5,"generic ring retains all operations");
        return grants;
    };
    check(run_core(false,false)==run_core(true,false),"sparse skipped-failure ring cursor equals per-cycle reference");
    check(run_core(false,false)==run_core(false,true),"host reinvocation adds no hardware ring attempts");
    // Managed SHFT and ALU contend for the same physical p0. Failed SHFT
    // does not rotate its family/mask cursor; successful ALU RR rotates.
    c=cfg();spr::CoreContinuation rr(c,0);std::vector<spr::FunctionalCoreUop> facts;
    for(unsigned i=0;i<4;++i){auto u=a;u.sequence=i;u.canonical_op=1;u.iq_op_class="Execution";u.integer_family=2;u.candidate_ports=0x41;facts.push_back(u);}
    rr.append(std::move(facts),0,true);p=rr.advance_until_blocked(3);
    std::vector<unsigned> ports;for(auto e:p.resources)if(std::string(e.kind)=="fu_grant")ports.push_back(e.value);
    check(ports==std::vector<unsigned>({0,6,0,6}),"family/mask RR success ordering survives failures");
}
void tests() {
    auto metadata=cfg();metadata.spr_shared_fu=false;
    auto traits=target_op_traits(metadata);
    check(traits[9].latency==14 && traits[9].pipelined && traits[29].latency==11,
          "causal SPR pure metadata selects source pipelined dividers without legacy scheduler");
    metadata.causal_spr_resources=false;traits=target_op_traits(metadata);
    check(traits[9].latency==metadata.float_divide_latency && traits[9].pipelined==metadata.float_divide_pipelined,
          "OFF mode preserves configured legacy divider traits");
    auto c=cfg(16);c.causal_runtime_audit_path="tmp/p3-bank.csv";
    run(c,{load(4096),load(4160),load(4224)});
    std::vector<std::uint64_t> cycles;
    for(const auto& e:events(c.causal_runtime_audit_path))if(e.kind=="sequencer_request")cycles.push_back(e.cycle);
    check(cycles.size()==3,"three load sends retained");
    check(cycles[0]==cycles[1] && cycles[2]==cycles[0]+1,"P3 third same-bank send must wait one cycle");
    // A bank-local refusal must not block an independent younger packet
    // whose AGU, bank and cache send port are available in this same cycle.
    std::vector<std::string> bank_hol_reference;
    for(unsigned chunk:{1u,2u,7u,128u}) {
        c=cfg(chunk);c.causal_runtime_audit_path="tmp/p3-bank-hol"+std::to_string(chunk)+".csv";
        run(c,{load(4096),load(4160),load(4224),load(4288),load(4352),load(8196)});
        std::vector<std::uint64_t> sends(6),grants(6);
        for(auto e:events(c.causal_runtime_audit_path)) {
            if(e.kind=="load_send")sends[e.sequence]=e.cycle;
            if(e.kind=="fu_grant")grants[e.sequence]=e.cycle;
        }
        check(grants[5]==1 && sends[4]==2 && sends[5]==1,
              "bank-local refusal must allow eligible younger different-bank load in cycle1");
        auto rows=lifecycle(c.causal_runtime_audit_path);
        if(!bank_hol_reference.empty())check(rows==bank_hol_reference,"bank-local bypass lifecycle is source-batch invariant");
        bank_hol_reference=rows;
    }
    // Fragments of one instruction retain their native sequential send loop:
    // blocked first fragment cannot be bypassed by that instruction's second.
    std::vector<std::string> fragment_reference;
    for(unsigned chunk:{1u,2u,7u,128u}) {
        c=cfg(chunk);c.causal_runtime_audit_path="tmp/p3-bank-fragment-order"+std::to_string(chunk)+".csv";
        auto blocker0=load(4096+60);blocker0.size=4;
        auto blocker1=load(8192+60);blocker1.size=4;
        auto split_bank15=load(12288+60);split_bank15.size=8;
        run(c,{blocker0,blocker1,split_bank15,load(16388)});
        std::vector<std::uint64_t> fragments;std::uint64_t younger=UINT64_MAX;
        for(auto e:events(c.causal_runtime_audit_path))if(e.kind=="load_send") {
            if(e.sequence==2)fragments.push_back(e.cycle);
            if(e.sequence==3)younger=e.cycle;
        }
        check(fragments==std::vector<std::uint64_t>({1,1}) && younger==1,
              "bank bypass must preserve per-instruction fragment order");
        auto rows=lifecycle(c.causal_runtime_audit_path);
        if(!fragment_reference.empty())check(rows==fragment_reference,"fragment retry lifecycle is source-batch invariant");
        fragment_reference=rows;
    }
    // Full ordinary Sequencer credits remain a global stop even if a younger
    // packet would be an otherwise eligible different-bank alias.
    c=cfg(16);c.spr_profile.limits.ordinary_requests=2;c.causal_runtime_audit_path="tmp/p3-bank-global-stop.csv";
    run(c,{load(4096),load(4100),load(8192),load(4104)});
    std::uint64_t global_blocked=0,global_younger=0;
    for(auto e:events(c.causal_runtime_audit_path))if(e.kind=="load_send") {
        if(e.sequence==2)global_blocked=e.cycle;if(e.sequence==3)global_younger=e.cycle;
    }
    check(global_blocked>0 && global_younger==global_blocked,"global receiver refusal must retain FIFO stop");
    // A bank-conflicting request and a full lower fill resource cannot
    // globally stop a younger alias with independent bank/Sequencer credits.
    c=cfg(128);c.spr_profile.limits.l1_data_fills=1;c.causal_runtime_audit_path="tmp/p3-bank-before-receiver.csv";
    run(c,{load(4096),load(4096),load(4096),load(4096),load(8192),load(4100)});
    std::uint64_t receiver_bypass=UINT64_MAX;
    for(auto e:events(c.causal_runtime_audit_path))if(e.kind=="load_send" && e.sequence==5)receiver_bypass=e.cycle;
    check(receiver_bypass==1,"bank and lower-controller stalls must preserve eligible Sequencer alias admission");
    // Followers are real accepted LSQ packets and pay bank/cache-port credits.
    c=cfg(16);c.causal_runtime_audit_path="tmp/p3-follower-bank.csv";
    auto result=run(c,{load(4096),load(4096),load(4096)});
    cycles.clear();for(auto e:events(c.causal_runtime_audit_path))if(e.kind=="load_send")cycles.push_back(e.cycle);
    check(cycles==std::vector<std::uint64_t>({0,0,1}),"coalesced follower sends consume actual bank slots");
    check(result.cores[0].l1d.misses==1,"bank-admitted followers do not reclassify cache");
    // Byte addresses select banks, not aligned cache-line addresses. One AGU
    // can generate two line fragments, making cache ports distinct from AGUs.
    c=cfg(16);c.causal_runtime_audit_path="tmp/p3-fragments.csv";
    auto split=load(4096+60);split.size=8;
    run(c,{split,load(8196),load(12296)});cycles.clear();
    for(auto e:events(c.causal_runtime_audit_path))if(e.kind=="load_send")cycles.push_back(e.cycle);
    check(cycles==std::vector<std::uint64_t>({0,0,0,1}),"three send ports independent of three AGU grants and four packets");
    // A rejected ordinary admission must not charge a bank; after credits
    // return, both waiting same-bank packets can consume the two real slots.
    c=cfg(16);c.spr_profile.limits.ordinary_requests=2;c.causal_runtime_audit_path="tmp/p3-failed-send.csv";
    run(c,{load(4096),load(4100),load(8192),load(8192)});cycles.clear();
    for(auto e:events(c.causal_runtime_audit_path))if(e.kind=="load_send")cycles.push_back(e.cycle);
    check(cycles.size()==4 && cycles[2]==cycles[3] && cycles[2]>cycles[0],"failed cache send must not charge bank");
    // Native IQ ignores the legacy aggregate IQ limit. The memory partition
    // holds accepted and not-yet-accepted loads through completion.
    c=cfg(256);c.iq_entries=1;c.causal_runtime_audit_path="tmp/p3-iq72.csv";
    run(c,std::vector<TraceRecord>(80,load(4096)));
    std::uint64_t maximum=0,release=UINT64_MAX,acquire72=0;
    for(auto e:events(c.causal_runtime_audit_path)) {
        if(e.kind=="iq_acquire" && e.line==1){maximum=std::max(maximum,e.issue);if(e.sequence==72)acquire72=e.cycle;}
        if(e.kind=="iq_release" && e.line==1)release=std::min(release,e.cycle);
    }
    check(maximum==72 && acquire72>=release,"active memory IQ72 capacity and completion release");
    c=cfg(256);c.causal_runtime_audit_path="tmp/p3-iq128.csv";
    std::vector<TraceRecord> dependent{load(4096)};
    for(unsigned i=1;i<=130;++i){auto u=compute(7);u.producer_dists[0]=i;dependent.push_back(u);}
    run(c,dependent);maximum=0;
    for(auto e:events(c.causal_runtime_audit_path))if(e.kind=="iq_acquire" && e.line==0)maximum=std::max(maximum,e.issue);
    check(maximum==128,"active execution IQ128 independent from outstanding memory IQ");
    // Physical p0 contention and separate OpClass intervals.
    c=cfg(16);c.causal_runtime_audit_path="tmp/p3-intervals.csv";
    run(c,{compute(9),compute(29),compute(9),compute(29)});
    std::vector<std::uint64_t> issue(4);for(auto e:events(c.causal_runtime_audit_path))if(e.kind=="fu_grant")issue[e.sequence]=e.cycle;
    check(issue==std::vector<std::uint64_t>({0,1,4,5}),"shared physical p0 and per-OpClass issue intervals");
    // Unified stores reserve address and data ports together; the memory IQ
    // remains owned until execute completion, before detached cache response.
    c=cfg(16);c.causal_runtime_audit_path="tmp/p3-store-pair.csv";run(c,{store(4096),store(4160),store(4224)});
    unsigned paired=0;std::uint64_t released=UINT64_MAX;
    for(auto e:events(c.causal_runtime_audit_path)){if(e.kind=="fu_store_data")++paired;if(e.kind=="iq_release")released=std::min(released,e.cycle);}
    check(paired==3 && released>0,"unified store paired ports and completion IQ release");
    // Complete lifecycle equality across host source chunk sizes, including
    // exhausted FU rings, cache sends, aliases and response wakeups.
    std::vector<TraceRecord> work;
    for(unsigned i=0;i<40;++i){work.push_back(load(4096+(i%8)*4));work.push_back(compute(i%2?7:9));}
    std::vector<std::string> reference;
    for(unsigned chunk:{1u,2u,7u,128u}){
        c=cfg(chunk);c.causal_runtime_audit_path="tmp/p3-chunk"+std::to_string(chunk)+".csv";
        auto out=run(c,work);check(out.cores[0].retired_instructions==work.size(),"chunk invariant full macro population");
        auto rows=lifecycle(c.causal_runtime_audit_path);if(!reference.empty())check(rows==reference,"source batching preserves complete resource/memory lifecycle");reference=rows;
    }
    // Physical bank and port budgets belong to each core, while the P2
    // shared-memory service domain still contends through the real backend.
    std::vector<std::string> multicore_reference;
    for(unsigned chunk:{1u,2u,4u}) {
        c=cfg(chunk);c.cores=2;c.causal_runtime_audit_path="tmp/p3-multicore"+std::to_string(chunk)+".csv";
        std::vector<std::unique_ptr<TraceSource>> sources;
        sources.push_back(std::make_unique<Source>(std::vector<TraceRecord>{load(4096),load(4160),load(4224)}));
        sources.push_back(std::make_unique<Source>(std::vector<TraceRecord>{load(4096),load(4160),load(4224)}));
        Simulator simulator(c,std::move(sources));const auto out=simulator.run();
        check(out.cores[0].retired_instructions==3 && out.cores[1].retired_instructions==3,"multicore retained populations");
        unsigned sends[2]={};for(auto e:events(c.causal_runtime_audit_path))if(e.kind=="load_send" && e.cycle==0)++sends[e.core];
        check(sends[0]==2 && sends[1]==2,"load bank budgets are per core");
        auto rows=lifecycle(c.causal_runtime_audit_path);if(!multicore_reference.empty())check(rows==multicore_reference,"multicore resource lifecycle independent of source batches");multicore_reference=rows;
    }
    for(int op:{1,kSprStoreAddress,kSprStoreData}){
        bool rejected=false;try{run(cfg(),{compute(op)});}catch(const std::invalid_argument&){rejected=true;}
        check(rejected,"missing integer facts or split-store pairing must fail closed");
    }
    standalone();

}
}
int main(){try{tests();std::cout<<"native core resource tests passed\n";}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
