/* Cached functional descriptors for gem5 x86 micro-ops. */

#ifndef __ARCH_X86_QEMU_MICROOP_DESCRIPTOR_HH__
#define __ARCH_X86_QEMU_MICROOP_DESCRIPTOR_HH__

#include <array>
#include <cstdint>
#include <map>
#include <string>
#include <unordered_map>
#include <vector>

#include "arch/x86/qemu_dependency_tracker.hh"
#include "cpu/static_inst_fwd.hh"
#include "cpu/reg_class.hh"
#include "fastsim/types.hpp"

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
    std::vector<RegId> flattenedSources;
    std::vector<RegId> flattenedDestinations;
    std::array<uint8_t, fastsim::kTrackedRegisterClasses>
        destinationCounts{};
    uint64_t dataSize = 0;
    uint64_t displacement = 0;
    bool hasDisplacement = false;
    bool isMemory = false;
    bool isLoad = false;
    bool isStore = false;
    bool isAtomic = false;
    bool isControl = false;
    bool isConditionalControl = false;
    bool isIndirectControl = false;
    bool isCall = false;
    bool isReturn = false;
    bool isMicroop = false;
    bool isSerializing = false;
    int16_t opClass = 0;
    uint8_t sourceCount = 0;
    uint8_t destinationCount = 0;
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
    bool executionPlanPrepared = false;
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
    static void prepareExecutionPlan(
        QemuMicroopDescriptor &descriptor, const BaseISA &isa);
};

} // namespace X86ISA
} // namespace gem5

#endif // __ARCH_X86_QEMU_MICROOP_DESCRIPTOR_HH__
