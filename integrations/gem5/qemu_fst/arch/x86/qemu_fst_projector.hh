#ifndef __ARCH_X86_QEMU_FST_PROJECTOR_HH__
#define __ARCH_X86_QEMU_FST_PROJECTOR_HH__

#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
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
    };

    FstProjector(
        uint32_t coreId, const std::string &outputDir);
    ~FstProjector();

    bool beginFrame(
        const CommitRecord &envelope, const StaticInstPtr &macro,
        const std::string &mnemonic);
    void projectMicroop(
        const QemuMicroopDescriptor &descriptor,
        std::vector<QemuDataRef> &dataRefs, bool eventMicroop,
        bool macroCompletion);
    void endFrame();
    void close();

    const Counts &counts() const { return counters; }

  private:
    uint32_t core;
    std::unique_ptr<fastsim::BinaryTraceWriter> writer;
    QemuAddressResolver addresses;
    QemuDependencyTracker dependencies;
    // Reused across micro-ops to avoid per-UOP heap allocation. Filled by
    // dependencies.producerFacts() on each projectMicroop call; contents never
    // persist between micro-ops.
    std::vector<uint64_t> producerDistancesScratch;
    std::vector<uint32_t> dependencyExtensionsScratch;
    Counts counters;
    using StaticInstructionKey = std::pair<uint64_t, uint64_t>;
    struct StaticInstructionKeyHash
    {
        size_t operator()(const StaticInstructionKey &key) const
        {
            return std::hash<uint64_t>{}(key.first) ^
                (std::hash<uint64_t>{}(key.second) << 1);
        }
    };
    std::unordered_map<
        StaticInstructionKey, fastsim::StaticInstructionInfo,
        StaticInstructionKeyHash> staticInstructions;
    std::unordered_set<
        StaticInstructionKey, StaticInstructionKeyHash>
        taintedStaticInstructions;
    std::set<uint64_t> addressSpaces;
    struct ActiveSyscallGateway
    {
        const CommitRecord *source = nullptr;
        std::set<QemuEncodedReg> sources;
        std::set<QemuEncodedReg> destinations;
    };
    std::optional<ActiveSyscallGateway> activeSyscallGateway;
    QemuPendingSyscallMap pendingSyscalls;
    std::map<QemuSyscallKey, uint32_t> ambiguousSyscalls;
    const CommitRecord *activeRecord = nullptr;
    StaticInstPtr activeMacro;
    std::string activeMnemonic;
    std::optional<fastsim::StaticInstructionInfo> activeStaticInstruction;
    bool activeFrameIsGateway = false;
    bool activeFrameMeasurement = false;
    uint64_t currentAddressSpaceId = 0;
    bool closed = false;

    void beginSyscall(const ActiveSyscallGateway &gateway);
    void completeSyscallIfPresent(const CommitRecord &transaction);
    void writeSyscall(
        const QemuPendingSyscall &syscall, bool update,
        const ActiveSyscallGateway *gateway = nullptr);
    void observeStaticMicroop(const QemuMicroopDescriptor &descriptor);
    void finishStaticInstruction();
};

} // namespace X86ISA
} // namespace gem5

#endif
