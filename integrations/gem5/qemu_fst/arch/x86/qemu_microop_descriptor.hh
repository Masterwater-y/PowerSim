/* Cached functional descriptors for gem5 x86 micro-ops. */

#ifndef __ARCH_X86_QEMU_MICROOP_DESCRIPTOR_HH__
#define __ARCH_X86_QEMU_MICROOP_DESCRIPTOR_HH__

#include <cstdint>
#include <string>
#include <map>
#include <unordered_map>
#include <vector>

#include "arch/x86/qemu_dependency_tracker.hh"
#include "cpu/static_inst_fwd.hh"

namespace gem5
{
class BaseISA;
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
    // A memory-class micro-op whose request carries Request::NO_ACCESS: it
    // only checks the address (e.g. Cda/StoreCheck) and performs no real
    // architectural access, so it binds no QEMU memory fact.
    bool noAccess = false;
    // A memory-class micro-op targeting the internal emulation-memory segment
    // (segment_idx::Ms): MSR/IO/internal register access, not guest RAM. It is
    // resolved from prestate, not from a QEMU RAM fact.
    bool internalSegment = false;
    // A memory-class micro-op whose access set cannot be determined without
    // running the official microcode (predicated access, string/REP splits).
    // Such macros are routed to the dynamic evaluator rather than generated.
    bool dynamicMemory = false;
};

class QemuMicroopDescriptorCache
{
  public:
    const QemuMicroopDescriptor &get(const StaticInstPtr &inst, const BaseISA &isa);

  private:
    std::unordered_map<const StaticInst *, QemuMicroopDescriptor> descriptors;
    std::unordered_map<const StaticInst *, std::map<unsigned, QemuMicroopDescriptor>>
        stackDescriptors;
    const QemuMicroopDescriptor &getStatic(const StaticInstPtr &inst);
};

} // namespace X86ISA
} // namespace gem5

#endif // __ARCH_X86_QEMU_MICROOP_DESCRIPTOR_HH__
