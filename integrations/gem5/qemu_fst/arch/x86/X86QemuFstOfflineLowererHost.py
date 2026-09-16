from m5.objects.BaseSimpleCPU import BaseSimpleCPU
from m5.objects.X86CPU import X86CPU
from m5.objects.X86MMU import X86MMU
class X86QemuFstOfflineLowererHost(BaseSimpleCPU, X86CPU):
    # A switched-out, never-activated lazy host for the official x86 Decoder,
    # X86ISA, ThreadContext and Fault ROM. It never ticks, issues no cache/TLB/
    # MMIO requests and runs no CPU model; it only decodes and fetches the
    # official micro-op sequence for offline FST v7 lowering.
    type = "X86QemuFstOfflineLowererHost"
    cxx_class = "gem5::X86ISA::X86QemuFstOfflineLowererHost"
    cxx_header = "arch/x86/qemu_fst_lowering_cpu.hh"

    mmu = X86MMU()
    @classmethod
    def memory_mode(cls):
        return "atomic"
