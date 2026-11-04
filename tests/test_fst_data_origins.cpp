#include "fastsim/fst_dynamic_translations.hpp"
#include "fastsim/trace.hpp"
#include "fastsim/fst_dynamic_translation_writer.hpp"
#include <cassert>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <set>
#include <unistd.h>
using namespace fastsim;
namespace {
using Bytes=std::vector<unsigned char>;
std::array<std::uint8_t,32> policy_digest{};
template<class F>void rejects(F f){bool failed=false;try{f();}catch(const std::exception&){failed=true;}assert(failed);}
Bytes read(const std::string&p){std::ifstream f(p,std::ios::binary);return Bytes(std::istreambuf_iterator<char>(f),{});}
void save(const std::string&p,const Bytes&b){std::ofstream f(p,std::ios::binary);f.write((const char*)b.data(),b.size());}
Bytes unpack(const std::string&p){auto f=gzopen(p.c_str(),"rb");assert(f);Bytes out;unsigned char buf[4096];int n;while((n=gzread(f,buf,sizeof(buf)))>0)out.insert(out.end(),buf,buf+n);assert(n==0&&gzclose(f)==Z_OK);return out;}
void pack(const std::string&p,const Bytes&b){auto f=gzopen(p.c_str(),"wb1");assert(f);assert(gzwrite(f,b.data(),b.size())==int(b.size()));assert(gzclose(f)==Z_OK);}
std::string policy_string(){std::string s;const char*x="0123456789abcdef";for(auto b:policy_digest){s+=x[b>>4];s+=x[b&15];}return s;}
fst::TranslationFragment fragment(std::uint64_t va,std::uint64_t pa,unsigned size,std::uint64_t origin=0x1000,bool path=true){
 fst::TranslationFragment f;f.virtual_address=va;f.physical_address=pa;f.size=size;
 auto&m=f.mapping;m.address_space_id=origin;m.walk_root=path?0x9000:0;m.virtual_base=va&~4095ull;
 m.physical_base=pa&~4095ull;m.page_bits=12;m.flags=path?7:1;m.levels=path?1:0;m.pte_addresses[0]=path?0x9000:0;return f;
}
struct Row {TraceRecord hot;std::uint64_t retirement;std::vector<fst::TranslationFragment> fragments;};
Row row(unsigned token,std::uint64_t retirement,std::uint64_t origin=0x1000,std::uint64_t pa=0x800020){
 Row r;r.retirement=retirement;r.hot.pc=0x5000;r.hot.address=pa;r.hot.size=8;r.hot.flags=kRetires|kLoad|kPhysicalAddress;
 if(token){r.hot.flags|=kVirtualPageToken;r.hot.reserved=token;}r.fragments={fragment(0x400020,pa,8,origin)};return r;
}
void make(const std::string&p,const std::vector<Row>&rows,bool origins=true){
 {BinaryTraceWriter w(p,0);w.enable_complete_dependencies();w.enable_static_uop_facts(policy_digest);w.enable_frontend_facts();
  std::set<unsigned> seen;
  for(unsigned i=0;i<rows.size();++i){const auto&r=rows[i];w.set_address_space_id(r.retirement);
   if(r.hot.virtual_page_token() && seen.insert(r.hot.virtual_page_token()).second){VirtualPageMapping m;
    m.token=r.hot.virtual_page_token();m.first_record_ordinal=i;m.virtual_page=0x400020>>12;m.physical_page=r.hot.address>>12;m.physical_page_valid=true;w.register_virtual_page_mapping(m);}
   fst::RetiredFrontendMacro macro;macro.pc=r.hot.pc;macro.address_space_id=r.retirement;macro.begin_record=i;macro.end_record=i+1;
   macro.observed_uops=1;macro.instruction_size=1;macro.bytes[0]=0x90;macro.code_identity=macro.semantic_identity=1;
   macro.known_flags=fst::kFrontendRequiredKnown;macro.flags=fst::kFrontendComplete|fst::kFrontendStartsMacro;w.register_frontend_macro(macro);
   fst::StaticUopFactsRow facts;std::vector<fst::StaticUopOperand> operands;
   w.append(r.hot,nullptr,{},fst::MemoryRequestRole::Data,0,false,false,nullptr,&facts,&operands);}w.close();}
 {std::fstream f(p,std::ios::binary|std::ios::in|std::ios::out);f.seekg(32);std::uint64_t features;f.read((char*)&features,8);features|=fst::kDynamicTranslations;f.seekp(32);f.write((char*)&features,8);}
 fst::DynamicTranslationWriter w(p+".xlat.gz",0,origins,policy_digest);
 std::uint64_t hash=14695981039346656037ull;
 for(unsigned i=0;i<rows.size();++i){const auto&r=rows[i];w.observe(i,&r.hot,r.hot.virtual_page_token(),r.retirement,
  r.fragments.empty()?0x400020:r.fragments.front().virtual_address,r.hot.size,r.fragments);hash=fst::static_uop_stream_hash(hash,&r.hot);}w.finish(rows.size(),hash);
}
}
int main(int argc,char**argv){
 assert((argc==2||argc==3)&&std::string(argv[1]).size()==64);
 for(unsigned i=0;i<32;++i)policy_digest[i]=std::stoul(std::string(argv[1]+2*i,2),nullptr,16);
 const auto d=std::filesystem::path("tmp")/("data-origins-"+std::to_string(getpid()));std::filesystem::create_directories(d);
 const auto p=(d/"trace.fst").string();const auto policy=policy_string();
 std::vector<Row> rows={row(1,0x2000),row(1,0x2000),row(1,0x2000,0x3000),row(2,0x4000,0x3000),row(2,0x4000,0x3000),row(0,0x4000),row(0,0x4000),row(0,0x4000)};
 auto&a=rows[5];a.hot.address=0x800ffc;a.fragments={fragment(0x400ffc,0x800ffc,4),fragment(0x401000,0xa00000,4,0x3000)};
 rows[6].fragments[0]=fragment(0x400020,0x800020,8,0x1000,false);rows[7].fragments.clear();
 for(unsigned kind=1;kind<=6;++kind){auto special=row(0,0x4000,0x3000,0xc000000000000020ull+kind*4096);
  auto& f=special.fragments[0];f.mapping={};f.mapping.address_space_id=0x3000;f.mapping.virtual_base=f.virtual_address&~4095ull;
  f.mapping.kind=static_cast<fst::TranslationKind>(kind);f.mapping.request_virtual_base=f.virtual_address;f.mapping.request_physical_base=f.physical_address;
  rows.push_back(special);}
 auto unknown=row(3,0x4000);unknown.fragments.clear();rows.push_back(unknown);
 unknown.hot.address+=8;rows.push_back(unknown); // sparse Unknown remains Unknown at another byte offset
 make(p,rows);if(argc==3)make(argv[2],rows);TraceRecord r;
 {BinaryTraceSource s(p);assert(s.dynamic_translation_version()==3);for(unsigned i=0;i<rows.size();++i){assert(s.next(r));
  assert(s.current_translation_record_ordinal()==i);assert(s.current_translation_retirement_asid()==rows[i].retirement);assert(s.current_address_space_id()==rows[i].retirement);
  const auto& facts=s.current_translation_fragments();if(rows[i].fragments.empty())assert(facts.size()==1&&facts[0].mapping.flags==0);
  else{assert(facts.size()==rows[i].fragments.size());for(unsigned j=0;j<facts.size();++j)assert(facts[j].mapping.identity()==rows[i].fragments[j].mapping.identity());}}
 assert(!s.next(r)&&!s.current_translation_record_ordinal()&&!s.current_translation_retirement_asid());}
 {InstructionSliceTraceSource s(std::make_unique<BinaryTraceSource>(p),2,2);assert(s.next(r)&&s.dynamic_translation_version()==3);assert(s.current_translation_record_ordinal()==2);assert(s.current_translation_retirement_asid()==0x2000);assert(s.current_translation_fragments()[0].mapping.address_space_id==0x3000);assert(s.next(r)&&s.current_translation_retirement_asid()==0x4000);}
 {WarmupInstructionTraceSource s(std::make_unique<BinaryTraceSource>(p),2,2);assert(s.next(r)&&s.next(r)&&!s.next(r));s.start_measurement();assert(s.next(r)&&s.current_translation_retirement_asid()==0x2000);assert(s.next(r)&&s.current_translation_retirement_asid()==0x4000);}
 auto hot=read(p),gz=read(p+".xlat.gz"),body=unpack(p+".xlat.gz");auto bad=[&]{rejects([&]{BinaryTraceSource s(p);});};
 for(auto offset:std::vector<std::size_t>{8,12,16,24,32,63,body.size()-1}){auto b=body;b[offset]^=1;pack(p+".xlat.gz",b);bad();save(p+".xlat.gz",gz);}
 for(unsigned kind=0;kind<4;++kind){auto b=gz;if(kind==0)b.pop_back();if(kind==1)b.push_back(42);if(kind==2)b[b.size()-8]^=1;if(kind==3)b.insert(b.end(),gz.begin(),gz.end());save(p+".xlat.gz",b);bad();save(p+".xlat.gz",gz);}
 {auto b=hot;b.back()^=1;save(p,b);bad();save(p,hot);}
 {auto asmap=read(p+".asmap");auto b=asmap;b.back()^=1;save(p+".asmap",b);bad();save(p+".asmap",asmap);}
 {std::filesystem::rename(p+".uops.gz",p+".uops.saved");bad();std::filesystem::rename(p+".uops.saved",p+".uops.gz");}
 {std::filesystem::rename(p+".asmap",p+".asmap.saved");bad();std::filesystem::rename(p+".asmap.saved",p+".asmap");}
 {std::filesystem::remove(p+".xlat.gz");bad();save(p+".xlat.gz",gz);}
 // Identical token may change origin in one retirement namespace; it cannot
 // identify another namespace or a different physical page.
 make(p,{row(1,0x2000),row(1,0x4000)});bad();
 make(p,{row(1,0x2000),row(1,0x2000,0x3000,0xa00020)});bad();
 // Constructor validation must not disappear when the hot token feature is
 // underdeclared. The invalid second row is outside this actual replay slice.
 const auto undeclare_tokens=[&]{auto b=read(p);b[32]&=~1u;save(p,b);std::filesystem::remove(p+".vmap");};
 const auto rejects_constructor_and_slice=[&]{
  rejects([&]{BinaryTraceSource s(p);});
  rejects([&]{InstructionSliceTraceSource s(std::make_unique<BinaryTraceSource>(p),0,1);});
 };
 make(p,{row(1,0x2000),row(2,0x4000)});
 {BinaryTraceSource s(p);assert(s.next(r)&&s.next(r)&&!s.next(r));}
 undeclare_tokens();rejects_constructor_and_slice();
 auto ua=row(1,0x2000),ub=row(1,0x4000);ua.fragments.clear();ub.fragments.clear();
 make(p,{ua,ub});undeclare_tokens();rejects_constructor_and_slice();
 ub.hot.reserved=2;make(p,{ua,ub});
 {BinaryTraceSource s(p);assert(s.next(r)&&s.current_translation_fragments()[0].mapping.flags==0);
  assert(s.next(r)&&s.current_translation_retirement_asid()==0x4000&&s.current_translation_fragments()[0].mapping.flags==0);}
 {InstructionSliceTraceSource s(std::make_unique<BinaryTraceSource>(p),1,1);
  assert(s.next(r)&&s.current_translation_record_ordinal()==1&&s.current_translation_retirement_asid()==0x4000);}
 rejects([&]{fst::DynamicTranslations direct(p,0,2,[](auto i){return i?0x4000:0x2000;},policy);});
 // Entirely tokenless v3 is valid through independent retirement contexts.
 make(p,{row(0,0x2000),row(0,0x4000)});
 {BinaryTraceSource s(p);assert(s.next(r)&&s.next(r)&&!s.next(r));}
 {InstructionSliceTraceSource s(std::make_unique<BinaryTraceSource>(p),1,1);
  assert(s.next(r)&&s.current_translation_record_ordinal()==1&&s.current_translation_retirement_asid()==0x4000);}
 // Direct v3 wire preserves ASID zero explicitly; existing full-source asmap
 // has no zero representation and remains fail closed.
 make(p,{row(0,0x2000)});{auto b=unpack(p+".xlat.gz"); // retirement ASID ULEB ends immediately before footer255
  auto footer=std::find(b.begin()+64,b.end(),255);assert(footer!=b.end());auto end=std::distance(b.begin(),footer);
  assert(b[end-2]==0x80&&b[end-1]==0x40);b.erase(b.begin()+end-2,b.begin()+end);b.insert(b.begin()+end-2,0);pack(p+".xlat.gz",b);
  fst::DynamicTranslations direct(p,0,1,[](auto){return 0;},policy);assert(direct.version()==3);assert(direct.resolve(0,row(0,0x2000).hot)[0].mapping.address_space_id==0x1000);
  rejects([&]{fst::DynamicTranslations wrong(p,0,1,[](auto){return 0x2000;},policy);});bad();}
 make(p,{row(1,0x2000,0x2000)});{BinaryTraceSource s(p);assert(s.next(r)&&s.current_translation_fragments()[0].mapping.address_space_id==s.current_address_space_id());}
 make(p,{row(0,0x2000,0)});{BinaryTraceSource s(p);assert(s.next(r)&&s.current_translation_fragments()[0].mapping.address_space_id==0);}
 // Old writer still demotes origin mismatch, without upgrading old zero facts.
 make(p,{row(1,0x2000)},false);{BinaryTraceSource s(p);assert(s.dynamic_translation_version()==2&&s.next(r));assert(s.current_translation_fragments()[0].mapping.flags==0);}
 std::filesystem::remove_all(d);std::cout<<"data translation origin transport tests passed\n";
}
