from m5.SimObject import SimObject
from m5.params import Param, VectorParam
from m5.util.pybind import PyBindMethod


class X86QemuFstTransfer(SimObject):
    type = "X86QemuFstTransfer"
    cxx_class = "gem5::X86ISA::X86QemuFstTransfer"
    cxx_header = "arch/x86/qemu_fst_transfer.hh"

    # The offline lowerer is not a simulated CPU. Python drives the whole
    # conversion by calling convert() directly after m5.instantiate(); there
    # is no conversion event, no scheduled tick and no m5.simulate().
    cxx_exports = [PyBindMethod("convert")]

    input_shard = Param.String(
        "single raw shard this worker lowers for its logical core"
    )
    output_dir = Param.String("directory for per-core FastSim FST v7 files")
    expected_num_cores = Param.UInt32("required logical core count")
    user_macro_target = Param.UInt64(
        "first-core measured CPL3 macro target"
    )
    trigger_core = Param.UInt32(
        "logical core that first reached the macro target"
    )
    worker_core_index = Param.Int32(
        "logical core lowered by this internal worker"
    )
    cpus = VectorParam.X86QemuFstOfflineLowererHost(
        "the worker's transactional lowering CPU"
    )
