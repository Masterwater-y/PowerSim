#include "arch/x86/qemu_microop_descriptor.hh"

#include <algorithm>

#include "arch/x86/insts/microldstop.hh"
#include "arch/x86/insts/microop_args.hh"
#include "arch/x86/regs/int.hh"
#include "arch/x86/regs/float.hh"
#include "arch/x86/regs/misc.hh"
#include "arch/generic/isa.hh"
#include "base/logging.hh"
#include "cpu/reg_class.hh"
#include "cpu/static_inst.hh"

namespace gem5
{
namespace X86ISA
{
namespace
{

void
trackedRegs(
    const StaticInstPtr &inst, bool sources,
    std::vector<QemuEncodedReg> &registers)
{
    const int count = sources ? inst->numSrcRegs() : inst->numDestRegs();
    registers.reserve(count);
    for (int index = 0; index < count; ++index) {
        const RegId &reg = sources
            ? inst->srcRegIdx(index) : inst->destRegIdx(index);
        uint8_t regClass = 255;
        switch (reg.classValue()) {
          case IntRegClass: regClass = 0; break;
          case FloatRegClass: regClass = 1; break;
          case VecRegClass: regClass = 2; break;
          case CCRegClass: regClass = 3; break;
          default: continue;
        }
        const uint32_t regIndex = reg.is(IntRegClass)
            ? reg.index() & ~IntFoldBit : reg.index();
        registers.emplace_back(regClass, regIndex);
    }
}

} // namespace

const QemuMicroopDescriptor &
QemuMicroopDescriptorCache::getStatic(const StaticInstPtr &inst)
{
    const auto found = descriptors.find(inst.get());
    if (found != descriptors.end()) {
        return found->second;
    }

    QemuMicroopDescriptor descriptor;
    descriptor.inst = inst;
    descriptor.mnemonic = inst->getName();
    trackedRegs(inst, true, descriptor.sources);
    descriptor.producerSources = descriptor.sources;
    std::sort(
        descriptor.producerSources.begin(),
        descriptor.producerSources.end());
    descriptor.producerSources.erase(
        std::unique(
            descriptor.producerSources.begin(),
            descriptor.producerSources.end()),
        descriptor.producerSources.end());
    trackedRegs(inst, false, descriptor.destinations);
    descriptor.isLoad = inst->isLoad();
    descriptor.isStore = inst->isStore();
    descriptor.isAtomic = inst->isAtomic();
    descriptor.noAccess = inst->isDataPrefetch();
    const bool memoryClass =
        (inst->isLoad() || inst->isStore() || inst->isAtomic()) &&
        !inst->isDataPrefetch();
    if (memoryClass) {
        const auto *memory = dynamic_cast<const MemOp *>(inst.get());
        fatal_if(!memory,
                 "memory micro-op has no x86 data size mnemonic=%s",
                 descriptor.mnemonic.c_str());
        // An address-only memory micro-op (Cda/StoreCheck, Clflushopt, Clwb,
        // Tia) derives from MemNoDataOp: it only probes the address for a
        // fault and performs no architectural data access, so it is not a
        // committed functional memory UOP and binds no QEMU fact.
        descriptor.noAccess = dynamic_cast<const MemNoDataOp *>(inst.get()) !=
            nullptr;
        descriptor.dataSize = memory->dataSize;
        if (const auto *address =
                dynamic_cast<const AddrOp *>(inst.get())) {
            descriptor.displacement = address->disp;
            descriptor.hasDisplacement = true;
            // The internal emulation-memory segment (Ms) is MSR/IO/internal
            // register space, not guest RAM. An MSR access is not a committed
            // functional RAM/cache/DTLB event, so like a prefetch it keeps its
            // official UOP identity but binds no QEMU memory fact.
            descriptor.internalSegment = address->segment == segment_idx::Ms;
        }
    }
    descriptor.isMemory =
        memoryClass && !descriptor.noAccess && !descriptor.internalSegment;
    return descriptors.emplace(inst.get(), std::move(descriptor))
        .first->second;
}

const QemuMicroopDescriptor &
QemuMicroopDescriptorCache::get(const StaticInstPtr &inst, const BaseISA &isa)
{
    const auto &base = getStatic(inst);
    const auto isStack = [](const QemuEncodedReg &reg) {
        return reg.first == 1 && reg.second >= float_reg::NumRegs;
    };
    if (!std::any_of(base.sources.begin(), base.sources.end(), isStack) &&
        !std::any_of(base.destinations.begin(), base.destinations.end(), isStack)) {
        return base;
    }
    const auto top = floatRegClass[float_reg::NumRegs].flatten(isa).index();
    auto &variants = stackDescriptors[inst.get()];
    const auto found = variants.find(top);
    if (found != variants.end()) return found->second;
    auto resolved = base;
    const auto resolve = [&](std::vector<QemuEncodedReg> &regs) {
        for (auto &reg : regs) {
            if (isStack(reg)) {
                reg.second = floatRegClass[reg.second].flatten(isa).index();
            }
        }
    };
    resolve(resolved.sources);
    resolve(resolved.producerSources);
    resolve(resolved.destinations);
    std::sort(resolved.producerSources.begin(), resolved.producerSources.end());
    resolved.producerSources.erase(
        std::unique(resolved.producerSources.begin(), resolved.producerSources.end()),
        resolved.producerSources.end());
    return variants.emplace(top, std::move(resolved)).first->second;
}

} // namespace X86ISA
} // namespace gem5
