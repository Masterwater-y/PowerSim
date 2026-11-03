# Buffer service local fallback reference

User approved implementation of the preceding conversational design: retain
old modes, add default-off local fallback, couple cache/PMU/timing decisions,
validate against full replay before claiming sparse/high-throughput completion.

## Concrete first implementation

- Add ruby.buffer_service_local_fallback; requires repair, excludes no_fallback.
- Keep one epoch transaction as the reference dependency closure. Monotonically
  disable service lookup AND parent insertion per request. Written lines across
  cores and special events start in the baseline set. Ordinary independent
  requests continue to participate in the shared controller and CPU solve.
- Baseline events replay at their original baseline origin. Retained modeled
  requests must satisfy final admission, response and horizon certification.
  Changed requests are marked baseline and the entire epoch is replayed from
  its snapshot, so cache sets/queues/counts are never spliced across executions.
  Parent removal naturally reclassifies its followers on the next pass.
- Preserve incoming committed service entries on successful mixed transactions;
  never change their response to make the current epoch converge. Unsafe write
  overlap, unsatisfied global permissions/controller constraints, insufficient
  credit or exhausted passes remain explicit broad escape paths. Such escape
  paths must be reported, including the legacy lost-identity approximation.
- Separate preflight skips, local baseline events, new dynamic downgrades,
  accepted service-modeled events/aliases, broad escapes, replay work, and
  degradation batch-size distribution. Do not count an all-baseline commit as
  successful service coverage.
- Credit/max-pass semantics stay bounded. Native diagnostic config with a larger
  bank may be evaluated separately; do not silently enlarge default credit.

## Work and validation

1. Config and a failing mixed X/Y regression; then suppression plumbing and
   reference local replay. Check off/no-fallback preservation.
2. Tests for event degradation, unrelated retained merges, same-set resources,
   FR-FCFS and special events, budget skip/broad escape, cross-epoch retention.
3. Default build/full tests, independent review, diff checks.
4. Native baseline/epoch-fallback/no-fallback/local comparisons with maintained
   identity/source validator and serial throughput. CPI abs error + MAE,
   PMU MAPE/WAPE, event coverage and work costs in the report.

Ruling: this is the explicitly discussed whole-epoch reference stage. A true
sparse dependency engine is not implemented by merely naming a per-event mask.
No production-default change or speculative performance claim is authorized.
Snapshots retain the preexisting dirty workspace; no commit/push requested.

## Implementation ledger

- Mixed X/Y regression first failed on the previous epoch-fallback engine;
  local suppression then passed. A moved RAW follower degrades exactly one
  event while Y remains merged (two passes versus one seeded-mask reference).
- Added a test-only thread-local mask seed, with no config ABI layout change,
  to compare dynamic mask discovery against the same engine's full replay with
  the final mask supplied upfront. This is not an independent hardware oracle.
- Same-set, I-side/all-baseline, FR-FCFS response change, multi-epoch prefix
  preservation, budget skip and retry-exhaustion tests added. Off modes retained.
- Initial native artifacts remain under tmp/buffer-service-local-20260924.
  Every broad local escape there was a budget failure. Ruling: reserve one
  downgrade replay before first execution (up to two passes, respecting the
  configured pass cap), including a conservative controller work bound. This
  prevents paying for an initial pass whose normal local correction cannot
  run; actual charging and 10% accrual remain unchanged. Final outputs are
  separately frozen under tmp/buffer-service-local-final-20260924.
- Final default build and full test executable passed. Independent review found
  no blocking issue; all three existing modes reproduce prior native target
  scope exactly. Five arms passed the two-case maintained native validation;
  20 serial counterbalanced replay measurements completed with target-scope
  repeatability. Results and remaining limitations:
  [local fallback report](../../buffer-tso-service-local-fallback-20260924.md).
