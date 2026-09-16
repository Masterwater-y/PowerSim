#ifndef __ARCH_X86_QEMU_FST_PROJECTOR_HH__
#define __ARCH_X86_QEMU_FST_PROJECTOR_HH__

#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "arch/x86/qemu_address_resolver.hh"
#include "arch/x86/qemu_dependency_tracker.hh"
#include "arch/x86/qemu_fst_types.hh"
#include "fastsim/trace.hpp"

namespace gem5
{
namespace X86ISA
{

class FstProjector
{
  public:
    struct Counts
    {
        uint64_t warmupRecords = 0;
        uint64_t warmupInstructions = 0;
        uint64_t warmupUserRecords = 0;
        uint64_t warmupUserInstructions = 0;
        uint64_t warmupMmioRecords = 0;
        uint64_t warmupPioRecords = 0;
        uint64_t measurementRecords = 0;
        uint64_t measurementInstructions = 0;
        uint64_t measurementUserRecords = 0;
        uint64_t measurementUserInstructions = 0;
        uint64_t measurementMmioRecords = 0;
        uint64_t measurementPioRecords = 0;
        uint64_t frameRamBytes = 0;
        uint64_t loweredKernelUopsBeforeIdleFilter = 0;
        uint64_t emittedKernelUops = 0;
        uint64_t excludedIdleKernelUops = 0;
    };

    FstProjector(
        uint32_t coreId, const std::string &outputDir,
        uint64_t userFstTarget);
    ~FstProjector();

    void beginRoi();
    void beginMeasurement();
    void endRoi();
    void vcpuIdle();
    void vcpuResume();
    bool beginFrame(
        const QemuTransaction &transaction, const StaticInstPtr &macro,
        const std::string &mnemonic);
    void projectMicroop(
        const QemuMicroopDescriptor &descriptor,
        const std::vector<QemuDataRef> &dataRefs, bool eventMicroop,
        bool macroCompletion);
    void endFrame();
    void close();

    const Counts &counts() const { return counters; }
    bool targetReached() const { return targetReached_; }

  private:
    uint32_t core;
    uint64_t userFstTarget;
    std::unique_ptr<fastsim::BinaryTraceWriter> writer;
    QemuAddressResolver addresses;
    QemuDependencyTracker dependencies;
    // Reused across micro-ops to avoid per-UOP heap allocation. Filled by
    // dependencies.producerFacts() on each projectMicroop call; contents never
    // persist between micro-ops.
    std::vector<uint64_t> producerDistancesScratch;
    Counts counters;
    std::map<std::pair<uint64_t, uint64_t>,
             fastsim::StaticInstructionInfo> staticInstructions;
    std::set<std::pair<uint64_t, uint64_t>> taintedStaticInstructions;
    std::set<uint64_t> addressSpaces;
    struct PendingSyscallGateway
    {
        QemuTransaction source;
        std::set<QemuEncodedReg> sources;
        std::set<QemuEncodedReg> destinations;
    };
    std::optional<PendingSyscallGateway> pendingSyscallGateway;
    QemuPendingSyscallMap pendingSyscalls;
    std::map<QemuSyscallKey, uint32_t> ambiguousSyscalls;
    const QemuTransaction *activeTransaction = nullptr;
    StaticInstPtr activeMacro;
    std::string activeMnemonic;
    std::optional<fastsim::StaticInstructionInfo> activeStaticInstruction;
    bool activeFrameExcluded = false;
    bool activeFrameIsGateway = false;
    uint64_t activeFrameKernelUops = 0;
    uint64_t currentAddressSpaceId = 0;
    bool roiActive = false;
    bool measurementActive = false;
    bool roiEnded = false;
    bool closed = false;
    bool vcpuIsIdle = false;
    bool targetReached_ = false;

    bool excludeIdle(const QemuTransaction &transaction);
    void beginSyscall(const PendingSyscallGateway &gateway);
    void completeSyscallIfPresent(const QemuTransaction &transaction);
    void writeSyscall(
        const QemuPendingSyscall &syscall, bool update,
        const PendingSyscallGateway *gateway = nullptr);
    void observeStaticMicroop(const QemuMicroopDescriptor &descriptor);
    void finishStaticInstruction();
};

} // namespace X86ISA
} // namespace gem5

#endif
