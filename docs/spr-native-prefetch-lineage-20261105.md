# Native prefetch lineage: bounded first slice

This is an **oracle diagnostic**, not a FastSim input or a retired hardware PMU
classification. The already captured FLAC/XZ/Graph500/Marian sources and their
gem5 binary remain unchanged. The new collector has been built, but has not
been used for a full capture.

## Evidence joined by this slice

For a private L2 controller's own `I, Prefetch` transition, an oracle generation
is assigned to its TBE and carried in the actual outgoing `RequestMsg` to the
shared LLC. The five existing LLC profiling actions record their native lookup
type under that generation. When `PF_IS, Load` consumes a real incoming demand,
the same TBE generation connects its actual trace context, sequence and nonzero
native attempt ID. Generation, physical line and controller identity must all
match. Lookup may happen before or after the demand attaches. At TBE release,
exactly one lookup and exactly one attached demand, without observed
invalidation, produce a *parent prefetch lookup witness*. A generation is never
reused, and a stale token cannot refer to a newer TBE.

The actual Ruby TBE key is a physical line within a controller. The witness does
not claim extra address-space or security distinctions absent from this protocol
key. The attempt is the existing `PerfTransactionId` assigned to an actual
sequencer hierarchy issuance, not a guessed fragment index. Because context and
sequence can cover multiple fragments or attempts, the registry also records
the issuance and witness attempts. A read-UOP diagnostic requires a resolved
lifecycle with exactly one admission, response and hierarchy issuance, no
coalescing, and single nonzero issuance and completion attempts both equal to
the sole witness attempt. Zero, missing or
untracked identity, absent/repeated lookup, multiple followers, capacity
overflow, invalidation, and any incomplete lifecycle remain unqualified. A
prefetch that started before observation was enabled is also untracked. The
host-side cap of 65536 is an observation bound, not target capacity; the JSON
diagnostic reports capacity overflow and live count as **simulation-global
cumulative values sampled at each core's finalization**, not per-core counts.

`native_service_diagnostics.l2_prefetch_parent_llc_unknown_uops` counts measured
read-UOP observations that are still unknown and have a qualifying parent
witness; indexes 0–4 are hit, tag miss, permission upgrade, merged miss and
remote supply. The five indexes are observation categories, not a partition of
retired macro loads. Per-sample
facts use the same order. These counts do not establish an architectural
`MEM_LOAD_RETIRED` hit or miss: the existing classifier is unchanged, and neither
native dynamic fact nor the generation is serialized into any FST companion.
With no functional pretracking, an issuance before BEGIN can have attempt zero
even if its demand later encounters a tracked prefetch. A missing witness in
this case does not establish that the prefetch had no LLC lookup.

This first slice intentionally excludes L1-prefetch→L2-prefetch chains, further
followers coalesced upstream, requests without a propagated attempt ID,
cross-cut missing lifecycle, and unsupported event or protocol paths. It cannot
derive all native cache outcomes or repair the existing unknown population by
itself. None of the 2026-11-05 four-case baseline outputs contains these new
parent witnesses.

## Pre-attempt-guard smoke (2026-11-05)

The pre-attempt-guard collector (`bcecb44c943f1521a6bf9e6b8372be323250163f8b40ab2a86f82fdcdcf1c481`)
and the frozen four-case collector
(`ebc2a7040ed014a36bac600cef6a68617022b69f916f1489780d84fb8d4a4505`)
each completed independent C4 FLAC smoke with 50k warmup and 100k user ROI in
466.33 s and 467.16 s respectively. Each run's four FST streams separately
matched their native task-clock source hash, record count and warmup/measurement
boundary; the `.sreq`, `.xlat` and `.core` companion probes also succeeded.
KVM execution interleavings and stream lengths differ across the runs; these
checks are not a bitwise or cycle-neutrality comparison.

The pre-attempt-guard collector's per-core parent-LLC diagnostic arrays were
`[0,4,0,0,0]`, `[0,0,0,0,0]`, `[0,4,0,0,0]`, `[0,0,0,0,0]`, with zero
reported global host-capacity overflows. These are raw observations from the
older code **before attempt uniqueness was enforced** and do not qualify the
whole UOP, retired macro PMU, or accuracy. Both short retirement audits report
`unavailable-common-clock` and remain unscored. Evidence:
`tmp/spr-native-prefetch-observation-20261105/lineage-smoke-evidence.json`,
`tmp/spr-native-prefetch-observation-20261105/lineage-smoke-parent-observations.json`,
and the two `lineage-smoke-*-retirement-audit.log` files.

## Attempt-guarded smoke (2026-11-05)

