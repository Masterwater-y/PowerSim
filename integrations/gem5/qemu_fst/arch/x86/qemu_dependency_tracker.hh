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
        std::array<uint64_t, 4> &distances,
        std::array<uint8_t, 4> &classes) const;

    void retire(const std::vector<QemuEncodedReg> &destinations);
    void retire();

    bool empty() const { return nextSequence == 1; }

  private:
    uint64_t nextSequence = 1;
    std::array<std::vector<uint64_t>, fastsim::kTrackedRegisterClasses>
        lastWriters;
};

} // namespace X86ISA
} // namespace gem5

#endif // __ARCH_X86_QEMU_DEPENDENCY_TRACKER_HH__
