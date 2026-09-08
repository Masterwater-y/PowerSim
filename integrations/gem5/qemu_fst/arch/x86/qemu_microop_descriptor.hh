/* Cached functional descriptors for gem5 x86 micro-ops. */

#ifndef __ARCH_X86_QEMU_MICROOP_DESCRIPTOR_HH__
#define __ARCH_X86_QEMU_MICROOP_DESCRIPTOR_HH__

#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

#include "arch/x86/qemu_dependency_tracker.hh"
#include "cpu/static_inst_fwd.hh"

namespace gem5
{
namespace X86ISA
{

struct QemuMicroopDescriptor
{
    StaticInstPtr inst;
    std::string mnemonic;
    std::vector<QemuEncodedReg> sources;
    std::vector<QemuEncodedReg> producerSources;
    std::vector<QemuEncodedReg> destinations;
    uint64_t dataSize = 0;
    uint64_t displacement = 0;
    bool hasDisplacement = false;
    bool isMemory = false;
    bool isLoad = false;
    bool isStore = false;
    bool isAtomic = false;
};

class QemuMicroopDescriptorCache
{
  public:
    const QemuMicroopDescriptor &get(const StaticInstPtr &inst);

  private:
    std::unordered_map<const StaticInst *, QemuMicroopDescriptor> descriptors;
};

} // namespace X86ISA
} // namespace gem5

#endif // __ARCH_X86_QEMU_MICROOP_DESCRIPTOR_HH__
