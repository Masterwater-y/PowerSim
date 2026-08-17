# FST instruction-map v2 operand pilot (2026-08-17)

## 1. Decision

The shared FST input can now carry static x86-64 register dependency masks
from either gem5 TaoTrace or a future ordinary drmemtrace module decoder.
FastSim validates and traverses those masks successfully. The operand fields
are still audit-only: they do not allocate rename, IQ, ROB, or LSQ state and
do not change CPI or architectural PMU counters.

This is therefore an accepted **input-observability repair**, not a CPI
accuracy result. The formal C4/C8 accuracy numbers in
[`fs-cpi-repair-validation-2026-08-16.md`](fs-cpi-repair-validation-2026-08-16.md)
remain unchanged.

## 2. Portable contract and implementation

`.fst.imap` v2 retains the v1 static geometry prefix and adds two 128-bit
little-endian register masks per row. The masks describe may-read/may-write
architectural rename dependencies. They exclude timing, target UOP counts,
gem5 operation classes, ports, physical registers, predictor outcomes,
cache/TLB state, PMU values, and oracle labels.

The byte-level and canonical-register contract is in
[`fst-v7-drmemtrace-conversion-contract.md`](fst-v7-drmemtrace-conversion-contract.md).
FastSim's reader/writer is implemented in `src/trace.cpp`; the producer-neutral
JSONL adapter and strict gate are `tools/build_fst_instruction_map.py` and
`tools/audit_fst_static_instruction_maps.py`.

The integrated gem5 producer changes are in:

```text
/data00/yinhaolang/gem5-fs/src/cpu/o3/probe/tao_trace.hh
/data00/yinhaolang/gem5-fs/src/cpu/o3/probe/tao_trace.cc
```

TaoTrace aggregates the source/destination dependencies of committed micro-ops
into one macro-instruction row. For a PC reached through REP or another
microcoded path, repeated observations are merged as a conservative static
may-read/may-write union. The producer sets operand-complete, but deliberately
leaves executable-scope-complete clear because dynamically observed committed
PCs are not a complete executable image.

The collector change in
`/data00/yinhaolang/TCSim/scripts/gem5_fs_roi.py` promotes `coreN.fst.imap`
beside `coreN.fst` and records its path, SHA-256, and byte size in `trace.json`.

## 3. Fail-closed issues found during integration

Two producer defects were exposed and corrected before accepting the pilot:

1. REP/microcode executions exposed different subsets of operands for the same
   macro PC. Requiring equality would reject legal x86 behavior, so the static
   row now stores the deterministic union over observations.
2. Direct-branch target decoding initially reused the committed dynamic
   `PCState`, leaking the resolved next PC into a static fact. The producer now
   constructs a clean PC/size/fallthrough state before calling the decoder.

Geometry and direct-target conflicts still fail closed. Missing operand maps,
partial operand coverage under `--require-operands`, invalid masks, reserved
bits, source-core mismatch, and source-record-count mismatch are also hard
errors.

## 4. Integrated C4 Neutron pilot

The retained pilot root is:

```text
tmp/taotrace-imap-v2-pilot-r4-20260817
```

It uses four cores, source-level functional warmup, and approximately 100,000
measurement FST records per core. This deliberately short run validates the
format/producer/collector/consumer path; it is not large enough for a formal
CPI comparison.

### Static-map gate

Artifact:

```text
tmp/taotrace-imap-v2-pilot-r4-20260817/audit/static-instruction-maps.json
```

| Check | Result |
|---|---:|
| FST / map files | 4 / 4 |
| Valid v2 maps | 4 |
| Static rows | 1,190 |
| Operand-valid rows | 1,190 |
| Operand coverage in map | 100% |
| Rows with reads | 988 |
| Rows with writes | 953 |
| Branch rows | 173 |
| Memory rows | 360 |
| Integrity errors | 0 |
| Executable scope complete | no, by design |

### Warmup boundary

