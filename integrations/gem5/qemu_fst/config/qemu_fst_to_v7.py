"""Lower QEMU native-kernel transactions into canonical FastSim FST v7."""

import argparse
import os

import m5
from m5.objects import (
    Root,
    SrcClockDomain,
    System,
    VoltageDomain,
    X86QemuFstOfflineLowererHost,
    X86QemuFstTransfer,
)


parser = argparse.ArgumentParser()
parser.add_argument("--input-trace", required=True)
parser.add_argument("--input-shard", required=True)
parser.add_argument("--output-dir", required=True)
parser.add_argument("--num-cores", required=True, type=int)
parser.add_argument("--user-fst-target", required=True, type=int)
parser.add_argument("--worker-core-index", required=True, type=int)
args = parser.parse_args()

if not os.path.isdir(args.output_dir):
    parser.error("--output-dir must already exist")
if not 2 <= args.num_cores <= 32:
    parser.error("--num-cores must be in [2,32]")

system = System(mem_mode="atomic")
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock="1GHz", voltage_domain=system.voltage_domain
)

if not 0 <= args.worker_core_index < args.num_cores:
    parser.error("--worker-core-index must identify one configured core")
cpus = [
    X86QemuFstOfflineLowererHost(cpu_id=0, switched_out=True)
]
system.cpus = cpus
for cpu in system.cpus:
    cpu.createThreads()

system.transfer = X86QemuFstTransfer(
    input_trace=args.input_trace,
    input_shard=args.input_shard,
    output_dir=args.output_dir,
    expected_num_cores=args.num_cores,
    user_fst_target=args.user_fst_target,
    worker_core_index=args.worker_core_index,
    cpus=cpus,
)

root = Root(full_system=True, system=system)
# The offline lowerer only constructs official Decoder/ISA/ThreadContext
# objects. Conversion is a direct call, not a simulated event queue: after
# instantiate() we invoke the exported convert() and never call m5.simulate().
m5.instantiate()
system.transfer.convert()

