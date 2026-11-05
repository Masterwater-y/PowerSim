# SPR private replacement C1: source contract before implementation

2026-11-04. Frozen design before code under the approved mechanism plan. Source audit was read-only; no C1 code or runtime acceptance exists yet. A dedicated EDITSTART brief is required after the current architectural-input worker stops. Parent runtime6bddd71a4a446ced6c738252ea077e23d3e0e5e9, child7e121b8a2e89e651aabf0b9bb895111b383ab427. Independent input work does not change these source protocol/cache-policy files.

## Decision

C1 is feasible independently of C2 **if it includes the receiving side of private writeback in the existing LLC**: `PUTX/PUTX_old`, `WB_ACK`, stable LLC `M`, and subsequent `M` demand service. “Private replacement only” cannot mean edits confined to the private controllers. No new Directory transaction or DRAM write is needed while every LLC victim still fails closed and there is no flush/external recall. A private dirty writeback terminates at the resident LLC; Directory retains its existing ownership of that LLC line.

Keep cold ordinary retired CPU data, complete existing functional read/store facts, strict `generic` timing preset, compatible bound topology, and both runtime/target early prepare OFF. Existing ordinary `.sreq`/physical read facts suffice for this slice. No new native source input or capture is needed; model-generated data provenance tokens are adequate for timing/ownership tests and are not application-value emulation. This does not close P2, enable full FS/frontends, or qualify CPI/PMU/throughput.

## 1. Native authority and exact scope

Paths below are relative to `gem5-spr/src/`. Physical L1D/private L2/LLC correspond to protocol L0/L1/L2. `mem/ruby/protocol/MESI_Three_Level.slicc` includes Three_Level L0/L1 but **Two_Level L2/dir**.

| Source | Binding needed for C1 |
|---|---|
| `mem/ruby/protocol/MESI_Three_Level-L0cache.sm:533-632,782-798,865-944,1239-1290` | Mandatory fill gate, actual selected victim, S silent eviction, E/M PUTX, no replacement TBE/ACK, deallocate without popping the demand. |
| `mem/ruby/protocol/MESI_Three_Level-L1cache.sm:304-313,412-467,635-755,912-1023,1074-1077,1187-1306,1398-1452` | Child-presence classification, own recall, replacement TBE snapshot, writeback races, conditional dirty updates, address-stall/wake. |
| `mem/ruby/protocol/MESI_Two_Level-L2cache.sm:285-303,706-724,774-784,996-1004,1146-1166,1210-1247,1385-1387` | PUTX current/old classification, receiving dirty merge, ACK-only paths, resident M service, blocked PUTX wake. |
| `mem/ruby/structures/CacheMemory.cc:300-398,420-438`; `mem/cache/replacement_policies/lru_rp.cc:48-92` | First free way; native replacement over all candidate ways; timestamp LRU strict less-than tie; reset/touch/invalidate sites. |
| `mem/slicc/symbols/StateMachine.py:1550-1604`; `mem/slicc/ast/FuncCallExprAST.py:195-242` | Successful transition consumes budget and restarts declaration-order port selection; resource stall has native next-cycle scheduling. |
| `mem/ruby/slicc_interface/AbstractController.cc:150-220`; `mem/ruby/network/MessageBuffer.cc:235-313,386-437` | Wait by triggering address, private wake-all, ranked LLC wake, ordered delay-preserving local transport, reanalysis. |

Include ordinary data interference with already accepted `IS_I`, `SM_IL0`, shared ACK and owner-transfer paths. Exclude LLC eviction/recall to Directory, memory WB acceptance, early prepare and prefetch, I-cache alias/inclusion, walker traffic, warmup/context/bootstrap, synchronization adapter, special/atomic/HTM/security/device traffic and broader network topologies. Keep late unsupported-input whole-run rejection. Preserve default OFF and old OFF/noncoherent paths.

## 2. L0 eviction is a transition on the victim, not the new demand

