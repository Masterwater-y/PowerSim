# FastSim

FastSim is a trace-driven, multicore, non-cycle-accurate simulator for gem5
functional traces. Software-thread trace state is separate from hardware-core
timing state. In the current static scheduling stage it uses one permanent
host trace worker per bound thread (not per configured core) to decode chunks
and replay the resident core's branch predictor, while a deterministic
model-time frontier coordinator commits shared-memory events to configurable
private caches, directory/coherence state, CHA slices, LLC, and DRAM. The
frontier orders events consistently with FastSim's timing model; it does not
establish that the order matches gem5 or hardware.

The current implementation is deliberately split into confidence levels:

- L1D/L2/LLC-tag, per-CHA LLC lookup, and branch-predictor PMUs are explicit
  state-machine results and have differential validation against gem5.
- Total cycle/IPC is a configurable penalty estimate. It is useful for
  diagnostics, but neither the scalar baseline nor the experimental interval
  paths are accepted as a calibrated O3 replacement yet.

## Build and test

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -- -j16
cd build
ctest --output-on-failure
```

The reported host-throughput measurements use
`-DFASTSIM_ENABLE_NATIVE=ON`; functional results do not depend on that flag.

The sanitizer suite used during development is:

```bash
cmake -S . -B build-asan -DCMAKE_BUILD_TYPE=Debug \
  -DCMAKE_CXX_FLAGS="-fsanitize=address,undefined -fno-omit-frame-pointer"
cmake --build build-asan -- -j16
ASAN_OPTIONS=detect_leaks=0 UBSAN_OPTIONS=print_stacktrace=1 \
  ./build-asan/fastsim_tests
```

## Run a gem5 functional trace

FastSim accepts either gem5 JSONL directly or the canonical v6 binary format.
The reader remains compatible with v2-v5 binaries when the selected model does
not require v6 destination-register classes.
For the repository's aligned Parquet traces, conversion is vectorized and
uses functional columns only:

```bash
/data00/yinhaolang/infer/.venv/bin/python \
  tools/convert_aligned_parquet.py \
  --input '/path/to/tao_trace/*.aligned.parquet' \
  --out-dir tmp/fastsim-trace

./build/fastsim simulate \
  --config configs/gem5/v28_1-time-epoch.cfg \
  --manifest tmp/fastsim-trace/manifest.txt \
  --output tmp/fastsim-stats.json
```

A compatibility manifest has one dense, zero-based entry per active thread.
The number of entries may be smaller than `sim.cores`; stream `t` is then
statically bound to core `t`, and the remaining hardware cores stay idle:

```text
0 fastsim-binary core0.fst
1 fastsim-binary core1.fst
```

Full-system traces that include a functional prefix before a common ROI can
use the two-phase form below. FastSim replays the prefix into target state,
waits for every active stream at the macro-instruction boundary, resets only
measurement counters/time, and then consumes the bounded ROI:

```text
0 fastsim-binary-warmup-slice core0.fst 0 12000000 10000000
1 fastsim-binary-warmup-slice core1.fst 1 11800000 10000000
```

An optional fourth source-core ID permits explicit trace remapping while
retaining binary-header validation. For example, this creates a 64-core
throughput manifest from a four-core gem5 capture without copying trace data:

```bash
/data00/yinhaolang/infer/.venv/bin/python \
  tools/replicate_binary_manifest.py \
  --input tmp/fastsim-trace/manifest.txt \
  --output tmp/fastsim-trace/manifest64.txt \
  --copies 16

./build/fastsim simulate \
  --config configs/gem5/v28_1-c04.cfg \
  --cores 64 \
  --manifest tmp/fastsim-trace/manifest64.txt \
  --output tmp/fastsim-c64.json
```

For a complete convert/simulate/validate case:

```bash
/data00/yinhaolang/infer/.venv/bin/python tools/run_gem5_case.py \
  --fastsim build/fastsim \
  --config configs/gem5/v28_1-time-epoch.cfg \
  --trace-glob '/path/to/tao_trace/*.aligned.parquet' \
  --gem5-stats /path/to/stats.txt \
  --out-dir tmp/fastsim-validation
