#include "fastsim/simulator.hpp"
#include "fastsim/core_continuation.hpp"
#include <algorithm>
#include <array>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <unistd.h>
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

struct Fact {TraceRecord record;fst::StaticUopFactsRow row;std::vector<fst::StaticUopOperand> operands;};
Fact op(int canonical,unsigned distance=0,unsigned reg=1) {
    Fact f;f.record.op_class=static_cast<std::int16_t>(canonical);f.record.n_dst=1;
    std::array<std::uint8_t,4> producers{255,255,255,255},destinations{};
    destinations[reg]=1;
    if(distance){f.record.n_src=1;f.record.producer_dists[0]=distance;producers[0]=reg;f.operands.push_back({distance,0,5,static_cast<std::uint8_t>(reg)});}
    f.record.set_register_class_metadata(producers,destinations);
    f.row.flags=fst::kStaticExecutionKnown|fst::kStaticOperandRolesKnown;
    f.row.source_count=f.operands.size();f.row.destination_count=1;
    return f;
}
struct Event {std::string kind;std::uint64_t cycle,sequence,value,issue,complete;};
struct Run {std::vector<Event> events;std::vector<std::string> tuples;};
class DelayedBinary final:public TraceSource {
    BinaryTraceSource source;
public:
    explicit DelayedBinary(const std::string& path):source(path){}
    bool next(TraceRecord& r) override {std::this_thread::sleep_for(std::chrono::microseconds(100));return source.next(r);}
    std::string description()const override{return source.description();}
    bool complete_dependencies()const override{return source.complete_dependencies();}
    bool functional_retirement_complete()const override{return source.functional_retirement_complete();}
    bool memory_request_roles_complete()const override{return source.memory_request_roles_complete();}
    std::optional<fst::MemoryRequestRole> current_memory_request_role()const override{return source.current_memory_request_role();}
    std::string static_uop_policy_identity()const override{return source.static_uop_policy_identity();}
    const fst::StaticUopFactsRow* current_static_uop_facts()const override{return source.current_static_uop_facts();}
    const std::vector<fst::StaticUopOperand>& current_static_uop_operands()const override{return source.current_static_uop_operands();}
};
Run run(SimulatorConfig c,const std::vector<Fact>& facts,bool raw=false,bool delayed=false) {
    static unsigned serial=0;
    const auto directory=std::filesystem::path("tmp")/("p3-generic-"+std::to_string(getpid())+"-"+std::to_string(serial++));
    std::filesystem::create_directories(directory);const auto path=(directory/"trace.fst").string();
    std::array<std::uint8_t,32> policy;policy.fill(0x42);
    {BinaryTraceWriter writer(path,0);writer.enable_complete_dependencies();writer.enable_memory_request_roles();
     writer.enable_functional_retirement();writer.enable_static_uop_facts(policy);
     for(const auto& f:facts)writer.append(f.record,nullptr,{},fst::MemoryRequestRole::Data,0,false,false,nullptr,&f.row,&f.operands);
     writer.close();}
    if(raw) {std::fstream file(path,std::ios::in|std::ios::out|std::ios::binary);std::uint64_t flags=0;file.seekg(32);file.read(reinterpret_cast<char*>(&flags),8);flags|=1ull<<10;file.seekp(32);file.write(reinterpret_cast<const char*>(&flags),8);}
    c.causal_static_integer=true;c.spr_profile.integer_nominal_shape_verified=true;
    // The fixture writer's 0x42 policy is explicit functional provenance.
    c.spr_profile.static_uop_policy_sha256="4242424242424242424242424242424242424242424242424242424242424242";
    c.causal_runtime_audit_path=(directory/"audit.csv").string();
    std::vector<std::unique_ptr<TraceSource>> sources;
    if(delayed)sources.push_back(std::make_unique<DelayedBinary>(path));else sources.push_back(std::make_unique<BinaryTraceSource>(path));
    Simulator simulator(c,std::move(sources));const auto stats=simulator.run();
    check(stats.cores[0].retired_uops==facts.size(),"every functional operation retired");
    Run result;std::ifstream in(c.causal_runtime_audit_path);std::string row;std::getline(in,row);
    while(std::getline(in,row)) {
        std::istringstream stream(row);std::vector<std::string> f;std::string field;while(std::getline(stream,field,','))f.push_back(field);
        check(f.size()==9,"audit schema");
        result.events.push_back({f[0],std::stoull(f[1]),std::stoull(f[3]),std::stoull(f[6]),std::stoull(f[6]),std::stoull(f[7])});
        if(f[0]!="core_resource_cost")result.tuples.push_back(row);
    }
    std::sort(result.tuples.begin(),result.tuples.end());std::filesystem::remove_all(directory);return result;
}
Event event(const Run& r,const char* kind,unsigned seq) {for(auto e:r.events)if(e.kind==kind && e.sequence==seq)return e;throw std::runtime_error(std::string("missing event ")+kind);}
void invariant(const std::vector<Fact>& f,SimulatorConfig c=cfg(),bool raw=false) {
    std::vector<std::string> ref;
    for(unsigned chunk:{1,2,7,128})for(bool delay:{false,true}) {c.chunk_instructions=chunk;auto r=run(c,f,raw,delay);if(ref.empty())ref=r.tuples;else check(ref==r.tuples,"whole grant/wake/completion/retire tuples independent of chunks and host delay");}
}
void dividers() {
    for(auto values:{std::array<int,3>{9,14,4},std::array<int,3>{29,11,3}}) {
        std::vector<Fact> f{op(values[0]),op(values[0]),op(values[0]),op(values[0]==9?29:9)};
        auto c=cfg(128);auto r=run(c,f);
        for(unsigned i=0;i<3;++i) {auto g=event(r,"fu_grant",i),t=event(r,"retire",i);check(g.value==0,"divider p0");check(g.cycle==i*values[2],"three independent divides use exact II");check(t.complete==g.cycle+values[1],"divider nominal completion latency");}
        check(event(r,"fu_grant",3).value==0 && event(r,"fu_grant",3).cycle==1,"p0 accepts other OpClass between divider acceptance slots");
        c.float_divide_latency=c.float_misc_latency=c.float_simple_latency=c.float_multiply_latency=41;c.simd_latency=41;
        c.float_divide_pipelined=c.float_complex_pipelined=c.float_simple_pipelined=false;
        check(run(c,f).tuples==r.tuples,"legacy FP knobs cannot perturb native generic stream");invariant(f);
    }
    auto delayed=cfg(128);delayed.issue_to_execute=1;
    auto pipeline=run(delayed,{op(9),op(29)});
    check(event(pipeline,"retire",0).complete==15 && event(pipeline,"retire",1).complete==13,"nominal divider finish plus explicit issue-to-execute stage delay");
    auto c=cfg();auto f=std::vector<Fact>{op(0)};auto baseline=run(c,f);c.integer_alu_latency=17;c.integer_alu_pipelined=false;
    check(run(c,f).tuples==baseline.tuples,"NoOp has source no-FU latency1 independent of legacy ALU");
}
void ordinary() {
    for(auto values:{std::array<int,3>{5,2,0},{6,2,0},{8,5,0},{12,1,0},{13,1,0},{14,1,0},{15,1,0},{16,1,0},{17,1,1},{18,3,0},{21,1,0},{25,2,1},{27,1,0},{28,1,0},{31,4,0},{87,2,0}}) {
        std::vector<Fact> f{op(values[0]),op(values[0],1),op(values[0])};auto c=cfg(128);auto r=run(c,f);
        check(event(r,"fu_grant",0).value==static_cast<unsigned>(values[2]),"ordinary source first port");
        for(unsigned i=0;i<3;++i){auto t=event(r,"retire",i);check(t.complete==t.issue+values[1],"ordinary source latency");}
        check(event(r,"fu_grant",1).cycle>=static_cast<unsigned>(values[1]),"dependent latency chain");
        check(event(r,"fu_grant",2).cycle<static_cast<unsigned>(values[1]) || values[1]==1,"independent overlap");
        c.simd_latency=c.float_simple_latency=31;c.float_simple_pipelined=false;
        check(run(c,f).tuples==r.tuples,"ordinary native latency independent of legacy knobs");invariant(f);
    }
    // Native wire IDs18/19/20 normalize to96/97/98 after bound companions read.
    for(auto values:{std::array<int,3>{18,3,5},{19,3,0},{20,1,5}}) {
        std::vector<Fact> f{op(values[0]),op(values[0],1),op(10)};auto r=run(cfg(128),f,true);
        check(event(r,"fu_grant",0).value==static_cast<unsigned>(values[2]),"raw SPR Pack/Extract/Insert source ports");
        auto t=event(r,"retire",0);check(t.complete==t.issue+values[1],"raw SPR Pack/Extract/Insert source latency");invariant(f,cfg(),true);
    }
    auto zero=op(14);zero.row.zero_idiom=1;auto r=run(cfg(),{op(14),zero});
    check(event(r,"zero_idiom",1).cycle==0,"certified zero still bypasses ordinary FU");
    bool denied=false;zero.row.integer_family=1;zero.row.candidate_ports=1;
    try{run(cfg(),{zero});}catch(const std::exception&){denied=true;}check(denied,"malformed zero facts reject");
}
void forwarding() {
    for(unsigned reg:{0,1}) {
        std::vector<Fact> f{op(4,0,reg),op(4,1,reg),op(7,2,reg)};auto c=cfg(128);auto r=run(c,f);
        const auto p=event(r,"retire",0);
        check(p.complete==p.issue+2,"same FloatAdd producer nominal latency");
        check(event(r,"fu_grant",1).cycle==p.complete,"FloatAdd consumer wake+0");
        check(event(r,"fu_grant",2).cycle==p.complete+(reg==1),"only FloatReg non-FloatAdd consumer wake+1");
        invariant(f);
    }
    // ROB1 forces producer retirement and source refill while the +1 edge lives.
    auto c=cfg();c.rob_entries=1;invariant({op(4),op(7,1)},c);
    auto r=run(c,{op(4),op(7,1)});check(event(r,"fu_grant",1).cycle==event(r,"retire",0).complete+1,"retired FloatAdd version retains late-source edge");
    auto zero=op(14);zero.row.zero_idiom=1;
    // At chunk1/ROB1 the zero is queued first, then dispatches at producer
    // retirement. Only then does BinaryTraceSource deliver the actual consumer.
    // Its extra edge must survive producer removal before that source refill.
    std::vector<Fact> late{op(4),zero,op(7,2)};r=run(c,late);
    check(event(r,"zero_idiom",1).cycle==event(r,"retire",0).cycle,"refill bridge dispatches at producer retirement");
    check(event(r,"fu_grant",2).cycle==event(r,"retire",0).complete+1,"newly read consumer finds retired functional-version tombstone");
    invariant(late,c);
    // A second FloatAdd writes an integer result: it cannot overwrite the
    // first producer version's FloatReg edge or acquire a fast-adder edge.
    invariant({op(4),op(4,0,0),op(7,2),op(7,2,0)});
    // Two older ready results consume WB slots before the fast adder.
    c=cfg(128);c.writeback_width=1;
    std::vector<Fact> f{op(25),op(25),op(4),op(7,1)};r=run(c,f);
    check(event(r,"retire",2).complete>event(r,"retire",2).issue+2,"writeback contention delays FloatAdd wake");
    check(event(r,"fu_grant",3).cycle==event(r,"retire",2).complete+1,"forward edge follows actual wake not nominal FU finish");check(event(r,"generic_wake",2).cycle==event(r,"retire",2).complete,"reported generic wake uses writeback completion boundary");invariant(f,c);
}
void admission() {
    auto c=cfg();c.spr_profile.generic_fu_preset.clear();bool denied=false;
    try{run(c,{op(4)});}catch(const std::invalid_argument&){denied=true;}
    check(denied,"selected runtime rejects an unverified generic profile");
    for(int opcode:{19,22,26,30,32,51,60,77,88}) {
        denied=false;try{run(cfg(),{op(opcode)});}catch(const std::invalid_argument&){denied=true;}
        check(denied,"uncertified OpClass remains fail closed");
    }
    for(std::uint16_t flag:{static_cast<std::uint16_t>(kBranch),static_cast<std::uint16_t>(kAtomic),static_cast<std::uint16_t>(kSerialize)}) {
        auto f=op(10);f.record.flags|=flag;denied=false;
        try{run(cfg(),{f});}catch(const std::exception&){denied=true;}
        check(denied,"functional branch atomic serialize gates remain closed");
    }
    auto f=op(4);f.record.reserved=0;f.record.producer_classes={255,255,255,255};denied=false;
    try{run(cfg(),{f});}catch(const std::invalid_argument&){denied=true;}
    check(denied,"FloatAdd with unknown destination register classes rejects");
    f=op(7);f.row.integer_family=1;f.row.candidate_ports=1;denied=false;
    try{run(cfg(),{f});}catch(const std::invalid_argument&){denied=true;}
    check(denied,"ordinary floating operation cannot masquerade as managed integer");
}
void sqrt_occupancy() {
    std::vector<Fact> f{op(11),op(9),op(4),op(17),op(10)};auto r=run(cfg(128),f);
    check(event(r,"fu_grant",0).value==0 && event(r,"retire",0).complete==24,"sqrt p0 latency24");
    check(event(r,"fu_grant",1).cycle==24,"sqrt occupies p0 through exact release at24");
    check(event(r,"fu_grant",2).cycle==0 && event(r,"fu_grant",3).cycle==0,"sqrt permits concurrent p1/p5 work");invariant(f);
    auto c=cfg(128);c.issue_to_execute=1;r=run(c,f);
    check(event(r,"retire",0).complete==25 && event(r,"fu_grant",1).cycle==24,"sqrt physical release is distinct from backend completion with stage delay");
    // Same prepared target facts, per-cycle stepping vs sparse advancement:
    // all-busy generic attempts must rotate once per actual issue cycle.
    auto stepped=[&](bool dense) {
        spr::CoreContinuation core(c,0);std::vector<spr::FunctionalCoreUop> facts;
        for(unsigned i=0;i<f.size();++i) {
            spr::FunctionalCoreUop u;u.sequence=i;u.canonical_op=f[i].record.canonical_op_class();u.iq_op_class="Execution";
            u.candidate_ports=spr_port_mask(f[i].record);u.issue_interval=spr_issue_interval(f[i].record);
            const auto trait=target_op_traits(c)[u.canonical_op];u.execution_latency=trait.latency;u.fu_occupancy_cycles=trait.pipelined?1:trait.latency;
            u.static_resources_complete=true;facts.push_back(u);
        }
        core.append(std::move(facts),0,true);std::vector<std::tuple<std::uint64_t,std::uint64_t,std::uint64_t>> grants;
        std::uint64_t cycle=0;
        for(;;) {
            auto pub=core.advance_until_blocked(cycle);
            for(auto e:pub.resources)if(std::string(e.kind)=="fu_grant")grants.emplace_back(e.sequence,e.cycle,e.value);
            if(pub.finished)break;
            check(pub.next_internal_cycle.has_value(),"sqrt sparse frontier remains live");cycle=dense?cycle+1:*pub.next_internal_cycle;
        }
        return grants;
    };
    check(stepped(false)==stepped(true),"sqrt generic failed-ring outcomes equal dense cycle stepping");
}
}
int main(int argc,char** argv) {
    const std::string which=argc>1?argv[1]:"all";
    try {if(which=="all"||which=="dividers")dividers();if(which=="all"||which=="ordinary")ordinary();if(which=="all"||which=="forwarding")forwarding();if(which=="all"||which=="sqrt")sqrt_occupancy();if(which=="all"||which=="admission")admission();std::cout<<"generic FU production "<<which<<" passed\n";return 0;}
    catch(const std::exception& e){std::cerr<<"generic FU production "<<which<<": "<<e.what()<<'\n';return 1;}
}
