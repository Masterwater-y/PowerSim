/* Ordered external observations and a temporary store overlay for one frame. */
#ifndef __ARCH_X86_QEMU_MEMORY_VIEW_HH__
#define __ARCH_X86_QEMU_MEMORY_VIEW_HH__

#include <array>
#include <cstddef>
#include <cstdint>
#include <unordered_map>
#include <vector>

namespace gem5
{
namespace X86ISA
{

struct QemuDataRef
{
    bool is_store = false;
    uint64_t vaddr = 0; // Guest VA for RAM/MMIO; port number for PIO.
    uint64_t paddr = 0;
    uint64_t virtual_page = 0;
    uint32_t virtual_page_token = 0;
    uint64_t size = 0;
    uint32_t attributes = 0;
    std::array<uint8_t, 16> value = {};
    uint8_t valueSize = 0;
    bool hasValue = false;
};

class QemuFrameMemory
{
  public:
    void reset(const std::vector<QemuDataRef> &observations);
    std::vector<QemuDataRef> access(uint64_t address, unsigned size,
                                    bool store, uint8_t *data,
                                    uint32_t accessAttributes = 0);
    void finish() const;

  private:
    struct MemoryByte
    {
        uint8_t value = 0;
        uint64_t physicalAddress = 0;
        uint32_t attributes = 0;
    };
    // Address-keyed byte entry. Frame RAM and RAM mappings are built once in
    // reset() and only point-queried afterward, so they use a sorted flat
    // vector with binary search instead of a per-byte red-black tree node.
    struct AddressedByte
    {
        uint64_t address = 0;
        MemoryByte byte;
    };
    // Returns the entry for `address`, or nullptr. `entries` is sorted by
    // address with unique keys.
    static const MemoryByte *findByte(
        const std::vector<AddressedByte> &entries, uint64_t address);
    // Sorts collected byte entries by address and collapses duplicate
    // addresses, invoking onConflict(kept, duplicate) for each collision.
    template <typename OnConflict>
    static void finalizeAddressedBytes(
        std::vector<AddressedByte> &entries, OnConflict onConflict);
    const std::vector<QemuDataRef> *source = nullptr;
    std::vector<AddressedByte> frameRam;
    std::vector<size_t> ioAccesses;
    size_t nextIoAccess = 0;
    std::vector<AddressedByte> ramMappings;
    std::unordered_map<uint64_t, MemoryByte> overlay;
};

} // namespace X86ISA
} // namespace gem5
#endif
