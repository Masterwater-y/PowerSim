/* Bind QEMU memory callbacks to statically lowered x86 micro-ops. */

#ifndef __ARCH_X86_QEMU_MEMORY_BINDER_HH__
#define __ARCH_X86_QEMU_MEMORY_BINDER_HH__

#include <cstddef>
#include <cstdint>
#include <vector>

#include "arch/x86/qemu_fst_types.hh"
#include "base/types.hh"

namespace gem5
{
namespace X86ISA
{

struct QemuStaticMemoryOp
{
    size_t microopIndex = 0;
    uint64_t dataSize = 0;
    uint64_t displacement = 0;
    uint64_t partitionOffset = 0;
    bool hasDisplacement = false;
    bool isLoad = false;
    bool isStore = false;
    bool isAtomic = false;
};

struct QemuStaticMemoryPlan
{
    std::vector<QemuStaticMemoryOp> operations;
    uint64_t partitionSize = 0;
    bool hasPartition = false;
};

class QemuMemoryBinder
{
  public:
    static void buildStaticPlan(QemuStaticMemoryPlan &plan);

    static void bindStatic(
        const QemuStaticMemoryPlan &plan,
        const std::vector<QemuDataRef> &evidence,
        std::vector<QemuDataRef> &bound,
        Addr pc,
        const char *mnemonic);
};

} // namespace X86ISA
} // namespace gem5

#endif // __ARCH_X86_QEMU_MEMORY_BINDER_HH__