The attempt-guarded collector was rebuilt with `-j128` and gold after fixing an
extension-type template compilation failure. Its binary SHA-256 was
`db0adbf6d4d767b1d09e6df1970bdc62053dc105af65771f5ff5548c88d6912c`.
Its independent FLAC C4 smoke, with 50k warmup and 100k user ROI, completed in
466.34 s. Each of its four FST streams separately matches the native task-clock
source hash, population and warmup/measurement boundary; `.sreq`, `.xlat` and
`.core` companion probes also succeeded. Its per-core parent-LLC diagnostic
arrays were `[0,2,0,0,0]`, `[0,0,0,0,0]`, `[0,7,0,0,0]`, `[0,0,0,0,0]`;
the simulation-global host tracking overflow was zero. These are observations
of unknown read UOPs, not retired PMU classification. The short retirement
audit reports `unavailable-common-clock` and remains unscored. Evidence:
`tmp/spr-native-prefetch-observation-20261105/lineage-v2-smoke-evidence.json`,
`tmp/spr-native-prefetch-observation-20261105/lineage-v2-smoke-parent-observations.json`,
and `tmp/spr-native-prefetch-observation-20261105/lineage-v2-after-retirement-audit.log`.

## Meaning of a unique parent witness

In `MESI_Three_Level-L1cache.sm`, `PF_IS, Load` observes the demand's private
L2 miss, retains the prefetch TBE and transitions to `IS`; a subsequent response
is written into the cache and sent to L0. When the independent per-UOP native
accounting proves the demand's direct L1/L2 accesses and population, the
existing classifier can report the gem5-side L2 miss without using the parent
LLC witness. In the v2 smoke, representative core 0 and 2 samples have
`membership=1092` (L1 miss, L2 miss, L3 unknown), one directly observed L2
miss, zero demand LLC accesses and a parent LLC tag-miss witness.

