#include "fastsim/trace.hpp"
#include "fastsim/fst_store_facts_io.hpp"
#include "fastsim/fst_dependencies.hpp"
#include "fastsim/fst_memory_requests.hpp"
#include <cassert>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <unistd.h>
using namespace fastsim;
using Bytes=std::vector<unsigned char>;
void le(Bytes& b,std::uint64_t v,unsigned n){while(n--){b.push_back(v&255);v>>=8;}}
void uv(Bytes& b,std::uint64_t v){while(v>=128){b.push_back((v&127)|128);v>>=7;}b.push_back(v);}
template<class F> void rejects(F f){bool bad=false;try{f();}catch(const std::exception&){bad=true;}assert(bad);}
int main(){
 const auto dir=std::filesystem::path("tmp")/("paged-request-"+std::to_string(getpid()));std::filesystem::create_directories(dir);const auto path=(dir/"core.fst").string();
 TraceRecord r;r.op_class=56;r.flags=kRetires|kLoad|kPhysicalAddress;r.address=0x4ff8;r.size=16;
 fst::StoreRequestFacts facts;facts.address=r.address;facts.size=r.size;facts.byte_enable=65535;facts.request_flags=0x63;
 facts.attributes=fst::kStoreFactsComplete|fst::kMemoryRead|fst::kOrdinaryLoad|fst::kStoreSplit|fst::kStorePagedFragments;
 fst::TranslationFragment a,b;a.virtual_address=0x1ff8;a.physical_address=r.address;a.size=8;
 b.virtual_address=0x2000;b.physical_address=0x9000;b.size=8;
 for(auto* f:{&a,&b}){f->mapping.kind=fst::TranslationKind::kPaged;f->mapping.flags=fst::kTranslationLeafKnown;}
 assert(fst::paged_request_fragments_match(r.address,r.size,std::vector<fst::TranslationFragment>{a,b}));
 assert(!fst::paged_request_fragments_match(r.address+1,r.size,std::vector<fst::TranslationFragment>{a,b}));
 assert(fst::paged_request_packet_matches(a.virtual_address,a.physical_address,a.size,std::vector<fst::TranslationFragment>{a,b}));
 assert(!fst::paged_request_packet_matches(a.virtual_address,a.physical_address,16,std::vector<fst::TranslationFragment>{a,b}));
 auto different=b;different.mapping.address_space_id=1;assert(!fst::paged_request_fragments_match(r.address,r.size,std::vector<fst::TranslationFragment>{a,different}));
 auto bad=b;bad.mapping.flags=0;assert(!fst::paged_request_fragments_match(r.address,r.size,std::vector<fst::TranslationFragment>{a,bad}));
 bad=b;bad.virtual_address+=1;assert(!fst::paged_request_fragments_match(r.address,r.size,std::vector<fst::TranslationFragment>{a,bad}));
 {BinaryTraceWriter w(path,0);w.enable_store_facts();rejects([&]{w.append(r,nullptr,{},fst::MemoryRequestRole::Data,0,false,false,&facts);});assert(w.record_count()==0);}
 {BinaryTraceWriter w(path,0);w.append(r);w.close();}
 {std::fstream f(path,std::ios::in|std::ios::out|std::ios::binary);std::uint64_t features=fst::kDynamicTranslations|fst::kStoreRequestFacts;f.seekp(32);f.write((char*)&features,8);}
 {fst::StoreFactsWriter w(path+".sreq",0);w.observe(0,&r,true,&facts);w.finish(1);}
 // Independent v1 translation wire: two real physical pages for one virtual span.
 Bytes wire;for(auto c:std::string("FSTXLT1\0",8))wire.push_back(c);for(auto v:{1u,32u,0u,1u})le(wire,v,4);le(wire,0,8);
 for(unsigned i=0;i<2;++i){wire.push_back(1);for(auto v:std::array<std::uint64_t,6>{i+1,1,0,i?0x2000ull:0x1000ull,i?0x9000ull:0x4000ull,0})uv(wire,v);wire.push_back(12);wire.push_back(0);wire.push_back(1);}
 for(unsigned i=0;i<2;++i){wire.push_back(2);uv(wire,0);le(wire,fst::dependency_record_hash(&r),4);for(auto v:std::array<std::uint64_t,4>{0,i?0x2000ull:0x1ff8ull,8,i+1})uv(wire,v);}
 wire.push_back(255);for(auto v:{1u,1u,2u,2u,1u})uv(wire,v);le(wire,fst::memory_request_stream_hash(14695981039346656037ull,&r),8);
 {auto out=gzopen((path+".xlat.gz").c_str(),"wb1");assert(out);assert(gzwrite(out,wire.data(),wire.size())==int(wire.size()));assert(gzclose(out)==Z_OK);}
 {BinaryTraceSource s(path);TraceRecord row;assert(s.next(row));assert(s.current_ordinary_load_eligible()==true);auto fs=s.current_translation_fragments();assert(fs.size()==2 && fs[1].physical_address==0x9000);assert(!s.next(row));}
 std::filesystem::rename(path+".xlat.gz",path+".xlat.saved");
 rejects([&]{BinaryTraceSource s(path);});
 std::filesystem::rename(path+".xlat.saved",path+".xlat.gz");
 {std::fstream f(path,std::ios::in|std::ios::out|std::ios::binary);std::uint64_t flags=fst::kStoreRequestFacts;f.seekp(32);f.write((char*)&flags,8);}
 rejects([&]{BinaryTraceSource s(path);TraceRecord row;s.next(row);});
 {std::fstream f(path,std::ios::in|std::ios::out|std::ios::binary);std::uint64_t flags=fst::kStoreRequestFacts|fst::kDynamicTranslations;f.seekp(32);f.write((char*)&flags,8);}
 {std::fstream f(path+".sreq",std::ios::in|std::ios::out|std::ios::binary);std::uint32_t v=1;f.seekp(8);f.write((char*)&v,4);}
 rejects([&]{BinaryTraceSource s(path);});
 std::filesystem::remove_all(dir);std::cout<<"paged request identity/qualification tests passed\n";
}
