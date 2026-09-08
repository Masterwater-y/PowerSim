"""Lower a QEMU user-only raw trace into canonical FastSim FST v7."""

import argparse
import os

import m5
from m5.objects import Root, X86Decoder, X86QemuUserFstLowerer


parser = argparse.ArgumentParser()
parser.add_argument("--input-trace", required=True)
parser.add_argument("--output-dir", required=True)
parser.add_argument("--num-cores", required=True, type=int)
parser.add_argument("--measurement-user-uops", required=True, type=int)
args = parser.parse_args()

if not os.path.isdir(args.output_dir):
    parser.error("--output-dir must already exist")

root = Root(full_system=False)
root.lowerer = X86QemuUserFstLowerer(
    decoder=X86Decoder(),
    input_trace=args.input_trace,
    output_dir=args.output_dir,
    expected_num_cores=args.num_cores,
    min_user_uops=args.measurement_user_uops,
)

m5.instantiate()
event = m5.simulate()
if event.getCause() != "simulate() limit reached":
    print(f"QEMU user-only FST lowering finished: {event.getCause()}")
