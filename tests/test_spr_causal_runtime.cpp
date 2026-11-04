#include "fastsim/simulator.hpp"
#include "fastsim/cache.hpp"
#include "fastsim/perf_cache_report.hpp"
#include "fastsim/retired_cache_report.hpp"
#include <stdexcept>
#include <vector>
#include <memory>
#include <iostream>
#include <fstream>
#include <sstream>
namespace {
void require(bool value,const char* message) {if(!value) throw std::runtime_error(message);}
class CausalSource final:public fastsim::TraceSource {
    std::vector<fastsim::TraceRecord> records;std::size_t cursor=0;
    std::optional<bool> ordinary;
public:
    explicit CausalSource(std::vector<fastsim::TraceRecord> r, std::optional<bool> eligible=true):records(std::move(r)),ordinary(eligible){}
    bool next(fastsim::TraceRecord& r) override {if(cursor==records.size())return false;r=records[cursor++];return true;}
    std::string description() const override{return "causal-functional-fixture";}
    bool complete_dependencies() const override{return true;}
    std::optional<bool> current_ordinary_load_eligible() const override{return ordinary;}
    bool functional_retirement_complete() const override{return true;}
    bool memory_request_roles_complete() const override{return true;}
    std::optional<fastsim::fst::MemoryRequestRole> current_memory_request_role() const override{return fastsim::fst::MemoryRequestRole::Data;}
};
fastsim::SimulatorConfig configuration(unsigned chunk) {
    fastsim::SimulatorConfig c;c.cores=2;c.core_model="interval_weave";
    c.causal_runtime=true;c.coherence=false;c.chunk_instructions=chunk;
    c.interval_target_uops=1;c.dispatch_to_issue=0;c.issue_to_execute=0;
    c.fetch_to_decode=0;c.decode_to_rename=0;c.rename_to_dispatch=0;
    c.execute_to_commit=0;c.load_response_to_ready=0;c.integer_alu_units=1;
    c.l1d_mshrs=2;c.l2_mshrs=2;c.llc_mshrs=1;c.cha_count=1;
    return c;
}
fastsim::SimulationStats run(unsigned chunk,const std::string& audit={}) {
    using namespace fastsim;
    auto c=configuration(chunk);c.causal_runtime_audit_path=audit;
    TraceRecord load;load.op_class=56;load.flags=kRetires|kLoad|kPhysicalAddress;load.address=4096;load.size=8;
    TraceRecord dependent;dependent.op_class=1;dependent.producer_dists[0]=1;dependent.n_src=1;
    TraceRecord independent;independent.op_class=1;
    auto repeat=load;repeat.producer_dists[0]=2;repeat.n_src=1;
    std::vector<std::unique_ptr<TraceSource>> sources;
    sources.push_back(std::make_unique<CausalSource>(std::vector<TraceRecord>{load,dependent,independent,repeat}));
    sources.push_back(std::make_unique<CausalSource>(std::vector<TraceRecord>{load,dependent,independent,repeat}));
    Simulator simulator(c,std::move(sources));return simulator.run();
}
}
void test_causal_review_findings() {
    using namespace fastsim;
    TraceRecord first;first.op_class=56;first.flags=kRetires|kLoad|kPhysicalAddress;first.address=4096;first.size=8;
    TraceRecord delay;delay.op_class=2;
    auto second=first;second.producer_dists[0]=1;second.n_src=1;
    auto c=configuration(4);c.cores=1;c.integer_multiply_latency=123;
    c.causal_runtime_audit_path="tmp/causal-fixed-l2-arrival.csv";
    std::vector<std::unique_ptr<TraceSource>> input;
    input.push_back(std::make_unique<CausalSource>(std::vector<TraceRecord>{first,delay,second}));
    Simulator sim(c,std::move(input));const auto result=sim.run();
    require(result.cores[0].l2.accesses==2 && result.cores[0].l2.misses==1 && result.cores[0].l2.hits==1,
            "actual L2 arrival must observe intervening fill after L1 miss admission");
    require(result.cha[0].requests==1 && result.cores[0].cycles==139,"L2 hit avoids false LLC request and late retirement");
    auto delayed=c;delayed.l2.fill_response_latency=10;delayed.l1d.fill_response_latency=7;
    delayed.causal_runtime_audit_path="tmp/causal-separated-fill-edges.csv";
    std::vector<std::unique_ptr<TraceSource>> delayed_input;
    delayed_input.push_back(std::make_unique<CausalSource>(std::vector<TraceRecord>{first,delay,second}));
    Simulator delayed_sim(delayed,std::move(delayed_input));const auto delayed_result=delayed_sim.run();
    require(delayed_result.cores[0].l2.misses==1 && delayed_result.cores[0].cycles==146,
            "L2 data is visible before downstream L1 and CPU responses");
    std::ifstream edge_file(delayed.causal_runtime_audit_path);std::string edge_row;
    std::uint64_t l2_fill=0,l1_fill=0,response=0;
    while(std::getline(edge_file,edge_row)) {
        std::istringstream fields(edge_row);std::vector<std::string> v;std::string item;
        while(std::getline(fields,item,','))v.push_back(item);
        if(v.size()!=8 || v[3]!="0")continue;
        if(v[0]=="l2_fill")l2_fill=std::stoull(v[1]);
        if(v[0]=="l1_fill")l1_fill=std::stoull(v[1]);
        if(v[0]=="response")response=std::stoull(v[1]);
    }
    require(l2_fill==124 && l1_fill==134 && response==141,"each private level installs at its own event");
    std::ostringstream retired,perf;
    write_retired_cache_report(retired,result,c);write_perf_cache_report(perf,result,c);
    require(retired.str().find("\"estimate_available\":true")==std::string::npos &&
            retired.str().find("\"count\":0")==std::string::npos &&
            retired.str().find("\"retired_macro_instructions\":null")!=std::string::npos,
            "disconnected retired-cache observer must serialize unavailable null populations");
    require(perf.str().find("\"estimate_status\":\"approximate\"")==std::string::npos &&
            perf.str().find("\"data_reads\":true")==std::string::npos &&
            perf.str().find("\"diagnostic_value\":0")==std::string::npos &&
            perf.str().find("\"retired_macro_instructions\":null")!=std::string::npos,
            "disconnected perf observer cannot claim estimates or observed zeros");
    for(const auto eligible:{std::optional<bool>{},std::optional<bool>{false}}) {
        bool rejected=false;auto cfg=configuration(1);
        try {std::vector<std::unique_ptr<TraceSource>> in;in.push_back(std::make_unique<CausalSource>(std::vector<TraceRecord>{first},eligible));Simulator bad(cfg,std::move(in));bad.run();}
        catch(const std::invalid_argument&){rejected=true;}
        require(rejected,"physical Data role is not an ordinary cacheable load certificate");
    }
    for(unsigned feature=0;feature<6;++feature) {
        auto cfg=configuration(1);
        if(feature==0){cfg.fetch_buffer_bytes=64;cfg.fetch_buffer_refill_latency=77;}
        if(feature==1)cfg.fetch_buffer_refill_latency=77;
        if(feature==2)cfg.committed_static_memory_ordering=true;
        if(feature==3)cfg.dtlb.speculative_path_state=true;
        if(feature==4)cfg.fetch_to_decode=1;
        if(feature==5)cfg.fetch_width=1;
        bool rejected=false;try{cfg.validate();}catch(const std::invalid_argument&){rejected=true;}
        require(rejected,"unsupported independent frontend/ordering mode must reject");
    }
    // A separate level event can observe replacement as well as fill.
    CacheConfig l1{64,1,64,4,ReplacementPolicy::kLru};
    CacheConfig l2{64,1,64,12,ReplacementPolicy::kLru};PrivateHierarchy cache(l1,l2);CoreCounters counts;
    cache.complete_fill_l2(64,counts.l2);
    const auto proposal=cache.prepare_probe_l1(64);
    require(!proposal.hit && cache.commit_probe_l1(proposal,false,counts).has_value(),"L1-only probe does not classify L2");
    require(counts.l2.accesses==0,"L1 admission must not charge early L2 lookup");
    cache.complete_fill_l2(65,counts.l2);
    const auto arrival=cache.commit_probe_l2(cache.prepare_probe_l2(64),counts.l2);
    require(arrival && arrival->level==HitLevel::kLlc && counts.l2.misses==1,"L2 arrival sees intervening replacement");
    require(!cache.complete_fill_l1(64,false,counts),"late L1 install cannot resurrect an evicted L2 parent");
    // Portable functional fixture for exercising the actual CLI emitters.
    // These are constructed facts, not collected native evidence.
    BinaryTraceWriter fixture("tmp/causal-report-fixture.fst",0);
    fixture.enable_complete_dependencies();fixture.enable_memory_request_roles();
    fixture.enable_functional_retirement();fixture.enable_store_facts();
    fst::StoreRequestFacts facts;facts.address=first.address;facts.size=first.size;facts.byte_enable=255;
    facts.attributes=fst::kStoreFactsComplete|fst::kMemoryRead|fst::kOrdinaryLoad;
    fixture.append(first,nullptr,{},fst::MemoryRequestRole::Data,0,false,false,&facts);
    fixture.append(delay);fixture.append(second,nullptr,{},fst::MemoryRequestRole::Data,0,false,false,&facts);
    fixture.close();
}