For incoming demand B, native mandatory handling first enforces the D-cache fill gate for an I-state request, then examines tag/availability. If full, `cacheProbe(B)` selects victim A and triggers `L0_Replacement(A)`. The demand for B remains at the input head; replacement does not execute `k_popMandatoryQueue`. The next successful controller iteration revisits the queues and can admit B in the same cycle if budget/resources allow. It is not a combined A-to-B allocation action.

| Victim A state | Required action and next state |
|---|---|
| S | Notify CPU eviction, deallocate A, I. No PUTS/PUTX, no TBE/fill, no ACK. Private parent can conservatively remain S and later obtain an ACK from absent L0. |
| E or M | Notify CPU eviction; enqueue local `PUTX(A)` at L0 `response_latency`; dirty payload only when the child cache Dirty bit is true, otherwise control-sized PUTX; deallocate A, I. No TBE, no ACK wait. |
| IS/IM/SM or otherwise protected victim | Stall/retry according to the actual victim transition/probe guard; do not choose a different non-LRU way, overwrite a reserved tag, or count a completed eviction. Existing fill/upgrade response must wake the dependent demand. |

L0 PUTX is distinct from `INV_DATA` replying to a parent recall, despite both carrying child data. Both travel on the ordered L0-to-private-L1 buffer. Preserve originating source order across PUTX, INV_ACK/INV_DATA, and subsequent GETS/GETX, even when their nominal request/response delays differ. Existing `local_last` implements delay preservation; extend its use without adding a NoC hop.

After native local E-to-M store, parent may still be E while child is M. Never infer child data cleanliness from parent state. Invoke the existing `hooks.eviction` at the actual L0 removal to invalidate StoreBuffer/forwarded-permission observations. The replacement packet owns a snapshot even after the L0 line record is erased. A later store to A reacquires permission through normal Sequencer ownership; the old PUTX must never complete that store.

## 3. Private L1 own recall and eviction transitions

Native `messageBufferFromL0` performs the fill gate **before** initiating a victim operation for an incoming GETS/GETX/UPGRADE in I. With no free way, it probes A. `inL0Cache` includes S/E/M/SM and S_IL0/E_IL0/M_IL0/SM_IL0, but not MM_IL0. Use native stable state/child-presence semantics, not physical dirty bits or `contains_l1` as a substitute.

| Victim/event | Action / next state / lifetime |
|---|---|
| S/E/M + own replacement requirement | `forward_eviction_to_L0_own` sends `INV_OWN` at private `l1_request_latency`, enters S_IL0/E_IL0/M_IL0; no TBE/fill allocated merely for this recall. Incoming demand B remains pending. |
| S_IL0 + L0_Ack | SS, consume response, wake all buffers waiting on A. |
| E_IL0 + L0_Ack | EE, consume/wake. |
| M_IL0 or MM_IL0 + L0_Ack | MM, consume/wake. |
| E_IL0/M_IL0 + L0_DataAck | Merge data/Dirty only if message Dirty=true, MM, consume/wake. |
| E_IL0/M_IL0 + ordinary child PUTX (`WriteBack`) | Conditional dirty merge, **MM_IL0**, consume/wake. The already sent recall ACK is still owed. Do not prematurely finish the recall or start replacement. |
| E/M + child PUTX | Conditional dirty merge, MM; consume child message. Even a clean E child PUTX yields MM by the source transition. |
| SS + L1_Replacement | Deallocate, I; no PUTX/ACK/TBE/fill. LLC may keep a conservative sharer bit. |
| EE/MM + L1_Replacement | Precheck one private-L1 TBE; allocate TBE and snapshot current DataBlk/Dirty; issue PUTX at private `l1_response_latency`; deallocate physical tag immediately; **M_I**. No line-fill credit. Demand B remains pending and may use the freed slot while A waits for WB_ACK. |
| M_I + WB_Ack | Release replacement TBE, I, pop response, idempotently deallocate A only if an A tag still exists, wake dependents on A. No demand callback. |
| M_I + forwarded GETX | Send TBE data to requestor, SINK_WB_ACK; retain replacement TBE until WB_ACK. |
| M_I + forwarded GETS | Send TBE data to requestor **and** LLC, SINK_WB_ACK; retain replacement TBE. |
| M_I + Inv | Send TBE data to LLC, SINK_WB_ACK. This branch is source-defined; external LLC replacement-generated Inv stays gated in C1. |
| SINK_WB_ACK + Inv | Send invalidation ACK, retain TBE. |
| SINK_WB_ACK + WB_Ack | Release TBE, I, consume/wake; no duplicate data publication. |

