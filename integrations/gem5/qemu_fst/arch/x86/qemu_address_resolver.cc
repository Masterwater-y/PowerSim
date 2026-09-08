#include "arch/x86/qemu_address_resolver.hh"

#include <cinttypes>

#include "base/logging.hh"
#include "fastsim/types.hpp"

namespace gem5
{
namespace X86ISA
{
namespace
{

constexpr uint64_t kFastSimPageSize = 4096;

bool
crossesPage(Addr address, uint64_t size)
{
    return (address & (kFastSimPageSize - 1)) + size >
        kFastSimPageSize;
}

} // namespace

QemuAddressResolution
QemuAddressResolver::observe(
    Addr virtualAddress,
    Addr physicalAddress,
    uint64_t size,
    uint64_t firstRecordOrdinal,
    Addr pc)
{
    const uint64_t pageOffset =
        virtualAddress & (kFastSimPageSize - 1);
    fatal_if((physicalAddress & (kFastSimPageSize - 1)) != pageOffset,
             "QEMU virtual/physical page offsets differ at pc=%#x "
             "vaddr=%#x paddr=%#x",
             pc, virtualAddress, physicalAddress);
    const uint64_t virtualPage = virtualAddress / kFastSimPageSize;
    const uint64_t physicalPage = physicalAddress / kFastSimPageSize;
    auto [page, inserted] = pages.try_emplace(
        virtualPage, Page{physicalPage, 0});
    if (!inserted && page->second.physicalPage != physicalPage) {
        page->second.physicalPage = physicalPage;
        page->second.token = 0;
    }

    QemuAddressResolution result{
        physicalAddress, virtualPage, 0, std::nullopt};
    if (crossesPage(virtualAddress, size)) {
        return result;
    }
    if (page->second.token == 0) {
        fatal_if(nextToken == 0 ||
                     nextToken >= fastsim::kDestinationClassCountsMarker,
                 "virtual page token overflow");
        page->second.token = nextToken++;
        result.newMapping = fastsim::VirtualPageMapping{
            page->second.token,
            firstRecordOrdinal,
            virtualPage,
            physicalPage,
            true,
        };
    }
    result.token = page->second.token;
    return result;
}

QemuAddressResolution
QemuAddressResolver::resolve(
    Addr virtualAddress,
    Addr evidencePhysicalAddress,
    uint64_t size,
    uint64_t firstRecordOrdinal,
    Addr pc)
{
    const uint64_t virtualPage = virtualAddress / kFastSimPageSize;
    const auto page = pages.find(virtualPage);
    fatal_if(page == pages.end(),
             "gem5-derived memory reference has no QEMU page mapping "
             "at pc=%#x addr=%#x",
             pc, virtualAddress);
    const Addr physicalAddress =
        page->second.physicalPage * kFastSimPageSize +
        (virtualAddress & (kFastSimPageSize - 1));
    fatal_if(evidencePhysicalAddress != 0 &&
                 evidencePhysicalAddress != physicalAddress,
             "gem5-derived memory reference disagrees with QEMU physical "
             "mapping at pc=%#x addr=%#x evidence_pa=%#x mapped_pa=%#x",
             pc, virtualAddress, evidencePhysicalAddress, physicalAddress);

    QemuAddressResolution result{
        physicalAddress, virtualPage, 0, std::nullopt};
    if (crossesPage(virtualAddress, size)) {
        return result;
    }
    if (page->second.token == 0) {
        fatal_if(nextToken == 0 ||
                     nextToken >= fastsim::kDestinationClassCountsMarker,
                 "virtual page token overflow");
        page->second.token = nextToken++;
        result.newMapping = fastsim::VirtualPageMapping{
            page->second.token,
            firstRecordOrdinal,
            virtualPage,
            page->second.physicalPage,
            true,
        };
    }
    result.token = page->second.token;
    return result;
}

} // namespace X86ISA
} // namespace gem5