void test_spr_causal_runtime() {
    test_causal_review_findings();
    const auto a=run(1,"tmp/causal-chunk1.csv"),b=run(2,"tmp/causal-chunk2.csv"),c=run(4,"tmp/causal-chunk4.csv");
    auto contents=[](const char* path){std::ifstream in(path);std::ostringstream out;out<<in.rdbuf();return out.str();};
    require(contents("tmp/causal-chunk1.csv")==contents("tmp/causal-chunk2.csv") &&
            contents("tmp/causal-chunk1.csv")==contents("tmp/causal-chunk4.csv"),"causal whole lifecycle chunk invariant");
    for(unsigned core=0;core<2;++core) {
        require(a.cores[core].retired_uops==4 && a.cores[core].retired_instructions==4,"causal complete functional population");
        require(a.cores[core].cycles>100,"causal retirement must include real cold DRAM response");
        require(a.cores[core].cycles==b.cores[core].cycles && a.cores[core].cycles==c.cores[core].cycles,"causal chunk invariant retire clocks");
        require(a.cores[core].l1d.accesses==2 && a.cores[core].l1d.misses==1,"causal probe and fill exactly once");
    }
    require(a.cha[0].dram_reads==1 && a.llc.misses==2 && a.cha[0].llc_merged_misses==1 &&
            a.cha[0].llc_unique_fills==1,"causal shared physical service coalesces while preserving demand population");
    auto cfg=configuration(1);cfg.spr_shared_fu=true;
    bool rejected=false;
    try {std::vector<std::unique_ptr<fastsim::TraceSource>> s;s.push_back(std::make_unique<CausalSource>(std::vector<fastsim::TraceRecord>{}));fastsim::Simulator sim(cfg,std::move(s));sim.run();}catch(const std::invalid_argument&){rejected=true;}
    require(rejected,"causal runtime refuses unsupported SPR mechanisms");
    using namespace fastsim;
    cfg=configuration(1);cfg.l1d_mshrs=1;cfg.l2_mshrs=1;
    cfg.causal_runtime_audit_path="tmp/causal-split.csv";
    TraceRecord split;split.op_class=56;split.flags=kRetires|kLoad|kPhysicalAddress;
    split.address=4096+60;split.size=8;
    std::vector<std::unique_ptr<TraceSource>> split_sources;
    split_sources.push_back(std::make_unique<CausalSource>(std::vector<TraceRecord>{split}));
    Simulator split_sim(cfg,std::move(split_sources));const auto result=split_sim.run();
    require(result.cores[0].retired_instructions==1 && result.cores[0].memory_accesses==2 &&
            result.cha[0].dram_reads==2,"split load keeps full fragment population with one actual credit");
    std::uint64_t first_response=0,second_probe=0;
    std::istringstream split_audit(contents("tmp/causal-split.csv"));std::string row;
    while(std::getline(split_audit,row)) {
        std::istringstream fields(row);std::vector<std::string> values;std::string value;
        while(std::getline(fields,value,','))values.push_back(value);
        if(values.size()!=8)continue;
        if(values[0]=="response" && values[4]=="0")first_response=std::stoull(values[1]);
        if(values[0]=="private_probe" && values[4]=="1")second_probe=std::stoull(values[1]);
    }
    require(first_response && first_response==second_probe,"same-cycle credit release admits split successor without invented tick");
    // Unsupported records fail the entire run; no scored prefix is returned.
    auto store=split;store.flags=kRetires|kStore|kPhysicalAddress;
    cfg.causal_runtime_audit_path.clear();
    rejected=false;
    try {std::vector<std::unique_ptr<TraceSource>> s;s.push_back(std::make_unique<CausalSource>(std::vector<TraceRecord>{store}));Simulator sim(cfg,std::move(s));sim.run();}catch(const std::invalid_argument&){rejected=true;}
    require(rejected,"causal runtime rejects stores instead of trimming population");
}
#ifdef FASTSIM_CAUSAL_RUNTIME_STANDALONE
int main(){try{test_spr_causal_runtime();std::cout<<"causal runtime tests passed\n";}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
#endif
