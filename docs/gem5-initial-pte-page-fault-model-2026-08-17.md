# gem5 ROI-entry page-state model

Status: dual snapshots and exact ROI-entry in-flight repair implemented;
FastSim unit tests and the 40-case C4/C8/C16/C32 combined-scope gate passed.
The paired user-only gate rejected the current whole-page cache-fill overlap.
The model is the default for the FS user+kernel profile as of 2026-08-20.

## Problem and scope

A committed functional trace says which user instruction eventually retired.
It does not say whether its virtual page was resident before the trace began,
nor whether a fault for that instruction had already entered the kernel when a
process-wide measurement marker opened. Treating every token's first observed
access as a measured page fault therefore confuses four cases:

1. the page was already present at functional-warmup start;
2. it was initially non-present but warmup populated it;
3. it was still non-present at measurement start and faults afterwards;
4. its fault entered before measurement start, while the retried committed
   instruction appears as the stream's first measured record.

Longer functional warmup cannot reconstruct case 1, and an initial-only
snapshot cannot distinguish cases 2--4.

For gem5 full-system runs, the restored checkpoint already contains guest
physical memory and x86 control registers. TaoTrace therefore reads the
**guest** page tables. It does not boot from process start, access Linux host
`/proc/pagemap`, or issue these diagnostic reads through gem5 timing caches,
TLBs, PMU counters, or Ruby.

The current implementation deliberately assumes one traced Linux process and
one CR3 root. Multi-address-space attribution remains out of scope.

## Producer algorithm

TaoTrace takes two functional x86-64 page-table snapshots:

1. `initial-pte-state.json` is captured before the first functional user
   record and supplies warmup initial state;
2. `roi-entry-page-state.json` is captured synchronously in the serial ROI
   marker callback, before any per-core measurement stream is opened. The
   retired producer filename `measurement-pte-state.json` remains readable.

Each walk starts at restored guest CR3, reads through `System::physProxy`,
scans the lower canonical half and user-accessible upper paths, normalizes
present 1 GiB/2 MiB leaves into 4 KiB pages, and records every leaf slot below
an existing PT. A range behind an absent upper-level entry stays unknown; it
is not expanded and guessed to be non-present.

The producer also maintains an exact pre-boundary exception state machine:

- commit's accepted `KernelEntryEvent` records a precise from-user x86
  `PageFault` while functional measurement is still closed;
- the next user commit clears it, proving that the handler returned before
  the marker;
- the process-wide marker freezes any still-pending `(core, virtual_page)`;
- that core/page is marked as an ROI-entry in-flight fault.

This is not the heuristic “suppress the first measurement record.” A real
post-marker fault on the first record remains unmarked and is charged. The
state is frozen before measurement and does not read the post-run page-fault
oracle.

For diagnosis only, gem5 also writes raw non-present PTE values and
`oracle/page_fault_events.jsonl`. Those files prove the implementation but are
never read by FST or FastSim.

## FST representation

The existing 32-byte `.fst.vmap` row carries all state; the 64-byte FST hot
record, vmap version, and row size do not change.

| vmap flag | Meaning |
|---:|---|
| bit 0 | physical page valid |
| bit 1 | initial PTE state valid |
| bit 2 | initial PTE present; requires bit 1 |
| bit 3 | ROI-entry page state valid |
| bit 4 | ROI-entry page present; requires bit 3 |
| bit 5 | this stream had a precise page fault in flight when ROI opened |

JSONL TaoTrace output carries the equivalent fields
`initial_pte_state_valid`, `initial_pte_present`,
`roi_entry_page_state_valid`, `roi_entry_page_present`, and
`roi_entry_inflight_page_fault`. Existing companions keep bits 1--5 clear and
remain byte-compatible; readers accept the retired `measurement_pte_*` names
as input-only aliases.

## FastSim decision rule

At construction, per-stream maps are merged by portable virtual page into the
current single-process catalog. A deterministic owner is selected by
`(first_record_ordinal, thread_id, token)` before worker threads start, so host
scheduling cannot choose which stream injects an event.

During functional warmup, an owner's first access uses the initial snapshot.
During measurement, it uses only the ROI-entry snapshot; a missing ROI-entry
state never falls back to stale initial state.

For a measurement first touch:

- known present: suppress the page-fault selector;
- known non-present, bit 5 clear: select one measured page fault;
- known non-present, bit 5 set: suppress measured kernel cycles, but retain the
  functional 4 KiB page-fill/cache-state effect of the already-running
  handler;
- unknown: fall through to the existing syscall-semantic/probability model.

The relevant result counters are the initial and `page_fault_roi_entry_*`
`known/present/nonpresent/unknown/selected` families, plus:

