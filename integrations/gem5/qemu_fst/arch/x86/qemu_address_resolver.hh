/* QEMU virtual/physical page and FST token resolution. */

#ifndef __ARCH_X86_QEMU_ADDRESS_RESOLVER_HH__
#define __ARCH_X86_QEMU_ADDRESS_RESOLVER_HH__

#include <cstdint>
#include <optional>
#include <unordered_map>

#include "base/types.hh"
#include "fastsim/trace.hpp"

namespace gem5
{
namespace X86ISA
{

struct QemuAddressResolution
{
    Addr physicalAddress = 0;
    uint64_t virtualPage = 0;
    uint32_t token = 0;
};

class QemuAddressResolver
{
  public:
    QemuAddressResolution observe(
        uint64_t addressSpaceId,
        Addr virtualAddress,
        Addr physicalAddress,
        uint64_t size,
        Addr pc);

    QemuAddressResolution resolve(
        uint64_t addressSpaceId,
        Addr virtualAddress,
        Addr evidencePhysicalAddress,
        uint64_t size,
        Addr pc);

    std::optional<fastsim::VirtualPageMapping> firstRecordMapping(
        uint64_t addressSpaceId,
        Addr virtualAddress,
        Addr physicalAddress,
        uint32_t token,
        uint64_t firstRecordOrdinal,
        Addr pc);

  private:
    struct Page
    {
        uint64_t physicalPage = 0;
        uint32_t token = 0;
        bool mappingPublished = false;
    };

    std::unordered_map<uint64_t, std::unordered_map<uint64_t, Page>> pages;
    uint32_t nextToken = 1;
};

} // namespace X86ISA
} // namespace gem5

#endif // __ARCH_X86_QEMU_ADDRESS_RESOLVER_HH__
