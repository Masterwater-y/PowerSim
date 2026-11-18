# Retained private-preview clock / dormant Estimated matrix

> Execute inline using superpowers:executing-plans. User explicitly selected retaining Estimated source/tests but disabling runtime. No rollback, default installation or milestone promotion is requested.

**Goal:** Preserve the tested selected-clock repair and evaluate latest current four-workload C4/C8/C16 FSTs for CPI, retired PMU diagnostics and primary measurement-only throughput.

**Architecture:** Unchanged reviewed Release/LTO16 ELF, source hashes verified. Explicit `core.unified_parent_estimated=false` in every effective profile. Use maintained source mapping and validation/PMU comparison functions; no model or native timing input changes.

**Spec:** User's retention decision and docs/graph500-private-preview-clock-repair-20261117.md. The failed Estimated experiment remains historical evidence, disabled for this batch.

## Constraints and review focus

- Atomic/QEMU-compatible functional retired inputs, existing two-stage framework; no timing fitting or extra FST capture.
- Preserve all existing source, code/tests and frozen M1-M4; add only working profile/report/experiment artifacts. No build/fastsim replacement.
- Cases: ZSTD, XZ, FLAC, Graph500 at C4/C8/C16 (12 total), correct successful C16 retry2 for XZ/Graph500. Native target/prefetch/early-prepare and per-source timings remain independently bound.
- CPU52-63 only, socket/NUMA1. C4 mask52-55 + C8 mask56-63; C16 serial on52-63. Declare host budget and oversubscription.
- One fresh source-complete run per case: validated preload, full input identity, no audit sampling. Primary user UOP/s excludes preparation/integrity/initialization/warmup; keep those and process wall separately. Single-run rates are not throughput acceptance.
- CPI uses maintained quick scorer. PMU uses maintained compare_retired_cache with saved input-bound audited references; zero-unknown primary gate retained. Unknown never zero-filled. Hardware equivalence unverified; no fresh strict-audit certification.

## Tasks

- [x] Freeze source/ELF/roster/profile and preserve Estimated code while explicitly inactive.
- [x] Bind effective source geometry, IQs, thread-state plan, native device timings; validate target identities and source artifacts.
- [x] Run C4/C8 first, C16 on full host budget next. Check all Estimated flags/counters remain inactive and field-fix baseline equivalence where available.
- [x] Score CPI12/12 through maintained quick validator, and per-case PMU through maintained zero-unknown comparator. Keep unavailable cases distinct.
- [x] Report CPI RE/AE/MAE/MAPE, primary three PMU raw/rate errors and coverage/unknown, throughput/cost/CPU scope. Preserve all cases/outliers and receipts.
- [x] Fresh read-only review of profile/evaluation pipeline and available report evidence; resolve findings and verify frozen default/source identities. Final numerical report verified locally after all12 completed; reviewer did not approve unfinished results.
