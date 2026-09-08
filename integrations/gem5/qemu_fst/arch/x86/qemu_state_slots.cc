#include "arch/x86/qemu_state_slots.hh"

#include "base/logging.hh"
#include "common/trace_entry_extensions.h"

namespace gem5
{
namespace X86ISA
{
namespace
{

constexpr uint64_t CompleteStateMask =
    (uint64_t{1} << qemu_trace_extensions::kUserStateFieldCount) - 1;

void
setStateField(
    QemuX86FunctionalState &state, uint16_t field, uint64_t value)
{
    if (field < qemu_trace_extensions::kUserStateGprCount) {
        state.gpr[field] = value;
        return;
    }
    if (field == qemu_trace_extensions::kUserStateRflags) {
        state.rflags = value;
        return;
    }
    if (field == qemu_trace_extensions::kUserStateFsBase) {
        state.fsBase = value;
        return;
    }
    if (field == qemu_trace_extensions::kUserStateGsBase) {
        state.gsBase = value;
        return;
    }
    fatal_if(field < qemu_trace_extensions::kUserStateXmmBase ||
                 field >= qemu_trace_extensions::kUserStateFieldCount,
             "QEMU CPL3 state field is unsupported=%u", unsigned(field));
    state.xmm[field - qemu_trace_extensions::kUserStateXmmBase] = value;
}

} // namespace

void
QemuStateSlots::begin(std::optional<uint8_t> protectedSlot)
{
    fatal_if(active || completeState,
             "QEMU CPL3 state overlaps another macro transaction");
    if (protectedSlot) {
        fatal_if(*protectedSlot >= states.size(),
                 "QEMU pending pre-state slot is invalid=%u",
                 unsigned(*protectedSlot));
        incomingSlot = *protectedSlot ^ 1u;
    } else {
        incomingSlot ^= 1u;
    }
    presence = 0;
    active = true;
}

void
QemuStateSlots::set(uint16_t field, uint64_t value)
{
    fatal_if(!active,
             "QEMU CPL3 state field is outside a state packet");
    fatal_if(field >= qemu_trace_extensions::kUserStateFieldCount,
             "QEMU state field is out of range=%u", unsigned(field));
    const uint64_t fieldBit = uint64_t{1} << field;
    fatal_if(presence & fieldBit,
             "QEMU CPL3 state field is duplicated=%u", unsigned(field));
    presence |= fieldBit;
    setStateField(states[incomingSlot], field, value);
}

void
QemuStateSlots::complete()
{
    fatal_if(!active || completeState,
             "QEMU ASID is outside a CPL3 macro preamble");
    fatal_if(presence != CompleteStateMask,
             "QEMU CPL3 pre-state is incomplete");
    active = false;
    completeState = true;
}

const QemuX86FunctionalState &
QemuStateSlots::incoming() const
{
    fatal_if(!completeState, "QEMU instruction has no completed CPL3 state");
    return states[incomingSlot];
}

const QemuX86FunctionalState &
QemuStateSlots::at(uint8_t slot) const
{
    fatal_if(slot >= states.size(),
             "QEMU pre-state slot is invalid=%u", unsigned(slot));
    return states[slot];
}

uint8_t
QemuStateSlots::consumeIncoming()
{
    fatal_if(!completeState, "QEMU instruction has no completed CPL3 state");
    completeState = false;
    return incomingSlot;
}

} // namespace X86ISA
} // namespace gem5
