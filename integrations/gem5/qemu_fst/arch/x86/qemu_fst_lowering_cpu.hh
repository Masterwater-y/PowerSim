#ifndef __ARCH_X86_QEMU_FST_LOWERING_CPU_HH__
#define __ARCH_X86_QEMU_FST_LOWERING_CPU_HH__

#include <cstdint>
#include <array>
#include <memory>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#include "arch/x86/regs/ccr.hh"
#include "arch/x86/regs/float.hh"
#include "arch/x86/regs/int.hh"
#include "arch/x86/regs/misc.hh"
#include "arch/x86/qemu_fst_types.hh"
#include "arch/x86/qemu_fst_projector.hh"
#include "arch/x86/qemu_microop_descriptor.hh"
#include "base/amo.hh"
#include "cpu/simple/base.hh"
#include "mem/port.hh"
#include "params/X86QemuFstOfflineLowererHost.hh"

namespace gem5
{
namespace X86ISA
{

class X86QemuFstOfflineLowererHost : public BaseSimpleCPU
{
  public:
    using Params = X86QemuFstOfflineLowererHostParams;

    explicit X86QemuFstOfflineLowererHost(const Params &params);

    void init() override;
    void startup() override;

    void lower(
        const QemuFrameEnvelope &envelope, FstProjector &projector);

    Fault readMem(
        Addr addr, uint8_t *data, unsigned size, Request::Flags flags,
        const std::vector<bool> &byteEnable = {}) override;
    Fault writeMem(
        uint8_t *data, unsigned size, Addr addr, Request::Flags flags,
        uint64_t *result, const std::vector<bool> &byteEnable = {}) override;
    Fault amoMem(
        Addr addr, uint8_t *data, unsigned size, Request::Flags flags,
        AtomicOpFunctorPtr operation) override;
    Fault initiateMemMgmtCmd(Request::Flags flags) override;

  protected:
    Port &getDataPort() override { return dataPort; }
    Port &getInstPort() override { return instructionPort; }

  private:
    class InertPort : public RequestPort
    {
      public:
        explicit InertPort(const std::string &name) : RequestPort(name) {}

      private:
        bool recvTimingResp(PacketPtr) override { return false; }
        void recvReqRetry() override {}
    };

    InertPort instructionPort;
    InertPort dataPort;
    QemuMicroopDescriptorCache descriptors;
    std::unordered_map<const StaticInst *,
        std::pair<StaticInstPtr, StaticInstPtr>> architecturalWidthLoads;
    SimpleExecContext *context = nullptr;
    const QemuTransaction *activeTransaction = nullptr;
    const QemuMicroopDescriptor *activeDescriptor = nullptr;
    QemuFrameMemory memory;
    std::vector<QemuDataRef> consumedRefs;
    // Read-only Observation-ABI guard: every micro-op source register must be
    // provided before it is read. Bulk state injection writes the full
    // architectural register file each frame, so the provided baseline is a
    // fixed template (computed once at init) that resetExecutionScratch copies
    // in; execution then marks additional micro-scratch destinations.
    std::array<bool, int_reg::NumRegs> integerProvided{};
    std::array<bool, float_reg::NumRegs> floatingProvided{};
    std::array<bool, cc_reg::NumRegs> conditionProvided{};
    std::array<bool, misc_reg::NumRegs> miscProvided{};
    std::array<bool, int_reg::NumRegs> integerProvidedTemplate{};
    std::array<bool, float_reg::NumRegs> floatingProvidedTemplate{};
    std::array<bool, cc_reg::NumRegs> conditionProvidedTemplate{};
    std::array<bool, misc_reg::NumRegs> miscProvidedTemplate{};

    void prepareDecoder(const QemuTransaction &transaction);
    void stageDecoderBytes(
        const QemuTransaction &transaction, Addr fetchPc);
    void initializeProvidedTemplates();
    void resetExecutionScratch();
    void importQemuState(const QemuTransaction &transaction);
    void lowerEvent(
        const QemuFrameEnvelope &envelope, FstProjector &projector);
    void executeFrame(
        const QemuFrameEnvelope &envelope, bool rom,
        FstProjector &projector);
    StaticInstPtr architecturalWidthMemoryLoad(
        const QemuTransaction &transaction,
        const StaticInstPtr &macro, const StaticInstPtr &micro);
    void consumeMemory(
        Addr addr, unsigned size, bool store, Request::Flags flags,
        uint8_t *data, uint32_t accessAttributes = 0);
    void provide(const RegId &reg);
    void provideFlattened(const RegId &reg);
    void requireSources(const QemuMicroopDescriptor &descriptor);
    void provideDestinations(const QemuMicroopDescriptor &descriptor);
};

} // namespace X86ISA
} // namespace gem5

#endif
