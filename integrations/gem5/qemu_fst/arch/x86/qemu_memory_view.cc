#include "arch/x86/qemu_memory_view.hh"

#include <algorithm>
#include <sstream>
#include <stdexcept>
#include <string>

#include "common/trace_entry_extensions.h"

namespace gem5
{
namespace X86ISA
{
namespace
{
using namespace qemu_trace_extensions;
constexpr uint64_t kPageBytes = 4096;

[[noreturn]] void
memoryError(const char *reason, uint64_t address)
{
    std::ostringstream message;
    message << "QEMU transaction memory " << reason << " addr=0x"
            << std::hex << address;
    throw std::runtime_error(message.str());
}
}

const QemuFrameMemory::MemoryByte *
QemuFrameMemory::findByte(
    const std::vector<AddressedByte> &entries, uint64_t address)
{
    const auto found = std::lower_bound(
        entries.begin(), entries.end(), address,
        [](const AddressedByte &entry, uint64_t key) {
            return entry.address < key;
        });
    if (found == entries.end() || found->address != address) {
        return nullptr;
    }
    return &found->byte;
}

template <typename OnConflict>
void
QemuFrameMemory::finalizeAddressedBytes(
    std::vector<AddressedByte> &entries, OnConflict onConflict)
{
    if (!std::is_sorted(
            entries.begin(), entries.end(),
            [](const AddressedByte &left, const AddressedByte &right) {
                return left.address < right.address;
            })) {
        std::stable_sort(
            entries.begin(), entries.end(),
            [](const AddressedByte &left, const AddressedByte &right) {
                return left.address < right.address;
            });
    }
    size_t unique = 0;
    for (size_t index = 0; index < entries.size(); ++index) {
        if (unique > 0 &&
            entries[unique - 1].address == entries[index].address) {
            onConflict(entries[unique - 1], entries[index]);
            continue;
        }
        entries[unique++] = entries[index];
    }
    entries.resize(unique);
}

void
QemuFrameMemory::reset(const std::vector<QemuDataRef> &observations)
{
    source = &observations;
    frameRam.clear();
    ioAccesses.clear();
    nextIoAccess = 0;
    ramMappings.clear();
    overlay.clear();
    size_t frameRamBytes = 0;
    size_t ramMappingBytes = 0;
    size_t ioCount = 0;
    size_t storeBytes = 0;
    for (const auto &ref : observations) {
        const bool frameRamAccess =
            ref.attributes & kMemoryAttributeFrameRam;
        const bool ioAccess =
            ref.attributes &
            (kMemoryAttributePio | kMemoryAttributeMmio);
        frameRamBytes += frameRamAccess ? ref.size : 0;
        ramMappingBytes += ioAccess ? 0 : ref.size;
        ioCount += ioAccess;
        storeBytes += !ioAccess && ref.is_store ? ref.size : 0;
    }
    frameRam.reserve(frameRamBytes);
    ramMappings.reserve(ramMappingBytes);
    ioAccesses.reserve(ioCount);
    overlay.reserve(storeBytes);
    for (size_t index = 0; index < observations.size(); ++index) {
        const auto &ref = observations[index];
        if (!ref.size ||
            ref.size > ref.value.size() ||
            ref.vaddr > UINT64_MAX - (ref.size - 1) ||
            ref.paddr > UINT64_MAX - (ref.size - 1)) {
            memoryError("has an invalid observation range", ref.vaddr);
        }
        const bool frameRamAccess =
            ref.attributes & kMemoryAttributeFrameRam;
        const bool ioAccess =
            ref.attributes &
            (kMemoryAttributePio | kMemoryAttributeMmio);
        if (frameRamAccess) {
            if (ref.is_store || ioAccess || !ref.hasValue ||
                ref.valueSize != ref.size) {
                memoryError("has invalid frame RAM evidence", ref.vaddr);
            }
            for (size_t offset = 0; offset < ref.size; ++offset) {
                frameRam.push_back(AddressedByte{
                    ref.vaddr + offset,
                    MemoryByte{ref.value[offset], ref.paddr + offset,
                               ref.attributes}});
            }
        } else if (ioAccess) {
            ioAccesses.push_back(index);
        } else if (!ref.is_store) {
            memoryError("has a RAM load outside frame RAM", ref.vaddr);
        }
        if (!ioAccess) {
            for (size_t offset = 0; offset < ref.size; ++offset) {
                ramMappings.push_back(AddressedByte{
                    ref.vaddr + offset,
                    MemoryByte{0, ref.paddr + offset,
                               static_cast<uint32_t>(
                                   ref.attributes & ~kMemoryAttributeFrameRam)}});
            }
        }
    }
    finalizeAddressedBytes(
        frameRam,
        [](const AddressedByte &kept, const AddressedByte &duplicate) {
            if (kept.byte.value != duplicate.byte.value ||
                kept.byte.physicalAddress != duplicate.byte.physicalAddress) {
                memoryError("has conflicting frame RAM evidence",
                            kept.address);
            }
        });
    finalizeAddressedBytes(
        ramMappings,
        [](const AddressedByte &kept, const AddressedByte &duplicate) {
            if (kept.byte.physicalAddress != duplicate.byte.physicalAddress ||
                kept.byte.attributes != duplicate.byte.attributes) {
                memoryError("has conflicting RAM mapping", kept.address);
            }
        });
}

void
QemuFrameMemory::access(
    uint64_t address, unsigned size, bool store, uint8_t *data,
    std::vector<QemuDataRef> &segments,
    uint32_t accessAttributes)
{
    if (!source || !size || size > QemuDataRef{}.value.size() || !data ||
        address > UINT64_MAX - (size - 1)) {
        memoryError("has an invalid access range", address);
    }
    std::array<uint64_t, 16> bytesPaddr{};
    std::array<uint32_t, 16> byteAttributes{};
    std::array<uint8_t, 16> loadValue{};
    const bool requestedPio =
        accessAttributes & kMemoryAttributePio;
    const auto consumeIo = [&]() -> const QemuDataRef * {
        if (nextIoAccess >= ioAccesses.size()) {
            return nullptr;
        }
        const auto &ref = source->at(ioAccesses[nextIoAccess]);
        if (ref.vaddr != address || ref.size != size ||
            ref.is_store != store ||
            (ref.attributes &
             (kMemoryAttributePio | kMemoryAttributeMmio)) !=
                (accessAttributes &
                 (kMemoryAttributePio | kMemoryAttributeMmio))) {
            return nullptr;
        }
        ++nextIoAccess;
        return &ref;
    };
    if (requestedPio) {
        const auto *ref = consumeIo();
        if (!ref) memoryError("has no matching PIO observation", address);
        if (!store) {
            if (!ref->hasValue || ref->valueSize != ref->size) {
                memoryError("PIO load value is unavailable", address);
            }
            std::copy_n(ref->value.begin(), size, data);
        }
        segments.push_back(*ref);
        return;
    }
    if (nextIoAccess < ioAccesses.size()) {
        const auto &next = source->at(ioAccesses[nextIoAccess]);
        if ((next.attributes & kMemoryAttributeMmio) &&
            next.vaddr == address && next.size == size &&
            next.is_store == store) {
            accessAttributes = kMemoryAttributeMmio;
            const auto *ref = consumeIo();
            if (!store) {
                if (!ref->hasValue || ref->valueSize != ref->size) {
                    memoryError("MMIO load value is unavailable", address);
                }
                std::copy_n(ref->value.begin(), size, data);
            }
            segments.push_back(*ref);
            return;
        }
    }
    unsigned resolved = 0;
    while (resolved < size) {
        const uint64_t byteAddress = address + resolved;
        const auto stored = overlay.find(byteAddress);
        if (!store && stored != overlay.end()) {
            bytesPaddr[resolved] = stored->second.physicalAddress;
            byteAttributes[resolved] = stored->second.attributes;
            loadValue[resolved] = stored->second.value;
            ++resolved;
            continue;
        }
        if (!store) {
            const auto found = findByte(frameRam, byteAddress);
            if (found == nullptr) {
                memoryError("has no frame RAM byte", byteAddress);
            }
            bytesPaddr[resolved] = found->physicalAddress;
            byteAttributes[resolved] = found->attributes;
            loadValue[resolved] = found->value;
            ++resolved;
            continue;
        } else {
            const auto mapping = findByte(ramMappings, byteAddress);
            if (mapping == nullptr) {
                memoryError("has no mapping evidence", byteAddress);
            }
            bytesPaddr[resolved] = mapping->physicalAddress;
            byteAttributes[resolved] = mapping->attributes;
            ++resolved;
            continue;
        }
    }
    for (unsigned offset = 0; offset < size; ++offset) {
        if (store) {
            overlay[address + offset] = MemoryByte{
                data[offset], bytesPaddr[offset], byteAttributes[offset]};
        } else {
            data[offset] = loadValue[offset];
        }
    }
    unsigned segmentStart = 0;
    for (unsigned offset = 1; offset <= size; ++offset) {
        if (offset < size) {
            const bool virtualPageBoundary =
                ((address + offset) & (kPageBytes - 1)) == 0;
            if (!virtualPageBoundary) {
                if (bytesPaddr[offset] != bytesPaddr[offset - 1] + 1 ||
                    byteAttributes[offset] != byteAttributes[segmentStart]) {
                    memoryError("mapping or attributes are not contiguous",
                                address);
                }
                continue;
            }
        }
        QemuDataRef segment;
        segment.is_store = store;
        segment.vaddr = address + segmentStart;
        segment.paddr = bytesPaddr[segmentStart];
        segment.attributes = byteAttributes[segmentStart];
        segment.size = offset - segmentStart;
        if (!store) {
            segment.hasValue = true;
            segment.valueSize = static_cast<uint8_t>(segment.size);
            for (unsigned index = 0; index < segment.size; ++index) {
                segment.value[index] = loadValue[segmentStart + index];
            }
        }
        segments.push_back(segment);
        segmentStart = offset;
    }
}

void
QemuFrameMemory::finish() const
{
    if (nextIoAccess != ioAccesses.size()) {
        memoryError(
            "has unconsumed I/O observation",
            source->at(ioAccesses[nextIoAccess]).vaddr);
    }
}

} // namespace X86ISA
} // namespace gem5