Forwarded GETS/GETX/Inv wait while S_IL0/E_IL0/M_IL0/MM_IL0 is unresolved. The accepted SM_IL0 upgrade race remains intact: its live demand TBE is not a replacement TBE. Own replacement attempts against SM and recall/transient states must park on A and resume after native completion. A blocked replacement does not make a new private demand TBE or fill reservation for B before native admission.

The crucial independent lifetimes are: physical slot A freed at EE/MM eviction; A writeback TBE retained until WB_ACK; B new demand owns its own TBE/fill; child recall has a distinct obligation; the original Sequencer service is not kept alive solely to represent A's writeback. Under private TBE capacity 1, B can have physical space yet wait for A WB_ACK. Under capacity 2, B can launch before that ACK. Both must be actual Simulator tests.

## 4. Existing LLC additions that belong to C1

Classify PUTX on **current** `Sharers.isElement(sender)` when the LLC services the packet, including after a parked packet wakes; do not freeze current/old classification at send time. Current A+B `sharers`/`exclusive` remain the one authority.

| LLC state/event | Native action |
|---|---|
| MT + current-owner PUTX | Clear sharers; conditional dirty data merge (`mr_writeDataToCacheFromRequest` writes only if dirty); send WB_ACK; enter M. No LLC TBE/fill, no Directory/DRAM request. Clear model exclusive-owner marker. |
| MT + old PUTX | ACK/pop only; do not overwrite data or disturb current owner. |
| NP/IS/ISS/IM/SS/M/MT_IB/MT_SB + applicable current/old PUTX | ACK/pop only per source table; no allocation, data overwrite or speculative directory update. In C1 NP+old is at most a defensive packet boundary; do not enable LLC eviction to manufacture it. |
| SS_MB/MT_MB/MT_IIB + either PUTX | Park request queue on A. Exclusive unblock or completion of owner-data/shared-unblock flow wakes it through existing LLC rank policy. |
| M + GETS | Send DATA_EXCLUSIVE from resident LLC; MRU/profile hit; MT_MB until exclusive unblock. No memory fetch. |
| M + GETX | Send DATA; MRU/profile hit; MT_MB until exclusive unblock. No memory fetch. |

WB_ACK is emitted at LLC **`to_l1_latency`**, i.e. current `causal_llc_to_l2_latency`, plus declared model network delay, not `llc_response_latency`. Private PUTX delay is `causal_l2_response_latency` plus model NoC delay; L0 PUTX delay is `causal_l1_response_latency` with no NoC. INV_OWN delay is `causal_l2_request_latency`. Existing remote forwarded data uses private response latency plus model NoC. All incoming port choices must follow source: child PUTX/recall replies at private port 2, WB_ACK at private response port 0, private PUTX at LLC request port 2; LLC unblocks port 0 precede responses port 1 and requests port 2.

An old writeback races with new ownership legitimately: GETX processed first may leave the old owner in SINK_WB_ACK, then LLC sees PUTX_old and ACKs without writing its obsolete payload. With GETS, PUTX can reach a blocked MT_IIB, or be accepted ACK-only in MT_IB/MT_SB while owner data and unblock still complete. Test both orders. Do not merge WB_ACK obligations with requestor invalidation ACKs or exclusive unblock.

Native LLC `m_writeDataToCache` copies response data but only sets Dirty when incoming dirty=true; `mr_writeDataToCacheFromRequest` copies neither data nor Dirty for clean PUTX. This matters after the LLC has accumulated dirty data and later sees a clean control writeback. No clean message clears conserved dirty authority.

## 5. Required changes to current production APIs and ownership

The current code cannot admit C1 just by removing the `can_allocate` victim exception.

