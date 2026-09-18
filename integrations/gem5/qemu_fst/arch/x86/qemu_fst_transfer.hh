#ifndef __ARCH_X86_QEMU_FST_TRANSFER_HH__
#define __ARCH_X86_QEMU_FST_TRANSFER_HH__

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "arch/x86/qemu_fst_lowering_cpu.hh"
#include "arch/x86/qemu_fst_projector.hh"
#include "params/X86QemuFstTransfer.hh"
#include "sim/sim_object.hh"

namespace gem5
{
namespace X86ISA
{

class X86QemuFstTransfer : public SimObject
{
  public:
    using Params = X86QemuFstTransferParams;

    explicit X86QemuFstTransfer(const Params &params);

    // Directly invoked from Python after m5.instantiate(). The offline
    // lowerer is not a simulated CPU: there is no startup() conversion event,
    // no schedule() and no exitSimLoop().
    void convert();

  private:
    const std::string inputShard;
    const std::string outputDir;
    const uint32_t expectedNumCores;
    const uint64_t userMacroTarget;
    const uint32_t triggerCore;
    const int32_t workerCoreIndex;
    std::vector<X86QemuFstOfflineLowererHost *> cpus;
    std::vector<std::unique_ptr<FstProjector>> projectors;

    void convertWorker(uint32_t core);
    void writeWorkerBoundaries(
        uint32_t core, uint64_t measurementUserMacros) const;
    std::string workerBoundaryJson(
        uint32_t core, uint64_t measurementUserMacros) const;
};

} // namespace X86ISA
} // namespace gem5

#endif
