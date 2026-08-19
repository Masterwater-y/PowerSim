# P2 native Ruby PMU contract and evidence (2026-08-19)

## Conclusion

The old TaoTrace `path_class` counters are not a gem5 cache-PMU baseline. They
are an observer-side proxy and can disagree with the real Ruby controller even
when their exactly-once UOP accounting is conserved. P2 therefore keeps the
proxy only as a confusion-matrix diagnostic and builds the baseline from the
MESI Three Level SLICC transition actions.

The native observer does not enter FST and does not change cache timing. It is
an offline gem5 label source. FastSim production inference still receives only
the functional trace. Native labels are now reduced online to one small JSON
summary per core; the former per-UOP JSONL is retained only as an explicit
debug option.

## Current repair status

The following defects are closed and have direct source, conservation, or A/B
evidence. They must not be reopened as speculative explanations for the
remaining FastSim CPI/PMU error.

| Closed defect | Implemented repair | Evidence |
|---|---|---|
| TaoTrace fallback omitted or could double-count a committed memory UOP | exactly-once packet/fallback accounting with separate memory-UOP, line-request, and scope ledgers | the P0 C4 gate has zero unaccounted, duplicate, rejected, and scope-conservation errors |
| fallback cache attribution manufactured dTLB misses | use the O3 timing-translation delayed-walk result independently of the cache fallback | zstd combined-active dTLB misses changed from 892,025 to 7,439; SPH 153,478 to 729; Graph500 126,263 to 185; NAMD 77,218 to 466; all unknown counts are zero |
| pre-final-config sidecar described wrapper defaults instead of the simulated target | generate the sole effective target and TaoTrace sidecar from final `config.ini`; hash the config, event dictionary, and observer sources | the strict gate identifies the actual 333-tick clock, four cores, TreePLRU private-L2/LLC, and eight memory controllers |
| LLC tag miss was reused as merge, fill, remote supply, or DRAM traffic | split all event populations and mark unavailable native populations unavailable rather than synthesizing them | the event dictionary and validators reject aliases and incomplete formal profiles |
| UOP-normalized work metric was reported as perf CPI | report `cycles_per_user_uop` and macro-instruction `perf_like_cpi` separately, with explicit user and active user+kernel scopes | CLI and P0 tests prove that the denominators differ and are both conserved |
| completion tables used CPU-local identity in process-global maps | make completion-to-commit state TaoTrace-instance-local | the source hazard is removed; four-case A/B oracles are byte-identical, so this was not a numerical residual source |
| a response could outlive ROI admission tracking or stall final drain | keep a finite native lifecycle across the measurement boundary and drain only already admitted identities without charging drain cycles | v4-v6 complete in 57/3/41/9 polls for zstd/Graph500/SPH/NAMD with zero unresolved admission/response identities |
| SLICC retained a CPU `RequestPtr` beyond packet lifetime | transport only `(ContextID, InstSeqNum)` values through Ruby messages | removes the reproduced extension-vtable lifetime crash |
| alias subtraction was used as a proxy for requests entering Ruby | count `Sequencer::issueRequest()` mandatory-queue enqueues directly | v5/v6 report the exact hierarchy-request population |
| old consumers could accept or silently ignore non-formal oracle data | v3 fail-closed merge, identity, accounting, and dataset gates | incomplete labels now return nonzero instead of producing formal APE/WAPE |
| formal collection emitted hundreds of GB of per-UOP native JSONL and rescanned it offline | retain the identity registry, reduce each resolved committed lifecycle directly into exact-scope PMU populations, and emit one bounded `native-summary-coreN.json` per core | the online-summary schema preserves every v6 conservation population; debug JSONL is default-off and the auditor prefers the summary even when both exist |

The strict gate is full-system O3 plus MESI Three Level Ruby. It is not an SE
atomic data path. Observer-enabled runs preserve the simulated CPI and PMU
timing state; native outcomes are written only to an offline sideband and are
not copied into FST.

## Production output form

`taotrace-native-summary-v1` is the production collection contract. The
existing identity registry remains authoritative: a committed memory UOP is
not aggregated until its Ruby lifecycle is terminal, including late responses
during the already-defined target drain. The reducer then increments the same
populations that a v6 JSONL row carried:

