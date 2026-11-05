// Offline comparison against an existing FST.  Context is fixed to one
// placeholder per physical stream because legacy FFE rows have no OS thread
// id; this does not establish equivalence across context switches.
#include "fastsim/trace.hpp"
#include "functional_store_pair.hh"
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
using namespace fastsim;
int main(int argc, char** argv) { try {
 if(argc!=2) throw std::runtime_error("manifest required");
 std::string p=argv[1], dir=p.substr(0,p.find_last_of('/')), line;
 std::ifstream manifest(p); if(!manifest) throw std::runtime_error("manifest missing");
 while(std::getline(manifest,line)) {
  if(line.empty()||line[0]=='#')continue;
  std::istringstream row(line); unsigned core,source; std::string kind,path;
  if(!(row>>core>>kind>>path>>source))throw std::runtime_error("bad row");
  BinaryTraceSource input(dir+"/"+path); TraceRecord r;
  std::uint64_t ord=0,pairs=0,mismatch=0,partial=0,uncertain=0,pending_group=0,pending_begin=0,pending_pc=0;
  gem5::functional_fst::StorePairer pairer;
  while(input.next(r)) {
   auto facts=input.current_static_uop_facts();
   if(!facts)throw std::runtime_error("missing static uop facts");
   auto frontend=input.current_frontend_macro();
   if(!frontend)throw std::runtime_error("missing macro facts");
   if(pending_group && frontend->begin_record!=pending_begin) {++partial;pending_group=0;}
   using fastsim::fst::StaticStoreRole;
   using gem5::functional_fst::StoreRole;
   const auto role=facts->store_role==StaticStoreRole::Address?StoreRole::Address:
     facts->store_role==StaticStoreRole::Data?StoreRole::Data:StoreRole::Other;
   const auto candidate=pairer.observe(ord,0,frontend->address_space_id,r.pc,
       unsigned(ord-frontend->begin_record),role,frontend->observed_uops==2,
       (facts->flags & fastsim::fst::kStaticAbandonStoreGroups)!=0);
   if ((role==StoreRole::Address || role==StoreRole::Data) && candidate!=facts->store_group) ++mismatch;
   if(facts->store_role==StaticStoreRole::Address) {
    if(pending_group) {++uncertain;pending_group=0;}
    if(!(facts->flags&fastsim::fst::kStaticStorePairKnown)||!facts->store_group) {++uncertain;}
    else {pending_group=facts->store_group;pending_begin=frontend->begin_record;pending_pc=r.pc;}
   } else if(facts->store_role==StaticStoreRole::Data) {
    if(!pending_group)++partial;
    else {
     if(!(facts->flags&fastsim::fst::kStaticStorePairKnown)||facts->store_group!=pending_group||r.pc!=pending_pc)++mismatch;
     ++pairs;pending_group=0;
    }
   }
   ++ord;
  }
  if(pending_group)++partial;
  std::cout<<"core="<<core<<" records="<<ord<<" pairs="<<pairs<<" mismatches="<<mismatch<<" partial="<<partial<<" uncertain="<<uncertain<<"\n";
  if (mismatch || partial || uncertain)
   throw std::runtime_error("incomplete or mismatched static store pairing");
 }
} catch(const std::exception& e) {std::cerr<<e.what()<<"\n"; return 1;} }
