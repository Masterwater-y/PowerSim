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
SimulatorConfig early_cfg(const std::string& label,unsigned chunk=1) {
    auto c=static_cfg(chunk);c.spr_profile.early_store_prepare=true;c.causal_early_store_prepare=true;
    c.causal_l0_transitions_per_cycle=32; // selected native L0 source default
    c.causal_runtime_audit_path="tmp/p2-early-"+label+"-"+std::to_string(chunk)+".csv";return c;
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
void early_before_data() {
    auto c=early_cfg("basic");
    // Long RAW producer delays SD independently while SA executes.
    auto producer=integer(0,0,1,7);
    auto sa=split_member(true),sd=split_member(false,2);
    auto result=run_static(c,{producer,sa,sd});
    std::uint64_t accepted=UINT64_MAX,data=0,commit=0;
    for(auto e:events(c.causal_runtime_audit_path)) {
        if(e.kind=="prepare_accepted")accepted=e.cycle;
        if(e.kind=="store_data_ready")data=e.cycle;
        if(e.kind=="store_commit")commit=e.cycle;
    }
    check(accepted<data && accepted<commit,"actual Simulator prepare must precede SD and commit");
    check(result.cores[0].retired_uops==3,"all source members retire");
}
void takeover_and_chunks() {
    std::vector<std::string> reference;
    for(unsigned chunk:{1u,2u,7u,128u}) {
        auto c=early_cfg("takeover",chunk);
        const auto stats=run_static(c,{integer(0,0,1,7),split_member(true),split_member(false,2)});
        const auto es=events(c.causal_runtime_audit_path);
        check(count(es,"prepare_taken_over")==1 && count(es,"prepare_fill")==0,"takeover returns exactly one hint credit");
        check(count(es,"llc_miss")==1 && count(es,"dram_selected")==1 && count(es,"l1_tag_reserved")==1 && count(es,"l2_tag_reserved")==1,"takeover retains exactly one physical hierarchy service");
        check(count(es,"sequencer_request")==1 && count(es,"sequencer_response")==1 && count(es,"store_write_complete")==1,"demand owns its independent ordinary identity");
        check(first(es,"prepare_taken_over").cycle<first(es,"l1_credit_release").cycle,"hint credit return does not free physical fill/TBE");
        check(first(es,"store_write_complete").cycle==first(es,"l1_fill").cycle,"architectural write waits for fill");
        check(stats.cores[0].cycles<first(es,"store_write_complete").cycle,"EOF retirement freezes before asynchronous physical callbacks");
        check(stats.cores[0].l1d.accesses==1 && stats.cores[0].l1d.misses==1,"hint lookup is not a second demand classification");
        const auto rows=lifecycle(c.causal_runtime_audit_path);
        if(!reference.empty())check(reference==rows,"complete runtime lifecycle invariant across chunk1/2/7/128");reference=rows;
    }
}
void caps_and_duplicates() {
    auto c=early_cfg("cap16");
    auto stats=run_static(c,delayed_stores(17,false,16));const auto es=events(c.causal_runtime_audit_path);
    check(count(es,"prepare_accepted")==16 && count(es,"prepare_dropped")==1,"independent prepare16 cap drops exactly seventeenth hint");
    check(count(es,"sequencer_follower")==15,"sixteen ordinary readers include fifteen independent-credit followers");
    check(count(es,"prepare_fill")+count(es,"prepare_taken_over")+count(es,"prepare_ignored")==16,"all accepted hint credits return once");
    check(stats.cores[0].retired_uops==50,"all records survive optional drops");
    std::map<std::uint64_t,unsigned> sends;unsigned hints=0,ordinary=0,max_hints=0,max_ordinary=0;
    for(auto e:es) {
        if(e.kind=="prepare_accepted"){++hints;max_hints=std::max(max_hints,hints);}
        if(e.kind=="prepare_fill" || e.kind=="prepare_taken_over" || e.kind=="prepare_ignored"){check(hints>0,"hint-credit conservation");--hints;}
        if(e.kind=="sequencer_request" || e.kind=="sequencer_follower"){++ordinary;max_ordinary=std::max(max_ordinary,ordinary);}
        if(e.kind=="sequencer_response"){check(ordinary>0,"ordinary-credit conservation");--ordinary;}
        if(e.kind=="prepare_accepted" || e.kind=="prepare_dropped" || e.kind=="store_write_accepted")check(++sends[e.cycle]<=2,"cap drops and hints consume shared CPU store ports");
    }
    check(hints==0 && ordinary==0 && max_hints==16 && max_ordinary==16,"independent prepare16 and ordinary16 populations reach separate caps including followers");
    c=early_cfg("duplicates");stats=run_static(c,delayed_stores(6,true));const auto duplicate=events(c.causal_runtime_audit_path);
    check(count(duplicate,"prepare_accepted")==6 && count(duplicate,"prepare_ignored")==5,"same-line duplicate hints each have independently returned credit");
    unsigned physical=0,dram=0;
    for(auto e:duplicate){physical+=e.kind=="prepare_physical_started" && e.line==64;dram+=e.kind=="dram_selected" && e.line==64;}
    check(physical==1 && dram==1,"same-line hints share one physical request and hierarchy traversal");
    // Small capacities are structural variations of distinct hint, ordinary,
    // fill and TBE ownership. Fill-full alone recycles once per model cycle.
    c=early_cfg("small");c.spr_profile.limits.prepare_requests=2;c.spr_profile.limits.ordinary_requests=1;
    c.spr_profile.limits.l1_data_fills=1;c.spr_profile.limits.l1_tbes=2;
    stats=run_static(c,delayed_stores(3));const auto small=events(c.causal_runtime_audit_path);
    check(count(small,"prepare_accepted")==2 && count(small,"prepare_dropped")==1 && count(small,"prepare_fill_recycle")>0,"small independent capacities exercise optional cap and fill recycle");
    check(stats.cores[0].retired_uops==7,"small declared limits drain all source records");
    c=early_cfg("tbe-wait");c.spr_profile.limits.l1_tbes=1;
    run_static(c,delayed_stores(3));const auto tbe=events(c.causal_runtime_audit_path);
    check(count(tbe,"prepare_resource_wait")>0 && count(tbe,"prepare_fill_recycle")==0,"TBE stalls wait for real credit return rather than blindly polling every cycle");
}
void load_stall_and_fill() {
    auto c=early_cfg("load-stall");std::vector<StaticFact> stream;
    // A chain delays SD beyond the prepare fill without importing timing.
    for(unsigned i=0;i<40;++i)stream.push_back(integer(0,0,1,7,i?1:0));
    auto sa=split_member(true),sd=split_member(false,2);
    stream.push_back(sa);stream.push_back(sd);stream.push_back(memory_load(4112));
    const auto result=run_static(c,stream);const auto es=events(c.causal_runtime_audit_path);
    check(count(es,"prepare_fill")==1 && count(es,"prepare_taken_over")==0,"untaken prepare completes exclusive clean fill");
    check(count(es,"mandatory_protocol_stall")==1 && count(es,"mandatory_protocol_wake")==1,
          "load stall_and_wait consumes one transition per parked owner and wakes once on physical line fill");
    Event probe{};bool found=false;
    for(auto e:es)if(e.kind=="private_probe" && e.sequence==42){probe=e;found=true;}
    check(found && probe.cycle>=first(es,"prepare_fill").cycle,"load on prepare transient stalls until actual fill");
    check(result.cores[0].l1d.misses==0,"filled prepare line serves demand as hits without synthetic architectural miss");
}
void optional_before_response() {
    auto c=early_cfg("same-cycle");std::vector<StaticFact> stream{split_member(true),split_member(false)};
    // 28 source FP-add dependencies and one multiply expose a later SA at
    // the first physical fill cycle. These are functional model latencies.
    for(unsigned i=0;i<28;++i)stream.push_back(integer(0,0,1,7,i?1:0));
    stream.push_back(integer(3,2,3,1,1));
    auto sa=split_member(true,1),sd=split_member(false);sa.row.store_group=sd.row.store_group=2;
    stream.push_back(sa);stream.push_back(sd);
    run_static(c,stream);const auto es=events(c.causal_runtime_audit_path);
    const auto ignored=first(es,"prepare_ignored"),fill=first(es,"l1_fill");
    check(ignored.cycle==fill.cycle,"functional fixture reaches optional/response equal-cycle arbitration");
    auto ignore_it=std::find_if(es.begin(),es.end(),[](auto e){return e.kind=="prepare_ignored";});
    auto fill_it=std::find_if(es.begin(),es.end(),[](auto e){return e.kind=="l1_fill";});
    check(ignore_it<fill_it,"optional declaration order arbitrates before same-cycle response");
}

void committed_sq_and_demand_before_optional() {
    auto c=early_cfg("committed-sq");run_static(c,{split_member(true),split_member(false)});
    auto es=events(c.causal_runtime_audit_path);
    check(first(es,"prepare_accepted").cycle==first(es,"store_commit").cycle &&
          first(es,"prepare_accepted").cycle<first(es,"store_combined").cycle,
          "architecturally committed but not offloaded SQ store remains prepare eligible");
    c=early_cfg("demand-first");c.spr_profile.limits.prepare_requests=1;
    auto a=split_member(true),ad=split_member(false),b=a,bd=ad;
    b.record.address=8192;b.row.store_group=bd.row.store_group=2;
    std::vector<StaticFact> stream{a,ad,b,bd};
    for(unsigned i=0;i<3;++i)stream.push_back(integer(0,0,1,7,i?1:0));
    auto late=split_member(true,1),late_data=split_member(false);late.record.address=8192;
    late.row.store_group=late_data.row.store_group=3;stream.push_back(late);stream.push_back(late_data);
    run_static(c,stream);es=events(c.causal_runtime_audit_path);
    check(count(es,"prepare_dropped")==1 && count(es,"prepare_ignored")==1,"occupied optional cap leaves earlier ordinary demand then independent late hint");
    Event probe{},optional{};unsigned physical=0;
    for(auto e:es)if(e.line==128) {
        if(e.kind=="private_probe")probe=e;
        if(e.kind=="optional_enqueued")optional=e;
        physical+=e.kind=="dram_selected";
    }
    check(probe.cycle<optional.completion && physical==1,"ordinary Store before optional entry is observed as transient and never duplicated");
}

void resource_stall_preserves_optional_head() {
    auto c=early_cfg("r1-head");c.spr_profile.limits.l1_tbes=1;c.spr_profile.limits.prepare_requests=3;
    std::vector<StaticFact> stream{integer(0,0,1,7)};
    for(unsigned i=0;i<4;++i) {
        auto sa=split_member(true,i==3?static_cast<unsigned>(stream.size()):0);
        auto sd=split_member(false,static_cast<unsigned>(stream.size()+1));
        sa.row.store_group=sd.row.store_group=i+1;
        sa.record.address=i==0 || i==2?4096:i==1?8192:12288;
        stream.push_back(sa);stream.push_back(sd);
    }
    run_static(c,stream);const auto es=events(c.causal_runtime_audit_path);
    check(count(es,"prepare_accepted")==3 && count(es,"prepare_dropped")==1,
          "resource-stalled optional B retains head so younger duplicate A cannot return cap credit before fourth hint");
    const auto ignored=first(es,"prepare_ignored");
    check(first(es,"prepare_dropped").cycle<ignored.cycle,"fourth hint sees held optional credit before younger A terminal callback");
    Event admitted{};bool found=false;
    for(auto e:es)if(e.kind=="prepare_physical_started" && e.line==128){admitted=e;found=true;}
    check(found && ignored.cycle>=admitted.cycle,"younger ignored A waits behind cold optional B physical admission");
}
void response_restarts_optional_before_mandatory() {
    auto c=early_cfg("r1-restart");c.spr_profile.limits.l1_tbes=1;
    std::vector<StaticFact> stream{split_member(true),split_member(false)};
    for(unsigned i=0;i<40;++i)stream.push_back(integer(0,0,1,7,i?1:0));
    auto b=split_member(true),bd=split_member(false,2);b.record.address=8192;
    b.row.store_group=bd.row.store_group=2;stream.push_back(b);stream.push_back(bd);
    stream.push_back(memory_load(12288));
    run_static(c,stream);const auto es=events(c.causal_runtime_audit_path);
    Event released{},optional{},mandatory{};bool have_release=false,have_optional=false,have_mandatory=false;
    for(auto e:es) {
        if(!have_release && e.kind=="l1_credit_release"){released=e;have_release=true;}
        if(e.kind=="prepare_physical_started" && e.line==128){optional=e;have_optional=true;}
        if(e.kind=="private_probe" && e.line==192){mandatory=e;have_mandatory=true;}
    }
    check(have_release && have_optional && have_mandatory && optional.cycle==released.cycle && optional.cycle<mandatory.cycle,
          "successful A response restarts optional B in same cycle before ready mandatory C can acquire freed TBE");
    auto response=std::find_if(es.begin(),es.end(),[](auto e){return e.kind=="response" && e.line==64;});
    auto restart=std::find_if(es.begin(),es.end(),[](auto e){return e.kind=="prepare_physical_started" && e.line==128;});
    check(response!=es.end() && restart!=es.end() && response<restart && response->cycle==restart->cycle,
          "optional successor runs after complete atomic A response and architectural callback, not inside lease release");
}

void fill_recycle_really_requeues_head() {
    auto c=early_cfg("r1-fill-recycle");c.spr_profile.limits.l1_tbes=1;
    c.spr_profile.limits.l1_data_fills=1;c.spr_profile.limits.prepare_requests=3;
    std::vector<StaticFact> stream{integer(0,0,1,7)};
    for(unsigned i=0;i<4;++i) {
        auto sa=split_member(true,i==3?static_cast<unsigned>(stream.size()):0);
        auto sd=split_member(false,static_cast<unsigned>(stream.size()+1));
        sa.row.store_group=sd.row.store_group=i+1;
        sa.record.address=i==0 || i==2?4096:i==1?8192:12288;
        stream.push_back(sa);stream.push_back(sd);
    }
    run_static(c,stream);const auto es=events(c.causal_runtime_audit_path);
    check(count(es,"prepare_fill_recycle")>0 && count(es,"prepare_accepted")==4 && count(es,"prepare_dropped")==0,
          "explicit I/no-fill recycle requeues B so younger A returns cap credit before fourth hint");
    check(first(es,"prepare_ignored").cycle<first(es,"l1_fill").cycle,
          "fill recycle permits younger ready ignored hint before occupied physical fill returns");
}

void protocol_stall_consumes_budget_once() {
    auto c=early_cfg("r2-stall-budget");c.causal_l0_transitions_per_cycle=1;
    std::vector<StaticFact> stream;
    for(unsigned i=0;i<40;++i)stream.push_back(integer(0,0,1,7,i?1:0));
    stream.push_back(split_member(true));stream.push_back(split_member(false,2));
    stream.push_back(memory_load(4112));stream.push_back(memory_load(12288));
    bool rejected=false;
    try{run_static(c,stream);}catch(const std::invalid_argument& e){rejected=std::string(e.what()).find("transition-budget exhaustion")!=std::string::npos;}
    const auto es=events(c.causal_runtime_audit_path);unsigned cold_admitted=0;
    for(auto e:es)cold_admitted+=e.kind=="private_probe" && e.line==192;
    check(rejected && cold_admitted==0,"Load-on-SP_IE Valid stall consumes budget1 before another ready mandatory request can mutate admission");
}
void recycle_then_response_before_younger_hint() {
    auto c=early_cfg("r3-response");c.spr_profile.limits.l1_data_fills=1;
    auto a=split_member(true),ad=split_member(false),b=a,bd=ad;b.record.address=8192;b.row.store_group=bd.row.store_group=2;
    std::vector<StaticFact> stream{a,ad,b,bd};
    for(unsigned i=0;i<28;++i)stream.push_back(integer(0,0,1,7,i?1:0));
    stream.push_back(integer(3,2,3,1,1));
    auto late=split_member(true,1),late_data=split_member(false);late.row.store_group=late_data.row.store_group=3;
    stream.push_back(late);stream.push_back(late_data);run_static(c,stream);const auto es=events(c.causal_runtime_audit_path);
    auto ignored=std::find_if(es.begin(),es.end(),[](auto e){return e.kind=="prepare_ignored" && e.line==64;});
    auto response=std::find_if(es.begin(),es.end(),[](auto e){return e.kind=="response" && e.line==64;});
    check(ignored!=es.end() && response!=es.end() && ignored->cycle==response->cycle && response<ignored,
          "I/no-fill recycle ends optional visit so full same-cycle response precedes younger ignored hint on successful restart");
}
void recycle_no_success_does_not_restart_visit() {
    auto c=early_cfg("r3-idle");c.spr_profile.limits.l1_data_fills=1;c.spr_profile.limits.prepare_requests=3;
    std::vector<StaticFact> stream{integer(2,0x41,2)};
    for(unsigned i=0;i<4;++i) {
        auto sa=split_member(true,i==3?static_cast<unsigned>(stream.size()):0);
        auto sd=split_member(false,static_cast<unsigned>(stream.size()+1));
        sa.row.store_group=sd.row.store_group=i+1;
        sa.record.address=i==0 || i==2?4096:i==1?8192:12288;
        stream.push_back(sa);stream.push_back(sd);
    }
    run_static(c,stream);const auto es=events(c.causal_runtime_audit_path);
    check(count(es,"prepare_accepted")==3 && count(es,"prepare_dropped")==1 &&
          first(es,"prepare_ignored").cycle>first(es,"prepare_dropped").cycle,
          "recycle and duplicate maturity notifications cannot restart optional port without successful lower transition; cap credit remains held this cycle");
}

void transition_budget_qualification() {
    auto c=early_cfg("budget-missing");c.causal_l0_transitions_per_cycle.reset();bool rejected=false;
    try{c.validate();}catch(const std::invalid_argument& e){rejected=std::string(e.what()).find("l0_transitions_per_cycle")!=std::string::npos;}
    check(rejected,"early adapter requires actual positive declared L0 controller budget");
    c=early_cfg("budget-zero");c.causal_l0_transitions_per_cycle=0;rejected=false;
    try{c.validate();}catch(const std::invalid_argument&){rejected=true;}check(rejected,"zero L0 budget rejects");
    c=early_cfg("budget-exhausted");c.causal_l0_transitions_per_cycle=1;
    auto a=split_member(true),ad=split_member(false),b=a,bd=ad;b.record.address=8192;
    b.row.store_group=bd.row.store_group=2;rejected=false;
    try{run_static(c,{a,ad,b,bd});}catch(const std::invalid_argument& e){rejected=std::string(e.what()).find("transition-budget exhaustion")!=std::string::npos;}
    check(rejected,"unsupported native per-cycle budget deferral rejects entire run before excess transition");
    const auto es=events(c.causal_runtime_audit_path);
    check(count(es,"l1_credit_acquire")==1 && count(es,"prepare_physical_started")==1,"budget rejection occurs before second resource mutation");
    const std::string path="tmp/p2-early-budget-parser.cfg";
    {std::ofstream out(path);out<<"spr.memory.l0_transitions_per_cycle=7\n";}
    check(load_simulator_config(path).causal_l0_transitions_per_cycle==7,"typed actual L0 budget parser preserves declared value");
}

void guards_and_off() {
    auto c=early_cfg("guard");c.causal_early_store_prepare=false;bool rejected=false;
    try{c.validate();}catch(const std::invalid_argument&){rejected=true;}check(rejected,"target flag alone cannot activate early adapter");
    c=early_cfg("guard");c.spr_profile.early_store_prepare=false;rejected=false;
    try{c.validate();}catch(const std::invalid_argument&){rejected=true;}check(rejected,"switch requires target enable");
    c=early_cfg("noaccess");auto no=integer(0,0,1,57);no.record=store(0);
    fst::StoreRequestFacts f;f.attributes=fst::kStoreFactsComplete|fst::kStoreNoAccess;f.request_flags=0x80000;f.address=0;f.size=8;f.byte_enable=255;no.memory_facts=f;
    run_static(c,{no});check(count(events(c.causal_runtime_audit_path),"prepare_accepted")==0,"NO_ACCESS never sends hint");
    c=early_cfg("replacement");c.l1d.size_bytes=64;c.l1d.associativity=1;rejected=false;
    try{run_static(c,delayed_stores(2));}catch(const std::invalid_argument& e){rejected=std::string(e.what()).find("early prepare private replacement")!=std::string::npos;}
    check(rejected,"unsupported early private replacement rejects whole run before resource mutation");
    c=static_cfg();c.causal_runtime_audit_path="tmp/p2-early-off.csv";
    auto result=run_static(c,{integer(0,0,1,7),split_member(true),split_member(false,2)});
    check(result.cores[0].retired_uops==3 && count(events(c.causal_runtime_audit_path),"prepare_accepted")==0,"OFF retains old demand-only path");
    c=early_cfg("malformed");rejected=false;
    try{run_static(c,{split_member(true)});}catch(const std::invalid_argument&){rejected=true;}
    check(rejected,"malformed pair rejects entire run rather than prefix score");
    c=early_cfg("footer");rejected=false;
    try{run_static(c,{split_member(true),split_member(false)},true);}catch(const std::exception&){rejected=true;}
    check(rejected,"corrupt companion footer rejects entire run");
}

}
int main(int argc,char** argv){try{
if(argc>1) {
    if(std::string(argv[1])=="r1-head")resource_stall_preserves_optional_head();
    else if(std::string(argv[1])=="r1-restart")response_restarts_optional_before_mandatory();
    else if(std::string(argv[1])=="r2-stall-budget")protocol_stall_consumes_budget_once();
    else if(std::string(argv[1])=="r3-response")recycle_then_response_before_younger_hint();
    else if(std::string(argv[1])=="r3-idle")recycle_no_success_does_not_restart_visit();
    else throw std::runtime_error("unknown targeted fixture");
    std::cout<<"SPR early prepare runtime targeted test passed\n";return 0;
}
protocol_stall_consumes_budget_once();recycle_then_response_before_younger_hint();recycle_no_success_does_not_restart_visit();resource_stall_preserves_optional_head();response_restarts_optional_before_mandatory();fill_recycle_really_requeues_head();early_before_data();takeover_and_chunks();caps_and_duplicates();load_stall_and_fill();optional_before_response();committed_sq_and_demand_before_optional();transition_budget_qualification();guards_and_off();std::cout<<"SPR early prepare runtime tests passed\n";return 0;}
catch(const std::exception& e){std::cerr<<"SPR early prepare runtime test failed: "<<e.what()<<'\n';return 1;}}
