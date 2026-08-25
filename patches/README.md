# Cross-repository patches

`gem5-taotrace-native-kernel-fst.patch` adds the opt-in mixed CPL3+CPL0 FST
producer while preserving the CPL3 record target and excluding classified
idle execution. `tcsim-native-kernel-fst-plumbing.patch` carries that mode
through the FS wrapper/matrix runner, gates trace scope and feature bit 4, and
validates `user-fst` against `measurement_user_records` rather than the mixed
total. Its CPI summary preserves both total and user-only trace populations.
Kernel microcode with non-portable x86 geometry is
kept in dynamic FST while its PC is omitted from the optional `.imap`, so a
static-companion limitation cannot abort native collection. Both patches were
dry-run against the current
`gem5-fs` and `TCSim` work trees on 2026-08-20. See
`docs/fst-native-kernel-trace.md` for application and validation commands.

`p0-external-baseline-contract.patch` contains the producer/runtime parts of
the P0 baseline contract owned by the sibling `gem5-fs`, `TCSim`, and
`taogen` work trees. `p0-external-baseline-contract-consumers.patch` upgrades
the remaining TCSim strict gate and CPI summarizer from the legacy v2 schema.
`p0-external-dtlb-attribution.patch` replaces the fallback-derived dTLB bit
with the real sticky O3 timing-translation result at retirement.
`p0-external-per-core-pending-attribution.patch` prevents equal local
ThreadID/InstSeqNum pairs on different O3 cores from aliasing in TaoTrace's
packet/commit pending tables.
`p1-external-native-response-sideband.patch` carries Ruby Sequencer response
facts on the original Request, aggregates split-access fragments, and emits an
oracle-only committed/late-response sideband. It does not change FST or promote
the current proxy PMU to formal accuracy.
`p1-external-native-lifecycle-drain.patch` is applied after that patch. It
joins LSQ issuance and bypass outcomes to Sequencer admissions/responses,
distinguishes no-Ruby terminal paths from missing responses, and delays the
global target exit until every measured committed memory UOP is resolved.
The current external gem5 work tree additionally contains the P2 native Ruby
hierarchy observer documented in
`docs/p2-native-ruby-pmu-contract-2026-08-19.md`. Its canonical overlay patch
is `p2-external-native-ruby-hierarchy.patch` and is applied after both P1
patches. `p2-external-native-boundary-inflight.patch` is applied after the
hierarchy overlay; it upgrades the observer to v6 and preserves only the
finite in-flight/ROB ledger across the exact warmup boundary.
`p2-external-native-online-summary.patch` is the final production-output
overlay. It keeps the v6 registry/join semantics but reduces resolved rows
online into `taotrace-native-summary-v1`; full per-UOP JSONL becomes an
explicit, default-off debug mode.
`p2-external-native-drain-terminal.patch` is applied last. It classifies O3's
independent memory-access predicate as an explicit no-Ruby terminal outcome,
adds bounded pending-identity diagnostics, and fails a malformed target drain
instead of polling forever.
`p2-external-native-identity-closure.patch` follows it. It prevents
fallback/proxy `SharedAttr` data from entering the native identity ledger,
retains response-complete identities until their SLICC hierarchy facts are
ready, imports a complete Request-carried lifecycle at a boundary, and
preserves split-request closure while aggregating fragments.
`p3-external-scoped-frontend-ledger.patch` adds a separate, timing-neutral O3
instruction-fetch ledger. TaoTrace pretracks in-flight requests, snapshots the
start population at each core's first CPL-accounted event, and freezes it at
that core's exact functional target; Fetch records real ITLB/I-cache request
lifecycles, retries, squashed responses, redirects, refetch causes, and status
cycles. TCSim aggregates the bounded per-core JSON objects. It does not add
events to FST and does not enable a FastSim wrong-path model by itself.
`p5-external-retired-bpred-oracle.patch` preserves the original BPred redirect
outcome as a sticky DynInst fact before Decode repairs the predicted target or
IEW redirects Fetch. TaoTrace samples that fact only when the branch retires
and labels the output `taotrace-retired-bpred-v1`. Apply it after the P3
overlay. It replaces the lossy retirement-time `DynInst::mispredicted()`
comparison; it does not change gem5 prediction, timing, or FST contents.
`p5-external-tcsim-retired-bpred-plumbing.patch` is the matching consumer
overlay. It preserves the source through aggregation and summaries and makes
matrix reuse and the strict user gate reject legacy branch labels.

