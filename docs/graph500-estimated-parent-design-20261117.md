# Existing-parent Estimated read service candidate

User authorization: 2026-11-17, implement and validate the previously discussed bounded extension to the existing in-flight parent mechanism.

## Existing mechanism and gap

ParentServicePool already owns a per-core line index, stable service IDs, data/controller boundaries, leases, copy-on-write snapshots and dispatch-watermark expiry. Certified followers already delay their response and reuse existing IQ/Sequencer release logic. UNKNOWN does not remove functional tags; it disables the parent timing gate. The previous field repair fixes missing private-preview clocks, while a late CPU admission can still reject a complete selected service.

## Chosen approximation

For a supported unique owner with selected issue S, selected response R, selected aggregate queue time Q, final admission A, and the existing admission delay D (all in the permitted equal-frequency domain):

```
unqueued_service = R - S - Q                 # validated, not a fitted constant
estimated_data = max(R, A + unqueued_service)
```

The selected absolute calendar completion is retained as one floor; the other floor prevents an owner admitted later from completing before its existing unqueued service could finish. The old queue interval is not moved and charged a second time. The owner's CPU response remains unchanged and can exceed this estimate. For a private L2 service Q=0. Selected read-return completion is used as a conservative data bound, not certified physical fill truth; the follower runs its existing local return path after this edge. This approximation can remain inaccurate when corrected arrivals substantially change lower queue order; no convergence or native timing input is introduced to hide that limitation.

Eligibility: ordinary functional load, safe line, non-carried, late admission relative to selected issue+D, valid increasing selected boundaries. Supported new owners are private L2 and replayable unique memory/LLC-hit services. Remote supply, merged-memory without an existing matching owner, special traffic, invalid timing and resource-source rejection retain UNKNOWN.

Quality is explicitly Estimated, mutually exclusive with strict certification. Existing strict source rejection ledger is retained; separate estimate counters identify usable estimates and fallbacks. Estimated read services never grant write permission and do not gain certified TBE/fill ownership.

## Owner reuse and resources

A same-line L1 follower uses the existing decision/gate path. A new private L2 read, or a merged-memory event with matching nonzero lower-fill generation, may reuse an existing Estimated load owner while its data remains outstanding, provided the line is safe and the owner has no store consumer. No immutable edge is extended by a follower. An explicitly invalidated generation cannot be recovered by a max-time heuristic. In wait mode, reusing a service does not attach a second legacy service lease; observe retains baseline lease allocations. The unchanged owner CPU callback bounds identity expiry and store permission fallback even when read data is estimated earlier; each CPU request still uses its normal IQ/Sequencer lifetime. Existing capacity admission is retained as a conservative approximation.

One uint64 lower-fill generation is added to the existing record. No second per-line table, per-cycle scheduler or full-window replay. One lookup per relevant new-fill reuse and constant arithmetic per new estimated owner. Existing snapshot/expiry storage remains bounded.

## Validation and acceptance

Use a true Simulator dependency-displaced sibling-load fixture, exact source estimates from hand-computed examples, rejected metadata/future/special cases, matching/mismatching generation, copy-on-write, and expiry tests. Then use existing full functional Graph500 C4/C8 and new short C4 sources with maintained quick scoring. Compare OFF against the prior field-fix ELF exactly. Audit mechanism coverage separately from aggregate CPI; if estimates trigger without improving accuracy, preserve and report that result without default promotion.

Use repeated high-frequency ON/OFF pairs with validated preload. Primary throughput is measured functional UOPs / measurement_seconds, with preparation/initialization/warmup/process wall separately reported. Five percent is a proposed throughput-loss target, not a certified bound. Defaults and immutable M1-M4 artifacts stay frozen.
