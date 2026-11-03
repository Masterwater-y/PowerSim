#include "taogen/shared/fst_dynamic_translation_writer.hh"
#include <array>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
using namespace fastsim::fst;
int main(int argc,char** argv) {
 assert(argc==2 || argc==3);
 DynamicTranslationWriter writer(argv[1],0);
 std::array<unsigned char,64> hot{};
 const std::uint16_t size=8,flags=4096|32768|1|4;
 std::memcpy(hot.data()+48,&size,2);std::memcpy(hot.data()+50,&flags,2);
 const std::uint32_t token=1;std::memcpy(hot.data()+60,&token,4);
 std::ofstream fst;
 std::uint64_t stream_hash=14695981039346656037ull;
 if(argc==3) {
  fst.open(argv[2],std::ios::binary);std::array<unsigned char,72> h{};
  std::memcpy(h.data(),"FSTRC01\0",8);const std::uint32_t v=7,hs=72,rs=64;
  const std::uint64_t count=1000005,features=kDynamicTranslations;
  std::memcpy(h.data()+8,&v,4);std::memcpy(h.data()+12,&hs,4);std::memcpy(h.data()+16,&rs,4);
  std::memcpy(h.data()+24,&count,8);std::memcpy(h.data()+32,&features,8);fst.write(reinterpret_cast<const char*>(h.data()),h.size());
 }
 const auto write_hot=[&] {if(fst.is_open())fst.write(reinterpret_cast<const char*>(hot.data()),hot.size());
  for(auto b:hot)stream_hash=(stream_hash^b)*1099511628211ull;};
 TranslationFragment f;f.virtual_address=0x400000;f.physical_address=0x800000;f.size=8;
 auto& m=f.mapping;m.address_space_id=m.walk_root=0x1000;m.virtual_base=0x400000;m.physical_base=0x800000;
 m.page_bits=12;m.levels=4;m.flags=kTranslationLeafKnown|kTranslationPathKnown;
 m.pte_addresses={0x1000,0x2000,0x3000,0x4000,0,0,0,0};
 std::memcpy(hot.data()+8,&f.physical_address,8);
 writer.observe(0,hot.data(),1,0x1000,f.virtual_address,8,{f});
 write_hot();
 for(std::uint64_t i=1;i<1000000;++i){writer.observe(i,hot.data(),1,0x1000,f.virtual_address,8,{f});write_hot();}
 assert(writer.definitions()==1 && writer.bindings()==1);
 m.page_bits=21;m.levels=3;m.pte_addresses[3]=0;
 writer.observe(1000000,hot.data(),1,0x1000,f.virtual_address,8,{f});
 write_hot();
 m.page_bits=12;m.levels=4;m.pte_addresses[3]=0x5000;
 writer.observe(1000001,hot.data(),1,0x1000,f.virtual_address,8,{f});
 write_hot();
 writer.observe(1000002,hot.data(),1,0x1000,f.virtual_address,8,{});
 write_hot();
 assert(writer.definitions()==3 && writer.bindings()==4 && writer.unknown_records()==1);
 auto first=f,second=f;first.size=second.size=4;second.virtual_address+=4;second.physical_address+=4;
 writer.observe(1000003,hot.data(),1,0x1000,f.virtual_address,8,{first,second});write_hot();
 writer.observe(1000004,hot.data(),1,0x1000,f.virtual_address,8,{f});write_hot();
 assert(writer.bindings()==7);
 writer.finish(1000005,stream_hash);
 std::ifstream in(argv[1],std::ios::binary|std::ios::ate);
 assert(in.tellg()<4096);
 {
  DynamicTranslationWriter special(std::string(argv[1])+".special",0);
  auto apic=f;apic.mapping.kind=TranslationKind::kLocalApic;
  apic.mapping.request_virtual_base=apic.virtual_address;
  apic.mapping.request_physical_base=0xa00000;apic.physical_address=0xa00000;
  std::memcpy(hot.data()+8,&apic.physical_address,8);
  special.observe(0,hot.data(),0,0x1000,apic.virtual_address,8,{apic});
  assert(special.unknown_records()==0);
  auto io=apic;io.mapping={};io.mapping.kind=TranslationKind::kIo;
  io.mapping.address_space_id=0x1000;io.mapping.virtual_base=io.virtual_address;
  io.mapping.request_physical_base=io.physical_address;
  io.mapping.request_virtual_base=io.virtual_address;
  special.observe(1,hot.data(),0,0x1000,io.virtual_address,8,{io});
  assert(special.unknown_records()==0 && special.definitions()==2);
  io.mapping.kind=TranslationKind::kMsr;
  special.observe(2,hot.data(),0,0x1000,io.virtual_address,8,{io});
  assert(special.unknown_records()==0 && special.definitions()==3);
  apic.mapping.flags=kTranslationLeafKnown;apic.mapping.levels=0;
  apic.mapping.walk_root=0;apic.mapping.pte_addresses={};
  special.observe(3,hot.data(),0,0x1000,apic.virtual_address,8,{apic});
  assert(special.unknown_records()==1);
  auto a=io,b=io;a.size=b.size=4;b.virtual_address+=4;b.physical_address+=4;
  b.mapping={}; // Missing ordinary evidence must remain an unknown member.
  special.observe(4,hot.data(),0,0x1000,io.virtual_address,8,{a,b});
  assert(special.unknown_records()==2 && special.bindings()==6);
  auto pci=io;pci.virtual_address=0xcfc;pci.physical_address=0xc000000000000000ull;pci.size=4;
  pci.mapping.virtual_base=0;pci.mapping.kind=TranslationKind::kPciConfig;
  pci.mapping.request_virtual_base=pci.virtual_address;
  pci.mapping.request_physical_base=pci.physical_address;
  std::memcpy(hot.data()+8,&pci.physical_address,8);
  special.observe(5,hot.data(),0,0x1000,pci.virtual_address,4,{pci});
  assert(special.unknown_records()==2 && special.definitions()==5);
  special.finish(6,0);
 }
 std::cout<<"dynamic translation writer tests passed\n";
}
