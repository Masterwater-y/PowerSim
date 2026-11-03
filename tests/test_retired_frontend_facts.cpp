#include "fastsim/retired_frontend_facts.hpp"
#include "fastsim/trace.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <vector>
using namespace fastsim;
namespace {
void check(bool value) { if(!value) throw std::runtime_error("retired frontend test failed"); }
template<class F> void rejects(F&& f) { bool bad=false; try {f();} catch(const std::runtime_error&) {bad=true;} check(bad); }
using Bytes=std::vector<unsigned char>;
Bytes read(const std::string& p) { std::ifstream f(p,std::ios::binary); return Bytes(std::istreambuf_iterator<char>(f),{}); }
void save(const std::string& p,const Bytes& b) { std::ofstream f(p,std::ios::binary); f.write(reinterpret_cast<const char*>(b.data()),b.size()); }
Bytes unpack(const std::string& p) { auto f=gzopen(p.c_str(),"rb"); check(f); Bytes b; unsigned char buf[4096]; int n; while((n=gzread(f,buf,sizeof(buf)))>0) b.insert(b.end(),buf,buf+n); check(n==0 && gzclose(f)==Z_OK); return b; }
void pack(const std::string& p,const Bytes& b) { auto f=gzopen(p.c_str(),"wb1"); check(f); check(gzwrite(f,b.data(),b.size())==static_cast<int>(b.size())); check(gzclose(f)==Z_OK); }
}
int main() {
    const std::string path="tmp/retired-frontend-fixture.fst", companion=path+".ffe.gz";
    std::filesystem::create_directories("tmp");
    std::vector<fst::RetiredFrontendMacro> macros;
    { BinaryTraceWriter w(path,3); w.enable_frontend_facts();
      for(unsigned i=0;i<7;++i) {
        fst::RetiredFrontendMacro m;
        m.begin_record=w.record_count(); m.observed_uops=i==1?3:(i==6?2:1); m.end_record=m.begin_record+m.observed_uops;
        m.pc=0x1000+i; m.address_space_id=i<3?0x1000:0x2000;
        m.code_identity=100+i; m.semantic_identity=200+i; m.known_flags=fst::kFrontendRequiredKnown;
        m.flags=fst::kFrontendComplete|fst::kFrontendStartsMacro|fst::kFrontendDsbEligible;
        if(i==1) m.flags|=fst::kFrontendSharedAllocation;
        if(i==2) m.flags|=fst::kFrontendMicrocode;
        if(i==3) m.flags|=fst::kFrontendPseudo|fst::kFrontendMicrocode;
        if(i==3) m.flags&=~fst::kFrontendDsbEligible;
        if(i==4) m.flags&=~(fst::kFrontendComplete|fst::kFrontendDsbEligible);
        if(i==6) m.flags&=~fst::kFrontendDsbEligible;
        // A leading shared suffix is representable, but does not certify that
        // the observed first UOP owns the missing allocation-group leader.
        if(i==5) m.flags=fst::kFrontendSharedAllocation;
        m.instruction_size=i==3?0:1; m.bytes[0]=i==3?0:0x90;
        w.set_address_space_id(m.address_space_id); w.register_frontend_macro(m); macros.push_back(m);
        for(unsigned j=0;j<m.observed_uops;++j) {
            TraceRecord r; r.pc=m.pc;
            if(m.observed_uops>1 || i>=4) r.flags|=kMicroOp;
            if((m.observed_uops>1 && j+1==m.observed_uops) || i==5) r.flags|=kLastMicroOp;
            if(i==6 && j==0) r.flags|=kBranch;
            w.append(r);
        }
      } w.close(); }
    BinaryTraceSource s(path); TraceRecord r;
    for(const auto& expected:macros) for(auto i=expected.begin_record;i<expected.end_record;++i) {
        check(s.next(r)); const auto* m=s.current_frontend_macro();
        check(m && m->begin_record==expected.begin_record && m->pc==r.pc && m->flags==expected.flags);
        check(m->address_space_id==s.current_address_space_id() && s.current_frontend_record_ordinal()==i);
        if(m->pc==0x1006) {
            check((m->flags&fst::kFrontendComplete)!=0 && (m->known_flags&fst::kFrontendDsbEligible)!=0);
            check((m->flags&fst::kFrontendDsbEligible)==0);
        }
        if(m->pc==0x1005) {
            check((m->known_flags&fst::kFrontendStartsMacro)!=0);
            check((m->flags&fst::kFrontendStartsMacro)==0);
            check((m->flags&fst::kFrontendSharedAllocation)!=0);
        }
    }
    check(!s.next(r) && s.current_frontend_macro()==nullptr && !s.current_frontend_record_ordinal());
    InstructionSliceTraceSource sliced(std::make_unique<BinaryTraceSource>(path),1,1);
    check(sliced.next(r) && sliced.current_frontend_macro()->begin_record==1 && sliced.current_frontend_record_ordinal()==1);
    WarmupInstructionTraceSource warm(std::make_unique<BinaryTraceSource>(path),1,1);
    check(warm.next(r) && warm.current_frontend_record_ordinal()==0);
    const auto original=read(companion), body=unpack(companion), hot=read(path);
    auto bad_source=[&] { rejects([&]{BinaryTraceSource bad(path);}); };
    auto corrupt=[&](std::size_t offset,unsigned char byte) { auto b=body; b.at(offset)^=byte; pack(companion,b); bad_source(); save(companion,original); };
    corrupt(16,1); // wrong source core
    corrupt(33,1); // descriptor coverage starts after source ordinal zero
    corrupt(33+48,128); // unknown semantic flag
    corrupt(33+53,4); // unknown validity bit
    corrupt(33+58,1); // noncanonical unused instruction bytes
    corrupt(body.size()-24,1); // source record count
    corrupt(body.size()-8,1); // full hot-stream identity
    for(unsigned kind=0;kind<4;++kind) {
        auto b=original;
        if(kind==0) b.pop_back();
        if(kind==1) b.push_back(42);
        if(kind==2) b[b.size()-8]^=1;
        if(kind==3) b.insert(b.end(),original.begin(),original.end());
        save(companion,b); bad_source(); save(companion,original);
    }
    { auto b=body; b.resize(b.size()-25); pack(companion,b); bad_source(); save(companion,original); }
    { auto b=body; b.push_back(0); pack(companion,b); bad_source(); save(companion,original); }
    { auto b=hot; b[72+64*6+16]^=1; save(path,b); bad_source(); save(path,hot); } // mutation outside sliced prefix
    std::filesystem::remove(companion); bad_source();
    for(const auto& suffix:{"",".asmap"}) std::filesystem::remove(path+suffix);
    { BinaryTraceWriter w(path,0); TraceRecord ordinary; w.append(ordinary); w.close(); }
    BinaryTraceSource legacy(path); check(legacy.next(r) && legacy.current_frontend_macro()==nullptr && !legacy.current_frontend_record_ordinal());
    std::filesystem::remove(path);
    std::cout << "retired frontend facts tests passed\n";
}
