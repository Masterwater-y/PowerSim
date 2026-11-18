# Validation-excluded end-to-end throughput implementation plan

> Execute inline with executing-plans and test-driven-development. User approved
> implementation after redefining the primary metric to include preprocessing.

**Goal:** report C4/C8 throughput from engine launch through final model drain,
including input read/decompression/translation, initialization and functional
warmup; extra integrity/audit work is outside this interval.

**Architecture:** keep production models unchanged. Build isolated copies with
an opt-in, externally validated-input mode that skips duplicate content hashes,
whole-source audit scans and projection audits. Parsing, functional companions,
allocation, translation and all model work remain. A launcher verifies frozen
input receipts before starting a monotonic wall interval and reads model-end
timestamps. All engines run sequentially on CPU 52–63. Existing results remain.

**Tech stack:** Python 3, C++17/20, isolated CMake/Ninja builds; project tmp only.
**Spec:** latest user instruction in this conversation; this supersedes prior
simulation-only throughput defaults for this comparison.

## Constraints and review focus

- No timing predictions or prepared DynUop/template caches produced outside the
  timed interval. All original FST/functional companions are consumed again.
- Trusted mode requires an unchanged complete input receipt. Necessary bounds,
  format and decode checks remain; CRC work naturally done during decoding is
  retained, but trusted replay need not force audit-only footer/EOF consumption.
- Use actual monotonic wall spans, never subtract worker CPU time or profiles.
- Missing end markers, stopped cores, changed inputs or population mismatch
  reject a throughput result. Preserve original outputs and source identities.
- Source snapshots freeze the current checkout; do not edit unrelated dirty
  FastSim files. Current binary/config identity is reported explicitly.

## Tasks

- [x] 1. Add tests for end-marker coverage, timestamp ordering, receipt drift,
  and preprocessing inclusion. Implement `e2e_timing.py` and launcher.
- [x] 2. Snapshot FastSim and Sniper; patch only duplicate validation plus
  completion stamps. Build native zsim against the isolated functional reader.
  Verify strict/trusted semantic and model equivalence on fixtures.
- [x] 3. Run ten C4/C8 cases in all three engines under equal exclusive CPU
  budget. Gate populations and input identity; generate end-to-end and auxiliary
  simulation-only tables, per-case JSON/CSV and implementation notes.

## Execution rulings

No automatic commits or cleanup of earlier experiment trees. Use
`tmp/e2e-throughput-20261116/` for isolated source/builds and the progress ledger.
One controlled sample per case is a resource-controlled diagnostic, not a
multi-repeat throughput acceptance claim.

Review ruling: a reviewer identified that skipping the `.ffe.gz` whole-source
audit also removes guaranteed footer/CRC completion in trusted replay. The
contract now explicitly relies on prior strict CRC/content audit plus unchanged
SHA/stat receipts, and does not claim repeated complete gzip CRC validation in
the timed pass. Functional payload consumption and decoding bounds remain.

Completion: all 30 sequential replays passed input identity and per-core
population gates. All 49 adapter/timing/report tests passed against the new
isolated binaries. The primary report is
`fastsim-sniper-zsim-e2e-throughput-20261116.md` with JSON/CSV details; the
campaign and review/equivalence evidence remain under
`tmp/e2e-throughput-20261116/`. No production model or installed ELF was changed
by this work, and no commit/push was made.
