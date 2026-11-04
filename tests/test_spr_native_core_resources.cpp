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
struct StaticFact {
    TraceRecord record;
    fst::StaticUopFactsRow row;
    std::vector<fst::StaticUopOperand> operands;
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
         memory.attributes=fst::kStoreFactsComplete|fst::kMemoryRead|fst::kOrdinaryLoad;
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
