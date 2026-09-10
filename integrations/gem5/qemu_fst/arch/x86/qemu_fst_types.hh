/* Shared functional data models for QEMU raw trace lowering. */

#ifndef __ARCH_X86_QEMU_FST_TYPES_HH__
#define __ARCH_X86_QEMU_FST_TYPES_HH__

#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <vector>

#include "arch/x86/decoder.hh"
#include "base/types.hh"
#include "common/trace_entry_extensions.h"
#include "fastsim/trace.hpp"

namespace gem5
{
namespace X86ISA
{

struct QemuMicroopDescriptor;

struct QemuDataRef
{
    bool is_store = false;
    Addr vaddr = 0;
    Addr paddr = 0;
    uint64_t virtual_page = 0;
    uint32_t virtual_page_token = 0;
    uint64_t size = 0;
    uint32_t attributes = 0;
    std::array<uint8_t, 16> value = {};
    uint8_t valueSize = 0;
    bool hasValue = false;
};

struct QemuX86FunctionalState
{
    std::array<uint64_t, qemu_trace_extensions::kUserStateGprCount> gpr{};
    std::array<uint64_t,
        qemu_trace_extensions::kUserStateXmmCount *
        qemu_trace_extensions::kUserStateXmmWords> xmm{};
    uint64_t rflags = 0;
    uint64_t fsBase = 0;
    uint64_t gsBase = 0;
};

struct QemuPendingInst
{
    Addr pc = 0;
    uint64_t size = 0;
    std::array<uint8_t, 16> bytes = {};
    bool userMode = true;
    uint64_t addressSpaceId = 0;
    bool is_control = false;
    bool is_cond = false;
    bool is_indirect = false;
    bool is_call = false;
    bool is_return = false;
    bool isSyscallGateway = false;
    std::optional<Addr> indirectTarget;
    bool taken = false;
    Addr actual_next = 0;
    std::vector<QemuDataRef> refs;
    uint8_t preStateSlot = 0;
    bool hasPreState = false;

    void resetForInstruction()
    {
        pc = 0;
        size = 0;
        bytes.fill(0);
        userMode = true;
        addressSpaceId = 0;
        is_control = false;
        is_cond = false;
        is_indirect = false;
        is_call = false;
        is_return = false;
        isSyscallGateway = false;
        indirectTarget.reset();
        taken = false;
        actual_next = 0;
        refs.clear();
        preStateSlot = 0;
        hasPreState = false;
    }
};

struct QemuPendingSyscall
{
    Addr pc = 0;
    uint64_t addressSpaceId = 0;
    uint64_t userContextId = 0;
    uint64_t userStackPointer = 0;
    uint64_t recordOrdinal = 0;
    bool measurementActive = false;
    uint64_t number = 0;
    std::array<uint64_t, fastsim::kMaximumSyscallArguments> arguments = {};
    uint8_t argumentCount = 0;
    std::optional<uint64_t> returnValue;
    std::optional<uint32_t> errorNumber;
    bool failed = false;
};

struct QemuExpandedMicroop
{
    const QemuMicroopDescriptor *descriptor = nullptr;
    bool internalBranch = false;
    bool internalTaken = false;
    size_t dataRefIndex = std::numeric_limits<size_t>::max();

    bool hasDataRef() const
    {
        return dataRefIndex != std::numeric_limits<size_t>::max();
    }
};

struct QemuRawTraceCapabilities
{
    bool hasVersion = false;
    bool hasHeader = false;
    bool hasFiletype = false;
    bool hasX86_64 = false;
    bool hasEncodings = false;
    bool hasFullSystem = false;
    bool hasFooter = false;
    uint64_t version = 0;
};

} // namespace X86ISA
} // namespace gem5

#endif // __ARCH_X86_QEMU_FST_TYPES_HH__
