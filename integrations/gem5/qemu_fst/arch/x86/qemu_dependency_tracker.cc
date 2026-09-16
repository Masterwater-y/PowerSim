#include "arch/x86/qemu_dependency_tracker.hh"

#include <algorithm>

#include "arch/x86/regs/ccr.hh"
#include "arch/x86/regs/float.hh"
#include "arch/x86/regs/int.hh"
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
    std::vector<uint64_t> &distances,
    std::array<uint8_t, 4> &classes) const
{
    distances.clear();
    classes.fill(255);
    // Full distance-sorted, distance-unique producer census. The first four
    // distinct distances populate `classes`; any beyond feed the complete
    // dependency extension. Reuse a member scratch buffer so the per-micro-op
    // path does not allocate.
    producersScratch.clear();
    for (const auto &source : sources) {
        const auto [regClass, index] = source;
        fatal_if(
            regClass >= lastWriters.size() ||
                index >= lastWriters[regClass].size(),
            "invalid source register class=%u index=%u",
            unsigned(regClass), index);
        const auto writer = lastWriters[regClass][index];
        if (writer == 0) {
            continue;
        }
        producersScratch.emplace_back(nextSequence - writer, regClass);
    }
    std::sort(producersScratch.begin(), producersScratch.end());
    producersScratch.erase(
        std::unique(
            producersScratch.begin(), producersScratch.end(),
            [](const auto &left, const auto &right) {
                return left.first == right.first;
            }),
        producersScratch.end());
    distances.reserve(producersScratch.size());
    for (size_t index = 0; index < producersScratch.size(); ++index) {
        distances.push_back(producersScratch[index].first);
        if (index < classes.size()) {
            classes[index] = producersScratch[index].second;
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
QemuDependencyTracker::forget(
    const std::vector<QemuEncodedReg> &destinations)
{
    for (const auto &[regClass, index] : destinations) {
        lastWriters.at(regClass).at(index) = 0;
    }
}

void
QemuDependencyTracker::beginTransaction()
{
    // Keep architectural producers and the output sequence across CPL/ASID
    // changes. Only gem5's per-execution scratch state loses its producers.
    const auto clearFrom = [this](size_t regClass, size_t first) {
        auto &writers = lastWriters[regClass];
        std::fill(writers.begin() + std::min(first, writers.size()),
                  writers.end(), 0);
    };
    clearFrom(0, int_reg::MicroBegin);
    clearFrom(1, float_reg::MicrofpBase);
    clearFrom(3, cc_reg::_EcfIdx);
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
