# SQLite ready checkpoint / XZ capture repair implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restore SQLite after database initialization and repair the evidenced XZ producer crash.

**Architecture:** Preserve the existing pre-fork checkpoint default. Add an explicit compute-ready phase with serial initialization, a KVM census, and a separate resume/switch marker. Bind both phase and startup policy into checkpoint identity. Diagnose XZ from the producer backtrace before changing code.

**Tech Stack:** gem5 full system, C launcher, Python runner and capture guards.

**Spec:** User request of 2026-10-22; existing collection flow in docs/spec-c8-fst-collection-20261022.md.

## Constraints

- Eight cores, eight memory channels, buffer TSO and 500K instruction warmup remain fixed.
- SQLite warmup wall cap 900s; total FST cap 200,000,000,000 bytes; reserve disk 20GB.
- Preserve historical capsules and checkpoint identity; new runner/launcher requires a fresh checkpoint.
- Never equate producer exit 0 with source qualification or model accuracy.

## Steps

- [x] Add failing tests for checkpoint phase transitions and serialized launcher initialization.
- [x] Implement compute-ready checkpoint and pre-checkpoint census; retain post-switch identity confirmation.
- [ ] Build a fresh SQLite checkpoint and restore it in a bounded short capture; verify all source gates.
- [ ] Symbolize and reproduce XZ crash, implement the smallest evidenced repair, run regression and bounded restore validation.
- [ ] Run full captures after short qualification, record progress and evidence without weakening gates.

## Review focus

Restore must not wait for an already consumed checkpoint event. Children remain blocked through save and CPU switch. Fresh and restored execution must switch exactly once. Serial initialization must detect failed children. Checkpoint policy must be part of identity. XZ failure is a producer crash, not a guest workload failure.

## Execution ledger

- SQLite tests first failed on missing state machine and concurrent setup; after implementation, 2/2 pass. Existing rate/input/native/XZ and guard tests pass.
- XZ native stack identifies an uninitialized pthread handle in APIC wakeup before worker startup. Atomic publication repair implemented; real kick/wakeup harness 2/2 pass.
- gem5 build passed with 153 jobs; FastSim build and full tests passed with GCC 11 runtime library. No commit or push requested.
- Fresh review found no marker/pipe/IRQ logic blocker. Auditor execution originally used mutable ROOT/tools despite frozen copies; redirected both experiment helpers to frozen tools and configuration snapshot, recording freeze-audit-amendment.json before any source audit.
- New producer SHA256 c2dd5b3f026d09be782ba97942dadc6e42c90a3ae601e72e7d0d1c7c51450cd3. Both new checkpoint constructions launched in parallel; real restore/source qualification remains pending.
- Runtime SQLite save exposed queued TTY census despite fflush. Added tcdrain before checkpoint, observed failing/passing wrapper regression, and rebuilt a separate final launcher capsule. Final compute-ready checkpoint saved; eight single-thread/no-swap processes and pre-release boundary audited, all payload hashes verified. Recovery-smoke is running.
- XZ two real 2e9-tick restores passed across the sub-quantum pending APIC window; complete recovery-smoke is running. Both workers automatically proceed to 10M source only after unchanged audits pass. Full source qualification is still pending.
