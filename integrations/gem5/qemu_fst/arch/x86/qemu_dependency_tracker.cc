#include "arch/x86/qemu_dependency_tracker.hh"

#include <algorithm>

#include "base/logging.hh"

namespace gem5
{
namespace X86ISA
{

QemuDependencyTracker::QemuDependencyTracker(
    size_t integerRegisters,
    size_t floatingPointRegisters,
    size_t vectorRegisters,
    size_t conditionCodeRegisters)
{
    lastWriters[0].resize(integerRegisters);
    lastWriters[1].resize(floatingPointRegisters);
    lastWriters[2].resize(vectorRegisters);
    lastWriters[3].resize(conditionCodeRegisters);
}

void
QemuDependencyTracker::producerFacts(
    const std::vector<QemuEncodedReg> &sources,
    std::array<uint64_t, 4> &distances,
    std::array<uint8_t, 4> &classes) const
{
    distances.fill(0);
    classes.fill(255);
    std::array<QemuEncodedReg, 4> selectedRegisters = {};
    size_t producerCount = 0;
    for (const auto &source : sources) {
        const auto [regClass, index] = source;
        bool duplicate = false;
        for (size_t prior = 0; prior < producerCount; ++prior) {
            if (selectedRegisters[prior] == source) {
                duplicate = true;
                break;
            }
        }
        if (duplicate) {
            continue;
        }
        if (regClass >= lastWriters.size() ||
            index >= lastWriters[regClass].size()) {
            continue;
        }
        const auto writer = lastWriters[regClass][index];
        if (writer == 0) {
            continue;
        }
        const uint64_t distance = nextSequence - writer;
        size_t insert = std::min(producerCount, distances.size());
        while (insert != 0) {
            const size_t prior = insert - 1;
            if (distances[prior] < distance ||
                (distances[prior] == distance &&
                 classes[prior] <= regClass)) {
                break;
            }
            if (insert < distances.size()) {
                distances[insert] = distances[prior];
                classes[insert] = classes[prior];
                selectedRegisters[insert] = selectedRegisters[prior];
            }
            insert = prior;
        }
        if (insert < distances.size()) {
            distances[insert] = distance;
            classes[insert] = regClass;
            selectedRegisters[insert] = source;
        }
        if (producerCount < distances.size()) {
            ++producerCount;
        }
    }
}

void
QemuDependencyTracker::retire(
    const std::vector<QemuEncodedReg> &destinations)
{
    for (const auto &[regClass, index] : destinations) {
        fatal_if(regClass >= lastWriters.size(),
                 "invalid destination class=%u", unsigned(regClass));
        auto &writers = lastWriters[regClass];
        fatal_if(index >= writers.size(),
                 "invalid destination register class=%u index=%u",
                 unsigned(regClass), index);
        writers[index] = nextSequence;
    }
    retire();
}

void
QemuDependencyTracker::retire()
{
    ++nextSequence;
}

void
QemuDependencyTracker::reset()
{
    nextSequence = 1;
    for (auto &writers : lastWriters) {
        std::fill(writers.begin(), writers.end(), 0);
    }
}

} // namespace X86ISA
} // namespace gem5