1. **Controller dispatch:** `State::run` currently chooses `c.lines[m.line]`, calls a bool handler, and erases that input after every accepted transition. Add an explicit successful-transition result distinguishing consumed input, retained input, and address-parked input. A replacement of A triggered by B consumes one controller transition but retains B. Victim transitions operate on A's line entry without creating/mutating B's entry prematurely. After each success restart ports; budget exhaustion schedules native next cycle. Do not recursively finish recall/replacement/demand in one unbudgeted handler.
2. **Address wait identity:** current `park(c,m)` uses `m.line`; C1 needs `park(wait_address=A, original_message=B)` and wake by A. Preserve original ready/from/order identity and deduplicate each parked message. Resumption must re-probe B against current cache revision; do not reuse a pre-recall stale way selection. A selected transient victim is not license to evict the next stable candidate. Register resource waits separately from victim-address waits; local TBE/fill releases must unblock cross-line demand waiters without requiring an unrelated successful message.
3. **Typed internal identity:** `enqueue` currently requires `m.line == m.service.line / line_size`, and request token/service are reused across all messages. Add a separate internal replacement/recall context with victim line, controller-local generation, triggering demand identity for audit, and operation kind. Do not fabricate a new SequencerService or rewrite the user's request line to satisfy the old envelope check. Ordinary demands keep the current validation. Replacement envelopes validate route, victim generation and message kind; they are model generated only. WB_ACK returns the replacement token; forwarded data uses the **forward request's** token even when payload comes from an old replacement TBE. Reject stale/duplicate ACKs before state/data/credit mutation.
4. **States/messages:** add explicit private `M_I`, `SINK_WB_ACK`, `MM_IL0`, own recall event, L0 PUTX, private-to-LLC PUTX and WB_ACK. Existing `MI` is not a reason to conflate private WB waiting with future LLC replacement. `Line::child_token` must coexist with replacement generation, pending demand identity and remote forward identity. `child_wait`/`child`/discard logic must cover MM_IL0 accurately. `validate`/`idle` must include retained writeback/recall records, not merely queues/calendar.
5. **Physical cache hooks:** reuse `prepare_native_l1/l2`, `NativeCacheAllocation{line,set,way,revision,victim}`, `reserve_native_l1/l2`, `update_native_l2_data`, `native_tag_dirty`, `invalidate_l1/invalidate_native_l2`, `touch_native_l1/l2`, and existing LLC storage. Add a revision-checked **victim removal** action against the proposed A and a no-MRU conditional data-update action for child/LLC writeback. Reject/re-propose if state or set revision changed before commit. Charge physical eviction and source-defined writeback counters exactly once at the explicit action; `reserve_native` must see the subsequently free slot rather than silently doing an extra replacement. Never call legacy hierarchy eviction helpers that immediately write/invalidate another level. `PrivateHierarchy::evict_native_l2` and legacy `complete_fill_l1` are not replacements for the controller protocol.
6. **Snapshot versus cache storage:** current protocol `Line.data` is provenance, actual tag/dirty live in PrivateHierarchy/SharedSystem. The writeback TBE's provenance snapshot and message snapshot are required native transient data, not a second cache. While resident, physical dirty/protocol authority must agree with each source action; after physical deallocation, only the owned replacement TBE/message can supply the old data. Never resurrect an old tag to answer a forward or WB_ACK.
7. **Credit API:** reuse `ProtocolCredits::peek/acquire/release` with unique internal generation, `ProtocolLevel::L2`, `tbe=true,data_fill=false` for private EE/MM replacement. The existing `lease()` derives level correctly but couples identity to demand token; explicitly separate internal operation identity. Do not debit OnlineSequencer ordinary credit for replacement, L0 replacement TBE, private recall TBE, LLC PUTX TBE, or Directory DMA resources. Precheck replacement TBE before removing EE/MM or sending PUTX; fill-gate checks for B follow native input ordering. No rollback after half-sending a transition.
8. **Existing simulator integration:** extend only coherent hooks in `src/simulator.cpp:6366-6436` and existing SharedSystem LLC hooks around 2089. Keep `protocol_source`, domain 30 dispatch, `next_cycle`, cutoff/drain and StoreBuffer callbacks around 6728/7119. Retained victim work survives the triggering core's completion; it must not dereference a removed `members` entry. Do not route these writebacks through legacy `writebacks`/immediate-directory callbacks or `submit_pending_memory(...write=true)`.

