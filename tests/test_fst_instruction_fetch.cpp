#include "fastsim/fst_instruction_fetch.hpp"
#include <iostream>
#include <stdexcept>
using namespace fastsim::fst;
namespace {
void check(bool value,const char* what){if(!value)throw std::runtime_error(what);}
TranslationFragment piece(std::uint64_t va,std::uint64_t pa,unsigned bytes,std::uint64_t root=0x1000,bool path=true) {
    TranslationFragment f;f.virtual_address=va;f.physical_address=pa;f.size=bytes;
    auto& m=f.mapping;m.address_space_id=m.walk_root=root;m.virtual_base=va&~4095ull;m.physical_base=pa&~4095ull;
    m.page_bits=12;m.flags=kTranslationLeafKnown|(path?kTranslationPathKnown:0);m.levels=path?4:0;
    if(path)m.pte_addresses={0x1100,0x2100,0x3100,0x4100,0,0,0,0};
    return f;
}
void tests(){
    // Recorded entry addresses have no entry-width field; native supports both
    // four- and eight-byte entries, and path presence is explicit metadata.
    for(const auto address:std::array<std::uint64_t,2>{0,0x1104}) {
        InstructionByteWitness geometry;geometry.begin(0x1000,0x1000);
        auto recorded=piece(0x1000,0x9000,8);recorded.mapping.pte_addresses[0]=address;
        geometry.observe(recorded);const auto facts=geometry.snapshot(4);
        check(facts->status==InstructionFetchStatus::Mapped && facts->path_complete &&
              facts->fragments[0].mapping.pte_addresses[0]==address,
              address?"four-byte-aligned native PTE must retain known path":"physical-zero native PTE must retain known path");
    }
    InstructionByteWitness misaligned;misaligned.begin(0x1000,0x1000);
    auto invalid_entry=piece(0x1000,0x9000,8);invalid_entry.mapping.pte_addresses[0]=0x1102;
    misaligned.observe(invalid_entry);const auto invalid_facts=misaligned.snapshot(4);
    check(invalid_facts->status==InstructionFetchStatus::Unknown &&
          invalid_facts->reason==InstructionFetchUnknown::InconsistentGeometry &&
          invalid_facts->fragments.empty(),"misaligned native PTE must fail closed");
    InstructionByteWitness w;w.begin(0x1ffc,0x1000);
    auto first=piece(0x1ff8,0x9ff8,8);w.observe(first);
    const auto partial=w.snapshot(8);check(partial->status==InstructionFetchStatus::Unknown && partial->reason==InstructionFetchUnknown::MissingBytes,"partial decoder has no future byte proof");
    w.observe(piece(0x2000,0x15000,8));const auto full=w.snapshot(8);
    check(full->status==InstructionFetchStatus::Mapped && full->path_complete && full->fragments.size()==2,"cross-page macro preserves two paths");
    check(full->fragments[0].virtual_address==0x1ffc && full->fragments[0].physical_address==0x9ffc && full->fragments[0].size==4 && full->fragments[1].physical_address==0x15000 && full->fragments[1].size==4,"exact instruction clipping without contiguous PA guess");
    w.clear();first.mapping.physical_base=0x8000;first.physical_address=0x8ff8;w.begin(0x1ffc,0x1000);w.observe(first);
    check(full->fragments[0].physical_address==0x9ffc,"immutable macro survives lease replacement/reset");
    w.begin(0x103e,0x1000);w.observe(piece(0x1038,0x9038,8));w.observe(piece(0x1040,0x9040,8));
    auto line=w.snapshot(8);check(line->status==InstructionFetchStatus::Mapped && line->fragments.size()==1 && line->fragments[0].size==8,"two cache lines in one leaf normalize without losing path");
    w.begin(0x1000,0x1000);w.observe(piece(0x1000,0x9000,8,0x1000,false));
    const auto leaf=w.snapshot(4);check(leaf->status==InstructionFetchStatus::Mapped && !leaf->path_complete && !(leaf->fragments[0].mapping.flags&kTranslationPathKnown),"restored leaf-only is not full path");
    w.begin(0x1000,0x1000);auto unknown=piece(0x1000,0x9000,8);unknown.mapping.flags=0;w.observe(unknown);
    check(w.snapshot(4)->reason==InstructionFetchUnknown::MissingLeaf,"missing extension cannot inherit a leaf");
    w.begin(0x1000,0x1000);w.observe(piece(0x1000,0x9000,8));w.observe(piece(0x1000,0xa000,8));
    check(w.snapshot(4)->reason==InstructionFetchUnknown::ConflictingBytes,"same bytes from different mappings fail closed");
    w.begin(0x1000,0x1000);w.observe(piece(0x1000,0x9000,8));auto changed=piece(0x1000,0x9000,8);changed.mapping.pte_addresses[1]=0x3300;w.observe(changed);
    check(w.snapshot(4)->reason==InstructionFetchUnknown::ConflictingBytes,"same leaf with different table path is not identical provenance");
    w.begin(0x1000,0x1000);w.observe(piece(0x1000,0x9000,8,0x2000));
    check(w.snapshot(4)->reason==InstructionFetchUnknown::ContextMismatch,"context cannot be inferred from a matching virtual PC");
    w.begin(0x1000,0x1000);auto bad=piece(0x1000,0x9000,8);bad.mapping.physical_base=0x8000;w.observe(bad);
    check(w.snapshot(4)->reason==InstructionFetchUnknown::InconsistentGeometry,"PA and functional leaf must agree");
    w.begin(0x1000,0x1000);bad=piece(0x1000,0x9000,8);bad.mapping.levels=0;w.observe(bad);
    check(w.snapshot(4)->reason==InstructionFetchUnknown::InconsistentGeometry,"path-known without PTE steps is invalid");
    w.begin(0x103f,0x1000);w.observe(piece(0x1038,0x9038,8));w.observe(piece(0x1040,0x9040,8));w.observe(piece(0x1048,0x9048,8));
    check(w.snapshot(15)->status==InstructionFetchStatus::Mapped && w.snapshot(15)->fragments[0].size==15,"maximum x86 length over three decoder chunks");
    w.begin(0x1000,0x1000);w.observe(piece(0x1000,0x9000,8));w.observe(piece(0x1000,0x9000,8));
    check(w.snapshot(8)->status==InstructionFetchStatus::Mapped,"duplicate identical byte witness is idempotent");
    w.clear();check(w.snapshot(1)->status==InstructionFetchStatus::Unknown,"reset does not reuse same PC history");
    bool invalid=false;try{w.begin(0,0);w.snapshot(16);}catch(const std::invalid_argument&){invalid=true;}check(invalid,"x86 instruction bound enforced");
    // Only the qualified ROM-pseudo producer can request this explicit status.
    const auto pseudo=InstructionFetchFacts::pseudo(0x1000);
    check(pseudo->status==InstructionFetchStatus::NotApplicable && pseudo->fragments.empty() && !pseudo->path_complete,"pseudo carries no invented fetch");
}
}
int main(){try{tests();std::cout<<"instruction byte/translation immutable witness tests passed\n";return 0;}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
