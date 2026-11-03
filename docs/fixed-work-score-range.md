# Continuous fixed-work score ranges

`fastsim-binary-score-range-v1` adds independent physical-core business windows
to an otherwise continuous canonical FST v7 source.

```text
<replay-core> fastsim-binary-score-range-v1 <fst-path> <source-core> <begin-record> <end-record> <execution-records>
```

The BEGIN marker is record `begin-record - 1`; the END marker is record
`end-record - 1`. Both must finish retired macros. Scoring uses the half-open
record interval `[begin-record,end-record)`, corresponding to `(BEGIN,END]`.
Execution always begins at source record zero. Prefix and suffix records remain
functional and affect all shared resources. Source-core header identity is
checked independently from dense replay-core IDs.

The first implementation supports dense one-to-four physical sources,
`interval_weave`/`time_epoch`, fixed reference frequency and the existing sparse
per-UOP response feedback. Warmup wrappers, functional sync plans, recorded
scheduling, synthetic IRQs and incompatible response modes are rejected.
No marker inserts a global warmup barrier, drains a pipeline, resets model
state, or uses producer ticks.

Business cycles come from corrected retirement of that source's BEGIN and
END anchors. `fixed_work` in CLI JSON reports each window and the team elapsed
reference-Q16 interval from earliest BEGIN to latest END. Physical-core macro
counts remain distinct from user/task counts. Strict task-active time is
unavailable. Request membership follows source sequence; pre-BEGIN and
post-END requests execute in the context domain and are excluded from business
cache counts. `context_execution` covers both unscored prefix and suffix in
this format.

The translation feature bit (`1 << 9`) declares the functional `.xlat.gz`
companion. The timing engine consumes its dynamic translation fragments for
DTLB mapping identity and, when configured, physical PTE hierarchy walks.
Independent maintained translation audits also validate the companion's
addresses and completeness before publication. The Python
auditor accepts v1 paged mappings and v2's explicit special-request kinds and
physical transforms, and rejects invalid kinds, page fields, overflow, hash
or population mismatches. `FASTSIM_FST_STREAM_HASH` can select the maintained
hash helper from the actual build directory.

Validation in this change uses `fastsim_tests --fixed-work-only`,
`fastsim_tests --context-only`, and `tests/test_fst_dynamic_translations.py`.
Real four-worker guest and business acceptance belongs to the collecting
framework's raw evidence. The historical first-core-common-end native PMU
validator does not certify this distinct fixed-work contract.