### Replacement-sensitive source differences to resolve explicitly

Current hooks intentionally sufficed for a no-victim accepted domain, but opening replacement makes exact policy observable:

- `hooks.install` touches a stable private tag and writes dirty unconditionally. Native `u_writeDataFromL0Request/Response` does neither MRU touch nor clean overwrite. Reusing install for child PUTX/recall data would choose different later victims and can erase older dirty authority. Use an action-specific no-touch merge; preserve fix1 data-before-last-ACK reserved-tag protection.
- Native private `h_data_to_l0/hh_xdata_to_l0` touch MRU, and demand allocation resets replacement state. Existing coherent private stable-hit `hooks.access` uses generic commit-probe; it does not itself update timestamp-LRU via `touch_native_l2`. L0 load hits similarly need the actual native callback MRU action, whereas stores touch only when callback reports `wrote`. Audit these exact sites rather than relying on generic LRU age updates, which are a different representation from `native_touch_cycles_` used for victim selection.
- Native L0 `u_writeDataToCache` copies DataBlk but does **not** copy parent Dirty; L0 Entry Dirty defaults false and actual writeCallback sets it. Current adapter propagates `m.dirty` into L0 on install. For C1 source-policy parity, distinguish parent/LLC dirty debt from locally modified child data. Otherwise an exclusive read of dirty parent data creates a child dirty writeback where the source emits clean control. This is a narrow prerequisite decision for the implementation brief, not a claim that the accepted A+B release was re-reviewed or invalidated here.
- `coherence_install_llc` currently touches and clears dirty on clean messages. PUTX must instead follow source `mr` conditional merge and no MRU; owner-response `m` has its separate copy-data/sticky-dirty semantics. Resident M demand actions still touch normally. Keep these actions explicit and test clean control after prior dirty authority.

## 6. Ordering, wake and source-policy boundary

Keep accepted deterministic unbounded model queues and declared `(ready, from, order)` remote tie policy; this is **not** native PerfectSwitch/Throttle contention/arbitration. Strict connected homogeneous full-mesh import and source identity remain required; no graph weakening. C1 must preserve local ordered MessageBuffers and native port declaration order, action delays, controller budgets and address wake policy independently of the approximate transport.

Native replacement/stall wake follows real obligations: child ACK/data wakes victim waiters; private WB_ACK releases its TBE and wakes; shared/exclusive unblock and owner data wake appropriate LLC request waiters. Data/unblock and PUTX/unblock arrival order must both be legal. Native fill-full recycle and transition resource-stall next-cycle scheduling are explicit source rules; do not introduce arbitrary +1 delays to repair missing wakeups, global fixed-point passes or ROI-wide scanning. The current `resource_blocked` flag persists across runs until another accepted transition clears it: C1 must ensure resource release/reanalysis actively provides a schedulable wake, including no-message silent eviction and cross-line credit return. A calendar-empty outstanding wait is an invariant failure, not permission to abandon the writeback.

Freeze model-source identity for CacheMemory and LRURP in addition to the already hashed SLICC/controller/MessageBuffer sources. The current importer digest list at `tools/generate_spr_mechanism_profile.py:857` does not include those cache-policy files. Extend a replacement source sub-contract or deliberately version the existing digest; do not silently relabel an old fingerprint. Config fixtures may shrink capacities/latencies to force races; that is constructed model testing, not new target parameter evidence.

## 7. Real Simulator adversarial acceptance matrix

Extend `tests/test_spr_native_coherence_runtime.cpp` using existing `Source`, `access`, `dependent`, `config`, `run`, `events`, `canonical`. Tests must enter `Simulator::run`, production hooks, real caches, Sequencer/StoreBuffer/credits/frontier, and terminate with conservation checks. Tiny capacities are deliberate fixtures: direct-mapped 1-line L0, 1- or 2-line private L1, large LLC with proven no conflicting victim; second group uses 2-way sets and controlled timestamp ties. Compare against source event/action invariants, not expected wall-clock CPI improvements.

