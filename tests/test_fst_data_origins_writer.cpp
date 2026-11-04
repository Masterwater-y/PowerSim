#include "taogen/shared/fst_dynamic_translation_writer.hh"
#include <array>
#include <cassert>
#include <cstring>
#include <iostream>
using namespace fastsim::fst;
int main(int argc,char**argv) {
 assert(argc==2);
 std::array<unsigned char,64> row{};
 std::uint64_t pa=0x800020;std::memcpy(row.data()+8,&pa,8);
 std::array<std::uint8_t,32> policy{};policy[0]=1;
 DynamicTranslationWriter out(argv[1],0,true,policy);
 TranslationFragment f;f.virtual_address=0x400020;f.physical_address=pa;f.size=8;
 auto&m=f.mapping;m.address_space_id=0x1000;m.walk_root=0x9000;
 m.virtual_base=0x400000;m.physical_base=0x800000;m.page_bits=12;m.levels=1;m.flags=3;m.pte_addresses[0]=0x9000;
 out.observe(0,row.data(),1,0x2000,f.virtual_address,8,{f});
 assert(out.unknown_records()==0 && out.definitions()==1 && out.bindings()==1);
 out.observe(1,row.data(),1,0x2000,f.virtual_address,8,{f});assert(out.bindings()==1);
 out.observe(2,row.data(),1,0,f.virtual_address,8,{f});assert(out.bindings()==2);
 out.observe(3,row.data(),1,0,f.virtual_address,8,{f});assert(out.bindings()==2);
 m.address_space_id=0x3000;out.observe(4,row.data(),1,0,f.virtual_address,8,{f});assert(out.bindings()==3 && out.definitions()==2);
 auto a=f,b=f;a.size=b.size=4;b.virtual_address+=4;b.physical_address+=4;b.mapping.address_space_id=0x4000;
 out.observe(5,row.data(),0,0,a.virtual_address,8,{a,b});assert(out.unknown_records()==0 && out.bindings()==5);
 out.finish(6,0);
 std::cout<<"data origin writer tests passed\n";
}
