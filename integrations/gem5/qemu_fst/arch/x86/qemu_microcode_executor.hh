/* Execute one x86 macro-op against QEMU architectural evidence. */

#ifndef __ARCH_X86_QEMU_MICROCODE_EXECUTOR_HH__
#define __ARCH_X86_QEMU_MICROCODE_EXECUTOR_HH__

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "arch/x86/qemu_fst_types.hh"
#include "arch/x86/qemu_microop_descriptor.hh"

namespace gem5
{
namespace X86ISA
{

class QemuMicrocodeExecutor
{
  public:
    QemuMicrocodeExecutor();
    ~QemuMicrocodeExecutor();

    QemuMicrocodeExecutor(const QemuMicrocodeExecutor &) = delete;
    QemuMicrocodeExecutor &operator=(const QemuMicrocodeExecutor &) = delete;

    bool execute(const QemuPendingInst &inst,
                 const QemuX86FunctionalState &preState,
                 const StaticInstPtr &macro,
                 const std::string &macroMnemonic,
                 QemuMicroopDescriptorCache &descriptors,
                 std::vector<QemuExpandedMicroop> &expanded,
                 std::vector<QemuDataRef> &boundRefs,
                 uint64_t *scalarSinglePaddingCount,
                 bool &allEvidenceConsumed);

  private:
    class Context;
    std::unique_ptr<Context> context;
};

} // namespace X86ISA
} // namespace gem5

#endif // __ARCH_X86_QEMU_MICROCODE_EXECUTOR_HH__
