from m5.SimObject import SimObject
from m5.params import Param

from m5.objects.X86Decoder import X86Decoder


class X86QemuUserFstLowerer(SimObject):
    type = "X86QemuUserFstLowerer"
    cxx_class = "gem5::X86ISA::QemuFstConverter"
    cxx_header = "arch/x86/qemu_fst_converter.hh"

    decoder = Param.X86Decoder("official gem5 x86 decoder")
    input_trace = Param.String("QEMU user-only raw trace directory or file")
    output_dir = Param.String("directory for per-core FastSim FST v7 files")
    expected_num_cores = Param.UInt32("required logical core count")
    min_user_uops = Param.UInt64(
        "minimum measured CPL3 FST records per core; close after the full "
        "macro instruction that crosses the target"
    )
