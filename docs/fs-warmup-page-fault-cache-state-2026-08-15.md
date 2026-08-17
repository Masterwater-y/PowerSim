# FS warmup and page-fault cache-state correction (2026-08-15)

## Conclusion

The dual marker and FastSim functional warmup are working. The short-window
CPI collapse was not caused by a missing reset barrier or simply by too few
warmup instructions. It was caused by a scope mismatch: the user FST replays
only user memory references, while gem5's user PMU observes caches already
modified by kernel page-fault handling.

On the four-case C4 diagnostic, every FastSim LLC miss before correction was a
physical cache line absent from all user warmup streams:

| workload | warmup records | measurement lines absent from user warmup | old FastSim LLC misses | page-fault kernel LLC misses | gem5 user LLC misses | prefetch |
|---|---:|---:|---:|---:|---:|---|
| zstd | 26,679,506 | 43,695 | 43,695 | 44,690 | 492 | off |
| SPH | 3,452,934 | 6,155 | 6,155 | 5,445 | 820 | off |
| Graph500 | 38,284,824 | 14,699 | 14,699 | 14,925 | 0 | off |
| NAMD | 2,936,942 | 3,580 | 3,580 | 1,834 | 445 | off |

The counts alone are not address-level proof because the oracle does not emit
kernel memory addresses. They are nevertheless a strong causal signature:
hardware prefetch is disabled, the user trace predicts one compulsory LLC miss
per unseen line, and the missing kernel page-fault path produces an almost
matching LLC-fill volume.

The machine-readable trace audit is
`tools/audit_fst_warmup_cachelines.py`. It reports same-core/other-core/global
warmup coverage, warmup-tail coverage, unseen physical pages, page-fault event
and PMU counts, and the gem5 prefetch setting.

## Why lengthening warmup is not the fix

Full warmup covers only 13.28%/0.02%/29.48%/36.75% of the measurement's unique
lines for zstd/SPH/Graph500/NAMD. The last one million warmup records cover
2.01%/0.02%/0.03%/36.75%. These programs start touching newly allocated data
after the measurement marker; replaying more of the earlier phase cannot
contain those future cache lines. Moving the marker or discarding the start of
the ROI would change the benchmark interval rather than repair the model.

## Implemented correction

FastSim now separates page-fault state from page-fault accounting:

- `page_fault.cache_state_model=true` applies a selected 4-KiB page fill to
  private cache, LLC and directory state immediately before the first user
  demand. The 64 state-only lines add no cycles, retired work, or PMU counts.
- `page_fault.event_model=true` remains responsible only for page-fault active
  time and synthetic kernel PMU.
- The user pipeline enables the state model while keeping all kernel event and
  service models disabled. The user+kernel pipeline uses the identical frozen
  selector and state transition, then adds kernel timing/PMU.
- Reports expose `page_fault_cache_state_pages` and
  `page_fault_cache_state_lines` explicitly.

The old syscall-window fit is only weakly identifiable from aggregate fault
labels. It selected mmap-adjacent reads but missed Graph500's background first
writes. A two-coefficient diagnostic candidate used only first read and first
write of a virtual-page token, with no workload-ID or syscall-specific
coefficient.

For the four diagnostic cases, first-write candidates are 804/87/327/29 and
gem5 page faults are 804/83/325/24. The frozen two-feature model therefore has
0.89% event WAPE on this calibration diagnostic and 1.54% leave-one-workload-
out event WAPE. This result did not survive the complete set below, so the
two-coefficient classifier is not the production default.

## Short-window causal gate

| workload | old user CPI APE | corrected user CPI APE | corrected user LLC misses | gem5 user LLC misses |
|---|---:|---:|---:|---:|
| zstd | 479.40% | 16.69% | 626 | 492 |
| SPH | 161.75% | 10.51% | 836 | 820 |
| Graph500 | 334.07% | 0.30% | 0 | 0 |
| NAMD | 10.90% | 18.13% | 1,826 | 445 |

Aggregate user CPI APE changed from mean/P50/P90/P99
246.53%/247.91%/435.80%/475.04% to
11.41%/13.60%/17.70%/18.09%. User+kernel CPI is
8.89%/7.03%/15.44%/18.56%. These are four-case, 500K-record diagnostic
numbers, not the formal result.

NAMD's regression and residual L1/L2 PMU errors mean the page-fill mechanism
is not yet a production promotion by itself.

## Complete-set gate

The 10-workload, 10M-record C4 calibration rejected the two-coefficient
selector. LBM, TeaLeaf and Neutron contain thousands of trace-first-touch
pages but have zero measured page faults. Leave-one-workload-out page-fault
event WAPE rose to 302.46%. The resulting state-model candidate reported user
CPI mean/P50/P90/P99 of 16.21%/10.82%/38.01%/42.60%, essentially no formal
tail improvement, and page-fault event WAPE of 115.46%.

Therefore the cache transition is implemented and causally validated, but its
portable selector remains blocked by missing page-residency identity. The
current FST contains an opaque virtual-page token and rich syscall metadata,
but no token-to-virtual-page dictionary. It cannot match an `mmap` return range
to later page tokens or prove that a page was prefaulted by the kernel before
the ROI. A deployable next version needs that portable dictionary/state bit
(available from both gem5 functional addresses and drmemtrace virtual
addresses), followed by fresh C4 calibration and frozen C8 held-out testing.
No workload-specific correction is allowed.

## Frozen C8 held-out result

