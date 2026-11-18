# Graph500 C4 native lifecycle diagnostic implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline. User authorized implementation on 2026-11-17; continue through capture, same-input replay and evidence review without another approval stop.

**Goal:** Test whether FastSim underestimates the lifetime of merged load members and consequently overestimates effective memory parallelism.

**Architecture:** Capture one short Graph500 C4 window using the existing frozen gem5 collector, native-response JSONL and upstream IQ debug events. Independently replay the same retired functional stream through frozen M4 with an isolated timing-neutral lifecycle probe. Join by preserved population and functional identity; native timing remains oracle-only.

**Tech Stack:** Existing gem5 executable/configuration, Python standard library analysis/tests, isolated FastSim C++ diagnostic build.

**Spec:** The user-approved four-step short-window diagnostic in this conversation; supporting evidence in docs/graph500-parent-clock-and-queue-audit-20261117.md.

## Global Constraints

- Preserve the parallel two-stage framework and retired functional input contract.
- All experiment/compiler scratch under project tmp; no default ELF/config changes or latency fitting.
- Read accuracy contract sections 0/8. Use maintained source preparation, identity and quick CPI validation.
- A fresh short window is a new source, not a subset of or timing companion attached to the old capture.
- Use CPU 52–63 through run_high_frequency.py; check disk reservations before capture.
- Existing staged checkpoints have a different identity; do not bypass their identity checks. Fresh boot if no exact-compatible checkpoint exists.

## Review Focus

- Preboundary in-flight requests: mark missing IQ admission outside observed window rather than zero lifetime.
- Multiple CPU/context sequence spaces: join core/context/sequence and reject ambiguous populations.
- Late and no-Ruby terminal records: retain reasons and coverage; never silently drop into scored populations.
- Original native tag truth: use Ruby/SLICC summaries, not proxy path classes.
- Debug logging is diagnostic; do not report its wall time as simulator throughput or claim full instrumentation neutrality.

## Task 1: short capture using existing observer capabilities

- [x] Freeze collector ELF/config/helpers/workload identities and original source command.
- [x] Add an isolated wrapper enabling native-response JSONL and IQ logging at measurement open; retain the source target config with hardware prefetch/early prepare off.
- [x] Capture 50,000 user UOPs with 500,000-instruction functional warmup, with a disk cap and command/environment receipt.
- [x] Verify collector reached common end, native logs exist and runtime config matches target.

## Task 2: same-input lifecycle extraction and join

- [x] Write focused failing tests for IQ event parsing, duplicate/missing joins and interval overlap accounting.
- [x] Implement a maintained Python analysis tool; tolerate explicit preboundary missing IQ admits while rejecting ambiguous identities.
- [x] Use source-native commit population and functional PC/line/order witnesses to establish exact per-member pairing.
- [x] Prepare source with maintained tools, generate source-bound M4 config and replay with bounded model event audit.
- [x] Run relevant tests and maintained quick CPI scoring, reporting absolute CPI error and MAE.

## Task 3: diagnostic decision and review

- [x] Compare leader/merged/local member request and queue lifetimes with distributions, coverage and outliers; compare unique-line overlap separately.
- [x] Explicitly distinguish confirmed deviation, candidate causal explanation and unavailable evidence.
- [x] Review implementation and evidence; write results and decide whether to continue parent timing repair or turn to core throughput modeling.

No commit/push/default promotion is part of this task. The known preview-clock omission remains an explicit control, not a presumed explanation of the remaining CPI gap.

## Execution rulings and results

- Ruling: use existing frozen native-response observer and IQ debug events instead of rebuilding gem5; both provide the required actual event edges.
- Ruling: no exact-compatible checkpoint, so fresh KVM boot; old resource path failure retained and same-content relocated resources verified by SHA-256.
- Ruling: IQ starts after first stats reset, including warmup; filter by measurement retired population, preserving boundary in-flight events.
- Ruling: tools are developed inline; final independent read-only review requested under executing-plans. No commit/push is requested.
- Result: 16454/16454 core0 loads paired, all native IQ PCs verified against functional FST, all model source ordinals/lines matched; no missing IQ edges.
- Result: 10 focused Python tests GREEN, isolated build complete, full M4/probe time_accounting exact-neutral, each quick scoring 1/1 CPI scored.
- Decision: pursue pre-acceptance IQ holding and admission-feedback modeling; do not promote any timing candidate or claim full CPI causal closure.
- Report: docs/graph500-native-lifecycle-paired-diagnosis-20261117.md.

- Final independent review: no blocking findings; scope and warmup wording clarified. Completion receipt: tmp/graph500-native-lifecycle-20261117/completion.json.