- committed memory UOP, architectural line request, Ruby admission, hierarchy
  request, response, coalescing, and explicit no-Ruby populations;
- L1D/private-L2/shared-LLC disjoint outcomes, unique fills, Ruby memory
  fetches, and accepted memory-port reads;
- all seven exact CPL classes independently, plus the proxy/native confusion
  matrix and lifecycle/terminal-reason conservation ledgers.

Each TaoTrace instance writes `oracle/native-summary-coreN.json` at final
drain/destruction. Only a bounded anomaly reservoir is retained; its default
limit is 32 samples per core and `retained + dropped` reports the exact total.
The limit is configurable with `--native-anomaly-limit`.

Full `native-response-coreN.jsonl` output is default-off. It is enabled only by
`--native-response-jsonl` for a targeted reproduction, and does not change the
online summary. The audit tool consumes `native-summary-core*.json` first and
does not scan a colocated debug JSONL. Legacy result directories without a
summary remain readable through the streaming JSONL fallback.

This changes representation, not semantics or timing: Ruby probes still only
record facts in memory, the UOP is still joined by `(ContextID, InstSeqNum)`,
and no native label enters FST or FastSim inference. It removes per-UOP text
serialization and the subsequent multi-hour scan from the formal collection
path.

## Distinct event populations

The following populations must never be substituted for one another:

| Population | Exact increment point | Meaning |
|---|---|---|
| committed memory UOP | TaoTrace retirement | architectural denominator |
| architectural line request | committed address/size split at 64 B | requested line span |
| Ruby admission | `Sequencer::makeRequest()` after Ready/Aliased acceptance | physical fragment accepted by the Sequencer |
| hierarchy request | `Sequencer::issueRequest()` immediately before mandatory-queue enqueue | request that actually enters the Ruby hierarchy |
| L1D outcome | MESI Three Level L0 data profile action | hit, tag miss, or permission upgrade |
| private-L2 outcome | MESI Three Level L1 profile action | hit, tag miss, or permission upgrade |
| shared-LLC outcome | MESI Two Level L2 profile action | hit, tag miss, permission upgrade, TBE merge, or remote supply |
| unique fill | shared-L2 parent TBE fill completion | one completed parent fill, not every waiting demand |
| Ruby memory fetch | shared L2 `a_issueFetchToMemory` | L2-to-directory protocol fetch, not DRAM |
| Ruby memory read | accepted `memoryPort.sendTimingReq()` read packet | Ruby memory-port transaction, not a DRAM command/burst |
| DRAM read/write | not implemented | requires native DRAM-controller command instrumentation |

An initially aliased request illustrates why these populations are separate.
A coalesced load can share an existing hierarchy request, while a write queued
behind a read can later be reissued and perform its own lookup. Consequently,
`admissions - aliased_admissions` is not the hierarchy-request population. The
v5 sideband records mandatory-queue enqueues directly. v6 additionally keeps
the finite set of requests still in flight or resident in the ROB across the
warmup boundary, and requires:

```text
Ruby admission fragments = Ruby response fragments
hierarchy request fragments = L1D SLICC outcomes
each level's accesses = hit + tag miss + upgrade + merge + remote supply
committed UOPs = Ruby-terminal UOPs + explicit no-Ruby UOPs
unresolved identities = 0
```

## Strict-gate findings

The first exact-request v5 run did not pass the structural gate. Lifecycle and
drain conservation passed, but the analyzer found 15/0/22/31 incomplete
hierarchy UOPs for zstd/Graph500/SPH/NAMD. Direct JSONL evidence for zstd core
0 included sequence numbers 10467443, 10467554, and 10467569; each had one
admission, one mandatory-queue enqueue, one response, and zero L1D SLICC
outcomes.

The v5 examples initially identified a real boundary-crossing population:
requests could enter Ruby before the measurement marker and retire after it.
v6 fixes that population rather than waiving it. Native tracking begins during
functional warmup, every UOP retired before the marker is erased immediately,
and only the finite in-flight/ROB set survives when measurement is enabled.
Squashed identities remain subject to the existing terminal cleanup.

The completed v6 gate shows that this boundary defect was real but was not the
whole residual:

