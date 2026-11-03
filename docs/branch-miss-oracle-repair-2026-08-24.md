# Retired branch-miss oracle repair (2026-08-24)

## Outcome

The previously reported branch-miss P99 of 48.86% is not a valid predictor
accuracy result. Its TaoTrace reference sampled `DynInst::mispredicted()` at
retirement. gem5 can detect a direct-target/BTB miss in Decode, squash the
younger path, and replace the DynInst predicted target with the correct target.
The later comparison then returns false even though BPred retains and commits
the original miss.

The repaired source is `taotrace-retired-bpred-v1`. The validator now rejects
all v3 PMU documents that do not explicitly carry that source, so an old trace
cannot silently produce another misleading MAPE or P99.

## Event contract

The repaired path is:

1. Fetch creates the normal BPred history and prediction; no timing changes.
2. Decode target repair or IEW direction/target resolution sets a sticky
   `BranchPredMispredicted` bit on the responsible DynInst before redirect.
3. Squash removes younger wrong-path instructions exactly as before.
4. If the responsible control instruction retires, TaoTrace counts the sticky
   bit in its CPL-scoped PMU row. A squashed branch never reaches this point.
5. The per-core and aggregate oracle rows identify the source as
   `taotrace-retired-bpred-v1`.

This preserves the committed branch-predictor outcome without adding wrong-path
instructions to FST and without changing the FastSim predictor. The external
producer overlay is `patches/p5-external-retired-bpred-oracle.patch`.

## Existing-data audit

Old traces cannot be relabeled instruction by instruction, but the final gem5
`branchPred.committed` and `branchPred.mispredicted` statistics can diagnose
cores whose final stats population closely matches the local TaoTrace window.
Using a maximum branch-population skew of 0.1% selected 14 of 600 per-core rows:

| reference | MAPE | P99 APE | WAPE | reference misses |
|---|---:|---:|---:|---:|
| legacy retirement-time DynInst label | 27.0550% | 57.5417% | 5.3391% | 278,566 |
| gem5 committed BPred statistic | 5.5619% | 20.9044% | 1.8216% | 292,603 |

The old label omitted 14,037 of 292,603 committed BPred misses in these aligned
rows. LBM C4 core 0 is the clearest example: its branch populations differ by
only one, while legacy TaoTrace reports 1,277 misses, gem5 BPred reports 1,837,
and FastSim predicts 1,849. The apparent FastSim error changes from 44.79% to
0.65% when compared with the scope-aligned BPred reference.

This audit is diagnostic, not a replacement formal result. A final gem5 stats
dump may include execution after an individual core reaches its TaoTrace stop,
which is why most old per-core rows cannot be compared safely. The formal
40-case MAPE/P99 requires recollection with the repaired producer.

Reproduce the diagnostic audit with:

```bash
python3 tools/audit_branch_miss_oracle.py \
  tmp/branch-spec-history-control-full40/summary.json \
  --max-branch-skew-ratio 0.001 \
  --output tmp/branch-miss-oracle-audit/summary.json \
  --markdown tmp/branch-miss-oracle-audit/summary.md
```

## Fresh 10M LBM tail replay

The old worst-tail representative was recollected at the original C4,
10M-user-UOP scope with P5. FastSim is unchanged and again predicts 9,422
misses. The corrected reference contains 8,381 misses instead of 6,189:

| C4 LBM branch metric | legacy oracle | repaired oracle |
|---|---:|---:|
| reference misses | 6,189 | 8,381 |
| FastSim misses | 9,422 | 9,422 |
| absolute percentage error | 52.2378% | 12.4210% |

The legacy oracle omitted 2,192 events, or 26.15% of the repaired reference.
The apparent error falls by 39.82 percentage points without changing the
FastSim predictor. The last-finishing core again aligns independently: 128,542
scoped branches versus 128,543 BPred commits, with exactly 1,837 misses in both
sources. The residual +12.42% overprediction is now a real modeling error, not
the former oracle artifact.

## Validation status

- The gem5 X86 MESI Three-Level optimized binary builds successfully with P5.
- FastSim merge and PMU validation tests exercise the exact source gate.
- The audit tool has focused parsing, filtering, percentile, and report tests.
- A fresh C4 LBM 1M-user-UOP producer pilot completed. The last-finishing core
  has 15,406 scoped branches versus 15,407 final BPred commits (0.0065%
  population skew) and both sources report exactly 607 misses. All four rows and the
  aggregate carry `taotrace-retired-bpred-v1`; the strict oracle validator and
  the end-to-end FastSim replay pass.
- The short pilot predicts 5,584 misses versus 4,631 repaired reference misses
  (20.58%); this cold, single-case window is diagnostic and is not a replacement
  for the 10M, 40-case accuracy gate.
- The fresh 10M C4 LBM replay and end-to-end validator pass with 9,422 predicted
  versus 8,381 reference misses (12.42% error, down from the invalid 52.24%).
- A fresh full 40-case recollection remains required for formal MAPE/P99.
