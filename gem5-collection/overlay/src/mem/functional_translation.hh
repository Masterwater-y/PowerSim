/* Functional translation observations only; never alter target behavior. */
#ifndef __MEM_FUNCTIONAL_TRANSLATION_HH__
#define __MEM_FUNCTIONAL_TRANSLATION_HH__

#include <cstddef>
#include <memory>
#include <vector>

#include "base/extensible.hh"
#include "fst_translation_facts.hh"

namespace gem5
{
class Request;

inline void
beginFunctionalTranslationWalk(fastsim::fst::TranslationMapping &mapping,
                               uint64_t root)
{
    mapping = {};
    mapping.walk_root = root;
    mapping.flags = fastsim::fst::kTranslationPathKnown;
}

inline void
recordFunctionalTranslationPte(fastsim::fst::TranslationMapping &mapping,
                               uint64_t address)
{
    if (mapping.levels < mapping.pte_addresses.size()) {
        mapping.pte_addresses[mapping.levels++] = address;
    } else {
        mapping.flags &= ~fastsim::fst::kTranslationPathKnown;
    }
}

inline void
completeFunctionalTranslationLeaf(fastsim::fst::TranslationMapping &mapping,
    uint64_t virtual_base, uint64_t physical_base, unsigned page_bits,
    bool global)
{
    mapping.virtual_base = virtual_base;
    mapping.physical_base = physical_base;
    mapping.page_bits = page_bits;
    mapping.flags |= fastsim::fst::kTranslationLeafKnown;
    mapping.flags &= ~fastsim::fst::kTranslationGlobal;
    if (global)
        mapping.flags |= fastsim::fst::kTranslationGlobal;
}

class FunctionalTranslationExtension
    : public Extension<Request, FunctionalTranslationExtension>
{
  public:
    fastsim::fst::TranslationFragment fragment;

    std::unique_ptr<ExtensionBase>
    clone() const override
    {
        return std::make_unique<FunctionalTranslationExtension>(*this);
    }
};

// Called only by a successful source translation/finalization branch.
// Preserve real paged leaf/path facts while recording encoded request PA.
template <class RequestPointer>
inline void
recordFunctionalSpecialTranslation(const RequestPointer &request,
    fastsim::fst::TranslationKind kind, uint64_t address_space_id)
{
    // KVM MMIO requests are physical-only; observation must not require a VA.
    if (!request->hasVaddr())
        return;
    auto extension = request->template getExtension<FunctionalTranslationExtension>();
    if (!extension)
        extension = std::make_shared<FunctionalTranslationExtension>();
    auto &fragment = extension->fragment;
    auto &mapping = fragment.mapping;
    fragment.virtual_address = request->getVaddr();
    fragment.physical_address = request->getPaddr();
    fragment.size = request->getSize();
    if (!(mapping.flags & fastsim::fst::kTranslationLeafKnown)) {
        mapping = {};
        mapping.virtual_base = request->getVaddr() & ~uint64_t(4095);
    }
    mapping.kind = kind;
    mapping.address_space_id = address_space_id;
    mapping.request_physical_base = request->getPaddr();
    mapping.request_virtual_base = request->getVaddr();
    request->setExtension(extension);
}

// Called in request order after translation completion, including NO_ACCESS.
// Missing observation remains an explicit unknown fragment with actual VA/PA.
template <class RequestPointer>
inline void
appendFunctionalTranslation(const RequestPointer &request,
    std::vector<fastsim::fst::TranslationFragment> &fragments)
{
    const auto extension = request->template getExtension<
        FunctionalTranslationExtension>();
    if (extension) {
        fragments.push_back(extension->fragment);
    } else {
        fastsim::fst::TranslationFragment fragment;
        fragment.virtual_address = request->getVaddr();
        fragment.physical_address = request->getPaddr();
        fragment.size = request->getSize();
        fragments.push_back(fragment);
    }
}

template <class Requests>
inline void
replaceFunctionalTranslationPrefix(const Requests &requests,
    std::size_t successful_count,
    std::vector<fastsim::fst::TranslationFragment> &fragments)
{
    fragments.clear();
    for (std::size_t index = 0; index < successful_count; ++index)
        appendFunctionalTranslation(requests[index], fragments);
}
} // namespace gem5
#endif
