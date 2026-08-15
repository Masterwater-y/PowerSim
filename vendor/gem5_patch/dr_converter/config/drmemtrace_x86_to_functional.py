from __future__ import annotations

import argparse

import m5
from m5.objects import (
    Root,
    SrcClockDomain,
    System,
    VoltageDomain,
    X86Decoder,
    X86DrTraceConverter,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert x86 DynamoRIO threads directly to FastSim FST v6"
    )
    parser.add_argument("--input-trace", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--roi-begin-func-id", required=True, type=int)
    parser.add_argument("--roi-end-func-id", required=True, type=int)
    parser.add_argument("--num-cores", required=True, type=int)
    parser.add_argument("--div-sidecar-dir", required=True)
    return parser


args = _parser().parse_args()

system = System()
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock="1GHz",
    voltage_domain=system.voltage_domain,
)
system.decoder = X86Decoder()
system.converter = X86DrTraceConverter(
    decoder=system.decoder,
    input_trace=args.input_trace,
    output_dir=args.output_dir,
    roi_begin_func_id=args.roi_begin_func_id,
    roi_end_func_id=args.roi_end_func_id,
    expected_num_cores=args.num_cores,
    div_sidecar_dir=args.div_sidecar_dir,
)

root = Root(full_system=False, system=system)
m5.instantiate()
event = m5.simulate()
if event.getCode() != 0:
    raise SystemExit(event.getCode())