| Scenario / stimulus | Required witness, beyond merely not throwing |
|---|---|
| L0 E and dirty M victims | A then dependent B in one-line L0; exactly one PUTX on ordered local buffer, no L0 replacement TBE/ACK; child removed, parent MM; reread A obtains latest provenance. Clean and dirty payload cases distinct. |
| L0 S silent eviction | Two cores share A; evict one child's A with B; no PUTX/PUTS. Later remote upgrade still succeeds through conservative sharer/absent-child ACK. |
| Private S/E/M child recall | L0 larger than private cache; force B to displace A still resident in child. INV_OWN precedes child removal; private S_IL0/E_IL0/M_IL0 resolves using actual child response; data never supplied from stale parent E. |
| PUTX during own/remote recall | Force L0 A eviction while parent INV_OWN or INV_ELSE is in flight. Observe E_IL0/M_IL0 -> MM_IL0 -> MM with remaining child ACK; no premature deallocation, duplicate snapshot or stranded B. |
| EE/MM private eviction with TBE pressure | L0 eviction first makes EE/MM-like no-child authority, then private replacement. At capacities 1 and 2 verify A tag removed before WB_ACK, A TBE retained, no replacement fill, B launch waits only when required. Clean E control still enters private M_I and waits ACK. |
| Resident LLC M reuse | Complete private PUTX then read/write A again; M->MT_MB->MT; no second Directory fetch/DRAM read for A; dirty/provenance preserved and exact ordinary callback count. |
| Forward versus replacement | Remote GETS/GETX overlaps A private M_I. Observe TBE-sourced data and SINK_WB_ACK, replacement lease retained through delayed WB_ACK. Read and write requestors receive correct service token and final authority. |
| PUTX before exclusive unblock | Long unblock delay or constrained controller budgets lets replacement PUTX reach MT_MB/SS_MB first. It parks and wakes on unblock; no data overwrite, lost ACK or deadlock. |
| GETS owner data/unblock both orders | Use existing actual-Simulator owner-transfer budget/latency fixtures; add immediate old-owner private replacement. Exercise MT_IIB/MT_IB/MT_SB PUTX handling and final SS; check dirty authority even when ACK is sent before both flow obligations finish. |
| Old owner PUTX after new owner | GETX moves ownership before queued old PUTX is serviced. ACK old packet without writing stale provenance or clearing new owner. Reaccess A verifies no old-line resurrection. |
| Timestamp replacement policy | 2-way set: A/B loads, hit A, then C chooses B; equal-cycle timestamps choose first native way; child writeback/recall update must not promote A. Selected transient victim stalls even if another way is stable. |
| Store concurrency | Multiple buffered stores to A interleave local eviction, remote invalidation and reacquisition; every accepted store completes exactly once, writes only under actual permission, no stale forwarding certificate. Include store follower merged into one ordinary service. |
| Backpressure/budgets/frontier | TBE=1/2 and fill=1, per-controller budget=1, multiple independent conflicts; source batch 1/2/7/128 and host delay 0/100us yield identical canonical model events. No unlimited waiting-line scan, retry-spin without future obligation, or early frontend progress beyond published protocol source. |
| Terminal cutoff | A replacement WB_ACK or remote protocol work outlives the triggering core's final retire/store response. Protocol drains and leases/messages vanish; finished core end-cycle does not move to the last ACK. Compare per-core finish witness with audit, not total runtime wall. |
| Existing negative controls | LLC forced victim remains rejected before its lease/tag/state mutation; strict nongeneric preset, early prepare, late special input, instruction/walker/full-FS paths remain rejected; no returned partial score. Preserve no-private-replacement negative only for an explicit old narrower capability, not as the C1 positive test. |

Supplemental production-dispatcher/real-cache packet tests cover duplicate/stale WB_ACK, wrong route/generation, reused victim address, impossible old-transaction completions and source-legal arrival orders unreachable under the current homogeneous model transport. Label these supplemental; they cannot replace the actual Simulator cases above or be reported as a native network test. Do not add an external packet injection production input.

