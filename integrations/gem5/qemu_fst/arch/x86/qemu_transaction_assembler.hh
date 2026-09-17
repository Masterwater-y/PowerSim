#ifndef __ARCH_X86_QEMU_TRANSACTION_ASSEMBLER_HH__
#define __ARCH_X86_QEMU_TRANSACTION_ASSEMBLER_HH__

#include <array>
#include <filesystem>
#include <optional>

#include "arch/x86/qemu_fst_types.hh"
#include "arch/x86/qemu_raw_trace_reader.hh"

namespace gem5
{
namespace X86ISA
{

class QemuTransactionAssembler
{
  public:
    explicit QemuTransactionAssembler(const std::filesystem::path &path);

    bool nextFrame(QemuFrameEnvelope &frame);
    uint32_t coreId() const;
    bool coreIdKnown() const { return coreIdValid; }
    bool sawRoiEnd() const { return roiEnded; }

  private:
    using Entry = QemuRawTraceReader::Entry;

    QemuRawTraceReader reader;
    std::filesystem::path path;
    QemuRawTraceCapabilities capabilities;
    std::optional<QemuTransaction> transaction;
    QemuX86State currentState;
    QemuX86State pendingState;
    std::array<uint64_t, 2> stateBitmap{};
    size_t nextStateField = 0;
    bool stateBitmapLowSeen = false;
    bool stateBitmapHighSeen = false;
    bool requireFullState = true;
    uint32_t logicalCoreId = 0;
    bool coreIdValid = false;
    bool identitySeen = false;
    bool roiActive = false;
    bool measurementActive = false;
    bool roiEnded = false;
    bool footerSeen = false;
    bool eofValidated = false;
    bool vcpuIdle = false;
    std::optional<QemuRawItem> pendingMacro;
    std::optional<QemuRawItem> bufferedRaw;
    uint32_t pendingMemoryAttributes = 0;
    bool pendingMemoryAttributesValid = false;
    std::optional<Addr> pendingPhysicalAddress;
    std::array<uint8_t, 16> encoding{};
    size_t encodingSize = 0;
    bool transactionHasState = false;
    bool transactionHasInstruction = false;
    bool transactionHasEventMetadata = false;

    bool nextRaw(QemuRawItem &item);
    bool consume(const Entry &entry, QemuRawItem &item);
    bool consumeMarker(uint16_t type, uint64_t value, QemuRawItem &item);
    void beginTransaction(QemuTransactionKind kind, Addr pc);
    void consumeState(uint16_t kind, uint64_t value);
    void finishState();
    void appendEncoding(const Entry &entry);
    void consumeInstruction(const Entry &entry);
    void consumeMemory(const Entry &entry, bool store);
    void consumeValue(const Entry &entry, bool final);
    void validateCapabilities() const;
    void validateEnd();
};

} // namespace X86ISA
} // namespace gem5

#endif
