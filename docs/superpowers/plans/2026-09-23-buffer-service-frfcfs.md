# Buffer service / FR-FCFS transaction

The user authorized continuation after the first implementation fell back on
every native epoch. Retain default-off, old paths, and counted work budgets.

## Design

Run the existing FR-FCFS controller repair inside the hierarchy transaction,
on independently issued requests only. CPU followers retain CPU slots but
never enter the controller input or create a cache/PMU lookup.

A response hint is a speculative absolute parent response from the previous
iteration. Apply it before constructing the line table; retain the natural
canonical response and descriptor separately. After controller scheduling,
compare every parent's used response with the produced response as well as
every request's admission. Any discrepancy rolls back cache, PMU, controller
and line state, then reclassifies requests on the next bounded attempt.
Retained parents from earlier epochs are already committed and immutable.

No-memory or configured pressure-bypass epochs use the normal canonical
controller result, not stale hints. A genuine controller repair failure still
falls back. Controller scanning/scheduling receives an additional conservative
work charge; insufficient credit rolls back, including already spent work.
Do not disable FR-FCFS or retime accepted parents after the transaction.

## Work

- [x] Replace blanket FR-FCFS-fallback regression with shared-response/one-lookup
  acceptance, and add forced controller response changes and rollback coverage.
- [x] Introduce canonical-versus-hinted response separation; filter controller
  parents; include controller outcome in fixed-point certification and budget.
- [x] Run complete build/tests and an independent code review.
- [x] Run maintained native identity/source validation on TeaLeaf and NAMD.
  Record actual accepted service events, fallback reasons, CPI absolute error
  and MAE alongside relative errors, PMU and serial throughput.

## Ledger

The managed dirty workspace is preserved using the pre-edit snapshots in
`tmp/buffer-service-frfcfs-20260923/before`. Existing work is not reverted.
This extends the bounded reference engine; sparse local dependency replay is
still a separate missing production capability. Do not equate controller
integration alone with completion of the throughput/coverage objective.

Final source/build/native outputs are frozen separately in
`tmp/buffer-service-frfcfs-final-20260923/`. Directed reordering converges in two
passes; the post-controller budget rollback preserves baseline callbacks and
PMU. Both native C4 cases use the existing one-entry controller bypass, so the
earlier scheduler-name exclusion was unnecessary. Increasing only the bank cap
to 65536 keeps 10% accrual but does not improve the scored CPI/miss counts.

The reviewed next design is written-line baseline fallback inside the same
whole-epoch transaction: suppress both new alias lookup and parent insertion on
those lines, preserve incoming write-overlap rejection, retain all requests in
CPU/controller certification, and report excluded events separately. This is
not implemented here. Special-event admission and sparse replay remain open.
