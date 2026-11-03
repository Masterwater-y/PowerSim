# Same-arrival DRAM controller differential

User authorized implementation of E0–E4. Execute inline, no additional approval
for the independent controller oracle, no full-system recapture or runner edits.

1. Export P1 Marian/G500 final accepted DRAM streams with an observer binary; require
   equality of every existing target field against their frozen baselines.
2. Add an independent gem5 oracle importing every controller/interface parameter
   from captured config.json, preserving both ranges and masks. Direct per-channel
   generators/monitors avoid adding XBar arbitration absent at controller ingress.
   Record exact accepted requests/responses and retries; TraceGen ignores input
   packet IDs, so bind requests by per-channel order/address/cmd. Monitored pkt_id
   is requestor identity, not transaction identity: pair responses by address FIFO,
   certify unique addresses when available and qualify mixed repeated-address
   subsets. Write acknowledgments are not DRAM write service.
3. Test cold deterministic flows with native and quantized gem5 parameters,
   refresh disabled where appropriate, and production DramModel probes in exact
   ticks and integer cycles. Cover hit/conflict/parallelism/PRE/queue/rank/refresh.
4. Replay exported real flows through the standalone oracle; preserve warmup and
   reset measured counters at the FS barrier. Verify injection/admission equality
   before calling it same-arrival. Retried/backpressured flows are separate evidence.
5. Report parameter copy identity, quantization, controller differences, interaction
   and residual arrival/state/population terms. Only implement a generic timing
   fix after a demonstrated active counterexample; no fitted constants or iterations.

Corrections to supplied proposal: actual FS arrival→data is ~124, not137 cycles;
the13-cycle term is LLC fill/NoC return. B3 is incompatible with bounded_analytic.
Simple fixed-plus-burst rounding is not a general error bound: other constraints
round and scheduling can change. Use measured G1−G0 and exact-tick tests instead.
Order-dependent ablation differences measure interaction, not a proven upper bound.
The last workload residual includes initial state and traffic population, not just
arrival approximation. Same-arrival controller results are not CPI predictions.

Implementation status (2026-11-02): E0 done; E1 completed13 fixed flows/four
variants, with exhaustive window/epoch partition properties still open; E2 done
for both frozen streams/four variants; E3 completed candidate/projected demand
read sensitivity with a fixed native controller, retaining original warmup/ifetch
when projection is unavailable and leaving writebacks unchanged. Native arrival
members remain unavailable; compare their existing aggregate distributions only.
E4 production timing changes and subsequent CPI/six-case/throughput validation
are not started: no80% CPI mechanism closure, no permission request is pending.
Results: [controller differential report](../../controller-differential-results-20261102.md).
