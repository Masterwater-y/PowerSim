# SPR ASID static maps and independent task clock

User direction: start repairing the two identified collector limitations, retaining the independent gem5-spr/yinhaolang repository and the parent's fastsim-spr branch. Preserve SPR timing/structure implementation and retired-stream-only model inputs.

## Decisions and alternatives

PC-only maps cannot safely describe multiple CR3 roots. Retaining their silent omission leaves missing static evidence; guessing or merging definitions mixes processes. Implement an opt-in ASID-keyed v4 map and keep legacy readers/defaults. Same-ASID/PC conflicts fail closed rather than inventing an instruction definition.

Existing assembly-entry ownership spans do not prove a native active-cycle numerator. Merely renaming that proxy or integrating elapsed ticks cannot repair the evidence. Add an independent native BaseCPU counter observer and an explicit paired task lifecycle contract, with identical functional boundaries used by model accounting. Hardware perf PMU equivalence remains separately unverified.

## Static map contract

`FSTIMP4`, version4, existing48-byte header,72-byte rows (ASID64 + existing64-byte v3 instruction row). FST feature bit12 declares the mandatory companion. Nonzero ASIDs must occur in the source .asmap. Lookups use (current ASID,PC), including A→B→A transitions. Unknown ASIDs cannot fall back to another process's table. Legacyv1–3 behavior remains supported and cannot certify multi-ASID maps. New source option functional_asid_static_map defaults false; native retired capture enables it. Producer tables remain explicit partial executable coverage; new identity must not be mistaken for complete ISA coverage.

## Independent clock contract

Contract `task-lifecycle-basecpu-active-v1`; counter domain `gem5-basecpu-active-cycles-v1`. This identifies an exact gem5 observation convention, not hardware CPU_CLK_UNHALTED certification.

For the audited Linux5.4.49 ELF da3d5e7dbf696b977f635d1123e06b324af873183d90111101ced005dc64e174:

- account_out after macro completion at ffffffff81517a0a closes prev; prev RBX, next R13.
- account_in after macro completion at ffffffff81067eef opens next; prev RBX, next R15.
- same-task scheduling bypasses both cuts. Out-to-in time is a separate transition bucket. Existing asm switch events remain scheduler facts and do not define the new accounting eligibility.
- IRQ work while enabled is charged to the eligible owner; transition IRQ remains transition. Idle is never a workload task. A workload halt is unsupported until modeled functional halt semantics exist, and makes strict comparison unavailable.

Functional events use names account_out/account_in, thread-state kinds8/9. Their fields carry task identity and retained source ordinal, never timestamps or cycle deltas. Plans carry optional TASK_CLOCK contract and initial/closure eligibility facts. Counter values and detailed observations exist only under oracle/.

The native observer subscribes to existing ActiveCycles, Sleeping and Cycles probes without calling updateCycleCounters or changing CPU state. It verifies and mirrors the actual notification order, including wake→first-tick exclusion, counter uint32 deltas, fixed period, and unsampled ON tails at ROI/cuts/common end. Independent counter snapshots and lifecycle segments must conserve; missing/duplicate cuts, changed frequency, unknown workload ownership, and workload halt refuse availability.

Macro denominators are independently counted over the same enabled task/phase intervals, excluding hardware-entry pseudo records using bound functional retirement evidence. Native timestamps, counter totals, durations and observed performance never enter FastSim. FastSim integrates its own corrected-retirement clocks and consumes the same functional lifecycle facts. Explicit closure is required for common-end eligibility; unknown tails stay unknown.

Legacy switch-entry certificates may establish ownership but cannot promote proxy ticks to a strict active-cycle oracle. Old captures lacking new cuts/counter observations remain proxy; no backfilling is permitted. Native PMU identity and hardware equivalence remain separate from gem5 contract validity.

## Verification

RED/GREEN tests cover ASID aliasing/conflicts/corruption/legacy fallback, real producer methods, independent native clock ordering and ROI tails, paired cuts and same-task bypass, separate transition/halt/unknown buckets, lifecycle-gated macro populations, tampered/missing evidence, legacy proxy non-promotion and plan timing pollution. Build both simulators, capture a new C4 source with audited assets, audit all companions/identity/population, replay with native-free plans, and use maintained validation with absolute CPI error and MAE whenever CPI is available. Preserve all non-probe SPR source hashes. Report unavailable evidence rather than weakening gates.
