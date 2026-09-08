/* Convert QEMU x86-64 execution streams directly into FastSim FST v7. */

#ifndef __ARCH_X86_QEMU_FST_CONVERTER_HH__
#define __ARCH_X86_QEMU_FST_CONVERTER_HH__

#include <array>
#include <cinttypes>
#include <cstddef>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#include "arch/x86/decoder.hh"
#include "arch/x86/qemu_address_resolver.hh"
#include "arch/x86/qemu_dependency_tracker.hh"
#include "arch/x86/qemu_fst_types.hh"
#include "arch/x86/qemu_memory_binder.hh"
#include "arch/x86/qemu_microcode_executor.hh"
#include "arch/x86/qemu_microop_descriptor.hh"
#include "arch/x86/qemu_state_slots.hh"
#include "base/types.hh"
#include "cpu/reg_class.hh"
#include "cpu/static_inst_fwd.hh"
#include "common/trace_entry_extensions.h"
#include "fastsim/trace.hpp"
#include "params/X86QemuUserFstLowerer.hh"
#include "sim/eventq.hh"
#include "sim/sim_object.hh"

namespace gem5
{
namespace X86ISA
{

class QemuFstConverter : public SimObject
{
  public:
    QemuFstConverter(const X86QemuUserFstLowererParams &params);
    ~QemuFstConverter() override;

    void startup() override;

  public:
    using DataRef = QemuDataRef;
    using X86FunctionalState = QemuX86FunctionalState;
    using PendingInst = QemuPendingInst;
    using ExpandedMicroop = QemuExpandedMicroop;

  private:
    using EncodedReg = QemuEncodedReg;

    struct StaticInstructionKey
    {
        Addr pc = 0;
        uint8_t size = 0;
        std::array<uint64_t, 2> encoding = {};
        bool isControl = false;

        bool operator==(const StaticInstructionKey &other) const
        {
            return pc == other.pc && size == other.size &&
                   encoding == other.encoding &&
                   isControl == other.isControl;
        }
    };

    struct StaticInstructionKeyHash
    {
        size_t operator()(const StaticInstructionKey &key) const;
    };

    struct StaticLowering
    {
        StaticInstPtr macro;
        std::string mnemonic;
        bool requiresExecution = false;
        bool loweringBuilt = false;
        uint64_t dynamicExecutions = 0;
        std::vector<const QemuMicroopDescriptor *> microops;
        QemuStaticMemoryPlan memoryPlan;
    };

    struct FstEmissionState
    {
        uint64_t recordCount = 0;
        uint64_t warmupRecordCount = 0;
        uint64_t warmupInstructionCount = 0;
        uint64_t measurementInstructionCount = 0;
        uint64_t measurementUserRecordCount = 0;
        bool measurementUserRecordTargetReached = false;
        uint64_t writtenAddressSpaceId = 0;
        std::unique_ptr<fastsim::BinaryTraceWriter> writer;
    };

    struct ThreadState
    {
        ThreadState();

        bool identityInitialized = false;
        int64_t drThreadId = 0;
        uint64_t currentAddressSpaceId = 0;
        QemuStateSlots userStates;
        std::optional<uint64_t> fixedAddressSpaceId;
        std::optional<Addr> pendingBranchTarget;
        uint32_t pendingMemoryAttributes = 0;
        std::optional<Addr> pendingMemoryPhysicalAddress;
        PendingInst pending;
        bool havePending = false;
        std::optional<QemuPendingSyscall> pendingSyscall;
        bool roiActive = false;
        bool measurementActive = false;
        bool roiCompleted = false;
        uint64_t roiBeginMarkers = 0;
        uint64_t measurementBeginMarkers = 0;
        uint64_t roiEndMarkers = 0;
        uint64_t logicalCoreId = 0;
        bool logicalCoreIdValid = false;
        QemuAddressResolver addresses;
        QemuDependencyTracker dependencies;
        std::unique_ptr<QemuMicrocodeExecutor> microcodeExecutor;
        std::vector<ExpandedMicroop> expandedMicroops;
        std::vector<DataRef> boundRefs;
        FstEmissionState emission;
    };

    X86ISA::Decoder *decoder;
    const std::string inputTrace;
    const std::string outputDir;
    const uint32_t expectedNumCores;
    const uint64_t minUserUops;
    EventFunctionWrapper convertEvent;
    QemuMicroopDescriptorCache microopDescriptors;
    std::unordered_map<StaticInstructionKey, StaticLowering,
                       StaticInstructionKeyHash> staticLowerings;
    std::vector<ThreadState> threads;
    ThreadState *currentThreadState = nullptr;
    int64_t currentQemuShardKey = 0;
    std::vector<int64_t> coreThreads;
    QemuRawTraceCapabilities rawCapabilities;
    uint64_t dynamicMacroCount = 0;
    uint64_t dynamicInternalControlCount = 0;
    uint64_t dynamicAtomicCount = 0;
    uint64_t dynamicSizeMismatchCount = 0;
    uint64_t dynamicFragmentedEvidenceCount = 0;
    uint64_t dynamicMicroopCount = 0;
    uint64_t dynamicMaximumMicroopCount = 0;
    uint64_t scalarSinglePaddingCount = 0;
    void convert();
    void convertQemuWindowedTrace();
    void finishConversion();
    void recordRawFiletype(uint64_t filetype);
    void validateRawCapabilities() const;
    void bindQemuWindowedCore(ThreadState &state, uint64_t core);
    void finalizeQemuWindowedRoi(ThreadState &state);
    void handleInstruction(ThreadState &state, uint16_t type, Addr pc,
                           uint16_t size, const uint8_t *encoding);
    void handleData(ThreadState &state, Addr address, uint16_t size,
                    bool isStore);
    void handleMarker(ThreadState &state, uint16_t markerType,
                      uint64_t markerValue);
    DataRef translateAddress(ThreadState &state, Addr vaddr, uint64_t size,
                             bool isStore);
    void resolveDynamicAddress(ThreadState &state, PendingInst &inst,
                               DataRef &ref);
    void emitInstruction(ThreadState &state, PendingInst &inst,
                         const StaticInstPtr &macro);
    void flushPending(ThreadState &state, Addr nextInstrPc);
    Addr pendingSuccessorAtBoundary(ThreadState &state);
    void openCoreOutput(ThreadState &state);
    void closeOutputs();
    StaticInstructionKey staticInstructionKey(const PendingInst &inst) const;
    StaticLowering &staticMacro(const PendingInst &inst);
    StaticLowering &staticLowering(const PendingInst &inst);
    StaticInstPtr decodeMacro(const PendingInst &inst);
    void writeDynamicTelemetry() const;
    void writeRecord(ThreadState &state, const PendingInst &inst,
                     const StaticInstPtr &micro, size_t microPc,
                     size_t microCount, const DataRef *dataRef,
                     const std::vector<EncodedReg> &sourceOperands,
                     const std::vector<EncodedReg> &producerSources,
                     const std::vector<EncodedReg> &destinations,
                     bool internalBranch = false, bool internalTaken = false);
    void writeSyscallRecord(ThreadState &state,
                            const QemuPendingSyscall &syscall);
    void retireRecord(ThreadState &state, bool userMode,
                      const std::vector<EncodedReg> *destinations);
    void completeInstruction(ThreadState &state, bool userMode);
    void recordAddressSpace(ThreadState &state, uint64_t addressSpaceId);
    void finalizeOutput(ThreadState &state);
    void writeBoundaryFile() const;
};

} // namespace X86ISA
} // namespace gem5

#endif // __ARCH_X86_QEMU_FST_CONVERTER_HH__