- `page_fault_roi_entry_inflight_suppressed`;
- `page_fault_process_shared_duplicate_pages`;
- `page_fault_cache_state_pages` and `page_fault_cache_state_lines`.

The model is enabled by `page_fault.roi_entry_page_state_model = true` and is
the default in `configs/gem5-v28_1-fs-user-plus-kernel.cfg`. User-only replay
remains an explicit candidate in
`configs/gem5-v28_1-fs-user-roi-entry-page-state.cfg`. SE and generic traces
are not required to carry page-state companions; an FST map with unknown bits
retains the portable fallback. The retired config and CLI spellings remain
input aliases. The bit-5 repair is intrinsic when a producer supplies it; it
is not a tunable workload rule.

## Default-enable gate (2026-08-20)

The formal gate replayed all 40 C4/C8/C16/C32 × 10-workload user+kernel cases
with the accepted static-instruction-span Fetch model held on. The only
candidate override was `page_fault.roi_entry_page_state_model=true`. Every
result reports the canonical configuration field as true; 40/40 cases passed
the runtime and conservation checks. Candidate artifacts are under
`tmp/roi-entry-page-state-uk-full-gate-20260820`; the paired control is
`tmp/fetch-supply-static-span-full-gate-20260820`.

| user+kernel metric (40 cases) | control | ROI-entry page state |
|---|---:|---:|
| trace-denominator CPI mean APE | 8.568% | 8.248% |
| CPI P50 / P90 / P99 APE | 9.983% / 15.792% / 20.745% | 9.782% / 13.246% / 19.629% |
| CPI maximum APE | 22.199% | 20.820% |
| perf-like CPI mean APE | 8.376% | 8.242% |
| perf-like CPI P90 / P99 APE | 14.778% / 20.401% | 13.261% / 19.614% |
| improved / regressed / unchanged | — | 20 / 9 / 11 |

The model is therefore default-enabled for FS user+kernel: it improves the
mean and every reported tail percentile, and it replaces a statistical
boundary guess with producer state. This is not a claim that the downstream
page-fill approximation is solved. Graph500 regresses by 5.987/4.131/2.284 pp
at C8/C16/C32 even though its page-fault counts are nearly exact, which points
to fault timing/cache-fill overlap rather than boundary-state identity. NAMD
C16/C32 still miss post-entry allocations: the snapshot selects only 36/47
exact non-present pages, while the fallback supplies most of the remaining
events. Those are follow-up model defects, not reasons to discard known
ROI-entry state.

The paired 40-case user-only gate is deliberately not accepted as a default:
mean APE changes from 6.962% to 8.370%, P90 from 12.199% to 13.804%, and only
6 cases improve while 23 regress and 11 are unchanged. The largest movements
are zstd C32 (3.472% to 18.862%), Graph500 C8 (6.472% to 16.956%), and NAMD C8
(2.414% to 9.415%). Because user-only excludes all synthetic kernel cycles,
these regressions isolate the current whole-page cache-fill placement/overlap,
not the fault-count or CPI-denominator contract. Its artifacts are under
`tmp/roi-entry-page-state-user-full-gate-20260820`.

Across the 40 cases, page-fault-count WAPE improves from 15.043% to 13.527%,
and 24 cases are exact. All 217,953 first measured process pages are explicitly
classified as 197,764 known and 20,189 unknown, a 90.737% known-state coverage;
unknown pages retain the portable fallback. Stockfish is exact at C4/C8/C32
and off by one at C16 (20/35/69/339 selected versus gem5 20/35/68/339).

The option is not a general cache/CHA PMU correction. In the same gate,
branch-miss WAPE improves from 5.734% to 4.945%, while L1D/private-L2/LLC tag
miss WAPE changes from 5.263/11.138/4.554% to 5.365/11.340/5.078%. Ruby/CHA
`remote_supplies`, `llc_merged_misses`, and `llc_unique_fills` do not have a
usable nonzero formal reference in this dataset, so their WAPE is intentionally
not used to accept the option. Observed sequential aggregate throughput is
5.39 M user UOP/s versus 5.62 M in the older control artifact (-4.17%); this is
a cross-run host measurement, not a same-host paired throughput claim.

## Address-level NAMD evidence

The validation uses 100,000 measured user FST records per core with source
functional warmup. All values below come from the same fresh traces and gem5
binary SHA-256
`4f583b7141e47b36a8d1c2c2c7f6f37c07238ee1a2a0a4b4c55990528b2b4534`.

| NAMD case | Process initial non-present | Measurement non-present | Boundary in-flight | gem5 measured `#PF` | FastSim selected |
|---|---:|---:|---:|---:|---:|
| C4 | 24 | 24 | 0 | 24 | 24 |
| C8 | 21 | 4 | 2 | 2 | 2 |