| Workload | Hierarchy requests | v5 incomplete | v6 incomplete | v6 ratio |
|---|---:|---:|---:|---:|
| zstd | 1,273,075 | 15 | 15 | 0.00118% |
| Graph500 | 182,681 | 0 | 0 | 0% |
| SPH | 234,140 | 22 | 16 | 0.00683% |
| NAMD | 149,692 | 31 | 24 | 0.01603% |
| **Total** | **1,839,588** | **68** | **55** | **0.00299%** |

All four cases complete admission/response drain; Graph500 is structurally
complete. The other three fail the exact `hierarchy request = L1D outcome`
assertion, so the overall audit deliberately exits 1. Every remaining example
has one admission, one mandatory-queue enqueue, one response, and zero L1D
SLICC demand outcomes. Examples occur beyond the initial boundary region, so
the remaining 55 rows must not be relabeled as another warmup-length defect.

This residual is numerically too small to explain the current cache-PMU or CPI
error: it is 0.00299% of hierarchy requests in aggregate and 0.01603% in the
worst case. The project therefore accepts it as a documented baseline
tolerance rather than spending another gem5 implementation and collection
cycle on it. It remains visible in reports but is not a release or collection
blocker. It must not be used to explain or fit the much larger FastSim PMU
residuals.

The strongest source-backed hypothesis is a request-population mismatch, not
a lost response. `Sequencer::issueRequest()` enqueues every Ruby request type,
while `MESI_Three_Level-L0cache.sm` maps `RubyRequestType:FLUSH` to
`Event:Flush`; its `Flush` transitions do not execute the demand
`uu_profileDataHit/Miss/Upgrade` actions. The v6 sideband does not record
`RubyRequestType`, so this is not proven for the 55 offending rows. This
hypothesis is recorded for provenance only; the request-type v7 diagnostic is
not on the active implementation path.

## Identity and lifetime

SLICC messages carry only `(ContextID, InstSeqNum)` values captured when the
`RubyRequest` is constructed. They never retain or dereference the CPU
`RequestPtr` after the Sequencer can free its raw `PacketPtr`. Earlier pilots
that transported `RequestPtr` into later controller actions crashed in the
extension vtable; the value-identity design removes that lifetime dependency.

At the measurement start boundary an admission may have occurred before
registry enable while its response occurs afterward. The Request extension is
the complete lifecycle record for exactly this case. If the registry has zero
admissions, P2 replaces its partial response-only snapshot with the extension;
it never merges the two and therefore cannot double-count the response.

The failure was reproduced before the fix:

- SPH core 1 and core 3: `admission=0,response=1`, sequence 1720419;
- NAMD core 0: `admission=0,response=1`, sequence 1128480;
- all functional targets had already been reached, but the drain remained
  nonzero until the 10-million-instruction safety exit.

With the fix, the uniform v4 gate using binary
`22c9b1782bd0c3c13e40fa66b9808e328cd378d49195862b03523a5e25598b61`
completed with drain polls 57/3/41/9 for zstd/Graph500/SPH/NAMD and zero
unresolved identities. This proves the lifecycle fix independently of the v5
hierarchy-request refinement.

## Execution path

The strict gate restores the FS checkpoint into O3 CPUs with
`cache=mesi-three-level`. Native admissions come from the Ruby Sequencer,
outcomes come from SLICC, and memory reads come from the Ruby memory port. It
does not route detailed-ROI data requests through the SE atomic cache model.
No-Ruby terminal UOPs are explicit O3 outcomes such as store forwarding, local
access, failed store conditional, predication, or zero-size/no-request; they
are not silently relabeled cache hits.

## Formal status

Passing native conservation makes these events valid gem5-internal baseline
populations. It does not make them perf-equivalent or prove FastSim accuracy:

- host raw PMU encodings and load/store/speculation scope remain unbound;
- FastSim still needs to reproduce the same lookup/merge/fill definitions from
  functional inputs without consuming native outcome labels;
- accepted Ruby memory reads must not be reported as `dram_reads`;
- observer enabled/disabled timing equivalence and wider-workload coverage are
  still required before promoting any cache model by default.

The event dictionary therefore marks native LLC merge/fill and Ruby memory
events `diagnostic`, not `strict`. Hardware DRAM events remain `unavailable`.

## Reproducible artifacts

