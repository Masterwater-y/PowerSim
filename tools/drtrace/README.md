# DynamoRIO to FastSim FST

This producer-local toolchain captures x86-64 user execution with DynamoRIO,
lowers the recorded instruction stream through gem5's x86 decoder, and writes
canonical FastSim FST v6. FastSim runtime still consumes FST only; raw
drmemtrace is never a simulator input. The global wire contract remains
`docs/gem5-trace-contract.md`, while current DynamoRIO status is tracked in
`docs/dynamorio-trace-conversion-status.md`.

## Requirements

Strict DynamoRIO conversion requires marker-backed physical addresses.
`collect-dr-trace` passes DynamoRIO's `-use_physical` option so captures carry
VA-to-PA marker evidence. Missing, masked, malformed, conflicting, cross-page,
or cross-address-space mappings fail closed.

Where the host grants pagemap access through sudoers, use `--sudo` on
`capture` or `collect-dr-trace`. The option uses non-interactive `sudo -n` and
cannot override host policy that masks `/proc/*/pagemap` PFNs.

Workload ROI code is shared under `workloads/common/roi`. The same workload
source builds two ABI variants: gem5 binaries under `bin/gem5` keep the m5
WORKBEGIN/WORKEND pseudo-op path, while DynamoRIO binaries under
`bin/dynamoRIO` expose `cpu_microarch_roi_thread_begin/end` for
`-record_function`.

## Command Flow

Build the reference gem5 producer and, when needed, the DynamoRIO converter:

```bash
tools/build_gem5.sh
TARGET=dr MODE=apply-and-build tools/build_gem5.sh
```

Run the staged matrix flow:

```bash
/data00/xuhaoen/py3.11/bin/python3.11 -m tools.drtrace collect-gem5-trace \
  --matrix configs/workloads/uarch_first.json --skip-build

/data00/xuhaoen/py3.11/bin/python3.11 -m tools.drtrace collect-dr-trace \
  --matrix configs/workloads/uarch_first.json --skip-build

/data00/xuhaoen/py3.11/bin/python3.11 -m tools.drtrace convert-gem5-fst \
  --matrix configs/workloads/uarch_first.json --fastsim build/fastsim

/data00/xuhaoen/py3.11/bin/python3.11 -m tools.drtrace convert-dr-fst \
  --matrix configs/workloads/uarch_first.json --fastsim build/fastsim
```

Validate FST pairs and run replay diagnostics:

```bash
/data00/xuhaoen/py3.11/bin/python3.11 -m tools.drtrace validate \
  --matrix configs/workloads/uarch_first.json --fst-root tmp/dr-fst \
  --out tmp/dr-validation-uarch-first

/data00/xuhaoen/py3.11/bin/python3.11 -m tools.drtrace simulate-replay \
  --matrix configs/workloads/uarch_first.json --fst-root tmp/dr-fst \
  --fastsim build/fastsim \
  --config configs/gem5/v28_1-c04.cfg \
  --dr-config configs/dynamoRIO/physical-v28_1-c04.cfg

/data00/xuhaoen/py3.11/bin/python3.11 -m tools.drtrace validate-replay \
  --matrix configs/workloads/uarch_first.json --fst-root tmp/dr-fst \
  --out tmp/dr-replay-validation-uarch-first
```

For one-off conversion of a captured DynamoRIO trace:

```bash
/data00/xuhaoen/py3.11/bin/python3.11 -m tools.drtrace convert \
  --trace-dir /path/to/drmemtrace.* \
  --output-dir tmp/dr-fst/manual-case --num-cores 4
```

## Artifacts

Matrix roots are partitioned by matrix stem:

- Raw capture root: `tmp/dr-traces/<matrix>/cXX/<workload>/{gem5,dr}`
- Final FST root: `tmp/dr-fst/<matrix>/cXX/<workload>/{gem5,dr}`
- Replay outputs: `tmp/dr-fst/<matrix>/cXX/<workload>/replay`

`convert-gem5-fst` consumes gem5 `tao_trace/*.records.micro.jsonl`, writes
`coreN.fst` and `manifest.txt`, validates the manifest, then deletes the raw
`tao_trace` directory. Failed conversion keeps the raw trace for debugging.

Each DynamoRIO FST conversion writes:

