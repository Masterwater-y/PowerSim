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
    uint64_t addressSpaceId,
    Addr virtualAddress,
    Addr physicalAddress,
    uint64_t size,
    Addr pc)
{
    fatal_if(addressSpaceId == 0,
             "QEMU page mapping has no address-space identity at pc=%#x",
             pc);
    const uint64_t pageOffset =
        virtualAddress & (kFastSimPageSize - 1);
    fatal_if((physicalAddress & (kFastSimPageSize - 1)) != pageOffset,
             "QEMU virtual/physical page offsets differ at pc=%#x "
             "vaddr=%#x paddr=%#x",
             pc, virtualAddress, physicalAddress);
    const uint64_t virtualPage = virtualAddress / kFastSimPageSize;
    const uint64_t physicalPage = physicalAddress / kFastSimPageSize;
    auto &addressSpacePages = pages[addressSpaceId];
    auto [page, inserted] = addressSpacePages.try_emplace(
        virtualPage, Page{physicalPage, 0, false});
    if (!inserted && page->second.physicalPage != physicalPage) {
        page->second.physicalPage = physicalPage;
        page->second.token = 0;
        page->second.mappingPublished = false;
    }

    QemuAddressResolution result{physicalAddress, virtualPage, 0};
    if (crossesPage(virtualAddress, size)) {
        return result;
    }
    if (page->second.token == 0) {
        fatal_if(nextToken == 0 ||
                     nextToken >= fastsim::kDestinationClassCountsMarker,
                 "virtual page token overflow");
        page->second.token = nextToken++;
    }
    result.token = page->second.token;
    return result;
}

QemuAddressResolution
QemuAddressResolver::resolve(
    uint64_t addressSpaceId,
    Addr virtualAddress,
    Addr evidencePhysicalAddress,
    uint64_t size,
    Addr pc)
{
    fatal_if(addressSpaceId == 0,
             "QEMU page resolution has no address-space identity at pc=%#x",
             pc);
    const uint64_t virtualPage = virtualAddress / kFastSimPageSize;
    const auto addressSpace = pages.find(addressSpaceId);
    fatal_if(addressSpace == pages.end(),
             "gem5-derived memory reference has no QEMU address space "
             "at pc=%#x asid=%#x", pc, addressSpaceId);
    const auto page = addressSpace->second.find(virtualPage);
    fatal_if(page == addressSpace->second.end(),
             "gem5-derived memory reference has no QEMU page mapping "
             "at pc=%#x asid=%#x addr=%#x",
             pc, addressSpaceId, virtualAddress);
    const Addr physicalAddress =
        page->second.physicalPage * kFastSimPageSize +
        (virtualAddress & (kFastSimPageSize - 1));
    fatal_if(evidencePhysicalAddress != 0 &&
                 evidencePhysicalAddress != physicalAddress,
             "gem5-derived memory reference disagrees with QEMU physical "
             "mapping at pc=%#x addr=%#x evidence_pa=%#x mapped_pa=%#x",
             pc, virtualAddress, evidencePhysicalAddress, physicalAddress);

    QemuAddressResolution result{physicalAddress, virtualPage, 0};
    if (crossesPage(virtualAddress, size)) {
        return result;
    }
    if (page->second.token == 0) {
        fatal_if(nextToken == 0 ||
                     nextToken >= fastsim::kDestinationClassCountsMarker,
                 "virtual page token overflow");
        page->second.token = nextToken++;
    }
    result.token = page->second.token;
    return result;
}

std::optional<fastsim::VirtualPageMapping>
QemuAddressResolver::firstRecordMapping(
    uint64_t addressSpaceId,
    Addr virtualAddress,
    Addr physicalAddress,
    uint32_t token,
    uint64_t firstRecordOrdinal,
    Addr pc)
{
    fatal_if(addressSpaceId == 0 || token == 0,
             "QEMU token publication lacks identity at pc=%#x", pc);
    const uint64_t virtualPage = virtualAddress / kFastSimPageSize;
    const uint64_t physicalPage = physicalAddress / kFastSimPageSize;
    const auto addressSpace = pages.find(addressSpaceId);
    fatal_if(addressSpace == pages.end(),
             "QEMU token publication has no address space at pc=%#x "
             "asid=%#x", pc, addressSpaceId);
    const auto page = addressSpace->second.find(virtualPage);
    fatal_if(page == addressSpace->second.end() ||
                 page->second.token != token ||
                 page->second.physicalPage != physicalPage,
             "QEMU token publication disagrees with page identity at pc=%#x "
             "asid=%#x vaddr=%#x paddr=%#x token=%u",
             pc, addressSpaceId, virtualAddress, physicalAddress, token);
    if (page->second.mappingPublished) {
        return std::nullopt;
    }
    page->second.mappingPublished = true;
    return fastsim::VirtualPageMapping{
        token,
        firstRecordOrdinal,
        virtualPage,
        physicalPage,
        true,
    };
}

} // namespace X86ISA
} // namespace gem5
