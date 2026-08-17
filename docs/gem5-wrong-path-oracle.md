# gem5 wrong-path attribution oracle

## Purpose and boundary

`oracle/wrong_path.jsonl` is a versioned, offline diagnostic sidecar emitted by
gem5 O3/TaoTrace. It answers a narrow causal question: when gem5 accepts a
branch-mispredict or memory-order squash, how many younger dynamic
instructions reached each observable pipeline stage before being discarded?

The sidecar is **not functional input**:

- it is never written into FST and is not accepted by the FastSim inference
  path;
- it may be used for root-cause attribution, model ablation, and evaluation;
- any production correction derived from it must later be expressible only in
  terms of portable FST fields and target microarchitecture configuration;
- it is gem5-oracle data, not metadata that portable DynamoRIO/drmemtrace can
  reproduce. In particular, wrong-path instruction identities and stage ticks
  are intentionally outside the shared FST contract.

This separation prevents an oracle-assisted experiment from being reported as
trace-only inference accuracy.

## Collection

Collection is default-off. The wrappers expose one explicit switch:

```text
scripts/gem5_fs_roi.py --measure-cpl --wrong-path-oracle ...
scripts/run_gem5_fs_cpi_matrix.py --measure-cpl --wrong-path-oracle ...
```

`--wrong-path-oracle` requires `--measure-cpl`. With source warmup, emission
starts only at the functional measurement marker and stops when the per-core
functional user-record target is frozen. An instruction fetched before the
boundary can appear only if its squash episode itself occurs inside this
measurement window; the episode is the unit of scope.

The implementation observes the existing architecture-independent O3 probes:
Fetch, Rename, Dispatch, Execute, ToCommit, DataAccessComplete, Commit, and
Squash. A small Commit hook records the exact squash cutoff before `ROB::squash`
removes younger instructions. It classifies direct IEW redirects as
`branch_mispredict` or `memory_order`; other Commit squash callbacks are retained
as `unattributed_commit_squash` rather than silently dropped.

## JSONL schema

Every newly collected row carries
`schema: "taotrace-wrong-path-oracle-v3"`. The first row is:

```json
{"schema":"taotrace-wrong-path-oracle-v3","record":"metadata","oracle_only":true,"fst_input":false,"scope":"functional-measurement","cpl_attribution":"decoded-x86-mode","late_data_complete_attribution":true,"tick_unit":"gem5_tick"}
```

Each `episode` row contains:

- identity: `episode_id`, functional `core_id`, hardware thread, `cause`, and
  decoded `cause_cpl`;
- exact boundary: `cause_seq`, `cutoff_seq`, `rob_youngest_seq`, `cause_pc`,
  `redirect_pc`, `include_cause`, and `squash_tick`;
- conserved totals: `instruction_records`, fetched, renamed, dispatched,
  issued/Execute-observed, ToCommit-observed, memory, data-completed, load, and
  store instruction counts, plus conserved user/kernel/unknown-CPL record
  counts.

The following `instruction` rows contain the corresponding dynamic sequence,
PC/micro-PC, OpClass and instruction-kind flags, source/destination register
counts, stage-presence flags, probe ticks, dynamic issue/complete ticks, and
memory address/size when observed. Every instruction also carries its decoded
x86 CPL. These are deliberately oracle fields.

The analyzer sums `n_src` and `n_dst` both across all scoped instructions and
across the `renamed=1` subset. It likewise reports
`renamed_memory_instructions`. The renamed subset is the stage-aligned oracle
for a FastSim ROB/rename-prefix audit. These are dynamic micro-op counts for
offline attribution; they are not portable FST input and must not be
substituted for the canonical `.fst.imap` v2 architectural masks.

Schema v3 adds a `late_data_complete` row when an issued wrong-path memory UOP
has already been emitted by a squash episode and its DataAccessComplete probe
arrives later. The row references the original episode and dynamic sequence,
records the squash/completion ticks, CPL, address and size, and says whether
the per-core measurement gate was still open at completion. Pre-squash
`data_completed_instructions` remains unchanged, so the two populations cannot
be double counted. An executed request that is cancelled or whose callback is
not observable remains an incomplete-at-squash candidate, not a fabricated
completion.

