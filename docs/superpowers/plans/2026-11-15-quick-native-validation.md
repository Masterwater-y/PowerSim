# Quick Native Validation Implementation Plan

**Goal:** Default to fast, provisional CPI diagnostics while retaining explicit strict validation.

**Architecture:** The maintained CLI selects a separate quick path that reuses saved accounting, checks source paths and sizes and matches participant populations. Strict mode retains the existing independent audits and formal pass semantics. Quick outputs cannot overwrite strict outputs and never count as strict passes.

**Constraints:** Keep task/end-policy and per-core population checks, unavailable evidence and unknowns explicit; never substitute cache path classification for native truth. Use project-local temporary storage.

- [x] Add tests for default selection, identity failure and separation of formal pass counts.
- [x] Implement parallel quick scoring and keep the old implementation behind `--validation-mode strict`.
- [x] Bind formal callers to strict mode and document the evidence boundaries.
- [x] Run focused regressions, C8 rescore, project build and `fastsim_tests`.
