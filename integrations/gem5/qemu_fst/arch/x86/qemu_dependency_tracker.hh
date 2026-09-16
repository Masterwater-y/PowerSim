/* Register dependency tracking for FST producer-distance projection. */

#ifndef __ARCH_X86_QEMU_DEPENDENCY_TRACKER_HH__
#define __ARCH_X86_QEMU_DEPENDENCY_TRACKER_HH__

#include <array>
#include <cstddef>
#include <cstdint>
#include <utility>
#include <vector>

#include "fastsim/types.hpp"

namespace gem5
{
namespace X86ISA
{

using QemuEncodedReg = std::pair<uint8_t, uint32_t>;

class QemuDependencyTracker
{
  public:
    QemuDependencyTracker(size_t integerRegisters,
                          size_t floatingPointRegisters,
                          size_t vectorRegisters,
                          size_t conditionCodeRegisters);

    void producerFacts(
        const std::vector<QemuEncodedReg> &sources,
        std::vector<uint64_t> &distances,
        std::array<uint8_t, 4> &classes) const;

    void retire(const std::vector<QemuEncodedReg> &destinations);
    void retire();
    void forget(const std::vector<QemuEncodedReg> &destinations);
    void beginTransaction();
    void reset();

    bool empty() const { return nextSequence == 1; }

  private:
    uint64_t nextSequence = 1;
    std::array<std::vector<uint64_t>, fastsim::kTrackedRegisterClasses>
        lastWriters;
    // Reused by producerFacts() to avoid per-micro-op allocation; never holds
    // state between calls.
    mutable std::vector<std::pair<uint64_t, uint8_t>> producersScratch;
};

} // namespace X86ISA
} // namespace gem5

#endif // __ARCH_X86_QEMU_DEPENDENCY_TRACKER_HH__
