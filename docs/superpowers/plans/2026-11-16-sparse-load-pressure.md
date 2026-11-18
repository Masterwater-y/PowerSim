# Sparse load pressure: rollback, implementation and Graph500 validation

Scope: execute the user-authorized rollback and replacement of failed P0/P1/P2. Preserve M4 model/profile/ELF and experiment evidence. Native timing is diagnostic only. No commits or pushes requested.

## Model decision

A blocked cohort consists of supported ordinary load fragments ready before the same earliest member-slot release R. With L target load ports, member n has an optimistic resource lower bound R + floor((n-1)/L). Compare with the existing successful-admission time; propagate only the excess through existing memory/dependency feedback. Each new R starts a cohort. This is a coarse success-demand bound, not an attempt scheduler: no speculative/wrong-path traffic, retry multiplier, per-UOP calendar or extra fixed latency. Unsupported accesses retain the old path. Per-core constant state survives chunks and feedback snapshots.

## Tasks

1. Back up the nine failed files; restore the six pre-failure files only after matching both failed and baseline hashes; remove the three failed-only code/config/test files. Save rollback receipt.
2. RED: tests must fail while the sparse helper is absent. GREEN: implement fixed-size cohort state; test uncongested coverage, 3-port bursts, existing-wait absorption, group changes, cross-copy/chunk continuity, invalid input/overflow. No dynamic containers or per-cycle loops.
3. Add default-off core.sparse_load_pressure, config/CLI/effective-output plumbing, per-core snapshots and counters. Gate only supported ordinary single-fragment loads with actual member-slot blocking. Retain existing packet/service/population paths.
4. Build with parallel LTO, run fastsim_tests and focused integration tests. Keep candidate and restore frozen installed M4 ELF after build. Verify no failed mechanism symbols remain in active code.
5. Same-input Graph500 C4/C8 OFF/ON quick scoring; compare population, mechanism coverage and CPI/AE. Build the maintained receipt-validated timing reader with identical model source. Run three sequential OFF/ON pairs per case, alternating order; report preparation/warmup/simulation/E2E separately. Suggested development budgets: E2E <=3%, simulation <=5% loss; no claim of formal 30-pair acceptance.
6. Independent final review and one fix pass if needed. Record results, rollback status and new approximation limits. Preserve all outcomes, including no CPI benefit. Do not promote the candidate without evidence and an explicit default decision.

## Review focus

Cohort eligibility/ownership, shared-service timing remains one-pass, special/forwarded/multifragment exclusions, no double-added wait, same-release cohort across chunks/copies, host costs and all negative samples retained.
