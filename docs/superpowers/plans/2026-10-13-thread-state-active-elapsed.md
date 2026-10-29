# Thread state and dual-cycle implementation plan

**Goal:** Implement auditable wait/scheduler facts and active/elapsed outputs, with active CPI guarded by evidence.
**Architecture:** Portable sparse state/ownership facts, independent timing oracle, state DAG consumer, explicit dual-cycle contract.
**Tech Stack:** C++17, Python, patched libgomp11.5, gem5-fs Linux x86-64.
**Spec:** docs/thread-state-active-elapsed-design-20261013.md
**Global Constraints:** preserve earlier uncommitted consumer; no guessed kernel offsets/timing; no trace timestamp scheduling; no duplicate kernel costs; FST budget20GB; no push requested.
**Review Focus:** physical CPU vs OS task identity; real block/wake pairing; wake != running; active CPI must not use elapsed when unknown.

### Task 1: Kernel collection interface feasibility and adapter
- Locate local guest kernel symbols/types/config; choose verified sched/futex hook boundary.
- Add fail-closed profile generator and guest/helper collector if available; output portable state facts and independent oracle.
- Test actual blocked vs EAGAIN/preempt and identity, with a bounded FS probe.

### Task 2: Runtime wait metadata
- Add request identity, OS TID and attempt/return markers in real x86 libgomp futex path.
- Extend gem5 marker adapter schema without modifying existing sync v1 interpretation.
- RED/GREEN host runtime tests cover fallback/EAGAIN/block paths and payload.

### Task 3: State graph and consumer
- Add cold validated state input; bind actual state events to source ordinal and sync predecessors.
- Add state/ownership tracking and active/inactive/unknown accounting; preserve per-core native execution while tasks blocked.
- Mechanism tests for causal block/wake/run, no whole-core freeze, phase/EOF/unknown fail-closed.

### Task 4: Dual outputs and maintained reporting
- gem5 explicit elapsed start/end/frequency, active scope/status; FastSim explicit elapsed and active availability.
- Maintained native validator scores active only; elapsed independently; preserve native PMU selection.
- Tests for null/error/source scope and max elapsed vs summed capacity; final source tests/build and FS evidence.

### Completion contract

Design documented; implemented slices and test evidence explicit; no placeholder data masquerading as measured OS state. Any support gap is described precisely with completed reviewable implementation and remaining work.