| Core | Warmup records | Warmup macro instructions | Measurement records |
|---:|---:|---:|---:|
| 0 | 4,780,584 | 2,670,481 | 100,001 |
| 1 | 4,899,948 | 2,705,743 | 100,001 |
| 2 | 0 | 0 | 100,001 |
| 3 | 4,948,989 | 2,722,271 | 100,000 |

Core 2 legitimately had no emitted user record before the global source
marker in this short run. The aggregate warmup is non-empty and the exact
per-core boundary is preserved. This asymmetry is another reason not to use
the 100k pilot as an accuracy datapoint.

### FastSim traversal

Artifact:

```text
tmp/taotrace-imap-v2-pilot-r4-20260817/audit/fastsim-speculative-path.json
```

| Counter | Value |
|---|---:|
| Measurement user UOPs | 400,003 |
| Speculative-path records | 306,657 |
| Traversed static instructions | 306,628 |
| Operand-valid traversals | 306,628 (100%) |
| Register reads observed | 454,491 |
| Register writes observed | 317,123 |
| Static-map misses | 0 |
| Unknown edges | 62 |
| Causal committed-PC profile coverage | 305,922 / 306,628 (99.77%) |

The reported FastSim CPI from this run is not compared with gem5: the run is
short, one core has no warmup prefix, and the existing weighted gem5 CPI and
FST user-UOP denominator are not an equivalent formal accuracy pair. Operand
coverage proves that the new input reaches the runtime; it does not prove a
timing correction.

## 5. Data retention

The failed r1/r2 pilots, the r3 duplicate lacking promoted instruction maps,
and r4's boundary-only scratch directory were deleted after verifying that no
formal index referenced them. The r4 FST, oracle/request outputs, manifest,
trace metadata, and audit artifacts are retained. The earlier formal v4
dataset remains valid and unchanged, but it has no `.fst.imap` v2 operands.

## 6. Next timing gate

The next model change should remain producer-neutral and parameter-frozen:

1. derive audit-only dependency-chain, live-destination, and IQ/ROB occupancy
   bounds from v2 masks plus the existing causal macro-to-UOP profile;
2. compare those bounds with gem5 wrong-path rename/IQ/squash counters on C4
   Neutron and at least one low-error control workload;
3. enable a timing effect only if resource conservation and direction hold
   without workload IDs or per-workload coefficients;
4. freeze the C4 parameters and validate C8 before collecting a full new
   C4/C8 formal matrix.

The existing 10M formal dataset does not need to be deleted or recollected for
this audit stage. New operand-bearing traces are required only for candidate
timing experiments; a full recollection is justified after the small pilot
shows a held-out CPI improvement without degrading PMU state.

## 7. Dependency and exact wrong-path gate outcome

This gate is now complete. The input-side resource estimator passed its small
held-out audit, but no timing term was promoted.

### 7.1 Corrections made before scoring

Two general implementation errors were fixed:

1. `IntervalCoreModel` could discard a predictor-built static fallthrough path
   merely because the same fallthrough had not yet appeared in committed PC
   history. The first PC supplied by the pre-repair predictor snapshot is now
   the authoritative speculative entry. In the C4 controls this reduced
   untracked entries to zero; PMU was identical and CPI changed by at most
   0.55% because only the already-enabled speculative L1I state was affected.
2. The resolution-time fetch budget is measured in UOPs, but the first
   dependency audit applied it only as a macro-record traversal limit. The
   renamed/ROB operand prefix is now capped by both available ROB UOPs and the
   fetch UOP budget. This second repair is audit-only: C4 Neutron CPI and PMU
   were bitwise unchanged while its estimated prefix fell from 490,084 to
   305,083 UOPs.

The first comparison also incorrectly placed user-only FastSim estimates next
to all-CPL gem5 `commitSquashedInsts`/rename counters. Graph500 made the scope
failure obvious: its 100k pilot had 5 retired user branch misses but 1,300
kernel branch misses. The final gate therefore uses only TaoTrace
`wrong_path.jsonl` v3 rows whose cause and victim UOPs are CPL3. The sidecar is
oracle-only and is never consumed by FastSim inference. Enabling it does not
alter the functional input: all four C4 Neutron FST SHA-256 values match the
earlier non-oracle r4 pilot exactly.

