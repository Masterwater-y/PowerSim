#include "arch/x86/qemu_memory_binder.hh"

#include <algorithm>
#include <cinttypes>
#include <vector>

#include "base/logging.hh"
#include "common/trace_entry_extensions.h"

namespace gem5
{
namespace X86ISA
{
namespace
{

constexpr uint64_t kFastSimPageSize = 4096;
constexpr uint32_t kQemuMemAttrAtomic =
    qemu_trace_extensions::kMemoryAttributeAtomic;

} // namespace

void
QemuMemoryBinder::buildStaticPlan(QemuStaticMemoryPlan &plan)
{
    plan.partitionSize = 0;
    plan.hasPartition = false;
    if (plan.operations.size() < 2) {
        return;
    }
    for (const auto &operation : plan.operations) {
        if (!operation.hasDisplacement) {
            return;
        }
        plan.partitionSize += operation.dataSize;
    }
    for (const auto &candidate : plan.operations) {
        std::vector<uint8_t> covered(plan.partitionSize, 0);
        bool valid = true;
        for (const auto &operation : plan.operations) {
            const uint64_t offset =
                operation.displacement - candidate.displacement;
            if (offset > plan.partitionSize ||
                operation.dataSize > plan.partitionSize - offset ||
                std::any_of(
                    covered.begin() + offset,
                    covered.begin() + offset + operation.dataSize,
                    [](uint8_t value) { return value != 0; })) {
                valid = false;
                break;
            }
            std::fill_n(
                covered.begin() + offset, operation.dataSize, uint8_t{1});
        }
        if (!valid ||
            std::any_of(
                covered.begin(), covered.end(),
                [](uint8_t value) { return value == 0; })) {
            continue;
        }
        for (auto &operation : plan.operations) {
            operation.partitionOffset =
                operation.displacement - candidate.displacement;
        }
        plan.hasPartition = true;
        return;
    }
}

void
QemuMemoryBinder::bindStatic(
    const QemuStaticMemoryPlan &plan,
    const std::vector<QemuDataRef> &evidence,
    std::vector<QemuDataRef> &bound,
    Addr pc,
    const char *mnemonic)
{
    bound.clear();
    bound.reserve(plan.operations.size());
    if (plan.operations.size() == evidence.size()) {
        for (size_t index = 0; index < plan.operations.size(); ++index) {
            const auto &operation = plan.operations[index];
            const auto &ref = evidence[index];
            fatal_if(ref.size != operation.dataSize,
                     "static memory reference size differs from its micro-op "
                     "pc=%#x micro=%zu ref=%" PRIu64 " required=%" PRIu64,
                     pc, operation.microopIndex, ref.size,
                     operation.dataSize);
            bound.push_back(ref);
        }
    } else if (evidence.size() == 1 && plan.operations.size() > 1) {
        const auto &ref = evidence.front();
        fatal_if(!plan.hasPartition || ref.size != plan.partitionSize,
                 "static memory callback has no complete displacement cover "
                 "pc=%#x callback=%" PRIu64 " micro_total=%" PRIu64,
                 pc, ref.size, plan.partitionSize);
        for (const auto &operation : plan.operations) {
            const uint64_t offset = operation.partitionOffset;
            fatal_if(offset > ref.size ||
                         operation.dataSize > ref.size - offset,
                     "memory micro-op slice exceeds callback pc=%#x "
                     "micro=%zu",
                     pc, operation.microopIndex);
            auto slice = ref;
            slice.vaddr += offset;
            slice.paddr += offset;
            slice.virtual_page = slice.vaddr / kFastSimPageSize;
            slice.virtual_page_token =
                slice.virtual_page == ref.virtual_page
                    ? ref.virtual_page_token : 0;
            slice.size = operation.dataSize;
            slice.hasValue = false;
            slice.valueSize = 0;
            bound.push_back(std::move(slice));
        }
    } else {
        fatal("static memory evidence cannot be bound without dynamic "
              "addresses pc=%#x mnemonic=%s callbacks=%u memory_uops=%u",
              pc, mnemonic, unsigned(evidence.size()),
              unsigned(plan.operations.size()));
    }

    for (size_t index = 0; index < plan.operations.size(); ++index) {
        const auto &operation = plan.operations[index];
        const auto &ref = bound[index];
        fatal_if(ref.is_store ? !operation.isStore : !operation.isLoad,
                 "memory direction mismatch pc=%#x micro=%zu callback=%s",
                 pc, operation.microopIndex,
                 ref.is_store ? "write" : "read");
        fatal_if(bool(ref.attributes & kQemuMemAttrAtomic) !=
                     operation.isAtomic,
                 "memory atomic attribute mismatch pc=%#x micro=%zu",
                 pc, operation.microopIndex);
    }
}

} // namespace X86ISA
} // namespace gem5