```

See [the input contract](docs/gem5-trace-contract.md) for required physical
address and branch fields.

## 64-core throughput

Release-build results on the current host, replaying 16 explicit mappings of
the four-core gem5 v28.1 GoFeed functional capture (64 simulated cores,
41,733,056 total instructions):

| Run | Aggregate MIPS |
|---:|---:|
| 1 | 26.6507 |
| 2 | 26.1270 |
| 3 | 25.9645 |
| Median | **26.1270** |

The minimum observed result is 25.96 times the required 1 MIPS. Replication is
a 64-core engine-throughput stress test, not a claim of 64-core workload
accuracy. A distinct real C32 GoFeed functional trace reached a three-run
median of 41.08 MIPS / 51.55 million UOP/s.

## Accuracy snapshot

The authoritative validation is the complete TCSim v28.1 seed0 corpus: 23
workloads each at native C4/C8/C16/C32. CPI uses TCSim's aggregate UOP
definition, `sum(core cycles) / sum(retired UOPs)`.

| Engine | C4 mean / median / P90 | C8 | C16 | C32 |
|---|---:|---:|---:|---:|
| Current C3-B1, fixed Q=1024 | 7.667% / 8.511% / 13.631% | 6.898% / 6.577% / 11.859% | 5.724% / 5.576% / 9.622% | 4.602% / 4.248% / 7.886% |
| gem5-parameterized Stage 3 | 12.263% / 7.221% / 22.597% | 12.637% / 6.616% / 34.279% | 13.436% / 8.540% / 26.659% | 14.639% / 12.564% / 36.044% |
| Time-epoch Stage 2 | 11.907% / 7.176% / 20.585% | 12.118% / 6.532% / 34.380% | 12.495% / 8.075% / 25.233% | 13.307% / 13.120% / 23.888% |
| Local TCSim v29 reference | 5.07% / 3.19% / 6.80% | 4.62% / 2.81% / 6.43% | — | — |

The current sparse response closure improves the historical headline CPI,
but still fails the independent 10% P99 gate: C4/C8/C16/C32 P99 is
13.843%/15.315%/12.303%/14.182%. All 92 simulator runs pass the independent
5M UOP/s gate. Aggregate cache/branch PMU counts are close, while DTLB miss
remains an explicit accuracy failure:

| Cores | L1D miss | Private L2 miss | CHA lookup | Branch miss | DTLB miss |
|---:|---:|---:|---:|---:|---:|
| 4 | 0.043% | 0.239% | 0.239% | 0.165% | 14.734% |
| 8 | 0.048% | 0.266% | 0.265% | 0.170% | 14.737% |
| 16 | 0.052% | 0.283% | 0.283% | 0.160% | 14.741% |
| 32 | 0.059% | 0.290% | 0.290% | 0.153% | 14.735% |

WAPE is total absolute count error divided by total gem5 count. Workload-equal
MAPE is 6.7%--11.9% for cache counters because compute microbenchmarks contain
only tens of reference misses. These PMU results validate aggregate L1D/L2,
CHA-lookup and branch-miss counts; they do not validate DTLB-miss accuracy,
cross-core event order, coherence messages, invalidations/upgrades, or
remote-supply timing. DTLB access WAPE is about 8.8% because gem5 counts
wrong-path load translations that a retired functional trace does not carry.
LLC tag misses are compared only with functional memory-path labels, not Ruby
protocol demand misses.

The `time_epoch` path preserves operation classes and producer distances,
models independently configurable pipeline widths/delays, ROB/IQ/LQ/SQ,
FU pools, memory ports, DTLB/page walkers, and cache miss capacity, decodes in
4096-UOP transport batches, and advances all cores over a fixed 1024-cycle
interval before weaving shared events. Cache/coherence/DRAM decisions consume
physical addresses while DTLB decisions consume an opaque virtual-page token.
Memory-free checkpoint segments may use an entry/exit-certified reduced
response loop; failed certificates retain the complete sparse scoreboard path.
The production response path also uses an incremental ROB block checkpoint:
it keeps the checkpoint-entry ring read-only and writes back only the final
ROB-sized exit window. Exact per-UOP ring writes remain available as an A/B
reference. Producer-created memory admission descriptors also remove the
feedback pass's repeated load/store event classification while preserving
atomic and MMIO semantics. Same-line inversion counting is now a shadow/CI
diagnostic rather than a production hot-path requirement. Conflicts are still
not repaired, so cross-core order and coherence timing remain uncertified.

Detailed commands, sources, and cycle-model caveats are in
[the validation report](docs/validation.md). The design and its differences
from TCSim and Zsim are in [the architecture document](docs/architecture.md).
The mapping from gem5 O3/Ruby/DRAM knobs to active FastSim parameters is in
[gem5 parameter coverage](docs/gem5-parameter-coverage.md).
The required interval execution protocol and acceptance gates are in
[the interval redesign](docs/interval-redesign.md). Stage 2 implementation,
ablation results, per-workload errors, and C4–C32 throughput are in
[the Stage 2 report](docs/time-epoch-stage2-validation.md).
Stage 3 implementation details, the full per-workload error table, DTLB
ablation, and C4--C32 throughput are in
[the gem5-parameterized Stage 3 report](docs/gem5-o3-dtlb-stage3-validation.md).
The stats-only gem5 SE microarchitecture sweep, first-batch matrix, and
one-command parallel collector are documented in
[the uarch generalization collection plan](docs/uarch-generalization-collection.md).
The CPI-error and host-throughput debugging playbook, including the FS C4
`lbm` case study and interview-ready summaries, is in
[the CPI/throughput debugging guide](docs/fastsim-cpi-throughput-debugging-interview.md).

## Current boundaries

- Input scope is gem5 functional JSONL or aligned Parquet converted to v5
  binary. The runtime consumes this already-lowered canonical functional IR
  and deliberately has no ISA decoder. Raw drmemtrace is not a current input;
  its planned path is an offline DR-to-FST adapter, and virtual-only DR traces
  cannot claim strict physical cache/coherence/CHA/DRAM equivalence.
- The scheduler stage currently requires `threads <= cores`, an injective
  static binding, and no migration/time slicing. `ThreadTraceBinding` already
  preserves thread ID, address-space ID, and initial/final core so a later
  scheduler can add oversubscription without putting trace ownership back in
  the core.
- An explicit functional `is_syscall` record is modeled as ROB drain, system-FU
  execution, optional `syscall.service_latency`, and frontend restart. No
  guest-kernel instruction stream is fabricated. Blocking/wakeup duration,
  futex semantics, migration, and context-switch cost require a future
  syscall/scheduling event sidecar and are not modeled in this stage.
- Cache PMUs currently cover L1D, private data-side L2, and shared LLC. The
  interval core now has configurable DTLB/page-walk timing and PMUs; L1I and
  ITLB are not implemented.
- Coherence is a deterministic directory/MESI approximation, not a complete
  Ruby protocol state machine.
- Per-CHA LLC lookup volume is validated. Permission-upgrade and snoop message
  classes are exposed but still need direct Ruby message-class validation.
- Scalar cycles omit a detailed ROB, dependency, functional-unit, I-cache,
  and TLB model. `interval_weave` adds configurable pipeline widths/delays,
  ROB/IQ/LQ/SQ, dependency/FU constraints, DTLB/page-walk timing, and batch
  feedback, but not physical-register/StoreSet/SMT state, I-cache/ITLB timing,
  wrong-path execution, or accepted event-level conflict replay. Its Stage 3
  C4–C32 mean error remains 12.26%–14.64%, so cycle and coherence-order
  accuracy are not claimed.
