# Marian store lifecycle observation

Execute the user's approved observation-only plan inline. Reuse the existing
bounded store role/latency observer; do not enable timing experiments, change
latencies, recapture gem5, or commit.

- Add a disabled-by-default compact binary audit for regular nonlocal store
  UOPs, emitted only from the final accepted timing transaction.
- Capture dispatch/issue/retire, eligibility, prior release, send, release,
  bounded prepare/permission, role, canonical level, fragments and population.
  A distinct protocol callback is not retained by the bounded model: mark it
  unavailable, never infer it by subtracting a cycle.
- Test decoding, stage conservation, interval clipping, overlap and unknown
  callback handling. Compile and run required FastSim and targeted tests.
- Replay Marian P1 on existing common-end input. Compare every existing target
  output field with the frozen baseline, excluding named host-only fields and
  the new diagnostic configuration field.
- Analyze per-core service/residence histograms, clipped busy unions, empty
  gaps and prepare roles. Compare to existing common-end Sequencer histograms
  with explicit population, boundary and endpoint limits. No CPI gain claim.

Acceptance: timing-neutral observation plus a reproducible per-core report;
if existing native evidence cannot align members or callbacks, identify the
exact missing evidence instead of declaring causal closure.
