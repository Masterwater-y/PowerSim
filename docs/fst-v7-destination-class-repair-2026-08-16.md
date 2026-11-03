# FST v7 destination-class repair (2026-08-16)

## Failure

The direct gem5 TaoTrace writer declared an FST v7 container and defined the
destination-class feature constant, but its hot-record path still wrote the
legacy producer-class bytes. On the previous C4/C8 formal dataset, a direct
record scan found feature bit 2 clear and zero marked records. For example,
C8 Neutron core 0 has 15,178,041 records, including 13,131,319 UOPs with an
architectural destination, and all 13,131,319 lack destination-class metadata.
The dataset builder copied native v7 inputs byte-for-byte and checked syscall
tables, but did not distinguish a v7 container from complete v7 timing input.

This omission does not alter replay under the prior default model because its
per-class rename free-list timing was disabled. It does invalidate experiments
that need exact Int/Float/Vec/CC allocation pressure and prevents a defensible
committed-pipeline resource attribution.

## Repair

- TaoTrace JSONL now emits `destination_class_counts` for every committed UOP.
- Direct FST output packs producer class in the low three bits and the matching
  destination count in the high five bits, sets record bit 31, and sets header
  feature bit 2. Zero-destination and syscall records carry an explicit zero
  vector rather than silently reverting to the legacy layout.
- Virtual-page token assignment preserves bit 31 instead of replacing the
  whole field.
- `audit_functional_warmup_matrix.py --require-destination-classes` checks
  every record, and both C4 gate and C4/C8 formal launchers enable that gate.
- `build_fst_v7_formal_dataset.py` now rejects incomplete destination classes
  by default. Its override is diagnostic-only and cannot be used as evidence
  for a formal timing dataset.
- FastSim's committed-pipeline audit is reset at the common warmup/measurement
  barrier while timing, dependency and resource state remain warm. The test
  now exercises real destination tokens across that boundary.

## Acceptance

A replacement dataset is acceptable only if every core satisfies all of the
following:

1. header version is 7 and feature bit 2 is set;
2. every hot record carries bit 31;
3. the four destination counts sum exactly to `n_dst` on every record;
4. syscall, warmup-boundary, virtual-page-map and oracle-identity gates still
   pass;
5. FastSim audit reports destination-token conservation and timing-neutral
   audit enable/disable results.

Only after this gate passes should per-class rename timing be evaluated as a
generic CPI candidate. It must not be assumed to fix Neutron: earlier
free-list ablations showed that register pressure alone is not the full CPI
tail mechanism.

## Implemented gate result

The parallel C4 100k gate at
`tmp/taotrace-fst-v7-destclass-c4-gate-20260816` passed for zstd, Graph500,
SPH, and NAMD:

- 4 configurations and 16 per-core FST files;
- 72,954,219 total records and 1,600,013 measurement records;
- 72,954,219/72,954,219 records carry destination-class markers;
- 57,199,438 UOPs with at least one destination have zero missing metadata;
- destination totals conserve exactly: 79,985,343 `n_dst` equals 45,769,345
  Int + 2,188,031 Float + 0 Vec + 32,027,967 CC operands;
- 72 syscall rows retain complete configured semantic fields;
- warmup/measurement boundaries, kernel oracle, syscall and virtual-page-map
  audits pass with zero integrity errors.

The final producer revision additionally counts destination operands before
dependency-identity de-duplication, matching gem5
`UnifiedRenameMap::canRename(inst)->numDestRegs(class)`. It compiled with the
requested 120-way build. The replacement 10-workload C4/C8 10M collection is
`tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816`.

## Formal C4/C8 closure

The replacement collection passed the complete formal gate:

- 20 configurations (10 C4 calibration and 10 core-count-held-out C8), 120
  per-core FST files, and zero integrity errors;
- 1,663,570,251 total records, of which 463,570,143 are functional warmup and
  1,200,000,108 are measurement records;
- every record carries the destination-class marker, all 1,417,937,936
  destination-bearing UOP rows conserve `n_dst`, and no metadata is missing;
- 828 syscall rows, virtual-page maps, kernel-event oracles, and the exact
  record-bounded warmup/measurement barriers all pass;
- the two formal accuracy pipelines reference 20/20 target-identical gem5
  results with zero oracle-profile mismatch.

The exact per-class free-list experiment is a negative result, not a timing
fix. Across all 120 cores it records zero rename free-list stall cycles and
reproduces baseline CPI bit-for-bit at aggregate precision:

| Split | baseline/candidate mean APE | P50 | P90 | P99 |
|---|---:|---:|---:|---:|
| C4 calibration | 13.543% / 13.543% | 11.338% / 11.338% | 25.186% / 25.186% | 40.449% / 40.449% |
| C8 held-out | 14.633% / 14.633% | 12.714% / 12.714% | 22.994% / 22.994% | 44.337% / 44.337% |

Finite rename state now works correctly across a functional-warmup barrier:
fully retired warmup allocations are drained while cache, branch, dependency,
controller and timing histories remain resident. The production default stays
`core.rename_free_list=false`; enabling it would add no accuracy on this
target. The remaining C8 tail correlates with gem5 wrong-path ROB/IQ pressure,
which destination counts for committed UOPs cannot reconstruct.

The complete accuracy and performance result is recorded in
[`fs-cpi-repair-validation-2026-08-16.md`](fs-cpi-repair-validation-2026-08-16.md).
