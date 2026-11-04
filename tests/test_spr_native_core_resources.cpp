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
    c.causal_native_memory=true;c.store_commit_to_iew_delay=0;
    c.causal_mandatory_queue_latency=1;c.causal_l1_data_access_latency=3;c.causal_mandatory_queue_ordered=false;c.causal_l1_request_latency=2;
    c.l1d.replacement=c.l2.replacement=c.llc.replacement=ReplacementPolicy::kLru;
    c.causal_l1_response_latency=2;c.causal_l2_request_latency=2;c.causal_l2_response_latency=2;c.causal_llc_to_l2_latency=1;
    c.causal_spr_resources=true;
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
    // A detached SD member has no architectural SQ owner and now fails at
    // the public continuation boundary; real pairs cover StoreData IQ below.
    a.model_allocation_group.reset();a.canonical_op=kSprStoreData;a.iq_op_class="X86StoreData";a.candidate_ports=(1<<4)|(1<<9);
    spr::CoreContinuation sd(c,0);bool missing_pair=false;
    try{sd.append({a},0,true);}catch(const std::invalid_argument&){missing_pair=true;}
    check(missing_pair,"detached store-data resource member must reject missing logical pair");
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
SimulationStats run_static(SimulatorConfig c,const std::vector<StaticFact>& facts,bool corrupt_footer=false) {
    const auto directory=std::filesystem::path("tmp")/("p3-static-"+std::to_string(getpid()));
    std::filesystem::create_directories(directory);const auto path=(directory/"trace.fst").string();
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
    if(corrupt_footer) {
        const auto companion=path+".uops.gz";std::vector<unsigned char> bytes;
        auto input=gzopen(companion.c_str(),"rb");check(input!=nullptr,"fixture companion read");
        unsigned char block[512];int count;
        while((count=gzread(input,block,sizeof(block)))>0)bytes.insert(bytes.end(),block,block+count);
        check(count==0 && gzclose(input)==Z_OK && bytes.size()>24,"fixture complete companion");
        bytes[bytes.size()-16]^=1;auto output=gzopen(companion.c_str(),"wb1");check(output!=nullptr,"fixture companion write");
        check(gzwrite(output,bytes.data(),bytes.size())==static_cast<int>(bytes.size()),"fixture corrupt footer write");
        check(gzclose(output)==Z_OK,"fixture corrupt footer close");
    }
    std::vector<std::unique_ptr<TraceSource>> sources;sources.push_back(std::make_unique<BinaryTraceSource>(path));
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
void paired_store_native_traits() {
    // OFF keeps the historical coarse copies; only the explicit native SPR
    // selector may use the selected SA/SD OpDesc defaults.
    auto legacy=static_cfg();legacy.causal_spr_resources=false;
    legacy.integer_alu_latency=7;legacy.integer_alu_pipelined=false;
    const auto off=target_op_traits(legacy);
    for(int op:{57,1,kSprStoreAddress,kSprStoreData})
        check(off[op].latency==7 && !off[op].pipelined,"OFF store traits preserve configured coarse ALU timing");
    std::vector<StaticFact> stream;
    for(unsigned i=0;i<4;++i) {
        auto sa=split_member(true),sd=split_member(false);
        sa.row.store_group=sd.row.store_group=i+1;sa.record.address+=64*i;
        stream.push_back(sa);stream.push_back(sd);
    }
    std::vector<std::tuple<std::string,std::uint64_t,std::uint64_t,std::uint64_t,std::uint64_t>> reference;
    for(bool perturb:{false,true}) {
        auto c=static_cfg(7);c.causal_runtime_audit_path=perturb?"tmp/p2-store-traits-perturbed.csv":"tmp/p2-store-traits-baseline.csv";
        if(perturb){c.integer_alu_latency=7;c.integer_alu_pipelined=false;}
        const auto result=run_static(c,stream);
        std::vector<std::tuple<std::string,std::uint64_t,std::uint64_t,std::uint64_t,std::uint64_t>> timing;
        for(auto e:events(c.causal_runtime_audit_path)) {
            if(e.kind=="fu_grant")timing.emplace_back(e.kind,e.sequence,e.cycle,e.issue,0);
            if(e.kind=="retire") {
                check(e.completion==e.issue+1,"actual BinaryTraceSource SA/SD completion uses source OpDesc latency1 despite legacy ALU perturbation");
                timing.emplace_back(e.kind,e.sequence,e.cycle,e.issue,e.completion);
            }
        }
        check(timing.size()==16 && result.cores[0].retired_uops==8,"all four native pairs retain grants and retirement");
        if(!perturb) {
            unsigned second_cycle=0;for(auto e:events(c.causal_runtime_audit_path))if(e.kind=="fu_grant" && e.cycle==1)++second_cycle;
            check(second_cycle==4,"SA/SD physical ports each accept a second pair after one cycle");reference=timing;
        } else check(timing==reference,"native SA/SD completion and physical port occupation ignore unrelated legacy ALU configuration");
    }
    auto native=legacy;native.causal_spr_resources=true;
    const auto selected=target_op_traits(native);
    for(int op:{kSprStoreAddress,kSprStoreData})
        check(selected[op].latency==1 && selected[op].pipelined,"causal SPR store metadata selects sourced latency1 and pipelining independently of legacy knobs");

}
void paired_store_runtime() {
    {
        auto c=cfg();spr::BufferProfile profile;profile.early_prepare=false;spr::StoreBuffer stores(profile);
        spr::CoreContinuation core(c,0,&stores);spr::FunctionalCoreUop sa;
        sa.sequence=0;sa.canonical_op=kSprStoreAddress;sa.iq_op_class="X86StoreAddress";
        sa.candidate_ports=(1<<7)|(1<<8);sa.static_resources_complete=true;sa.memory_write=true;sa.store_group=1;
        sa.store_facts={0,0,0,0,8,spr::StoreKind::Ordinary};sa.store_address=4096;
        bool rejected=false;try{core.append({sa},0,false);}catch(const std::invalid_argument&){rejected=true;}
        check(rejected,"continuation must not consume pair IDs without selected source policy");
    }
    // Actual BinaryTraceSource: one logical store keeps two independent IQ/FU
    // members. SD can execute before a RAW-blocked SA without publishing a write.
    for(unsigned delay:{0u,1u,3u})for(bool data_first:{false,true}) {
        std::vector<std::string> reference;
        for(unsigned chunk:{1u,2u,7u,128u}) {
            auto c=static_cfg(chunk);c.store_commit_to_iew_delay=delay;
            c.causal_runtime_audit_path="tmp/p2-pair-"+std::to_string(delay)+"-"+std::to_string(data_first)+"-"+std::to_string(chunk)+".csv";
            auto sa=split_member(true,data_first?1:0),sd=split_member(false,data_first?0:2);
            const auto stats=run_static(c,{integer(3,2,3),sa,sd});
            std::uint64_t retire=0,commit=UINT64_MAX,canwb=UINT64_MAX,combined=UINT64_MAX,sa_issue=0,sd_issue=0;
            unsigned allocated=0,accepted=0,grants=0,paired=0;bool memory_iq=false,data_iq=false;
            for(auto e:events(c.causal_runtime_audit_path)) {
                if(e.kind=="retire" && e.sequence==2)retire=e.cycle;
                if(e.kind=="store_allocate")++allocated;
                if(e.kind=="store_commit")commit=e.cycle;
                if(e.kind=="store_can_writeback")canwb=e.cycle;
                if(e.kind=="store_combined")combined=e.cycle;
                if(e.kind=="store_write_accepted")++accepted;
                if(e.kind=="fu_grant" && e.sequence>0){++grants;if(e.sequence==1){sa_issue=e.cycle;check(e.issue==7 || e.issue==8,"SA AGU only");}else {sd_issue=e.cycle;check(e.issue==4 || e.issue==9,"SD data FU only");}}
                paired+=e.kind=="fu_store_data";
                if(e.kind=="iq_acquire" && e.sequence==1)memory_iq=e.line==1;
                if(e.kind=="iq_acquire" && e.sequence==2)data_iq=e.line==2;
            }
            check(allocated==1 && grants==2 && accepted==1 && paired==0,"pair owns one SQ/two FU/one memory write");
            check(memory_iq && data_iq,"pair charges memory and data IQ independently");
            check(data_first?sd_issue<sa_issue:sa_issue<sd_issue,"SA/SD RAW readiness is independent");
            check(commit==retire+delay && canwb==commit+1 && combined==canwb,"source commit publication and next writeback scan");
            check(stats.cores[0].cycles==retire,"detached publication/drain freezes retirement endpoint");
            const auto rows=lifecycle(c.causal_runtime_audit_path);
            if(!reference.empty())check(rows==reference,"paired lifecycle invariant across source chunk sizes");reference=rows;
        }
    }
    // SD RAW stalls only overlapping younger loads once SA is known. Both
    // functional memory requests remain independent from the detached store.
    {
        auto c=static_cfg(1);c.causal_runtime_audit_path="tmp/p2-pair-loads.csv";
        auto producer=integer(0,0,1,56);producer.record=load(8192);
        auto independent=producer;independent.record=load(12288);
        auto overlap=producer;overlap.record=load(4096);
        run_static(c,{producer,split_member(true),split_member(false,2),independent,overlap});
        std::uint64_t sd_issue=UINT64_MAX,sd_ready=UINT64_MAX,sd_release=UINT64_MAX,sa_ready=UINT64_MAX,sa_release=UINT64_MAX,disjoint_issue=UINT64_MAX,overlap_issue=UINT64_MAX;
        for(auto e:events(c.causal_runtime_audit_path)) {
            if(e.kind=="fu_grant" && e.sequence==2)sd_issue=e.cycle;
            if(e.kind=="fu_grant" && e.sequence==3)disjoint_issue=e.cycle;
            if(e.kind=="fu_grant" && e.sequence==4)overlap_issue=e.cycle;
            if(e.kind=="store_data_ready")sd_ready=e.cycle;
            if(e.kind=="store_address_ready")sa_ready=e.cycle;
            if(e.kind=="iq_release" && e.sequence==1)sa_release=e.cycle;
            if(e.kind=="iq_release" && e.sequence==2)sd_release=e.cycle;
        }
        check(disjoint_issue<sd_issue && overlap_issue>=sd_ready,"ready SA disambiguates independent load while SD RAW waits");
        check(sd_release==sd_issue && sa_release==sa_ready,"SD IQ releases at issue, SA IQ at completion");
    }
    {
        auto c=static_cfg(1);c.spr_profile.limits.shared_stores=1;c.causal_runtime_audit_path="tmp/p2-pair-credit.csv";
        auto sa2=split_member(true),sd2=split_member(false);sa2.row.store_group=sd2.row.store_group=2;sa2.record.address=4160;
        run_static(c,{split_member(true),split_member(false),sa2,sd2});
        unsigned allocations=0;std::uint64_t first_release=UINT64_MAX,second_allocate=0;
        for(auto e:events(c.causal_runtime_audit_path)) {
            if(e.kind=="store_allocate"){++allocations;if(e.sequence==2)second_allocate=e.cycle;}
            if(e.kind=="store_credit_released")first_release=std::min(first_release,e.cycle);
        }
        check(allocations==2 && second_allocate>=first_release,"one shared SQ credit serves two complete pairs without double charge");
    }
    for(unsigned delay:{0u,1u,3u}) {
        auto c=static_cfg();c.store_commit_to_iew_delay=delay;c.causal_runtime_audit_path="tmp/p2-unified-delay.csv";
        auto unified=integer(0,0,1,57);unified.record=store(4096);unified.row.store_role=fst::StaticStoreRole::Unified;
        const auto result=run_static(c,{unified});std::uint64_t retire=0,commit=0,allowed=0;
        for(auto e:events(c.causal_runtime_audit_path)){if(e.kind=="retire")retire=e.cycle;if(e.kind=="store_commit")commit=e.cycle;if(e.kind=="store_can_writeback")allowed=e.cycle;}
        check(commit==retire+delay && allowed==commit+1 && result.cores[0].cycles==retire,"unified store obeys sourced delayed publication and retirement freeze");
    }
    {
        auto c=static_cfg(1);auto sa=split_member(true),sd=split_member(false);
        sa.record.flags|=kMicroOp;sd.record.flags|=kMicroOp|kLastMicroOp;
        const auto result=run_static(c,{sa,sd});
        check(result.cores[0].retired_uops==2 && result.cores[0].retired_instructions==1 && result.cores[0].memory_uops==1 && result.cores[0].memory_accesses==1,"split store retains two UOPs, one macro, one functional memory request");
    }
    {auto c=static_cfg(1);c.causal_static_integer=false;run_static(c,{split_member(true),split_member(false)});}
    auto reject=[&](SimulatorConfig c,std::vector<StaticFact> f){bool failed=false;try{run_static(c,f);}catch(const std::exception&){failed=true;}check(failed,"malformed pair must reject complete run");};
    const auto sa=split_member(true),sd=split_member(false);
    reject(static_cfg(),{sa});reject(static_cfg(),{sd});reject(static_cfg(),{sa,sa,sd});reject(static_cfg(),{sa,sd,sd});
    auto bad=sd;bad.row.store_group=2;reject(static_cfg(),{sa,bad});
    bad=sd;bad.row.store_role=fst::StaticStoreRole::Address;reject(static_cfg(),{sa,bad});
    bad=sd;bad.row.flags&=~fst::kStaticStorePairKnown;reject(static_cfg(),{sa,bad});
    bad=sd;bad.row.flags|=fst::kStaticAbandonStoreGroups;reject(static_cfg(),{sa,bad});
    bad=split_member(false,1);bad.row.address_operands=1;reject(static_cfg(),{sa,bad});
    auto c=static_cfg();c.store_commit_to_iew_delay.reset();reject(c,{sa,sd});
    c=static_cfg();c.spr_profile.static_uop_policy_sha256.clear();reject(c,{sa,sd});
    c=static_cfg();c.store_commit_to_iew_delay.reset();
    auto readonly=integer(0,0,1,56);readonly.record=load(8192);run_static(c,{readonly});
    auto masked=sa;fst::StoreRequestFacts memory;memory.address=4096;memory.size=8;memory.byte_enable=0x7f;memory.attributes=fst::kStoreFactsComplete|fst::kStoreOrdinary;masked.memory_facts=memory;
    reject(static_cfg(),{masked,sd});
    memory.byte_enable=0xff;memory.attributes=fst::kStoreFactsComplete|fst::kStoreSpecial;masked.memory_facts=memory;reject(static_cfg(),{masked,sd});
    auto conflict=split_member(false,1);conflict.operands[0].producer_distance=0;reject(static_cfg(),{sa,conflict});

}
void zero_idiom_runtime() {
    // A full execution IQ blocks ordinary work but cannot consume a slot or
    // FU/WB grant for rename-eliminated constants. The dependent load wakes
    // before the older multiply completes, while ROB retirement stays ordered.
    for(unsigned kind=1;kind<=4;++kind) {
        auto c=static_cfg();c.spr_profile.iq_entries[0]=1;c.commit_width=1;c.execute_to_commit=2;
        c.causal_runtime_audit_path="tmp/p3-zero-kind"+std::to_string(kind)+".csv";
        auto zero=integer(0,0,1,kind<=2?1:kind==3?14:12);zero.row.zero_idiom=kind;
        zero.record.n_dst=zero.row.destination_count=kind<=2?4:2;
        auto consumer=integer(0,0,1,56,1);consumer.record=load(4096);
        consumer.record.n_src=1;consumer.record.n_dst=1;
        consumer.record.producer_dists[0]=1;consumer.record.producer_classes[0]=0;
        auto result=run_static(c,{integer(3,2,3),integer(1,0x863,1,1,1),zero,consumer});
        std::uint64_t admitted=UINT64_MAX,load_issue=UINT64_MAX,retired=0;
        unsigned zero_iq=0,zero_fu=0,zero_count=0,allocated=0,released=0;
        for(auto e:events(c.causal_runtime_audit_path)) {
            if(e.sequence==2) {
                if(e.kind=="zero_idiom"){admitted=e.cycle;++zero_count;}
                if(e.kind=="destination_allocate")check(e.issue==(kind<=2?6u:4u),"zero retains all declared destination allocations");
                zero_iq+=e.kind=="iq_acquire";zero_fu+=e.kind=="fu_grant";
                allocated+=e.kind=="destination_allocate";released+=e.kind=="destination_release";
                if(e.kind=="retire"){retired=e.cycle;check(e.completion==admitted,"zero completes at modeled rename boundary");}
            }
            if(e.sequence==3 && e.kind=="fu_grant")load_issue=e.cycle;
        }
        check(admitted==0 && load_issue==0,"zero bypasses full execution IQ and wakes dependent load immediately");
        check(zero_count==1 && zero_iq==0 && zero_fu==0,"zero consumes no IQ or FU grant");
        check(allocated==1 && released==1 && retired>=4,"zero retains destination accounting and ordered ROB retirement");
        check(result.cores[0].retired_uops==4 && result.cores[0].retired_instructions==4,"zero retained retirement population");
    }
    auto c=static_cfg();c.dispatch_to_issue=3;c.issue_to_execute=7;c.execute_to_commit=5;
    c.causal_runtime_audit_path="tmp/p3-zero-stage.csv";
    auto zero=integer(0,0,1);zero.row.zero_idiom=1;
    auto out=run_static(c,{zero});check(out.cores[0].cycles==5,"rename eliminated zero retains commit delay without issue-stage latency");
    c=static_cfg();c.dispatch_width=1;c.writeback_width=1;c.causal_runtime_audit_path="tmp/p3-zero-writeback.csv";
    run_static(c,{integer(1,0x863,1),zero});
    std::uint64_t zero_completion=UINT64_MAX;
    for(auto e:events(c.causal_runtime_audit_path))if(e.kind=="retire" && e.sequence==1)zero_completion=e.completion;
    check(zero_completion==1,"zero completion consumes no WB slot when ordinary WB is fully occupied");
    // Constant-result elimination still occupies the ROB until retirement.
    c=static_cfg();c.rob_entries=1;c.execute_to_commit=4;c.causal_runtime_audit_path="tmp/p3-zero-rob.csv";
    run_static(c,{zero,zero});std::vector<std::uint64_t> admission;
    for(auto e:events(c.causal_runtime_audit_path))if(e.kind=="zero_idiom")admission.push_back(e.cycle);
    check(admission==std::vector<std::uint64_t>({0,4}),"zero must retain ROB occupancy until commit credit is released");
}
void static_integer_runtime() {
    // Break caught: refusing real static descriptors or replacing nominal
    // family/mask/latency with the coarse IntAlu OpClass would fail this chain.
    auto c=cfg(16);c.causal_static_integer=true;c.spr_profile.integer_nominal_shape_verified=true;
    c.spr_profile.static_uop_policy_sha256="4242424242424242424242424242424242424242424242424242424242424242";
    c.causal_runtime_audit_path="tmp/p3-static-chain.csv";
    auto result=run_static(c,{integer(3,2,3),integer(2,0x41,1,1,1),integer(0,0,1,2,1)});
    std::vector<std::uint64_t> issue(3),completion(3),ports(3);
    for(auto e:events(c.causal_runtime_audit_path)) {
        if(e.kind=="fu_grant"){issue[e.sequence]=e.cycle;ports[e.sequence]=e.issue;}
        if(e.kind=="retire")completion[e.sequence]=e.completion;
    }
    check(issue==std::vector<std::uint64_t>({0,3,4}),"actual static integer dependency nominal latency");
    check(ports==std::vector<std::uint64_t>({1,0,1}),"static family masks and family0 generic port fallback");
    check(completion==std::vector<std::uint64_t>({3,4,7}),"family0 uses generic FU latency rather than default static latency");
    check(result.cores[0].retired_instructions==3,"complete static macro population");
}
template<class F> void must_reject(F f,const char* reason) {
    bool rejected=false;try{f();}catch(const std::exception&){rejected=true;}check(rejected,reason);
}
void static_integer_gates() {
    const auto fact=integer(1,0x863,1);
    for(const auto& hash:std::vector<std::string>{"",std::string(64,'0'),std::string(64,'g'),std::string(63,'4'),std::string(64,'3')}) {
        auto c=static_cfg();c.spr_profile.static_uop_policy_sha256=hash;
        must_reject([&]{run_static(c,{fact});},"missing, malformed, zero or mismatching expected policy must reject entire invocation");
    }
    auto c=static_cfg();c.spr_profile.integer_nominal_shape_verified=false;
    must_reject([&]{run_static(c,{fact});},"missing or nonempty-result-path target shape unsupported");
    c=static_cfg();c.causal_spr_resources=false;
    must_reject([&]{run_static(c,{fact});},"static integer requires partitioned native resource adapter");
    c=static_cfg();must_reject([&]{run(c,{compute(7)});},"static adapter requires actual source policy declaration");
    c=static_cfg();c.rename_free_list=true;
    must_reject([&]{run_static(c,{fact});},"physical free-list backpressure stays unsupported");
    c=static_cfg(1);
    must_reject([&]{run_static(c,{fact,fact},true);},"corrupt full-stream identity must reject invocation even after an executable prefix");
    c=static_cfg();auto incomplete=fact;incomplete.row.flags=fst::kStaticExecutionKnown;
    must_reject([&]{run_static(c,{incomplete});},"incomplete static operand capability must reject");
    auto invalid_port=fact;invalid_port.row.candidate_ports=4;
    must_reject([&]{run_static(c,{invalid_port});},"managed ALU cannot use a port without target IntegerALU");
    // Native decoder clears GPR zero source registers and execution family;
    // media zero also has zero sources. Codec-valid contradictory semantics
    // must be rejected by the actual consumer, without erasing RAW edges.
    auto zero=integer(0,0,1);zero.row.zero_idiom=1;
    auto source_zero=integer(0,0,1,1,1);source_zero.row.zero_idiom=1;
    must_reject([&]{run_static(c,{fact,source_zero});},"zero with complete genuine RAW source must reject instead of deleting dependency");
    auto managed_zero=zero;managed_zero.row.integer_family=1;managed_zero.row.candidate_ports=0x863;
    must_reject([&]{run_static(c,{managed_zero});},"zero must clear managed execution family and ports");
    auto memory_zero=zero;memory_zero.record=load(4096);memory_zero.record.n_dst=1;
    must_reject([&]{run_static(c,{memory_zero});},"memory operation cannot be rename-eliminated zero");
    auto no_destination=zero;no_destination.row.destination_count=0;no_destination.record.n_dst=0;
    must_reject([&]{run_static(c,{no_destination});},"zero requires a destination result population");
}
void static_integer_invariance() {
    // Managed family3 needs three cycles; its consumer must wake at result
    // availability even when backend execute/WB/commit notification is later.
    auto c=static_cfg();c.issue_to_execute=4;c.writeback_width=1;c.causal_runtime_audit_path="tmp/p3-result-ready.csv";
    run_static(c,{integer(3,2,3),integer(2,0x41,1,1,1)});
    std::uint64_t consumer=UINT64_MAX;for(auto e:events(c.causal_runtime_audit_path))if(e.kind=="fu_grant" && e.sequence==1)consumer=e.cycle;
    check(consumer==3,"empty result paths wake managed dependents at nominal value time before backend completion");
    std::vector<StaticFact> work;
    for(unsigned i=0;i<4;++i)work.push_back(integer(2,0x41,1));
    work.push_back(integer(1,0x863,1));work.push_back(integer(4,0x822,1));
    auto zero=integer(0,0,1);zero.row.zero_idiom=2;work.push_back(zero);
    work.push_back(integer(1,0x863,1,1,1));work.push_back(integer(3,2,3));
    std::vector<std::string> reference;
    for(unsigned chunk:{1u,2u,7u,128u}) {
        c=static_cfg(chunk);c.causal_runtime_audit_path="tmp/p3-static-chunk"+std::to_string(chunk)+".csv";
        auto out=run_static(c,work);check(out.cores[0].retired_uops==9,"static runtime preserves every retained UOP");
        auto rows=lifecycle(c.causal_runtime_audit_path);
        if(!reference.empty())check(rows==reference,"static integers and zero lifecycle invariant across source batching");reference=rows;
        std::vector<std::uint64_t> ports;
        for(auto e:events(c.causal_runtime_audit_path))if(e.kind=="fu_grant" && e.sequence<4)ports.push_back(e.issue);
        check(ports==std::vector<std::uint64_t>({0,6,0,6}),"actual family RR rotates only on successful port grants");
    }
}
void causal_micro_macro_counts() {
    auto first=compute(7);first.flags|=kMicroOp;
    auto last=first;last.flags|=kLastMicroOp;
    const auto out=run(cfg(),{first,last,compute(7)});
    check(out.cores[0].retired_uops==3 && out.cores[0].records==3 && out.cores[0].retired_instructions==2,
          "causal micro plus last UOP records contribute one macro denominator");
    auto a=integer(1,0x863,1);a.record.flags|=kMicroOp;
    auto b=a;b.record.flags|=kLastMicroOp;
    const auto binary=run_static(static_cfg(1),{a,b,integer(1,0x863,1)});
    check(binary.cores[0].retired_uops==3 && binary.cores[0].retired_instructions==2,
          "actual static binary runtime separates micro UOP and macro denominators");
}
void tests() {
    paired_store_native_traits();
    paired_store_runtime();
    static_integer_runtime();
    zero_idiom_runtime();
    static_integer_gates();
    static_integer_invariance();
    causal_micro_macro_counts();
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
