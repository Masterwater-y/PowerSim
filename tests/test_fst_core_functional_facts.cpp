#include "fastsim/trace.hpp"
#include "fastsim/fst_core_functional_facts.hpp"
#include <cassert>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <unistd.h>
using namespace fastsim;
template<class F>void rejects(F f,const std::string& label="expected rejection"){bool yes=false;try{f();}catch(const std::exception&){yes=true;}if(!yes)std::cerr<<label<<" did not reject\n";assert(yes);}
std::vector<unsigned char> unpack(const std::string& path){
    auto f=gzopen(path.c_str(),"rb");assert(f);std::vector<unsigned char> v;unsigned char b[4096];int n;
    while((n=gzread(f,b,sizeof(b)))>0)v.insert(v.end(),b,b+n);
    assert(n==0);assert(gzclose(f)==Z_OK);return v;
}
void pack(const std::string& path,const std::vector<unsigned char>& v){auto f=gzopen(path.c_str(),"wb1");assert(f);
    assert(gzwrite(f,v.data(),v.size())==static_cast<int>(v.size()));assert(gzclose(f)==Z_OK);}

// Real source fixtures: the final ordinal is deliberately beyond a one-record
// slice. Earlier records remain independently valid and need no dependencies.
void write_operand_fixture(const std::string& path,const fst::CoreFunctionalHeader& header,
                           const std::vector<fst::CoreFunctionalOperand>& final_ops,
                           const std::vector<std::uint32_t>& distances) {
    BinaryTraceWriter w(path,0);w.enable_complete_dependencies();w.enable_raw_syscall_microops();
    w.enable_functional_retirement();w.enable_frontend_facts();w.enable_static_uop_facts(header.policy_sha256);
    w.enable_core_functional_facts(header);
    for(unsigned i=0;i<6;++i) {
        const auto ops=i==5?final_ops:std::vector<fst::CoreFunctionalOperand>{};
        TraceRecord hot;hot.pc=16384+i;hot.op_class=1;hot.n_src=static_cast<std::uint8_t>(ops.size());
        std::vector<std::uint32_t> extra;
        if(i==5)for(unsigned j=0;j<distances.size();++j) {
            if(j<4)hot.producer_dists[j]=distances[j];else extra.push_back(distances[j]);
        }
        fst::StaticUopFactsRow stat;stat.flags=fst::kStaticOperandRolesKnown;
        stat.source_count=static_cast<std::uint16_t>(ops.size());
        std::vector<fst::StaticUopOperand> static_ops;
        for(unsigned j=0;j<ops.size();++j) {
            fst::StaticUopOperand op;op.consumer_operand=static_cast<std::uint16_t>(j);
            const auto cls=ops[j].original_class;op.reg_class=cls==6?3:cls<3?cls:255;
            if(j<distances.size())op.producer_distance=distances[j];
            static_ops.push_back(op);
        }
        fst::RetiredFrontendMacro m;m.begin_record=i;m.end_record=i+1;m.pc=hot.pc;
        m.bytes[0]=0x90;m.instruction_size=1;m.observed_uops=1;m.code_identity=11;m.semantic_identity=19;
        m.flags=fst::kFrontendComplete|fst::kFrontendStartsMacro|fst::kFrontendDsbEligible;
        m.known_flags=fst::kFrontendRequiredKnown;
        fst::CoreFunctionalRow row;row.record_ordinal=i;row.macro_begin=i;row.pc=hot.pc;
        row.code_identity=m.code_identity;row.semantic_prefix_identity=m.semantic_identity;
        row.source_count=stat.source_count;row.operand_count=static_cast<std::uint32_t>(ops.size());
        row.known_flags=fst::kCoreOperandsKnown|fst::kCorePinsKnown|fst::kCoreNormalizationKnown;
        row.normalization_status=fst::CoreNormalizationStatus::Complete;
        w.append(hot,nullptr,extra,fst::MemoryRequestRole::Data,0,false,false,nullptr,&stat,&static_ops,&row,&ops);
        w.register_frontend_macro(m);
    }
    w.close();
    BinaryTraceSource source(path);TraceRecord record;unsigned count=0;
    while(source.next(record))++count;
    assert(count==6);
}
// Layout 1 is bound to the actual x86 RegClass definitions by the native
// header compile gate. Exercise its highest integer slot through the reader.
void layout_regressions(const std::filesystem::path& dir,const fst::CoreFunctionalHeader& header) {
    unsigned failures=0;
    try {fst::validate_core_header(header,0);}
    catch(const std::exception& e) {++failures;std::cerr<<"LAYOUT RED: actual 39-slot header rejected: "<<e.what()<<'\n';}
    auto old=header;old.architectural_registers[0]=38;
    try {fst::validate_core_header(old,0);++failures;std::cerr<<"LAYOUT RED: fabricated 38-slot header accepted\n";}
    catch(const std::exception&) {}
    assert(failures==0);
    fst::CoreFunctionalRow row;row.source_count=1;row.operand_count=1;
    row.known_flags=fst::kCoreOperandsKnown|fst::kCoreNormalizationKnown;
    row.normalization_status=fst::CoreNormalizationStatus::Complete;
    fst::CoreFunctionalOperand op;op.original_class=op.flattened_class=0;
    op.original_index=op.flattened_index=header.architectural_registers[0]-1;
    op.flags=fst::kCoreOriginalKnown|fst::kCoreFlatKnown|fst::kCoreOriginalIsFlat;
    op.normalization_origin=fst::CoreNormalizationOrigin::RenameOperand;
    fst::validate_core_row(header,row,{op});
    const auto path=(dir/"integer-last-slot.fst").string();
    write_operand_fixture(path,header,{op},{});
    auto bad=op;++bad.original_index;++bad.flattened_index;
    rejects([&]{fst::validate_core_row(header,row,{bad});},"integer one-past-end");
    op.flags&=~fst::kCoreOriginalIsFlat;op.original_index|=64;
    fst::validate_core_row(header,row,{op});
    bad=op;--bad.flattened_index;
    rejects([&]{fst::validate_core_row(header,row,{bad});},"last-slot wrong IntFold");
    bad=op;bad.flags|=fst::kCoreOriginalIsFlat;
    rejects([&]{fst::validate_core_row(header,row,{bad});},"already-flat folded integer");
    bad=op;++bad.original_index;
    rejects([&]{fst::validate_core_row(header,row,{bad});},"folded integer one-past-end");
    auto bytes=unpack(path+".core.gz");const std::uint32_t stale_count=38;
    std::memcpy(bytes.data()+64,&stale_count,4);pack(path+".core.gz",bytes);
    rejects([&]{InstructionSliceTraceSource slice(std::make_unique<BinaryTraceSource>(path),0,1);
        TraceRecord r;assert(slice.next(r));},"fabricated source layout before slice");
}
void review_regressions(const std::filesystem::path& dir,const fst::CoreFunctionalHeader& header) {
    unsigned failures=0;
    const auto rejected=[&](auto action,const std::string& name) {
        try {action();++failures;std::cerr<<"REVIEW RED: "<<name<<" accepted\n";}
        catch(const std::exception&) {std::cout<<"review rejection: "<<name<<'\n';}
    };
    const auto operand=[](unsigned cls,unsigned original,unsigned flat,bool already_flat=false) {
        fst::CoreFunctionalOperand op;op.original_class=op.flattened_class=static_cast<std::uint8_t>(cls);
        op.original_index=original;op.flattened_index=flat;
        op.flags=fst::kCoreOriginalKnown|fst::kCoreFlatKnown|(already_flat?fst::kCoreOriginalIsFlat:0);
        op.normalization_origin=fst::CoreNormalizationOrigin::RenameOperand;return op;
    };
    const std::vector<fst::CoreFunctionalOperand> originals={
        operand(6,2,2,true),operand(7,10,10,true),operand(0,65,1),operand(1,20,20)};
    const std::vector<std::string> names={"already-flat CC","already-flat Misc","IntFold","ordinary Float"};
    for(unsigned i=0;i<originals.size();++i) {
        auto op=originals[i];fst::CoreFunctionalRow row;row.source_count=1;row.operand_count=1;
        row.known_flags=fst::kCoreOperandsKnown|fst::kCoreNormalizationKnown;
        row.normalization_status=fst::CoreNormalizationStatus::Complete;
        fst::validate_core_row(header,row,{op});
        ++op.flattened_index;
        rejected([&]{fst::validate_core_row(header,row,{op});},names[i]+" codec");
        const auto path=(dir/("register-"+std::to_string(i)+".fst")).string();
        write_operand_fixture(path,header,{originals[i]},{});
        auto bytes=unpack(path+".core.gz");
        // Five empty rows/macros precede the last row and its sole operand.
        const std::size_t flat_offset=96+5*(1+128+1+80)+1+128+8;
        ++bytes[flat_offset];
        const auto hash=fst::core_stream_hash(fst::kCoreHashSeed,bytes.data()+96,bytes.size()-96-41);
        std::memcpy(bytes.data()+bytes.size()-8,&hash,8);pack(path+".core.gz",bytes);
        rejected([&]{InstructionSliceTraceSource slice(std::make_unique<BinaryTraceSource>(path),0,1);
            TraceRecord r;assert(slice.next(r));},names[i]+" repaired checksum after slice");
    }
    // TOP is captured at rename: all eight actual x87 stack mappings remain
    // possible, while an uncaptured operand stays explicitly Unknown.
    fst::CoreFunctionalRow row;row.source_count=1;row.operand_count=1;
    row.known_flags=fst::kCoreOperandsKnown|fst::kCoreNormalizationKnown;
    row.normalization_status=fst::CoreNormalizationStatus::Complete;
    for(unsigned top=0;top<8;++top)fst::validate_core_row(header,row,{operand(1,50,(top+2)%8)});
    auto unknown=operand(1,50,0);unknown.flattened_class=255;unknown.flags=fst::kCoreOriginalKnown;
    unknown.normalization_origin=fst::CoreNormalizationOrigin::Unknown;
    row.known_flags=fst::kCoreOperandsKnown;row.normalization_status=fst::CoreNormalizationStatus::Unknown;
    fst::validate_core_row(header,row,{unknown});
    for(bool extension:{false,true}) {
        const auto path=(dir/(extension?"raw-extension.fst":"raw-inline.fst")).string();
        std::vector<fst::CoreFunctionalOperand> ops;std::vector<std::uint32_t> distances;
        for(unsigned i=0;i<(extension?5u:1u);++i) {
            auto op=operand(0,i,i);op.operand_index=static_cast<std::uint16_t>(i);ops.push_back(op);distances.push_back(i+1);
        }
        write_operand_fixture(path,header,ops,distances);
        auto bytes=unpack(path+".uops.gz");
        const std::size_t offset=64+5*64+64+(extension?4:0)*8;
        const std::uint32_t zero=0;std::memcpy(bytes.data()+offset,&zero,4);pack(path+".uops.gz",bytes);
        // Distance zero is structurally valid; every other file/hash is intact.
        rejected([&]{InstructionSliceTraceSource slice(std::make_unique<BinaryTraceSource>(path),0,1);
            TraceRecord r;assert(slice.next(r));},extension?"extension RAW after slice":"inline RAW after slice");
    }
    assert(failures==0);
}
int main(int argc,char**){
    const auto dir=std::filesystem::path("tmp")/("core-functional-"+std::to_string(getpid()));
    std::filesystem::create_directories(dir);const auto path=(dir/"trace.fst").string();
    std::array<std::uint8_t,32> policy{};policy.fill(0x42);
    fst::CoreFunctionalHeader header;header.policy_sha256=policy;
    header.architectural_registers={39,48,1,2,1,0,6,512};
    layout_regressions(dir,header);
    review_regressions(dir,header);
    {
        BinaryTraceWriter w(path,0);w.enable_complete_dependencies();w.enable_raw_syscall_microops();
        w.enable_functional_retirement();w.enable_frontend_facts();w.enable_static_uop_facts(policy);
        w.enable_core_functional_facts(header);
        for(unsigned i=0;i<3;++i){
            TraceRecord hot;hot.pc=4096+i;hot.op_class=1;
            fst::RetiredFrontendMacro m;m.begin_record=i;m.end_record=i+1;m.pc=hot.pc;
            m.bytes[0]=0x90;m.instruction_size=1;m.observed_uops=1;
            m.flags=fst::kFrontendComplete|fst::kFrontendStartsMacro|fst::kFrontendDsbEligible;
            m.known_flags=fst::kFrontendRequiredKnown;
            const std::uint64_t context=0x31;
            m.code_identity=fst::frontend_hash(fst::frontend_hash(fst::kFrontendHashSeed,&context,8),m.bytes.data(),1);
            m.semantic_identity=100+i;
            fst::StaticUopFactsRow uop;std::vector<fst::StaticUopOperand> uops;
            fst::CoreFunctionalRow row;row.record_ordinal=i;row.macro_begin=i;row.pc=hot.pc;
            row.code_identity=m.code_identity;row.semantic_prefix_identity=m.semantic_identity;
            row.known_flags=fst::kCoreDecoderKnown|fst::kCoreFrontendKnown|fst::kCoreStaticKnown|fst::kCoreOperandsKnown|fst::kCorePinsKnown;
            row.decoder_context=context;row.decoder_origin=fst::CoreDecoderOrigin::ExactByteWitness;
            row.frontend_asid=0x5000;row.frontend_from_user=1;row.frontend_origin=fst::CoreFrontendOrigin::ObserverRecord;
            std::vector<fst::CoreFunctionalOperand> operands;
            rejects([&]{w.append(hot,nullptr,{},fst::MemoryRequestRole::Data,0,false,false,nullptr,&uop,&uops);});
            assert(w.record_count()==i);
            w.append(hot,nullptr,{},fst::MemoryRequestRole::Data,0,false,false,nullptr,&uop,&uops,&row,&operands);
            w.register_frontend_macro(m);
        }
        w.close();
    }
    {
        BinaryTraceSource source(path);assert(source.core_functional_version()==1);
        assert(source.core_functional_header()->architectural_registers[2]==1);
        assert(source.core_functional_coverage()->records==3);
        assert(source.core_functional_coverage()->normalization[0]==3);
        TraceRecord r;for(unsigned i=0;i<3;++i){assert(source.next(r));assert(source.current_core_functional_facts()->record_ordinal==i);}
        assert(!source.next(r));assert(!source.current_core_functional_facts());assert(source.current_core_functional_operands().empty());
    }
    {
        InstructionSliceTraceSource slice(std::make_unique<BinaryTraceSource>(path),1,1);TraceRecord r;
        assert(slice.next(r));assert(slice.current_core_functional_facts()->record_ordinal==1);
        assert(!slice.next(r));assert(!slice.current_core_functional_facts());
    }
    {
        ScoreRangeTraceSource slice(std::make_unique<BinaryTraceSource>(path),1,2,2);TraceRecord r;
        assert(slice.next(r));assert(slice.next(r));assert(!slice.next(r));assert(!slice.current_core_functional_facts());
    }
    {
        WarmupInstructionTraceSource warm(std::make_unique<BinaryTraceSource>(path),1,1,1,1,true);TraceRecord r;
        assert(warm.next(r));assert(warm.current_core_functional_facts()->record_ordinal==0);
        assert(!warm.next(r));assert(!warm.current_core_functional_facts());warm.start_measurement();
        assert(warm.next(r));assert(warm.current_core_functional_facts()->record_ordinal==1);
        assert(!warm.next(r));assert(!warm.current_core_functional_facts());
    }
    const auto converted=(dir/"converted.fst").string();
    rejects([&]{upgrade_binary_trace_to_v7(path,converted,SyscallAbi::kUnknown);},"maintained FST upgrade");
    assert(!std::filesystem::exists(converted));
    const auto sidecar=path+".core.gz";const auto pristine=unpack(sidecar);
    using Mutator=std::function<void(std::vector<unsigned char>&)>;
    const std::vector<Mutator> corruptions={
        [](auto&v){v[8]=2;},[](auto&v){v[60]=2;},[](auto&v){v[24]^=1;},
        [](auto&v){v[97]^=1;}, // first ordinal
        [](auto&v){v[97+64]^=1;}, // hot record hash
        [](auto&v){v[97+84]=1;}, // invalid operand count
        [](auto&v){v[97+96]=1;}, // reserved
        [](auto&v){v[97+69]|=128;}, // unknown known bit
        [](auto&v){v[97+89]=1;}, // candidate mask without known
        [](auto&v){v[97+80]=255;v[97+81]=255;}, // bounded allocation
        [](auto&v){v[96+129+1+8]^=1;}, // macro end
        [](auto&v){v[v.size()-40+8]^=1;},[](auto&v){v[v.size()-1]^=1;},
        [](auto&v){v.pop_back();},[](auto&v){v.push_back(0);},
        [](auto&v){v[96+2*(129+81)+1+64]^=1;}, // corruption AFTER slice's returned prefix
        [](auto&v){
            // Self-consistent payload checksum cannot legalize a forged
            // out-of-slice decoder context against independent .ffe bytes.
            v[96+2*(129+81)+1+48]^=1;
            const auto hash=fst::core_stream_hash(fst::kCoreHashSeed,v.data()+96,v.size()-96-41);
            std::memcpy(v.data()+v.size()-8,&hash,8);
        }
    };
    unsigned corruption=0;
    for(const auto& mutate:corruptions){auto bad=pristine;mutate(bad);pack(sidecar,bad);
        rejects([&]{InstructionSliceTraceSource slice(std::make_unique<BinaryTraceSource>(path),0,1);TraceRecord r;slice.next(r);},"corruption "+std::to_string(corruption++));}
    pack(sidecar,pristine);
    {std::ofstream f(sidecar,std::ios::binary|std::ios::app);f.put('x');}
    rejects([&]{BinaryTraceSource source(path);});pack(sidecar,pristine);
    {std::fstream f(path,std::ios::binary|std::ios::in|std::ios::out);f.seekg(32);std::uint64_t flags=0;
     f.read(reinterpret_cast<char*>(&flags),8);flags&=~fst::kCoreFunctionalFacts;f.seekp(32);f.write(reinterpret_cast<char*>(&flags),8);}
    rejects([&]{BinaryTraceSource source(path);},"undeclared sidecar");
    // Legacy feature-OFF input remains accepted and never gains core facts.
    const auto old=(dir/"old.fst").string();{BinaryTraceWriter w(old,0);TraceRecord r;r.pc=4096;w.append(r);w.close();}
    {BinaryTraceSource source(old);TraceRecord r;assert(source.next(r));assert(!source.core_functional_header());}
    // A common-end cut may retain a long REP/microcode prefix. The writer
    // streams every UOP and binds the incomplete macro without buffering it.
    const auto partial=(dir/"partial.fst").string();
    {
        BinaryTraceWriter w(partial,0);w.enable_complete_dependencies();w.enable_raw_syscall_microops();
        w.enable_functional_retirement();w.enable_frontend_facts();w.enable_static_uop_facts(policy);
        w.enable_core_functional_facts(header);
        TraceRecord hot;hot.pc=8192;hot.flags|=kMicroOp;
        fst::StaticUopFactsRow static_row;std::vector<fst::StaticUopOperand> static_ops;
        fst::CoreFunctionalRow row;row.pc=8192;row.code_identity=11;row.semantic_prefix_identity=19;
        std::vector<fst::CoreFunctionalOperand> operands;
        for(unsigned i=0;i<1582;++i)
            w.append(hot,nullptr,{},fst::MemoryRequestRole::Data,0,false,false,nullptr,&static_row,&static_ops,&row,&operands);
        fst::RetiredFrontendMacro m;m.begin_record=0;m.end_record=1582;m.pc=8192;m.code_identity=11;m.semantic_identity=19;
        m.bytes[0]=0x90;m.instruction_size=1;m.observed_uops=1582;
        m.flags=fst::kFrontendStartsMacro|fst::kFrontendMicrocode;m.known_flags=fst::kFrontendRequiredKnown;
        w.register_frontend_macro(m);w.close();
    }
    {BinaryTraceSource source(partial);TraceRecord r;unsigned count=0;while(source.next(r)){
        assert(source.current_core_functional_facts()->macro_begin==0);++count;}
     assert(count==1582 && source.core_functional_coverage()->macros==1);}
    if(argc==1)std::filesystem::remove_all(dir);
    else std::cout<<"retained partial fixture: "<<partial<<'\n';
    std::cout<<"core functional full-source binding and wrapper tests passed\n";
}