The launcher copies the PMU event dictionary into the run root before any
sample starts. TCSim passes that immutable path to the final-`config.ini`
sidecar generator, and the identity validator checks the same snapshot. A
later repository edit can therefore neither silently relabel a running sample
nor make an independent `audit` invocation use a different contract. The
validator also reads the actual ROI target from each result's `request.json`
instead of reusing the launcher's default.

- lifecycle gate: `tmp/p1-native-lifecycle-drain-c4-gate-v3-20260819/`
- boundary-inflight v4 gate:
  `tmp/p2-native-hierarchy-boundary-inflight-c4-gate-v4-20260819/`
- exact hierarchy-request v5 gate:
  `tmp/p2-native-hierarchy-exact-request-c4-gate-v5-20260819/`
- preboundary-inflight v6 gate:
  `tmp/p2-native-boundary-inflight-c4-gate-v6-20260819/`
- machine-readable audit in each successful gate:
  `audit/p1-native-response-sideband.json`

The v6 gate uses gem5 binary
`fcefeb08b27d920fdaca858c6251f54b09397ff49abec0e379f29d08f3b9ae3d`
and the run-local event-dictionary hash
`a0e56537d2cda107bf9a641721eb30e60236a88bebca25ea029b8299f86a34e1`.
The launcher completed; its final audit exit code is 1 only because the strict
structural assertion above is fail-closed.

The production-summary implementation was rebuilt and A/B checked with gem5
binary `5127da5d32e1cf41fcda286b558cf1eb5a098c8a06b62f42504d6bacc198faf8`
on Graph500 C4 at a 1,000-record-per-core strict target. Both default and
debug runs completed with identical CPI/PMU (`weighted_core_cpi=0.960365`,
64,413 retired instructions). The default run emitted four summaries of
6,774--6,916 bytes and zero native-response JSONLs. Its audit reported 3,038
committed memory UOPs, 2,915 admissions/responses, 123 explicit no-Ruby UOPs,
zero unresolved identities, zero hierarchy gaps, and complete target drain.
Enabling debug emitted the four v6 JSONLs while all four online summaries were
byte-identical to default after removing only `full_jsonl_enabled`. Artifacts
are under `tmp/native-summary-smoke-20260819/{default-graph,debug-graph}/`.

`tools/audit_p1_native_response_sideband.py` still reads legacy multi-gigabyte
JSONL files in one streaming pass. The old and streaming implementations
produced byte-identical v4 output on zstd/Graph500; the streaming run took
110.13 s and 19,256 KiB maximum RSS. New collections instead validate the
kilobyte-scale online summaries and never open a colocated debug JSONL.

## Next accuracy step and data-collection decision

The remaining 0.00299% v6 structural residual is an accepted, reported
tolerance. Do not implement or wait for a request-type v7 diagnostic before
collecting the next accuracy data.

The next collection is a fresh gem5 **label** collection with the final P0/P2
binary, effective-target identity, dual CPI denominators, and native Ruby PMU
summary. Begin with a representative C4/C8 gate: Stockfish as a
frontend/branch non-regression control and LBM, zstd, Graph500, SPH, and NAMD
for cache/transient coverage. After that gate passes, collect the remaining
C4/C8 workload matrix. Defer C16/C32 until the C4/C8 absolute and trend
results justify the additional cost.

This is not logically a new FST requirement. CPI/PMU are gem5 labels, whereas
FST is the committed functional input to FastSim. The native observer, dTLB
oracle accounting, and report-contract changes do not add functional FST
fields or alter FastSim input semantics, so an existing FST with matching
workload, ROI, core binding, schema, and identity can be reused. A current gem5
collection may still emit another FST as a side effect of the integrated
TaoTrace pipeline; that duplicate should be used for record-count/hash
integrity comparison and need not become a second canonical dataset.

FST cannot be replayed through gem5 to reconstruct the missing native Ruby
labels: it contains committed functional execution only, not the speculative,
kernel, transient Ruby, or controller state needed to reproduce gem5 CPI/PMU.
Therefore the label run itself must be a fresh gem5 execution even when its
FST output is redundant.

Then score the existing FastSim cache model against the new native labels and
optimize in this order: L1/private-L2 lookup state, shared-LLC transient/TBE
merging, unique fill timing, and only then a separately instrumented DRAM
model. Do not tune against legacy `path_class` LLC/DRAM counters.
