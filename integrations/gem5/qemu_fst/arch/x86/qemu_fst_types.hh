/* Transaction and lowering data models for native QEMU FST conversion. */

#ifndef __ARCH_X86_QEMU_FST_TYPES_HH__
#define __ARCH_X86_QEMU_FST_TYPES_HH__

#include <array>
#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "arch/x86/decoder.hh"
#include "arch/x86/qemu_memory_view.hh"
#include "base/types.hh"
#include "common/trace_entry_extensions.h"
#include "fastsim/trace.hpp"

namespace gem5
{
namespace X86ISA
{

struct QemuMicroopDescriptor;

struct QemuX86State
{
    std::array<uint64_t, qemu_trace_extensions::kStateFieldCount> fields{};

    uint64_t get(uint16_t field) const
    {
        return fields.at(field);
    }
};

enum class QemuTransactionKind
{
    Macro,
    Event,
};

struct QemuEventMetadata
{
    qemu_trace_extensions::StructuredEventKind kind =
        qemu_trace_extensions::StructuredEventKind::kInterrupt;
    qemu_trace_extensions::StructuredEventDisposition disposition =
        qemu_trace_extensions::StructuredEventDisposition::
            kBetweenInstructions;
    uint32_t vector = 0;
    uint32_t errorCode = 0;
    bool vectorValid = false;
    bool errorCodeValid = false;
};

struct QemuTransaction
{
    QemuTransactionKind kind = QemuTransactionKind::Macro;
    Addr pc = 0;
    Addr successor = 0;
    uint64_t size = 0;
    std::array<uint8_t, 16> bytes = {};
    uint8_t privilege = 0;
    uint64_t addressSpaceId = 0;
    QemuX86State state;
    bool measurement = false;
    bool isControl = false;
    bool isConditional = false;
    bool isIndirect = false;
    bool isCall = false;
    bool isReturn = false;
    bool isSyscallGateway = false;
    bool taken = false;
    QemuEventMetadata event;
    std::vector<QemuDataRef> refs;
    std::optional<uint64_t> tscRead;
    std::optional<uint32_t> tscAuxRead;

    bool userMode() const { return privilege == 3; }
};

struct QemuPendingSyscall
{
    Addr pc = 0;
    uint64_t addressSpaceId = 0;
    uint64_t userStackPointer = 0;
    Addr expectedReturnPc = 0;
    uint64_t recordOrdinal = 0;
    bool measurementActive = false;
    uint64_t number = 0;
    std::array<uint64_t, fastsim::kMaximumSyscallArguments> arguments = {};
    uint8_t argumentCount = 0;
    std::optional<uint64_t> returnValue;
    std::optional<uint32_t> errorNumber;
    bool failed = false;
};

using QemuSyscallKey = std::tuple<uint64_t, uint64_t, uint64_t>;
using QemuPendingSyscallMap =
    std::map<QemuSyscallKey, QemuPendingSyscall>;

struct QemuRawTraceCapabilities
{
    bool hasVersion = false;
    bool hasHeader = false;
    bool hasFiletype = false;
    bool hasX86_64 = false;
    bool hasEncodings = false;
    bool hasFullSystem = false;
    bool hasTransactional = false;
    bool hasRawProtocol = false;
    bool hasFooter = false;
    uint64_t version = 0;
    uint16_t rawAbiMajor = 0;
    uint64_t rawCapabilities = 0;
};

enum class QemuRawItemKind
{
    Transaction,
    RoiBegin,
    MeasurementBegin,
    RoiEnd,
    VcpuIdle,
    VcpuResume,
};

struct QemuRawItem
{
    QemuRawItemKind kind = QemuRawItemKind::Transaction;
    QemuTransaction transaction;
};

} // namespace X86ISA
} // namespace gem5

#endif // __ARCH_X86_QEMU_FST_TYPES_HH__