An observed parent LLC **hit** proves that prefetch request's lookup hit; a
parent **miss** proves that request's lookup missed. Neither fact alone proves
which retired-load L3 event applies to the demand, whose state-machine path
joined an outstanding transaction rather than sending a separate LLC request.
Coherence or response provenance, final successful demand consumption,
split/retry/forwarding coverage and retired-macro membership must be accounted
for; invalidation, ambiguous attempts and missing identity remain unqualified.
The existing project policy also prohibits copying a parent's miss to a
follower solely from request merging. Intel's
[Sapphire Rapids event dictionary](https://perfmon-events.intel.com/platforms/sapphirerapids/core-events/core/)
defines L2/L3 events for retired loads and lists fill-buffer hits separately;
it does not itself specify how to map a gem5 late-prefetch parent lookup to a
retired demand. A controlled SPR microkernel for these exact late-prefetch
hit/miss and FB paths is needed before adopting such a mapping as hardware
equivalent. The common-clock and full-population gates are independent of this
semantic question. No unknown is cleared by this assessment.

The Ruby response path supplies only part of the missing lineage. The
private-L2 `h_data_to_l0` response carries its own controller as Sender but not
the ultimate LLC/remote/memory source or parent generation. L0 `hx_load_hit`
calls `Sequencer::readCallback(address,data,true)` with default
`MachineType_NUM`. Sequencer `hitCallback` records the associated `pkt->req`
context/sequence, the L0 external-miss flag and coalescing, then removes the
packet's perf transaction. The v2 smoke binary did not record the completed
attempt ID in the native snapshot; its issuance-to-parent join and single
response therefore did not independently bind the response to that attempt.
The subsequent oracle change records the completion attempt before
the packet mapping is removed and checks it against issuance and parent, while
missing, repeated or mismatched attempts remain unqualified. Dedicated tests
exercise this identity join. A subsequent slice observes DATA/DATA_EXCLUSIVE and
the message's immediate sender at the existing private-L2 prefetch TBE before
it is released. It carries this observation through the existing parent
generation, issuance attempt and completion attempt; invalidation, duplicate
responses, retry/forwarding and incomplete identity remain unqualified or
explicitly unknown. The diagnostic arrays for unknown read UOPs are
`l2_prefetch_parent_sender_unknown_uops` (unknown, shared LLC controller,
private peer controller) and `l2_prefetch_parent_response_unknown_uops` (unknown,
DATA, DATA_EXCLUSIVE). A shared controller is an **immediate message sender**,
not proof that data originated in resident LLC rather than memory. Per-sample
sender IDs are native controller indices, not target CPU IDs. This observation
still does not certify the final retired load's data source. Translating native
facts to SPR retired PMU events needs independent target-event evidence above.
The completion and response slices are present in the v3 binary; neither is
present in the v2 smoke binary.

## Follow-up: L1 prefetch and final-service qualification

Source review of the separate L1D-prefetch path confirms that the current
lineage slice does not close its unknowns. In protocol `L0cache.sm`, an
`I, PF_Load` prefetch sends `pa_issuePfGETS` without the later retired
demand's `PerfTransactionId`; `PF_IS, Load` records the late-prefetch demand
identity, profiles the L1 miss, and consumes the demand without sending its
own lower-level lookup. Thus the demand cannot be joined to the earlier
private-L2 lookup through the existing direct-demand attempt alone. The later
`IS, Data` callback reports a Sequencer response; `IS, Data_Stale` also
returns data but invalidates the line. A line address or matching count alone
does not prove which attempt supplied every byte of the retired macro.

The existing private-L2-prefetch witness covers a different path:
`L1cache.sm` attaches the attempt on `PF_IS, Load` and records the parent LLC
lookup plus an immediate sender/response before TBE release. Even when the
single issuance, parent witness and completion attempt match, the parent's
LLC tag result is a request observation: `L2Cache` as immediate sender does
not distinguish resident LLC data from DRAM, and a peer can supply data.
Invalidation, stale responses, multiple attempts/fragments and forwarding need
explicit treatment. The v3 witness remains diagnostic and does not change
`classifyRetiredCache`.

An observer-only L1 prefetch generation carried from L0 TBE into its actual
L2 request, and joined to the same demand completion attempt, could add a
typed parent lookup witness. That alone would **not** define whether a
retired load that joined an in-flight prefetch is an L2/L3 hit or miss under
the project's perf-like retired-event contract. A valid classification also
needs proven final accepted response/fragment membership and a documented,
tested event mapping; parent hit/miss cannot be inherited solely because the
requests merged. The target event dictionary marks hardware equivalence
unverified. The existing observations therefore do not justify clearing any
unknown, and this follow-up makes no simulator/FST changes or new capture.

Before implementing a broader oracle, define an explicit outcome contract for
the late-prefetch and FB cases, then test a genuine protocol path with
resident-L2, LLC, DRAM and peer/stale cases, repeated/retried attempts,
split loads and macro retirement; unmatched or ambiguous cases must stay
unknown. Any future generation/response identifiers and native service
facts belong exclusively to independent baseline output, never to FST or
FastSim timing input. Existing four-case sources do not contain the v3
witness and cannot be retroactively rescored as if they did.

## Completion-attempt and response smoke (v3)

The v3 collector SHA-256 is
`84dba78724bc827691d94e69d7c5702f2b26fed5db247d1cd3ccfe20d1e5966f`.
Its independent FLAC C4 smoke (50k warmup and 100k user ROI) exited normally
in 466.86 s. Each of the four FST streams separately matched its own native
task-clock hash, record count and warmup/measurement boundaries. The `.sreq`,
`.xlat` and `.core` companion probes also succeeded.

The four per-core parent LLC arrays were `[0,1,0,0,0]`, `[0,0,0,0,0]`,
`[0,6,0,0,0]`, `[0,0,0,0,0]`. Immediate sender arrays (unknown, shared LLC
controller, private peer) were `[0,1,0]`, `[0,0,0]`, `[0,6,0]`, `[0,0,0]`;
response arrays (unknown, DATA, DATA_EXCLUSIVE) were `[0,0,1]`, `[0,0,0]`,
`[0,0,6]`, `[0,0,0]`. The shared sender does not establish LLC vs DRAM as the
ultimate supplier. The source guards the observations on matching issuance and
completion attempts; attempt IDs are not exported per sample, so the smoke
does not independently demonstrate their equality beyond that code path. Host
tracking reported zero overflow. The native retirement audit reports
`unavailable-common-clock`; no CPI/PMU accuracy result is implied. Evidence:
`tmp/spr-native-prefetch-observation-20261105/lineage-v3-smoke-evidence.json`,
`tmp/spr-native-prefetch-observation-20261105/lineage-v3-smoke-parent-observations.json`,
and `tmp/spr-native-prefetch-observation-20261105/lineage-v3-after-retirement-audit.log`.

## Verification and remaining gates

The standalone oracle tests exercise prefetch lookup before/after follower,
same-line generation reuse, stale IDs, wrong controller/line, missing attempt,
duplicate lookup, invalidation, and host capacity. The native lifecycle fixture
joins a witness into the real response registry and retirement projection while
asserting L2/L3 remain unknown. Another retirement test checks unknown
cross-count JSON and unchanged classifier. Protocol source wiring is checked,
and the actual SLICC parser successfully generated C++ into an isolated `tmp`
directory. The later full gem5 C++ compilation and the separate short runtime
functional checks succeeded, within the scoring limits described above.

The four-case collector and the first lineage binary have been frozen with
their hashes. The separate attempt-guarded short FS capture checks schema,
witness population and functional binding; it does not establish timing
neutrality or PMU accuracy. A zero observed witness does not prove the path
works. Any new independent capture must preserve existing sources.
