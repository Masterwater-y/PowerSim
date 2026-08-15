from m5.SimObject import SimObject
from m5.params import Param

from m5.objects.X86Decoder import X86Decoder


class X86DrTraceConverter(SimObject):
    type = "X86DrTraceConverter"
    cxx_class = "gem5::X86ISA::DrTraceConverter"
    cxx_header = "arch/x86/dr_trace_converter.hh"

    decoder = Param.X86Decoder("gem5 x86 decoder")
    input_trace = Param.String("DynamoRIO canonical trace directory or file")
    output_dir = Param.String("directory for per-core FastSim FST v6 files")
    roi_begin_func_id = Param.UInt64("record_function id for per-thread ROI begin")
    roi_end_func_id = Param.UInt64("record_function id for per-thread ROI end")
    expected_num_cores = Param.UInt32("required logical core count")
    div_sidecar_dir = Param.String("retired DIV/IDIV operand sidecar directory")
