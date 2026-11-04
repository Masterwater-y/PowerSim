#include "fastsim/trace.hpp"
#include "fastsim/fst_store_requests.hpp"
#include <cassert>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <unistd.h>
using namespace fastsim;
template<class F> void rejects(F f) { bool yes=false;try{f();}catch(const std::exception&){yes=true;} assert(yes); }
int main() {
    const auto dir=std::filesystem::path("tmp")/("fst-store-facts-"+std::to_string(getpid()));
    std::filesystem::create_directories(dir);
    const auto path=(dir/"trace.fst").string();
    TraceRecord alu{};alu.pc=0x10;alu.flags=kRetires;
    TraceRecord store{};store.pc=0x20;store.address=0x1000;store.size=4;store.flags=kRetires|kStore|kPhysicalAddress;
    fst::StoreRequestFacts facts{};facts.attributes=fst::kStoreFactsComplete|fst::kStoreOrdinary;
    facts.address=store.address;facts.size=4;facts.byte_enable=15;facts.request_flags=0x8200ull;
    facts.context=3;facts.requestor=5;facts.micro_pc=7;
    auto bad=facts;bad.request_flags|=0x800;
    rejects([&]{fst::validate_store_facts(bad,bad.address,bad.size);});
    bad=facts;bad.attributes|=fst::kStoreAtomic;
    rejects([&]{fst::validate_store_facts(bad,bad.address,bad.size);});
    {
        BinaryTraceWriter writer(path,0);writer.enable_store_facts();
        rejects([&]{writer.append(store);}); // No implicit ordinary classification.
        assert(writer.record_count()==0);
        auto atomic=store;atomic.flags|=kAtomic;
        const auto before=std::filesystem::file_size(path);
        rejects([&]{writer.append(atomic,nullptr,{},fst::MemoryRequestRole::Data,7,false,false,&facts);});
        assert(writer.record_count()==0 && std::filesystem::file_size(path)==before);
        writer.append(alu);
        writer.append(store,nullptr,{},fst::MemoryRequestRole::Data,7,false,false,&facts);
        auto load=store;load.flags=kRetires|kLoad|kPhysicalAddress;
        auto load_facts=facts;load_facts.attributes=fst::kStoreFactsComplete|fst::kMemoryRead|fst::kOrdinaryLoad;
        writer.append(load,nullptr,{},fst::MemoryRequestRole::Data,7,false,false,&load_facts);
        writer.close();
    }
    {
        BinaryTraceSource source(path);TraceRecord record;
        assert(source.store_facts_complete());
        assert(source.next(record)&&!source.current_store_facts());
        assert(source.next(record));auto row=source.current_store_facts();
        assert(row && row->record_ordinal==1 && row->request_flags==facts.request_flags);
        assert(row->attributes==facts.attributes && row->byte_enable==15 && row->micro_pc==7);
        assert(row->context==3 && row->requestor==5);
        assert(source.next(record) && source.current_ordinary_load_eligible()==true && !source.current_store_facts());
        assert(source.current_memory_facts() && (source.current_memory_facts()->attributes&fst::kMemoryRead) && source.current_memory_facts()->context==3);
        assert(!source.next(record));
    }
    {
        InstructionSliceTraceSource slice(std::make_unique<BinaryTraceSource>(path),1,1);
        TraceRecord record;assert(slice.store_facts_complete()&&slice.next(record));
        assert(slice.current_store_facts()->record_ordinal==1);
    }
    const auto legacy=(dir/"legacy.fst").string();
    {BinaryTraceWriter w(legacy,0);w.append(store);w.close();}
    {BinaryTraceSource s(legacy);TraceRecord r;assert(!s.store_facts_complete());assert(s.next(r)&&!s.current_store_facts());}
    // Payload/hash corruption and missing declared companion fail closed.
    {
        std::fstream out(path+".sreq",std::ios::binary|std::ios::in|std::ios::out);
        out.seekp(sizeof(fst::StoreRequestHeader)+8);std::uint32_t bad=0;out.write(reinterpret_cast<const char*>(&bad),4);
    }
    rejects([&]{BinaryTraceSource s(path);TraceRecord r;while(s.next(r)){};});
    std::filesystem::remove(path+".sreq");
    rejects([&]{BinaryTraceSource s(path);});
    std::filesystem::remove_all(dir);
    std::cout << "FST functional store facts identity/legacy gates passed\n";
}
