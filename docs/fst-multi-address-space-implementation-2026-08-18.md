# FST multi-address-space implementation and gate (2026-08-18)

## Result

FastSim and gem5 TaoTrace now preserve address-space identity end to end. The
hot FST v7 record remains 64 bytes; a sparse `.fst.asmap` companion maps record
ordinal runs to a producer-local address-space ID. FastSim isolates DTLB,
virtual-page/PTE, residency, first-touch, mmap/munmap, and deterministic page-
fault selector state by that ID.

This change fixes a demonstrated contract violation. The prior gem5 producer
selected one CR3 for PTE snapshots and emitted no virtual-page token for
records from another CR3. Downstream strict-token/PTE configurations could
therefore reject a valid functional trace, while configurations which accepted
it could merge equal virtual pages from distinct processes.

## Implemented semantics

- `.fst.asmap` v1 uses a 48-byte header and 16-byte
  `(record_ordinal, address_space_id)` rows. The first row is ordinal zero;
  rows are emitted only on identity changes.
- JSONL accepts `address_space_id`, then `asid`, then `cr3`. Binary conversion
  and v7 upgrade preserve the transition stream.
- TaoTrace emits the live x86 CR3 page-table root for normal committed records
  and synthetic syscall markers.
- Token interning uses `(ASID, virtual_page, physical_page)`. All valid
  committed same-page memory references receive tokens, including non-snapshot
  CR3 roots. Only the selected snapshot root receives known PTE bits; other
  roots remain explicitly unknown.
- Process memory state uses `(ASID, virtual_page)`. Per-thread first-touch,
  allocation-recency, syscall mmap ranges, and probability phases are also
  AS-scoped.
- Architectural and timing DTLB keys use `(ASID, token)`. On an AS transition,
  FastSim clears DTLB contents, pending walks, and not-yet-installed walk
  completions. This is the supported conservative subset of gem5 x86, which
  calls `flushNonGlobal()` on CR3 writes; FastSim has no global-page classifier.
- Thread JSON reports initial/final effective ASID, distinct AS count, and AS
  switch count for the measured phase.
- `.imap` v1/v2 is PC-only. TaoTrace suppresses it for a stream with multiple
  address spaces, and FastSim disables an accidentally supplied PC-only imap
  in that case.
- Collector, formal-dataset builder, static-map materializer, and TCSim's
  direct/JSON conversion publishers preserve the sidecar. The FastSim
  collector validates its header/rows and records its SHA-256.
- The vmap audit keys process PTE state by `(ASID, virtual_page)` and rejects a
  token used in more than one address space.

## Evidence

Unit/regression gates cover:

1. `.asmap` writer/reader round trip and FST v7 upgrade preservation;
2. streaming transition ordinals and measured AS coverage counters;
3. a DTLB hit within one AS becoming a miss after AS7 -> AS11 -> AS7;
4. the same virtual page being present in AS11 and non-present in AS7 without
   conflict or false process-sharing;
5. PC-only imap suppression for a multi-AS stream.

`cmake --build build -- -j16` and `./build/fastsim_tests` pass.

The patched TaoTrace object and full gem5 binary were rebuilt. The object and
binary contain `FSTASM1`, the missing-CR3 guard, and the
`address_space_runs` finalization diagnostic, proving the patched source is in
the executable.

A real C4 `777.zstd_r` source-warmup gate used the production restore path and
10,000 measured user records per core. The four streams had distinct CR3 roots:

| Core | Address-space ID | Hex |
|---:|---:|---:|
| 0 | 100,515,840 | `0x5fdc000` |
| 1 | 131,973,120 | `0x7ddc000` |
| 2 | 131,981,312 | `0x7dde000` |
| 3 | 131,989,504 | `0x7de0000` |

This is direct evidence that one benchmark case is not necessarily one address
space. Each stream contained one AS run in this gate, so the inter-stream
isolation path was exercised; the intra-stream transition/flush path is covered
by regression tests and still needs a real switching workload gate.

The strict vmap/asmap audit passed all four files:

- 26,719,510 total FST records;
- 4 valid `.asmap` files, 256 bytes total;
- 323 token/page mappings and 323 distinct `(ASID, virtual_page)` identities;
- 80 mappings with known initial PTE state in the selected snapshot root;
- 243 mappings correctly left unknown in the other roots;
- no token crossed an address space.

FastSim consumed the production manifest with full functional warmup and the
current C4 user configuration. Measured output reported the four effective
ASIDs above, zero vmap misses, zero false process-shared duplicate pages, and
61 DTLB/page first-touch candidates. Runtime was 2.006 seconds; measurement
throughput was 4.333 M uops/s. This short gate proves correctness/coverage, not
a stable performance or CPI-accuracy result.

## Deployment issue found by the gate

The first real run showed that TaoTrace generated four 64-byte `.asmap` files
in trace scratch, but TCSim's direct-FST publisher moved only `.vmap/.imap`.
The final trace set therefore lost address-space identity even though producer
and consumer supported it. `TCSim/scripts/gem5_fs_roi.py` now preserves and
hashes `.asmap` in both direct-FST and JSON-conversion publication paths.

The repaired direct publisher was then invoked on hard-linked copies of the
real four-core gate (same inodes, no duplicate bulk trace data). It produced
all four final `coreN.fst.asmap` files and recorded each 64-byte size, path,
and SHA-256 in `trace.json`. This separately verifies the publisher fix rather
than relying on syntax checking.

This is why future acceptance must inspect the final `coreN.fst.asmap`, not
only producer scratch or source code.

## Remaining work

1. Run a real workload/core count with an intra-stream CR3 transition and
   verify the recorded transition ordinal plus DTLB flush counters.
2. Rebuild the formal C4/C8/C16/C32 datasets with `.asmap`; old FSTs cannot be
   repaired from vmap alone because CR3 identity was not recorded.
3. Re-run the rejected-case gate and then the user/user+kernel CPI/PMU matrix.
   The expected benefit is removal of false sharing/rejection; no CPI gain is
   claimed until those paired results exist.
4. If speculative I-side modeling is required for a genuinely switching
   stream, define an AS-scoped imap revision. Do not re-enable PC-only imap by
   assumption.
5. Extend gem5 PTE capture to multiple roots only if a producer-side snapshot
   ordering contract is defined. Unknown PTE state is correct and preferable
   to copying another root's state.
