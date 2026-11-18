# Zstd PMU diagnosis and bounded observer repair

**Goal:** Identify zstd retired-load miss discrepancies and validate a generic, opt-in repair without changing the frozen M5 default.

**Spec:** The approved implementation sequence in the conversation: neutral member diagnostics; L1 clock/identity evidence; separate lower-cache provenance; cross-workload validation.

**Architecture:** Keep the parallel two-stage model. Observer-only instrumentation reads accepted functional members and existing model clocks. Native observations are offline references only. Work in `tmp/zstd-pmu-repair-20261117/source`, a snapshot of the current dirty tree, and build with Release/LTO16 in the same project temporary directory.

## Constraints and review focus

- Preserve installed M5 ELF, immutable profiles and all existing working changes.
- No speculative input, synthetic shadow traffic, cycle loop or whole-window convergence.
- Distinguish cache-line samples from architectural macro PMU; preserve incomplete and unknown populations.
- Diagnostic logging must not change time accounting or any PMU predictions.
- Candidate defaults off; validate forwarding, multiple fragments, zero clocks, half-open boundaries, and carried intervals.
- Missing current native member evidence remains missing; historical captures do not establish current causal attribution.

## Tasks

1. **Neutral observer.** Add estimator decision metadata and capped member logging with complete transition counters. Test decisions/boundaries, truncation and source identity. Run RED then GREEN, build isolated ELF, replay zstd C4 and prove neutrality against M5.
2. **Clock hypothesis.** Record final CPU admission separately from selected parent service clocks. Check actual decision distributions. If supported, add a default-off clock option with forwarding/boundary tests; keep service begin separate from child admission. Compare each changed category against the baseline. Reject unsupported changes.
3. **Evaluation.** Replay zstd and XZ/FLAC/Graph500 controls, then C4/C8/C16 as justified. Use maintained PMU comparator and quick CPI validator with unchanged native unknown counts. Report CPI RE/AE/MAE, all PMU regressions and same-input host cost separately. Run direct C++ tests and relevant Python tests.
4. **Lower-cache evidence and handoff.** Check existing sealed native fill artifacts; use maintained pairing only for their actual source identity. Record missing witnesses and any required new capture. Publish verified source changes, results and limits with an independent final review. Do not install a candidate by inference.

**Execution:** User requested implementation; continue inline without an additional approval round. Preserve experiment evidence under project `tmp/`.
