#include <cassert>
#include <memory>
#include <stdexcept>
#include <vector>
#include "base/extensible.hh"
#include "mem/functional_translation.hh"

struct FakeRequest : gem5::Extensible<gem5::Request> {
    uint64_t va=0x4010, pa=0x9010;
    uint32_t size=16;
    bool virtual_valid=true;
    bool hasVaddr() const { return virtual_valid; }
    uint64_t getVaddr() const {
        if (!virtual_valid) throw std::logic_error("physical-only request has no VA");
        return va;
    }
    uint64_t getPaddr() const { return pa; }
    uint32_t getSize() const { return size; }
};

int main()
{
    using namespace fastsim::fst;
    // KVM MMIO finalization is physical-only and must not observe a VA.
    auto physical_only = std::make_shared<FakeRequest>();
    physical_only->virtual_valid = false;
    gem5::recordFunctionalSpecialTranslation(physical_only, TranslationKind::kLocalApic, 0);
    assert(!physical_only->getExtension<gem5::FunctionalTranslationExtension>());
    TranslationMapping map;
    gem5::beginFunctionalTranslationWalk(map, 0x1000);
    for (uint64_t address : {0x1008, 0x2010, 0x3018, 0x4020})
        gem5::recordFunctionalTranslationPte(map, address);
    gem5::completeFunctionalTranslationLeaf(map, 0x4000, 0x9000, 12, true);
    assert(map.levels == 4 && map.walk_root == 0x1000);
    assert(map.flags == (kTranslationLeafKnown | kTranslationPathKnown |
                         kTranslationGlobal));
    assert(map.pte_addresses[3] == 0x4020);
    auto req = std::make_shared<FakeRequest>();
    auto ext = std::make_shared<gem5::FunctionalTranslationExtension>();
    ext->fragment = {req->va, req->pa, req->size, map};
    ext->fragment.mapping.address_space_id = 0x7000;
    ext->fragment.mapping.pcid = 3;
    req->setExtension(ext);
    auto copy = std::make_shared<FakeRequest>(*req);
    ext->fragment.mapping.pte_addresses[3] = 0x5000;
    std::vector<TranslationFragment> fragments;
    gem5::appendFunctionalTranslation(copy, fragments);
    assert(fragments.size() == 1);
    assert(fragments[0].mapping.pte_addresses[3] == 0x4020);
    assert(fragments[0].mapping.address_space_id == 0x7000);
    assert(fragments[0].mapping.walk_root == 0x1000);
    assert(fragments[0].mapping.pcid == 3);
    // Encoded APIC PA must preserve the actual paged geometry and PTE path.
    copy->pa = 0xa010;
    gem5::recordFunctionalSpecialTranslation(copy, TranslationKind::kLocalApic, 0x7000);
    fragments.clear();
    gem5::appendFunctionalTranslation(copy, fragments);
    assert(fragments[0].mapping.physical_base == 0x9000);
    assert(fragments[0].mapping.pte_addresses[3] == 0x4020);
    assert(fragments[0].mapping.page_bits == 12);
    assert(fragments[0].mapping.request_physical_base == 0xa010);
    assert(fragments[0].mapping.request_virtual_base == copy->va);
    for (auto kind : {TranslationKind::kIo, TranslationKind::kMsr}) {
        auto special = std::make_shared<FakeRequest>();
        gem5::recordFunctionalSpecialTranslation(special, kind, 0x7000);
        const auto &m = special->getExtension<gem5::FunctionalTranslationExtension>()->fragment.mapping;
        assert(m.kind == kind && m.flags == 0 && m.page_bits == 0 && m.levels == 0);
        assert(m.walk_root == 0 && m.physical_base == 0 && m.pte_addresses[0] == 0);
    }
    fragments.clear();
    gem5::appendFunctionalTranslation(copy, fragments);
    // A request without observed translation remains explicit unknown.
    copy->removeExtension<gem5::FunctionalTranslationExtension>();
    copy->va += 16; copy->pa += 16;
    gem5::appendFunctionalTranslation(copy, fragments);
    assert(fragments[1].virtual_address == 0x4020);
    assert(fragments[1].mapping.flags == 0);
    // Completion order does not reorder accesses; partial faults retain only
    // the successful prefix and a retry replaces rather than appends facts.
    std::vector<std::shared_ptr<FakeRequest>> requests = {req, copy};
    gem5::replaceFunctionalTranslationPrefix(requests, 2, fragments);
    assert(fragments.size() == 2);
    assert(fragments[0].virtual_address == 0x4010);
    assert(fragments[1].virtual_address == 0x4020);
    gem5::replaceFunctionalTranslationPrefix(requests, 1, fragments);
    assert(fragments.size() == 1);
    gem5::replaceFunctionalTranslationPrefix(requests, 0, fragments);
    assert(fragments.empty());
    // A reused walker must discard all earlier page/path facts.
    gem5::beginFunctionalTranslationWalk(map, 0x8000);
    gem5::recordFunctionalTranslationPte(map, 0x8008);
    gem5::recordFunctionalTranslationPte(map, 0x9010);
    gem5::recordFunctionalTranslationPte(map, 0xa018);
    gem5::completeFunctionalTranslationLeaf(map, 0, 0x200000, 21, false);
    assert(map.levels == 3 && map.page_bits == 21);
    assert(map.pte_addresses[3] == 0 && !(map.flags & kTranslationGlobal));
    gem5::beginFunctionalTranslationWalk(map, 0x1000);
    for (uint64_t address : {0x1008, 0x2010, 0x3018, 0x5020})
        gem5::recordFunctionalTranslationPte(map, address);
    gem5::completeFunctionalTranslationLeaf(map, 0x4000, 0x9000, 12, true);
    assert(map.levels == 4 && map.page_bits == 12);
    // Same VA/PA as the original walk, different actual leaf PTE location.
    assert(map.pte_addresses[3] == 0x5020);
    assert(fragments.empty());
    // A restored TLB entry can report its leaf but cannot invent a path.
    map = {};
    gem5::completeFunctionalTranslationLeaf(map, 0, 0x200000, 21, false);
    assert(map.flags == kTranslationLeafKnown && map.levels == 0);
}
