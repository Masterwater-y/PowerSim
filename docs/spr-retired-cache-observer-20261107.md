# SPR retired cache membership observer

This observer evaluates architectural retired load cache membership from native
Ruby service evidence. It does not supply timing or cache facts to FastSim's
functional input, change the simulated hierarchy, or certify hardware PMU
equivalence. No hardware perf collection is required or performed.

## Production integration

`gem5-spr/src/mem/taotrace_response.hh` retains up to sixteen physical
attempt/service records per read UOP. Hierarchy outcomes carry the existing
`PerfTransactionId` from L1D through private L2 and LLC. Completion joins the
same ID before the native observer releases its packet mapping. A Sequencer
coalesced follower has a separate zero-issuance service record and its actual
resident/miss callback. Overflow, missing IDs and contradictory completions
fail closed.

The retirement ledger evaluates these records before registry erasure. Its
macro aggregation retains seven event labels rather than the whole request
history. Original `classify`, `classifyIndependent`, `classifyRetiredCache`,
retirement event counters and diagnostic namespaces remain available.
`retired_cache.events` uses the new membership evaluator and declares
`membership_policy=retired-load-cache-membership-v1`.

Each event records `count`, `unknown`, `nonmember`, `exact_count`,
`estimated_count`, `exact_nonmember`, `estimated_nonmember` and a complete
reason histogram. The audit requires:

```text
count + unknown + nonmember = retired_loads.count
count = exact_count + estimated_count
nonmember = exact_nonmember + estimated_nonmember
sum(reason counts) = retired_loads.count
```

Reason histograms must separately conserve all three certainty populations.
Per-core totals conserve every field and reason. The maintained
`tools/retired_cache_contract.py` reuses the existing independent FST, owner,
window, identity and native source auditor; it does not select proxy truth.

## Documentation boundary

The event dictionary binds the Intel SPR V1.40 JSON SHA256 recorded in the
design specification. Exact means closed native evidence under the stated
event mapping; all outputs retain `hardware_equivalence=unverified`.

Full store forwarding, resident permission upgrades, coalesced FB service,
prefetch-parent inheritance, remote supply, merged LLC requests and pure
device bypass are explicit estimates. Intel does not fully specify physical
split-fragment aggregation; positive split membership is estimated. L2 lacks
the explicit any-uop wording of L1/L3 miss descriptions, so positive multi-UOP
L2 aggregation is also estimated. Native prefetch lineage alone never proves
hardware event membership.

The machine-readable rule dictionary is
`configs/retired-load-cache-membership-v1.json`. Unsupported ISA membership,
scope/warmup crossings, stale service, conflicting parents and incomplete
lifecycles remain unknown.

## Verification and recollection

The direct FastSim build/test gate passed. Focused production-header tests
cover split attempts, coalesced service and sixteen-record overflow, in
addition to the existing PIO, prefetch-parent and legacy-classification cases.
Membership tests cover per-event OR, forwarding estimates, rejected parents
and certainty/reason conservation. Existing retired-cache audit regressions
passed.

The gem5 build uses `-j128` and gold. The previously verified binary is retained
at `tmp/spr-retired-membership-20261107-build/gem5.opt.known-good`. Build and
test receipts are in that directory.

The maintained runner:

```bash
python3 tools/run_spr_retired_cache_campaign.py \
  --output tmp/spr-retired-membership-c4-20261107 --run
```

selects the original Marian/Graph500/XZ/FLAC C4 executable and input identities,
10M measured user UOPs, 500K warmup and the same common-end policy. It preserves
the audited SPR DDR5/32GiB split-range configuration and scheduler profile.
Four captures run concurrently with atomic FST reservations and storage
receipts; total FST stops at 395GB and at least 30GB disk remains reserved.
Native target identity is checked separately during preparation.

Acceptance requires nonempty eligible workload-task loads, zero unknown load
population and zero L1/L2/L3 retired miss unknown on all four cores of every
case. Reports retain exact/estimated counts, source gaps and unverified
hardware equivalence even when the zero-unknown gate passes.

The final `-j128`/gold build completed successfully
(`tmp/spr-retired-membership-20261107-build/build-verified.log`).
The Ruby SE hello smoke retained 5,701 instructions, 31,114 cycles and
15,556,500 ticks. That smoke does not exercise the active FS retirement observer.

All four C4 captures have been admitted and are running under
`tmp/spr-retired-membership-c4-20261107/`. `freeze.json` records the binary,
observer source, dictionary, executable and input archive hashes; `plan.json`
records the commands, windows, CPU masks and storage budgets.
The four-load zero-unknown result remains pending until capture, preparation
and independent source-audit receipts exist.

A separate XZ pair uses 20K measured user UOPs and 5K warmup to check native
observer neutrality. The new capture waits for the old capture's reservation
release. Equality requires functional content identity, target identity,
retired populations, exact windows, legacy observations and native execution
counters. Independent KVM boots may prevent matching content or counters;
an unequal pair must not be reported as neutrality qualification.
No historical source is rewritten or counted as a new capture.

The initial old-binary short capture completed cleanly, but preparation
rejected `core 1: CPL/common end mismatch`. That core had 31,768 measured
functional records and zero measured user records; its CPL row retained
zero timestamps. The 20K/5K pair therefore provides no neutrality
qualification, and the new half was not launched. A new, separate pair uses
200K measured user UOPs and 500K warmup in
`tmp/spr-retired-membership-neutrality-{before,after}-retry1-20261107`.
The original failure receipts remain intact; no boundary field or validation
gate is relaxed. The four-load main campaign already uses 500K warmup.
