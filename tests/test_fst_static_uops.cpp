#include "fastsim/trace.hpp"
#include "fastsim/fst_static_uops.hpp"
#include <cassert>
#include <filesystem>
#include <iostream>
#include <functional>
#include <unistd.h>
using namespace fastsim;
template<class F> void rejects(F f){bool yes=false;try{f();}catch(const std::exception&){yes=true;}assert(yes);}
int main(){
    const auto dir=std::filesystem::path("tmp")/("static-uop-"+std::to_string(getpid()));std::filesystem::create_directories(dir);
    const auto path=(dir/"trace.fst").string();std::array<std::uint8_t,32> policy{};policy.fill(0x42);
    TraceRecord a{};a.pc=4096;a.op_class=1;a.n_dst=1;
    TraceRecord b=a;b.pc=4100;b.n_src=1;b.n_dst=0;b.producer_dists[0]=1;b.producer_classes[0]=0;
    fst::StaticUopFactsRow fa;fa.flags=fst::kStaticExecutionKnown|fst::kStaticOperandRolesKnown;fa.integer_family=1;fa.candidate_ports=0x863;fa.destination_count=1;
    auto fb=fa;fb.source_count=1;fb.destination_count=0;
    std::vector<fst::StaticUopOperand> none,operand{{1,0,1,0}};
    {BinaryTraceWriter w(path,0);w.enable_complete_dependencies();w.enable_static_uop_facts(policy);
     rejects([&]{w.append(a);});assert(w.record_count()==0);
     w.append(a,nullptr,{},fst::MemoryRequestRole::Data,0,false,false,nullptr,&fa,&none);
     auto bad=operand;bad[0].producer_distance=0;
     rejects([&]{w.append(b,nullptr,{},fst::MemoryRequestRole::Data,0,false,false,nullptr,&fb,&bad);});
     assert(w.record_count()==1);
     w.append(b,nullptr,{},fst::MemoryRequestRole::Data,0,false,false,nullptr,&fb,&operand);w.close();}
    {BinaryTraceSource s(path);TraceRecord r;assert(s.next(r));auto f=s.current_static_uop_facts();assert(f&&f->candidate_ports==0x863&&f->integer_family==1);
     assert(s.static_uop_policy_identity()=="4242424242424242424242424242424242424242424242424242424242424242");
     assert(s.next(r));assert(s.current_static_uop_operands().size()==1&&s.current_static_uop_operands()[0].producer_distance==1);assert(!s.next(r));}
    {InstructionSliceTraceSource s(std::make_unique<BinaryTraceSource>(path),1,1);TraceRecord r;assert(s.next(r)&&s.current_static_uop_facts()->record_ordinal==1);}
    // The compressed companion must reject cross-record swaps, forged RAW,
    // incomplete population and trailing payload rather than score a prefix.
    const auto companion=path+".uops.gz";
    std::vector<unsigned char> pristine;
    {auto in=gzopen(companion.c_str(),"rb");assert(in);unsigned char bytes[512];int n;
     while((n=gzread(in,bytes,sizeof(bytes)))>0) pristine.insert(pristine.end(),bytes,bytes+n);
     assert(n==0);assert(gzclose(in)==Z_OK);}
    using Mutator=std::function<void(std::vector<unsigned char>&)>;
    const std::vector<Mutator> corruptions={
        [](auto& v){std::fill(v.begin()+24,v.begin()+56,0);},
        [](auto& v){v[72]^=1;},
        [](auto& v){v[128]^=1;},
        [](auto& v){v[192]=0;},
        [](auto& v){v[v.size()-16]^=1;},
        [](auto& v){v.pop_back();},
        [](auto& v){v.push_back(0);}
    };
    for(const auto& corrupt:corruptions) {
        auto bytes=pristine;corrupt(bytes);auto out=gzopen(companion.c_str(),"wb1");assert(out);
        assert(gzwrite(out,bytes.data(),bytes.size())==static_cast<int>(bytes.size()));assert(gzclose(out)==Z_OK);
        rejects([&]{BinaryTraceSource source(path);TraceRecord record;while(source.next(record)){};});
    }
    std::filesystem::remove(path+".uops.gz");rejects([&]{BinaryTraceSource s(path);});
    std::filesystem::remove_all(dir);std::cout<<"static UOP descriptor/projection/identity tests passed\n";
}
