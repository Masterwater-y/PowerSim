#include "fastsim/fst_instruction_translations.hpp"
#include "fastsim/trace.hpp"
#include <cassert>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <unistd.h>
using namespace fastsim;
namespace {
using Bytes=std::vector<unsigned char>;
template<class F> void rejects(F f){bool bad=false;try{f();}catch(const std::exception&){bad=true;}assert(bad);}
Bytes read(const std::string& p){std::ifstream f(p,std::ios::binary);return Bytes(std::istreambuf_iterator<char>(f),{});}
void save(const std::string& p,const Bytes& b){std::ofstream f(p,std::ios::binary);f.write((const char*)b.data(),b.size());}
Bytes unpack(const std::string& p){auto f=gzopen(p.c_str(),"rb");assert(f);Bytes out;unsigned char buf[4096];int n;while((n=gzread(f,buf,sizeof(buf)))>0)out.insert(out.end(),buf,buf+n);assert(n==0&&gzclose(f)==Z_OK);return out;}
void pack(const std::string& p,const Bytes& b){auto f=gzopen(p.c_str(),"wb1");assert(f);assert(gzwrite(f,b.data(),b.size())==int(b.size()));assert(gzclose(f)==Z_OK);}
fst::TranslationFragment fragment(std::uint64_t va,std::uint64_t pa,unsigned size,bool path=true) {
    fst::TranslationFragment f;f.virtual_address=va;f.physical_address=pa;f.size=size;
    f.mapping.address_space_id=f.mapping.walk_root=0x1000;f.mapping.virtual_base=va&~4095ull;
    f.mapping.physical_base=pa&~4095ull;f.mapping.page_bits=12;f.mapping.flags=fst::kTranslationLeafKnown;
    if(path){f.mapping.flags|=fst::kTranslationPathKnown;f.mapping.levels=4;f.mapping.pte_addresses={0,0x2004,0x3000,0x4000};}
    return f;
}
}
int main() {
    const auto directory=std::filesystem::path("tmp")/("instruction-translations-"+std::to_string(getpid()));
    std::filesystem::create_directories(directory);
    const auto path=(directory/"trace.fst").string(),companion=path+".ixlat.gz";
    std::array<std::uint8_t,32> policy{};policy[0]=42;
    std::vector<fst::RetiredFrontendMacro> macros;
    std::vector<std::shared_ptr<const fst::InstructionFetchFacts>> expected;
    std::vector<TraceRecord> hot;
    for(unsigned index=0;index<6;++index) {
        fst::RetiredFrontendMacro m;m.pc=index==0?0x1ffc:0x1000;m.address_space_id=0x1000;
        m.begin_record=hot.size();m.observed_uops=(index==1||index==5)?3:1;m.end_record=m.begin_record+m.observed_uops;
        m.code_identity=100+index;m.semantic_identity=200+index;m.known_flags=fst::kFrontendRequiredKnown;
        m.flags=fst::kFrontendComplete|fst::kFrontendStartsMacro;m.instruction_size=index==0?8:1;m.bytes[0]=0x90;
        if(index==3){m.flags|=fst::kFrontendPseudo|fst::kFrontendMicrocode;m.instruction_size=0;m.bytes[0]=0;}
        if(index==5)m.flags&=~fst::kFrontendComplete;
        fst::InstructionByteWitness witness;witness.begin_observed(m.pc,index==4?0x2000:m.address_space_id);
        if(index==0){witness.observe(fragment(0x1ffc,0x9ffc,4));witness.observe(fragment(0x2000,0x15000,4));}
        else if(index!=2&&index!=3) {
            auto supplied=fragment(m.pc,index==4?0x25000:0x9000,8,index!=1);
            if(index==4){supplied.mapping.address_space_id=0x3000;supplied.mapping.walk_root=0x7000;supplied.mapping.flags|=fst::kTranslationGlobal;}
            witness.observe(supplied);
        }
        expected.push_back(index==3?fst::InstructionFetchFacts::pseudo(m.pc):witness.snapshot(m.instruction_size));
        macros.push_back(m);
        for(unsigned member=0;member<m.observed_uops;++member) {
            TraceRecord r;r.pc=m.pc;
            if(m.observed_uops>1||index==3)r.flags|=kMicroOp;
            if((m.observed_uops>1||index==3)&&member+1==m.observed_uops&&index!=5)r.flags|=kLastMicroOp;
            hot.push_back(r);
        }
    }
    {BinaryTraceWriter writer(path,0);writer.enable_complete_dependencies();writer.enable_static_uop_facts(policy);
     writer.enable_frontend_facts();writer.set_address_space_id(0x1000);
     for(const auto& m:macros){writer.register_frontend_macro(m);
         for(auto ordinal=m.begin_record;ordinal<m.end_record;++ordinal){fst::StaticUopFactsRow f;std::vector<fst::StaticUopOperand> operands;
             writer.append(hot[ordinal],nullptr,{},fst::MemoryRequestRole::Data,0,false,false,nullptr,&f,&operands);}}
     writer.close();}
    {fst::InstructionTranslationWriter writer(companion,0,policy);
     for(const auto& r:hot)writer.observe_record(&r);
     for(unsigned i=0;i<macros.size();++i)writer.append(macros[i],*expected[i]);writer.finish(hot.size());}
    {std::fstream f(path,std::ios::in|std::ios::out|std::ios::binary);f.seekg(32);std::uint64_t flags;f.read((char*)&flags,8);flags|=fst::kInstructionTranslations;f.seekp(32);f.write((char*)&flags,8);}
    TraceRecord record;std::shared_ptr<const fst::InstructionFetchFacts> frozen;
    {BinaryTraceSource source(path);assert(source.instruction_translations_declared());assert(source.instruction_translation_version()==2);
     for(unsigned index=0;index<macros.size();++index)for(auto ordinal=macros[index].begin_record;ordinal<macros[index].end_record;++ordinal){
         assert(source.next(record));auto facts=source.current_instruction_fetch();assert(facts&&fst::same_instruction_fetch(*facts,*expected[index]));
         assert(source.current_frontend_record_ordinal()==ordinal);if(index==0)frozen=facts;
         if(index==4){assert(facts->decode_context_known&&facts->decode_address_space_id==0x2000);
             assert(facts->fragments[0].mapping.address_space_id==0x3000&&facts->fragments[0].mapping.walk_root==0x7000);
             assert(source.current_frontend_macro()->address_space_id==0x1000);}
         if(index==5)assert(!(source.current_frontend_macro()->flags&fst::kFrontendComplete));}
     assert(!source.next(record)&&!source.current_instruction_fetch());}
    assert(frozen->fragments[1].physical_address==0x15000);
    assert(!expected[1]->path_complete&&expected[1]->status==fst::InstructionFetchStatus::Mapped);
    assert(expected[2]->status==fst::InstructionFetchStatus::Unknown&&expected[3]->status==fst::InstructionFetchStatus::NotApplicable);
    {InstructionSliceTraceSource source(std::make_unique<BinaryTraceSource>(path),1,1);
     assert(source.next(record)&&source.instruction_translations_declared()&&source.current_frontend_record_ordinal()==1);
     assert(fst::same_instruction_fetch(*source.current_instruction_fetch(),*expected[1]));}
    {WarmupInstructionTraceSource source(std::make_unique<BinaryTraceSource>(path),1,1);
     assert(source.next(record)&&source.current_frontend_record_ordinal()==0);
     assert(fst::same_instruction_fetch(*source.current_instruction_fetch(),*expected[0]));
     assert(!source.next(record));source.start_measurement();assert(source.next(record)&&source.current_frontend_record_ordinal()==1);
     assert(fst::same_instruction_fetch(*source.current_instruction_fetch(),*expected[1]));}
    const auto original=read(companion),body=unpack(companion),hot_original=read(path);
    auto bad_source=[&]{rejects([&]{BinaryTraceSource source(path);});};
    auto corrupt=[&](std::size_t offset,unsigned char byte){auto b=body;b.at(offset)^=byte;pack(companion,b);bad_source();save(companion,original);};
    for(auto offset:std::vector<std::size_t>{0,6,8,12,16,20,24,56,65,73,81,89,97,105,121,145,146,147,148,149,150,161,169,177,181,189,213,221,229,295,297,299,body.size()-32,body.size()-24,body.size()-16,body.size()-8})corrupt(offset,1);
    corrupt(296,4); // known path cannot have zero levels
    // Duplicate/missing span, late footer, strict single gzip member/CRC/trailer.
    for(unsigned kind=0;kind<5;++kind){auto b=original;if(kind==0)b.pop_back();if(kind==1)b.push_back(42);if(kind==2)b[b.size()-8]^=1;
        if(kind==3)b.insert(b.end(),original.begin(),original.end());if(kind==4)b.resize(b.size()/2);
        save(companion,b);bad_source();save(companion,original);}
    {auto b=body;b.resize(b.size()-33);pack(companion,b);bad_source();save(companion,original);}
    {auto b=body;Bytes duplicate(b.begin()+64,b.begin()+441);b.insert(b.begin()+64,duplicate.begin(),duplicate.end());pack(companion,b);bad_source();save(companion,original);}
    {auto b=body;b.erase(b.begin()+64,b.begin()+441);pack(companion,b);bad_source();save(companion,original);}
    {auto b=hot_original;b.back()^=1;save(path,b);bad_source();save(path,hot_original);}
    {auto b=hot_original;b[33]&=~8u;save(path,b);bad_source();save(path,hot_original);} // declared ixlat requires .ffe
    {auto b=hot_original;b[34]|=1;save(path,b);bad_source();save(path,hot_original);} // bit16 stays unknown
    std::filesystem::remove(companion);bad_source();save(companion,original);
    // Old input explicitly declares no instruction facts; no synthetic fallback.
    {auto b=hot_original;b[33]&=~128u;save(path,b);BinaryTraceSource old(path);assert(!old.instruction_translations_declared());
     assert(old.next(record)&&!old.current_instruction_fetch());save(path,hot_original);}
    rejects([&]{auto bad=*expected[0];bad.fragments[1].physical_address=0xa000;fst::validate_instruction_binding(macros[0],bad);});
    {auto historical=*expected[0];historical.fragments[1].mapping.address_space_id=0x2000;
     fst::validate_instruction_binding(macros[0],historical); // distinct origins for distinct bytes
     assert(fst::instruction_origin_decode_asid_mismatch(historical));
     assert(fst::instruction_origin_retirement_asid_mismatch(historical,macros[0]));
     historical.decode_context_known=false;historical.decode_address_space_id=0;
     rejects([&]{fst::validate_instruction_binding(macros[0],historical,1);});}
    rejects([&]{auto bad=*expected[0];bad.decode_context_known=false;fst::validate_instruction_binding(macros[0],bad);});
    rejects([&]{auto bad=*expected[0];bad.path_complete=false;fst::validate_instruction_binding(macros[0],bad);});
    rejects([&]{auto bad=*expected[0];bad.status=fst::InstructionFetchStatus::NotApplicable;fst::validate_instruction_binding(macros[0],bad);});
    {fst::InstructionTranslationWriter w((directory/"bad.ixlat.gz").string(),0,policy);
     w.append(macros[0],*expected[0]);rejects([&]{w.append(macros[0],*expected[0]);});rejects([&]{w.finish(hot.size());});}
    // Legacy v1 keeps its original strict context semantics. Historical v1
    // Unknown rows are not repaired or upgraded by the v2 reader.
    std::vector<fst::InstructionFetchFacts> legacy;
    for(unsigned i=0;i<expected.size();++i) {
        auto facts=*expected[i];facts.decode_context_known=false;facts.decode_address_space_id=0;
        if(i==4){facts.status=fst::InstructionFetchStatus::Unknown;facts.reason=fst::InstructionFetchUnknown::ContextMismatch;
            facts.path_complete=false;facts.fragments.clear();}
        legacy.push_back(facts);
    }
    {fst::InstructionTranslationWriter writer(companion,0,policy,1);
     for(const auto& r:hot)writer.observe_record(&r);
     for(unsigned i=0;i<macros.size();++i)writer.append(macros[i],legacy[i]);writer.finish(hot.size());}
    {BinaryTraceSource source(path);assert(source.instruction_translation_version()==1);
     for(unsigned i=0;i<macros.size();++i)for(auto ordinal=macros[i].begin_record;ordinal<macros[i].end_record;++ordinal){
         assert(source.next(record));assert(fst::same_instruction_fetch(*source.current_instruction_fetch(),legacy[i]));}
     assert(!source.next(record));}
    {auto legacy_body=unpack(companion);legacy_body[6]='2';pack(companion,legacy_body);bad_source();}
    std::filesystem::remove_all(directory);
    std::cout<<"instruction translation writer/reader tests passed\n";
}