### 7.2 Exact pilot identity and integrity

The retained roots are:

```text
tmp/taotrace-imap-v2-wrong-path-c4-pilots-20260817
tmp/taotrace-imap-v2-wrong-path-c8-pilots-20260817
```

C4 and C8 each contain Neutron plus the independent NAb control, with 100k
measurement user UOPs per core, source warmup, `.fst.imap` v2, dual-CPL PMU,
and the CPL-aware wrong-path sidecar. The C4/C8 gates contain 8/16 FST files
and 800,005/1,600,009 measurement records respectively. Both matrix audits
report zero integrity errors; static-map operand coverage and both v3
wrong-path validators pass.

FastSim branch occurrence and static-path handoff are already close to the
retired user oracle:

| Case | FastSim/oracle user branch misses | miss APE | operand path/FastSim miss |
|---|---:|---:|---:|
| C4 Neutron | 3,019 / 2,967 | 1.75% | 99.30% |
| C4 NAb | 436 / 476 | 8.40% | 97.02% |
| C8 Neutron | 5,747 / 5,711 | 0.63% | 99.74% |
| C8 NAb | 741 / 757 | 2.11% | 97.30% |

Nested redirects can make accepted CPL3 branch episodes exceed retired misses;
the exact episode totals are 3,407/480 at C4 and 6,478/769 at C8. They are not
double-counted PMU events.

### 7.3 Frozen resource-scale held-out result

Only C4 Neutron selects scales. C4 NAb is workload-held-out and both C8 cases
are core-count-held-out. The selected values are 0.627033 for renamed UOPs and
0.703170 for renamed memory UOPs. Destination operands are reported as a
negative control because gem5 dynamic `n_dst` and the portable canonical
architectural mask do not have identical semantics.

| Held-out case | renamed APE | renamed-memory APE | destination APE |
|---|---:|---:|---:|
| C4 NAb | 5.65% | 0.75% | 21.86% |
| C8 Neutron | 3.58% | 1.25% | 2.89% |
| C8 NAb | 11.70% | 9.81% | 14.37% |

Thus the renamed and memory population estimators stay below 12% on these
three held-out points. The destination estimator is rejected. This is a small
mechanism gate, not a formal CPI/PMU accuracy distribution and not permission
to fit per-workload coefficients.

### 7.4 Timing decision

Accurate wrong-path population still does not identify additive lost cycles.
The exact union of active wrong-path windows covers only the following part of
the same-window FastSim CPI deficit:

| Case | gem5 − FastSim user CPI | active-window share of gap | per-core gap/window Pearson | `FastSim + ceiling` APE |
|---|---:|---:|---:|---:|
| C4 Neutron | 0.380950 | 46.78% | -0.350 | 19.30% |
| C4 NAb | 0.061025 | 33.24% | -0.960 | 14.29% |
| C8 Neutron | 0.429884 | 38.34% | -0.569 | 26.38% |
| C8 NAb | 0.074406 | 25.98% | -0.428 | 18.88% |

The active window is already a deliberately pessimistic ceiling, not an
additive cost, and its per-core direction is opposite the remaining CPI gap in
all four cases. Consequently no wrong-path cycle, IQ/ROB occupancy, rename,
or memory penalty is enabled. The formal 10M CPI/PMU/throughput report remains
unchanged. The next CPI repair must return to the committed-path
dependency/memory-response ledger and explain cycles not conserved there;
the accepted wrong-path estimator may serve only as an audit feature.

Primary machine-readable artifacts are:

```text
tmp/taotrace-imap-v2-wrong-path-c4-pilots-20260817/audit/dependency-workload-heldout-c4.json
tmp/taotrace-imap-v2-wrong-path-c8-pilots-20260817/audit/dependency-frozen-c4-to-c8.json
tmp/taotrace-imap-v2-wrong-path-c8-pilots-20260817/audit/dependency-heldout-c8.json
```