The complete 10-workload C8 replay confirms the C4 rejection. CPI APE
mean/P50/P90/P99 is 16.52%/12.82%/36.00%/46.81% for user scope and
16.84%/15.07%/23.67%/41.24% for user+active-kernel scope. Mean replay
throughput is 10.98 and 10.82 M uops/s respectively. The complete PMU table is
stored in `accuracy/held-out-c8/summary.md` under the formal run root.

The C8 physical-line audit also rules out a missing warmup segment. Full user
warmup covers 23.03% of zstd measurement lines and 25.29% of NAMD lines, while
gem5 observes 11,335 and 1,971 user LLC misses versus FastSim's 110,744 and
21,865. Page-fault kernel LLC misses are 97,369 and 19,795. Conversely, LBM,
TeaLeaf, and Neutron have zero page faults but large trace-first-touch sets.
That split is exactly why a global first-touch probability overfits.

## Syscall-entry semantic defect found by the formal audit

The v7 structural/coverage audit originally proved that argument fields were
present, but not that they represented the syscall-entry register state. A
real successful zstd `mmap` row contained `flags=0` and `fd=-1`; Linux requires
one of MAP_SHARED, MAP_PRIVATE, or MAP_SHARED_VALIDATE. The row therefore
cannot be used to infer mapping residency even though its validity bits are
set.

The cause is O3 sampling: TaoTrace's Execute callback read architectural
ThreadContext while older argument-producing instructions could still be in
flight. TaoTrace now refreshes the six ABI registers at PreCommit, after all
older instructions have committed and before the syscall updates the rename
map. `tools/audit_fst_syscall_metadata.py` now has
`--require-semantic-plausibility`; both collection launchers enable it and
reject a successful `mmap` without a valid MAP_TYPE. The current formal set is
valid for the already reported functional replay but is invalid as training
input for syscall-semantic page-residency selection. It must be replaced after
the corrected C4 gate passes.

The missing identity path is now implemented without changing the 64-byte hot
record or the FST v7 file body. Each tokenized stream may carry a binary
`.fst.vmap` companion containing token, virtual page, optional physical page,
and first-record ordinal. TaoTrace, the aligned-Parquet converter, the C++
JSONL converter/writer/reader, the TCSim collector, and the formal dataset
builder preserve it. `tools/audit_fst_virtual_page_map.py` enforces its
cross-file invariants.

FastSim also implements `page_fault.syscall_semantic_model=true`. Successful
Linux x86-64 `mmap` adds a demand-faultable virtual range unless MAP_POPULATE
is present; successful `munmap` removes its range. A token's first access is a
page-fault candidate only when its `.vmap` virtual page lies in a live range.
The exact semantic selector fails closed on incomplete mmap/munmap metadata or
a missing page map.

Source-level traces do not necessarily contain startup-time VMA creation. The
implemented hybrid therefore gives exact successful mmap ranges priority and
uses one frozen workload-independent probability only for residual first
writes outside those ranges. The residual coefficient is reported as a proxy
for preexisting anonymous/COW demand-zero state, not as exact syscall
semantics. Exact candidates, residual candidates, and residual selections are
separate counters.

## Corrected semantic gate (2026-08-16)

The corrected 500K-record C4 gate contains 4 configurations and 16 FST files;
the C8 core-count-held-out gate contains 4 configurations and 32 FST files.
Both have zero integrity errors, full dual-marker warmup/measurement slices,
100% syscall entry/return field coverage, zero syscall semantic violations,
and a valid `.fst.vmap` for every stream. C8 contains 69,340,547 warmup records
and 16,000,018 measurement records.

The C4 calibration fit one shared residual-write rate of 959,410 ppm after 976
exact semantic pages. Its page-fault training WAPE is 1.21%; the reported
leave-one-workload-out diagnostic is 7.28%. Workload identity is not an
inference input.

With that C4 rate frozen, the four-workload C8 result is:

| scope | CPI mean APE | P50 | P90 | P99 | WAPE | bias |
|---|---:|---:|---:|---:|---:|---:|
| user | 11.18% | 10.57% | 13.57% | 14.55% | 12.14% | -7.52% |
| user+active-kernel | 7.29% | 8.03% | 9.18% | 9.34% | 6.63% | -6.63% |

Mean/P50/P90/P99/minimum replay throughput is
12.82/13.59/14.46/14.67/9.41 M user uops/s for the user pipeline and
10.83/11.05/13.51/14.08/7.06 M user uops/s for user+kernel. Page-fault event
WAPE is 0.85% (2116 predicted versus 2112 reference), although workload-equal
MAPE is 22.03% because SPH and NAMD have only 15 and 10 reference events.

The user PMU result is already strong for branches (0.49% WAPE), DTLB accesses
(1.14%), and L1D accesses (1.54%), but cache-miss state remains the principal
residual: L1D/L2/LLC miss WAPE is 20.99%/33.68%/73.08%. The paired combined
values are 8.38%/18.86%/16.06%. IRQ and idle are still unobservable from the
portable user trace and remain explicit zero predictions rather than being
folded into page-fault or syscall time.

These are core-count-held-out results over the same four workload names, not a
formal cross-workload claim. The subsequent 10-workload C4/C8 corrected run,
`tmp/taotrace-fst-v7-c4-c8-formal-v3-semantic-20260816`, passed every gate and
is reported in
[`fs-semantic-formal-report-2026-08-16.md`](fs-semantic-formal-report-2026-08-16.md).
The invalid v2 formal directory was then removed.
