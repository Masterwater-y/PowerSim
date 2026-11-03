# Native FS FR-FCFS default and parallel C8 replay plan

> Execute inline with superpowers:executing-plans. The user explicitly confirmed FR-FCFS candidate and authorized changing its default and parallel FastSim evaluation. No additional approval, commit or push.

Goal: Select the existing arrival-constrained FR-FCFS approximation in the maintained native FS alias and report several other same-source C8 workloads.
Architecture: set scheduler=frfcfs, topology_scaled_window=false, selection_window=0 (actual read-buffer capacity), causal_selection=true only in the maintained alias. Keep all versioned profiles and generic C++ defaults unchanged. Supply a legacy overlay for exact rollback. Freeze old/new include closures and the binary, then run maintained native validation on each case/mode.
Spec: user confirmed the existing FR-FCFS candidate; prior Marian controlled/throughput reports define its approximation limits. No latency fitting or other store/branch mechanism promotion.

- [x] Freeze pre-change profiles. RED: maintained effective-default policy and rollback parity tests.
- [x] Change maintained native profile and add old-policy rollback profile. GREEN: actual CLI configuration plus old-policy target parity.
- [x] Prepare C8 Zstd, Graph500, FLAC from final config.ini. Audit all directly represented parameters before replay. Preserve frozen sources and per-case thread-state plans.
- [x] Run old/new modes with concurrent case jobs=3, same frozen binary; use maintained source/native scoring, expected one case per job. Do not treat parallel walls as throughput acceptance.
- [x] Compare old/new CPI signed/absolute errors and case-equal MAPE/MAE plus task-macro-weighted WAPE/bias. Retain all failures and unavailable PMU coverage.
- [x] Run required build/C++ tests and profile tests; fresh read-only review of this default change; document default/rollback and experiment results.

Ruling: use the existing C8 set with complete FST files: Zstd, Graph500 and FLAC. FLAC has a known independent native task-population inconsistency, so it remains in expected population but cannot enter qualified accuracy aggregates unless current maintained gates actually pass. SQLite and XZ complete-source gates are not qualified; no substitution with short captures or new collection.
Ruling: zero selection_window means complete configured read buffer. All three actual sources have 64 read entries, so this is the already tested 64-entry candidate without hardcoding another hardware queue size. This is a user-selected approximation default; source/accuracy/throughput/hardware gates stay separate. Historical old-model snapshots are immutable.

Ruling: retain configs/gem5-exp-shared-service-constraints.cfg on the explicit legacy-dram overlay because its existing C++ guard accepts only the canonical FCFS/topology-window1 domain. A CLI regression reproduced the unsupported new-default error before the include fix. No C++ guard relaxation. Replay snapshots had already been frozen; this compatibility-only experiment profile change does not alter the running case configs.

Verification so far: 3 runtime-profile CLI tests pass; cmake build and fastsim_tests pass. Legacy rollback matches pre-change frozen native effective configuration, scope metrics (excluding host throughput), time accounting, per-core/per-thread cycles and CHA counters. All 6 source/mode preflight profiles match 130 directly represented hardware fields. Independent source hardware identity valid on all3. One fresh review found no remaining Important/Critical issues after shared-service profile compatibility fix.

Completion: all6 replays return0, actual profiles130/130 each; new runtime window64 active on all3. Maintained native scores pass both modes for Zstd/Graph500 and reject FLAC in both modes for identical independent owner population discrepancy. Qualified2/3 MAPE7.8837415→6.3907207%, CPI MAE0.145193594→0.112013612 cycles/macro; failures retained, no main retired-cache accuracy claimed. Report docs/frfcfs-native-default-and-c8-results-20261027.md.
