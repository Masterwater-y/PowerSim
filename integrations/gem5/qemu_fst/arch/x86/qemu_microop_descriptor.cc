#include "arch/x86/qemu_microop_descriptor.hh"

#include <algorithm>

#include "arch/x86/insts/microldstop.hh"
#include "arch/x86/insts/microop_args.hh"
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
        registers.emplace_back(
            regClass, static_cast<uint32_t>(reg.index()));
    }
}

} // namespace

const QemuMicroopDescriptor &
QemuMicroopDescriptorCache::get(const StaticInstPtr &inst)
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
    descriptor.isMemory =
        (inst->isLoad() || inst->isStore() || inst->isAtomic()) &&
        !inst->isDataPrefetch();
    descriptor.isLoad = inst->isLoad();
    descriptor.isStore = inst->isStore();
    descriptor.isAtomic = inst->isAtomic();
    if (descriptor.isMemory) {
        const auto *memory = dynamic_cast<const MemOp *>(inst.get());
        fatal_if(!memory,
                 "memory micro-op has no x86 data size mnemonic=%s",
                 descriptor.mnemonic.c_str());
        descriptor.dataSize = memory->dataSize;
        if (const auto *address =
                dynamic_cast<const AddrOp *>(inst.get())) {
            descriptor.displacement = address->disp;
            descriptor.hasDisplacement = true;
        }
    }
    return descriptors.emplace(inst.get(), std::move(descriptor))
        .first->second;
}

} // namespace X86ISA
} // namespace gem5