Expected terminal invariants: every ordinary owner callback exactly once; each private replacement TBE release exactly once; no live fill/ordinary credit retained solely for writeback; no dirty provenance lost; no cache installation by stale ACK; no reserved tag leaked; no parked victim/demand/recall context; no protocol memory work escaped from `idle`/`validate`; no LLC victim or DRAM write acceptance was traversed.

## 8. Ownership and implementation stop points

One later C1 owner should own `include/fastsim/spr_native_coherence.hpp`, `src/spr_native_coherence.cpp`, `include/fastsim/cache.hpp`, `src/cache.cpp`, coherent integration in `src/simulator.cpp`, and the existing runtime test file together. These are coupled by victim dispatch, physical tag removal and credit identity; splitting simultaneous edits across independent workers would obscure atomicity. `ProtocolCredits` changes, if needed, stay limited to expressive internal identity, not resource policy. Profile/config/CLI source-bound capability metadata and importer tests are a coordinated small second patch or same owner; parent retains curated docs/release validation/commit decisions. No child gem5 mutation or functional-input wire change is proposed.

Implementation order within that ownership: first victim dispatch/retain/wait identity and exact cache action hooks; then L0 PUTX/own recall/MM_IL0; then private M_I/SINK_WB_ACK and LLC receiving M paths; then real Simulator adversarial closure and rejection regression. Each stage remains gated until the combined C1 domain passes; component-only green must not remove production rejection.

If the implementation cannot retain dirty authority and answer in-flight forwarding without introducing a new standalone hierarchy, or cannot keep victim TBE work independent of retired owner lifetime, stop and return the design for revision. Those are C1 integration obligations, not evidence that C2 is intrinsically necessary. C2 starts when LLC capacity eviction/recall/Directory WB/DRAM frontend logical ACK is opened. Early-prepare/coherence interaction is separate again. The present source audit supplies a bounded implementation brief; no implementation or release claim follows.


## 9. Root release decisions

- Add an explicit default-OFF `core.causal_private_replacement` subdomain option;
  require the accepted native-coherence/SPR resource mode, strict generic preset,
  bound graph/protocol plus replacement source contract, ordinary cold data, and
  runtime/target early-prepare disabled. Preserve the accepted narrower A+B path
  and old/default OFF predictions. Keep LLC replacement and external DMA gated.
  This is one existing hierarchy with extended actions, not a new solver.
- Opening the domain includes action-specific timestamp/MRU updates, L0
  locally-written dirty distinction, child no-touch/sticky-dirty merges and LLC
  PUTX/current-owner handling above. Do not silently apply an eviction algorithm
  behind legacy immediate hierarchy callbacks. The source-sensitive corrections
  belong to selected C1 behavior; they do not retroactively certify replacement
  in the previous no-victim domain.
- Retain the existing A+B source fingerprint. Add a separately named replacement
  source sub-contract, optional importer capability and parser/CLI evidence;
  importing it describes the target and must not enable runtime automatically.
  Its exact source manifest is linked below. A change to one bound file requires
  a deliberate new audit/version, not trusting a source_bound boolean.
- Coherent private time/tie/MRU decisions use the model's own calendar timestamps,
  never native ticks. Preserve current declared network approximation and native
  controller budget/port/wake semantics. Source-required next-cycle resource
  retries are allowed; arbitrary delay added to hide a missing wake is not.
- Root owns CMake/docs/canonical/source snapshots/Git/experiments. C1 changes no
  native child or functional schema and needs no fresh collection. It requires
  real Simulator tests and scoped review before enabling any production path;
  whole four-case/new-CPI/throughput/full-P2 gates remain later.

Replacement source SHA256: `d035641e759032f40d6b2f3bd08bf474e99e3cd6bc53f5c917b579dd2a37628c`.
See [exact source manifest](layouts/spr-private-replacement-v1.source.json).
The algorithm hashes the canonical sorted path-to-SHA256 JSON object, without
trailing LF; the manifest file itself is separately ordinary JSON with LF.
