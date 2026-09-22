/* Transaction and lowering data models for native QEMU FST conversion. */

#ifndef __ARCH_X86_QEMU_FST_TYPES_HH__
#define __ARCH_X86_QEMU_FST_TYPES_HH__

#include <array>
#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <variant>
#include <vector>

#include "arch/x86/decoder.hh"
#include "arch/x86/qemu_memory_view.hh"
#include "base/types.hh"
#include "common/qemu_fst_record.h"
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

using CommitKind = qemu_fst_record::Kind;
using QemuFramePhase = qemu_fst_record::Phase;
using QemuEventMetadata = qemu_fst_record::Event;

struct CommitRecord : qemu_fst_record::Header
{
    const QemuX86State *state = nullptr;
    std::vector<qemu_fst_record::Access> refs;

    uint8_t privilege() const
    {
        return static_cast<uint8_t>(
            state->get(qemu_trace_extensions::kStateCpl));
    }

    uint64_t addressSpaceId() const
    {
        return state->get(qemu_trace_extensions::kStateCr3) &
            ~UINT64_C(0xfff);
    }

    bool userMode() const { return privilege() == 3; }

    bool syscall() const
    {
        return flags & qemu_fst_record::kHasSyscallEvent;
    }

    std::optional<uint64_t> tscRead() const
    {
        if (!(flags & qemu_fst_record::kHasTsc)) {
            return std::nullopt;
        }
        return tsc;
    }

    std::optional<uint32_t> tscAuxRead() const
    {
        if (!(flags & qemu_fst_record::kHasTscAux)) {
            return std::nullopt;
        }
        return tscAux;
    }
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

} // namespace X86ISA
} // namespace gem5

#endif // __ARCH_X86_QEMU_FST_TYPES_HH__