The `p4-external-fst-instruction-page-map.patch`, p4b fallback, p4c namespace,
and `p4-external-tcsim-ifmap-plumbing.patch` files are withdrawn prototypes.
Do not apply them to production collection. The p4b Stockfish pilot reached a
Ruby functional read fatal, and the observation path did not establish a
complete CR3-scoped identity contract. They are retained only to preserve the
failed experiment and its review trail; see
`docs/native-cpi-ifetch-map-repair-2026-08-21.md`.

The patch has been checked against the current trees with:

```bash
cd /data00/yinhaolang
patch --dry-run --batch --forward -p1 \
  < FastSim/patches/p0-external-baseline-contract.patch
patch --dry-run --batch --forward -p1 \
  < FastSim/patches/p0-external-baseline-contract-consumers.patch
patch --dry-run --batch --forward -p1 \
  < FastSim/patches/p0-external-dtlb-attribution.patch
patch --dry-run --batch --forward -p1 \
  < FastSim/patches/p0-external-per-core-pending-attribution.patch
patch --dry-run --batch --forward -p1 \
  < FastSim/patches/p1-external-native-response-sideband.patch
patch --dry-run --batch --forward -p1 \
  < FastSim/patches/p1-external-native-lifecycle-drain.patch
patch --dry-run --batch --forward -p1 \
  < FastSim/patches/p2-external-native-ruby-hierarchy.patch
patch --dry-run --batch --forward -p1 \
  < FastSim/patches/p2-external-native-boundary-inflight.patch
patch --dry-run --batch --forward -p1 \
  < FastSim/patches/p2-external-native-online-summary.patch
patch --dry-run --batch --forward -p1 \
  < FastSim/patches/p2-external-native-drain-terminal.patch
patch --dry-run --batch --forward -p1 \
  < FastSim/patches/p2-external-native-identity-closure.patch
patch --dry-run --batch --forward -p1 \
  < FastSim/patches/p3-external-scoped-frontend-ledger.patch
patch --dry-run --batch --forward -p1 \
  < FastSim/patches/p5-external-retired-bpred-oracle.patch
patch --dry-run --batch --forward -p1 \
  < FastSim/patches/p5-external-tcsim-retired-bpred-plumbing.patch
```

Apply it only from a workspace where those three repositories are writable,
then rebuild gem5. Do not treat the FastSim-side v3 tools as formal until the
patch is applied: the validator deliberately rejects the old TaoTrace LRU-only
runtime and `taotrace-path-class-v2` accounting.

The patch does four things:

- fixes TaoTrace committed-memory PMU attribution across packet and fallback
  paths and emits a fail-closed v3 coverage ledger;
- emits macro-instruction perf-like CPI alongside cycles per user UOP;
- upgrades TCSim's merger/result gate to preserve the v3 fields;
- adds gem5-compatible TreePLRU support to the shared TaoTrace cache model and
  generates `uarch_profile.json`/`effective-target.json` after gem5 writes its
  final `config.ini` but before C++ SimObjects are created.

The consumer patch makes the usergate require v3 exactly-once accounting and
both CPI denominators, and makes the TCSim summary preserve those v3 fields
instead of silently dropping the kernel-event document.

The dTLB patch records whether `TLB::translateTiming` delayed any LSQ
translation fragment for a page walk. Packet/fallback cache attribution no
longer decides hit versus miss; an unknown retirement outcome is counted
separately and fails the formal gate.

The per-core pending patch keeps only truly shared cache/coherence state
process-global. Completion-to-commit correlation remains instance-local,
because gem5 O3 ThreadID and InstSeqNum identify an instruction only within
one CPU; neither value is a system-wide core identifier.

The P1 sideband patch observes `Sequencer::hitCallback()` without changing
Ruby behavior. `externalHit`, coalescing, responder identity, and split-line
fragment counts are joined to committed UOPs in a separate JSONL oracle. Late
responses remain observable after the CPI/retirement window freezes; unresolved
entries explicitly quantify the target-drain work still required.

