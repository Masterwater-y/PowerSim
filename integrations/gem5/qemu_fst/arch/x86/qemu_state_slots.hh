/* Double-buffered QEMU architectural pre-state packets. */

#ifndef __ARCH_X86_QEMU_STATE_SLOTS_HH__
#define __ARCH_X86_QEMU_STATE_SLOTS_HH__

#include <array>
#include <cstdint>
#include <optional>

#include "arch/x86/qemu_fst_types.hh"

namespace gem5
{
namespace X86ISA
{

class QemuStateSlots
{
  public:
    void begin(std::optional<uint8_t> protectedSlot);
    void set(uint16_t field, uint64_t value);
    void complete();

    bool packetActive() const { return active; }
    bool hasCompleteState() const { return completeState; }

    const QemuX86FunctionalState &incoming() const;
    const QemuX86FunctionalState &at(uint8_t slot) const;
    uint8_t consumeIncoming();

  private:
    std::array<QemuX86FunctionalState, 2> states = {};
    uint64_t presence = 0;
    uint8_t incomingSlot = 0;
    bool active = false;
    bool completeState = false;
};

} // namespace X86ISA
} // namespace gem5

#endif // __ARCH_X86_QEMU_STATE_SLOTS_HH__