- `coreN.fst`
- `manifest.txt`
- `trace.json`
- `address-provenance.json`

The provenance sidecar records the accepted PID and VA-page/PA-page/token
mapping for each logical core. FST v6 has no ASID field, so crossing address
spaces within one output core is rejected.

## Validation Boundaries

`validate` is the DR conversion gate. It checks FST structure and
core-reconstructable fields: PCs, branch facts, memory classification, op
class, dependency metadata, producer classes, destination class counts, and
syscall number when syscall records are present. Raw PA values and raw
virtual-token IDs are not compared across gem5 and DynamoRIO because they come
from independent physical and virtual-page namespaces.

`simulate-replay` and `validate-replay` are downstream diagnostics. Functional
totals are exact-match checks; cache, CHA, DRAM, and timing-derived counters
remain diagnostic-only unless a separate topology acceptance layer is defined.

Known boundaries:

- `v28_int_div_serial` is unsupported because stock drmemtrace lacks gem5's
  dynamic internal microPC evidence.
- The `business_excitation` matrix exercises the same boundary much more
  broadly. Static inspection finds runtime integer `div` in 11 of its 12
  DynamoRIO binaries; the three captured cases
  (`mysql_hot_index_48k`, `gofeed_shared_hotspot`, and
  `gofeed_graph_48m_random`) all stop at
  `dynamic_internal_microcode_control`. The common source pattern is
  `uniform_line(lines, key)`, whose `key % lines` uses a runtime divisor.
- Cross-page memory records fail closed because one FST v6 record carries one
  address and one virtual-page token.
- Syscall arguments, return values, blocking behavior, and kernel duration are
  outside the hot FST record.

## DIV/IDIV Semantic Boundary

DynamoRIO decodes the architectural x86 instruction correctly. The failure is
in the current requirement that a DynamoRIO producer emit the same dynamic
micro-op stream as gem5. gem5 implements x86 `DIV` and `IDIV` with
`Div1`/`Div2` operations and an internal conditional microcode loop before
`Divq`/`Divr`. The loop path depends on runtime dividend and divisor values.
Stock drmemtrace records the retired architectural instruction, its encoding,
architectural branch results, and memory references; it does not record gem5's
private microPC or the runtime state needed to execute that loop.

There are two coherent designs:

1. **DynamoRIO architectural FST.** Use the official drmemtrace
   `decode_cache_t` and `DR_ISA_REGDEPS` model to preserve macro-instruction
   categories and architectural register dependencies. FastSim consumes or
   lowers these architectural records using its own timing model. Cross-source
   validation compares a macro-instruction projection, not gem5-private
   micro-ops. `DIV` is one architectural integer-division record in this
   design. This path needs FastSim and validation-contract work, but no custom
   DynamoRIO register-value client and no gem5 division state machine.
2. **gem5-style micro-op FST.** Keep the current strict per-micro-op contract.
   A custom DynamoRIO client must selectively capture pre-instruction state
   such as `RAX`, `RDX`, and the divisor, including commit/fault handling. A
   version-matched gem5 microcode executor must then replay the dynamic path.
   This preserves pairwise micro-op validation but couples the collector and
   converter to gem5 internals and has the highest maintenance cost.

Emitting a single synthetic `IntDiv` record while continuing to label the file
as gem5-style strict FST is not supported: it changes record counts, micro-op
flags, internal branch facts, and producer distances. Avoiding `div` in the
workload can unblock experiments, but is a workload change rather than a trace
conversion solution.

Official reference points:

- DynamoRIO core-simulation trace model:
  <https://dynamorio.org/sec_drcachesim_core.html>
- DynamoRIO instruction decode cache:
  <https://dynamorio.org/classdynamorio_1_1drmemtrace_1_1decode__cache__t.html>
- DynamoRIO synthetic register-dependency ISA:
  <https://dynamorio.org/dr__ir__encode_8h.html>
- gem5 x86 micro-op ISA:
  <https://www.gem5.org/documentation/general_docs/architecture_support/x86_microop_isa/>
- gem5 TraceCPU and Elastic Trace, where gem5 itself is the micro-op/dependency
  producer: <https://www.gem5.org/documentation/general_docs/cpu_models/TraceCPU>