The lifecycle/drain patch upgrades that stream to
`taotrace-native-response-v2`. `line_requests` remains the architectural
cache-line span, while `native_admission_count` is the number of physical
fragments actually accepted by Ruby. A committed UOP is complete only when
all admitted fragments respond after issuance closes, or when O3 records a
specific no-Ruby outcome such as store forwarding, local access, failed SC,
zero-size access, or predication. The drain observer is timing-neutral with
respect to the frozen CPI/FST boundary and never serializes into FST.

The first P2 overlay upgrades the stream to v5. It transports only captured
`(ContextID, InstSeqNum)` values through SLICC, records actual
`Sequencer::issueRequest()` mandatory-queue enqueues, and observes disjoint
L1D/private-L2/shared-LLC outcomes plus unique fills, Ruby directory fetches,
and accepted Ruby memory-port reads. The latter is deliberately not called a
DRAM transaction.

The boundary overlay upgrades the stream to v6. Native admission begins during
functional warmup, but every memory UOP retired before the measurement marker
is immediately erased. Therefore only requests still in flight or resident in
the ROB survive into the measurement epoch; their pre-boundary SLICC outcomes
remain available without counting the warmup population. The launcher also
freezes the PMU event dictionary under its run root and passes that snapshot to
the final-config sidecar generator and identity validator.

The online-summary overlay does not replace or approximate the identity
registry. Once a committed UOP becomes lifecycle-terminal, it folds the v6
row into exact CPL-scope counters, hierarchy populations, lifecycle ledgers,
and the proxy/native confusion matrix. It writes one summary JSON per core and
retains only a configurable bounded set of anomaly samples. TCSim records the
debug/limit settings in `request.json`; `--native-response-jsonl` recreates the
complete v6 stream only for targeted diagnosis. The FastSim auditor prefers
the summary and falls back to legacy JSONL only when no summary exists.

The drain-terminal overlay closes an O3 semantic distinction that
`hasRequest()` cannot express. Fully masked accesses and fault-suppressed
prefetches may keep an LSQ request object while `readMemAccPredicate()` is
false; they retire but intentionally never enter Ruby. The overlay marks them
`predicated_off`, polls the frozen drain every 64 cycles, logs only
power-of-two pending snapshots, and aborts after 32768 polls rather than
publishing an incomplete baseline.

The identity-closure overlay separates functional/proxy attribution from the
native Ruby ledger. A fallback `SharedAttr` can legitimately be reused across
memory micro-ops in one x86 macro-instruction, so its native fields are never
safe to join to a different `(ContextID, InstSeqNum)`. Packet facts remain
eligible only for the exact UOP; fallback UOPs wait for their Request extension
and registry callbacks. The reducer also treats lifecycle completion and
hierarchy completion as separate barriers: it does not erase a response-
complete identity until every hierarchy request has an L1D controller outcome.
Split responses merge the existing main-request extension before fragment
extensions so `issuanceClosed` and explicit terminal state cannot be lost.

The scoped-front-end overlay fixes the measurement-window ambiguity that made
raw O3 `stats.txt` unusable for NAb attribution: global stats reset/dump can
include work after a faster core reaches its local target. Its request
population is fail-closed:
`inflight_at_start + requests_started = all terminals + inflight_at_end`.
Mode, request-reason, send-attempt, and status-cycle populations are conserved
independently. The production output is one nested `frontend_accounting`
object in each existing `kernel-events-coreN.json`; no per-request JSONL is
written. The start must be the per-core first CPL event rather than the global
serial marker: otherwise a core that is in kernel mode at the marker can add an
unmeasured marker-to-first-user prefix to the Fetch status population.

The P5 branch overlay fixes a separate oracle ambiguity. Decode can identify a
direct-target miss, request a BPred squash, and then overwrite the DynInst's
predicted target with the correct target. A comparison performed later at
retirement consequently reports a hit even though BPred commits a miss. The
sticky bit is set on the Decode and IEW redirect paths and is counted only if
that same control instruction retires, matching the committed BPred population
without serializing speculative or wrong-path instructions into FST.