`unattributed_commit_squash` is a victim-level fallback: the callback knows the
discarded instruction but not the redirecting cause. Its `cause_cpl` is always
255 (unknown), while the instruction row retains the victim CPL. A user-scope
analysis must exclude this fallback; treating victim CPL as cause CPL would
misclassify interrupt/exception squashes.

`completed_instructions` means observed at the O3 `ToCommit` probe. It does not
mean architecturally retired: all instruction rows in this sidecar were
subsequently squashed.

## Validation

Run:

```bash
python3 tools/validate_wrong_path_oracle.py \
  RESULT/oracle/wrong_path.jsonl \
  --gem5-stats RESULT/stats.txt \
  --json-out RESULT/wrong-path-validation.json \
  --markdown-out RESULT/wrong-path-validation.md
```

The validator accepts legacy v1/v2 for audit and current v3. It fails closed on
a missing/duplicate metadata row, schema or
oracle/FST separation mismatch, invalid episode reference, duplicate dynamic
instruction, sequence outside its squash cutoff, non-boolean stage flag,
non-monotonic pipeline ticks, CPL outside 0--3/255, any per-episode stage count
mismatch, or failed user+kernel+unknown CPL conservation. For v3 it also checks
late-completion identity, CPL, tick ordering, and non-duplication against a
pre-squash completion. It reports
gem5's aggregate squash diagnostics as a cross-check; these statistics have
different stage populations and therefore are not required to equal every
sidecar total.

For a same-window attribution report, first replay the sidecar run's own
warmup-slice manifest with `measurement_scope=user` and write the result to
`RESULT/fastsim-baseline.json`. Then run:

```bash
python3 tools/analyze_wrong_path_oracle.py RESULT --scope user \
  --json-out RESULT/wrong-path-attribution.json \
  --markdown-out RESULT/wrong-path-attribution.md
```

The analyzer defaults to user scope and rejects v1 data, a non-user FastSim
scope, or a different measured-UOP count. Legacy v1 can only be inspected with
`--scope all`; it cannot support a user-CPI claim because it has no decoded CPL.
The analyzer reports stage-population distributions, full-width capacity
equivalents, the union of wrong-path active intervals, pre-squash completed
address footprints, executed/incomplete-at-squash candidates, and v3
post-squash completion footprints/delay distributions. The active interval is only
a direct-interference window ceiling: useful older work overlaps the interval,
while cache/TLB pollution can outlive it. Neither quantity is an additive CPI
term.

The JSONL format favors auditability over size. The C8 Neutron 100k-user-UOP
per-core v2 pilot produced 353,263,437 bytes for 800,005 measured user UOPs
(about 442 bytes per measured UOP). At the same density, a 10M-per-core C8 run
would require about 32.9 GiB. Keep this collector targeted and default-off; do
not enable it across the formal matrix unless a compact sidecar format and a
separate storage budget are approved.

## Required attribution sequence

For a CPI tail such as C8 Neutron, use the sidecar in layers:

1. verify branch-miss occurrence count against gem5 PMU/statistics;
2. measure fetch-to-rename, fetch-to-dispatch, fetch-to-issue, wrong-path
   memory, pre-squash data completions, and v3 post-squash completions;
3. run an oracle-assisted ablation to bound the maximum CPI recovery from exact
   squash population and stage residency;
4. only if the bound is material, construct a held-out trace-only estimator
   from committed control-flow history and target configuration;
5. rerun the unchanged 92-case SE gate and the FS C4/C8 gate. A correction is
   rejected if it fixes Neutron by regressing the prior P99 contract.

Oracle-assisted CPI is an attribution result, never the production accuracy
number. Formal CPI/PMU/throughput reports continue to follow
[`accuracy-reporting-contract.md`](accuracy-reporting-contract.md).