C8's two in-flight pages are exactly:

| Core | Boundary-in-flight page | First measured access | Measured `#PF` page |
|---:|---:|---:|---:|
| 4 | `0x775fd800a` | record delta 0 | `0x775fd8009` |
| 7 | `0x775fb800c` | record delta 0 | `0x775fb800b` |

The in-flight pages are present in `roi-entry-page-state.json` (the original
artifact used the retired `measurement-pte-state.json` filename) as
non-present raw PTE zero, but absent from the measurement-gated architectural
`#PF` stream. The two lower pages occur in that stream with error code 6. This
proves that a PTE snapshot alone overcounts C8 4 versus 2 and that the pending
exception state, not page adjacency, is the missing variable.

C4 independently rejects an adjacent-page coalescing repair: its 24
non-present pages form one contiguous range, are touched mostly by the
backward `__memcpy_sse2_unaligned_erms` MOVNTDQ loop at PC `0x587472`, and all
24 produce separate architectural page faults.

Audit artifacts are under
`tmp/measurement-pte-pilot-20260817/namd-c{4,8}-inflight-v1-vmap-audit.json`;
FastSim reports are under
`tmp/measurement-pte-pilot-20260817/fastsim-inflight-v1/`.

A partial cross-load gate found no spurious selection in the cases that had
usable checkpoints. NAb C4/C8 had respectively 85/96 process virtual pages,
all measurement-present, no boundary-in-flight flag, and zero gem5 page-fault
entries. Neutron C4 had 4,261 process pages with the same all-present/zero-fault
result. Fourteen other topology/workload pairs lacked a compatible cached ROI
checkpoint and therefore did not run. Neutron C8 failed closed before trace
publication because probes observed different CR3 roots; it was not coerced
into the single-address-space model. The long NAb C8 run reached all eight
measurement targets and its eight vmap files passed a combined audit, but its
bulk trace promotion was interrupted after the files split between scratch and
result directories, so it is evidence for map/fault conservation rather than
a complete FastSim CPI report.

## What the repair solves, and what remains

The repair makes NAMD page-fault **event identity/count** exact for both pilot
topologies. It does not by itself solve the remaining CPI error. The same
100k-record diagnostic exposes two independent residuals:

On C8, the dual-snapshot build before bit 5 selected four events and reported
user/user+kernel CPI 0.325816/0.391726. After the repair it selects two,
suppresses two boundary-in-flight events, retains four cache-state page fills,
and reports 0.325816/0.357111. The unchanged user CPI proves that the repair
preserved page-fill state; the lower inclusive CPI proves that it removed only
the incorrectly charged kernel service. The old overcount had been masking a
separate user-timing deficit, so its superficially smaller inclusive CPI error
was compensation, not accuracy.

| Case | gem5 user CPI | FastSim user CPI | user APE | gem5 user+kernel CPI | FastSim user+kernel CPI | incl. APE |
|---|---:|---:|---:|---:|---:|---:|
| NAMD C4 | 0.839575 | 0.538776 | 35.83% | 2.578129 | 1.823493 | 29.27% |
| NAMD C8 | 0.371965 | 0.325816 | 12.41% | 0.410685 | 0.357111 | 13.04% |

For C4, gem5 attributes 496,307 cycles to 24 page-fault handlers while the
fixed FastSim profile contributes 325,272 cycles, a 34.46% underprediction.
The syscall profile is already close (189,196 predicted versus 185,247 gem5
cycles). Separately, user execution is short by 120,320 cycles. For C8, the
two page-fault handlers are 28,660 gem5 cycles versus 27,106 predicted
(5.42% low), while the larger residual is again user timing. A single fixed
page-fault latency therefore cannot explain both C4 and C8; the next page-fault
work is a contention/state-sensitive service model, and the larger priority is
the user microarchitecture residual.

Stockfish is not a page-residency problem in the collected pilot: its traced
pages are known present and gem5 reports zero page faults. This model should
not be credited with, or tuned to compensate for, Stockfish's CPI error.

## Acceptance boundary

Before enabling the PTE candidate in the main FS profiles:

1. run `tools/audit_fst_virtual_page_map.py` on every trace and require valid
   bits 1--5 plus process-level snapshot conservation;
2. run the standard build and `fastsim_tests` gate;
3. compare selected faults against collection-time architectural entries on a
   diagnostic split only, never consume that oracle during inference;
4. rerun the complete C4/C8 workload matrix with all other parameters frozen;
5. report Stockfish and NAMD separately and reject aggregate-only gains;
6. establish a policy for DR-derived traces whose optional PTE fields remain
   unknown, without substituting host PTE state for gem5 guest state.
