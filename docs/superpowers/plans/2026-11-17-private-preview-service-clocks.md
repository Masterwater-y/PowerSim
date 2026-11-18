# Private preview service clock repair plan

> **For agentic workers:** Use superpowers:executing-plans inline. User asks whether the known missing fields can be repaired; implementation of the smallest reviewable fix is authorized. Do not promote the default milestone.

**Goal:** Restore selected request/response model clocks on ordinary private-preview reads that bypass shared replay.

**Architecture:** Reuse the same pending reference issue and existing local response duration as the normal replay path. Retain parent certification, functional population, cache lookup order and all latency values. Build/test in project tmp; installed M4 stays frozen.

**Tech Stack:** FastSim C++17, existing Simulator/ReadSource integration fixture, ordinary Release with LTO16.

**Spec:** docs/graph500-parent-clock-and-queue-audit-20261117.md and the user request on private-preview field omission. Estimated timing is a separate design, not a hidden relaxation in this patch.

## Global Constraints

- Preserve the two-stage functional-stream framework and high throughput.
- No native O3 timings as input, no fixed delay fitting, no additional shared replay.
- Preserve unrelated dirty workspace files; back up touched files and identify the actual patch.
- All temporary/build outputs under tmp/private-preview-clock-fix-20261117.
- Do not overwrite installed build/fastsim, frozen M1–M4 profiles or default alias.

## Review Focus

- L2 preview hits with no lower service must still expose nonzero selected clocks when the request starts after cycle zero.
- The local/reference conversion and issue-to-admission split must match normal replay.
- Forwarded/special/instruction/PTE requests are not upgraded to ordinary data service.
- Carried events retain their existing provenance and are not fabricated anew.
- More time metadata must not imply strict source certification when final admission was shifted.

## Task 1 — integration regression

- [x] Add a real Simulator fixture forcing L1 eviction while preserving an L2 resident line.
- [x] Assert the reaccess is L2, starts after zero, and retains the configured service interval.
- [x] Build isolated baseline, observe the exact missing-clock assertion fail and preserve baseline ELF.

## Task 2 — minimal repair

- [x] Fill selected issue/response clocks from pending issue and existing private latency, only on the supported ordinary preview read path.
- [x] Run parent integration tests and the isolated main test executable.
- [x] Review the diff and check installed M4/profile identities remain unchanged.

## Task 3 — same-input quick checks

- [x] Replay fresh C4 short input and full Graph500 C4/C8 with baseline and/or preserved M4 as appropriate.
- [x] Verify population and report unknown-source fractions and CPI AE/MAE separately from validator status.
- [x] Document estimated-time design separately and get an independent read-only review of the repair and evidence.

No commit, push, milestone/default advancement or formal throughput gate is part of this task.

Completion: source repair + supported regression + main tests + three same-input quick replays + fresh read-only review complete. Results: docs/graph500-private-preview-clock-repair-20261117.md. Estimated timing remains design only; default M4 unchanged.
