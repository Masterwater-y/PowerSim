# P0 baseline and measurement-contract implementation

Date: 2026-08-18

This document records what P0 changes, what evidence motivated them, and what
must be true before any new CPI/PMU number is called formal. The normative
project objective remains `project-goal-and-semantic-contract.md`.

## 1. Confirmed defects

### 1.1 TaoTrace fallback PMU loss

The existing TaoTrace sequence was:

1. `accountCplCommit()` counted retirement and, when no
   `DataAccessComplete` attribution existed, inserted only a
   `pending_cpl_data_class_` entry;
2. `accumulateMicro()` derived and emitted a fallback `SharedAttr`, but did not
   consume that pending entry or update PMU counters;
3. the `functional_user_only` kernel/syscall early-return paths could skip
   `accumulateMicro()` entirely.

Therefore a committed memory UOP with no usable packet callback could appear
in FST while being absent from the gem5 PMU oracle. This is a source-level
control-flow defect, not a fitted hypothesis. The external patch changes the
state machine to the following exactly-once rule:

```text
committed memory UOP
  +-- packet attribution already present --> account once as packet
  +-- accumulateMicro will run -----------> freeze scope/line count;
  |                                          account emitted fallback once
  +-- functional early return ------------> account fallback immediately

late packet after fallback ----------------> observe; never account twice
```

Every v3 row carries `memory_accounting` with committed, packet-attributed,
fallback-attributed, rejected, unaccounted, duplicate, late-packet, and line
request counts. Formal validation requires:

```text
committed_memory_uops
  = packet_attributed_uops
  + fallback_attributed_uops
  + explicitly_rejected_uops

unaccounted_uops = duplicate_accounting_uops
                 = explicitly_rejected_uops = 0
```

It separately checks that per-scope `memory_uops` and `line_requests` sum to
the coverage ledger. A cross-line UOP contributes one memory UOP and multiple
64-byte line requests.

### 1.2 Three different LLC/memory populations were conflated

The old reports used `llc_misses` as if it could also stand for controller
merges, fills, and DRAM traffic. It cannot. P0 gives each population its own
field:

- LLC demand/tag miss;
- permission upgrade;
- remote private-cache supply;
- secondary demand merged behind an outstanding fill;
- unique fill allocation;
- accepted DRAM read/write transaction.

`configs/pmu-event-dictionary-v1.json` is the machine-readable semantic
dictionary. The current TaoTrace path classifier can provide only proxy tag,
upgrade, and remote-supply counts. It cannot observe Ruby TBE merge identity
or MemCtrl transaction identity, so merged misses, unique fills, and DRAM
transactions remain explicitly `unavailable`; they are never populated by
copying LLC tag misses. The comparison pipeline reads the dictionary's unique
`report_field` mapping, avoids double-weighting legacy aliases, and excludes
`unavailable` fields from APE/WAPE while retaining their status in the report.
Native Ruby/MemCtrl probe points are a later P1 task.

### 1.3 The runtime sidecar did not describe the final gem5 target

The old wrapper generated `uarch_profile.json` from CLI/default values before
the final SimObject tree existed. A real final C4 `config.ini` proves that its
private L2 and LLC use `TreePLRURP`, its clock period is 333 ticks, its memory
controller queues are 64/128 entries, and it has eight memory controllers.
The old sidecar instead recorded LRU and a synthetic queue window.

`tools/generate_fs_effective_target.py` now derives both the TaoTrace sidecar
and the sole `effective-target.json` identity from the final `config.ini`.
The manifest hashes:

- final `config.ini`;
- the PMU dictionary;
- the semantic content of `uarch_profile.json`;
- the shared TaoTrace profile/cache-model source headers.

It also records the effective core, cache, TLB, coherence-controller,
network, and DRAM parameters. The external wrapper patch hooks gem5's
`_dump_configs`: generation occurs after the final INI is written and before
`_create_cpp_objects`, so TaoTrace consumes that exact file at runtime rather
than a post-hoc replacement.

The checked current shared model supports LRU only, while the baseline uses
TreePLRU in L2/LLC. P0 therefore includes a TreePLRU implementation following
gem5's own `TreePLRURP` bit convention. Until that external patch is applied,
the manifest records `taotrace_cache_replacement_supported=false` and the
identity gate rejects the case.

### 1.4 Fallback attribution manufactured dTLB misses

The first v3 gate closed memory accounting but exposed a separate semantic
defect. `SharedAttr.dtlb_hit` defaulted to false, the fallback line-state path
never assigned it, and `accountCplDataPmu()` interpreted false as a miss.
Consequently nearly every fallback-attributed cache access became a dTLB miss,
even though cache attribution and address translation are independent state
machines. This is demonstrated by the first gate's aggregate counts: zstd had
891,292 fallback UOPs and 892,025 dTLB misses; SPH had 153,334 and 153,478;
Graph500 had 126,158 and 126,263; NAMD had 76,821 and 77,218.

The repaired oracle takes the result from gem5's real timing translation
path. gem5 documents `BaseMMU::Translation::markDelayed()` as the signal for a
hardware page-table walk. O3 `LSQRequest::markDelayed()` now sets a sticky bit
on the `DynInst`; retirement freezes `MISS`, `HIT`, or `UNKNOWN` independently
of packet/fallback cache attribution. A split access with any delayed fragment
is one committed memory-UOP miss, not one miss per cache line. Formal v3
validation requires:

```text
dtlb_unknown_uops = 0
dtlb_accesses = dtlb_hits + dtlb_misses
dtlb_accesses = memory_uops                 # in each reported PMU scope
```

The cross-repository implementation is
`patches/p0-external-dtlb-attribution.patch`. It deliberately does not use
TaoTrace's independent functional-TLB feature model as the gem5 PMU oracle.

### 1.5 Completion-to-commit tables had process-global keys with CPU-local identity

The follow-up source audit found that `pending_shared_attr_` and
`pending_cpl_data_class_` were `static`, while their key was only
`(ThreadID << 48) | InstSeqNum`. Both `ThreadID` and `InstSeqNum` are local to
an O3 CPU, so equal keys from different cores are not a system-wide
instruction identity. This is a real source-level aliasing hazard even though
the shared cache/coherence line state does need to be process-global.

`patches/p0-external-per-core-pending-attribution.patch` makes only the two
completion-to-commit maps TaoTrace-instance-local. A fresh four-workload A/B
gate found all four merged `kernel_events.json` files byte-identical before
and after this change. Therefore the defect is fixed defensively, but there is
positive evidence that it did **not** numerically cause the residual PMU error
in these C4 100K windows. It must not be presented as an observed accuracy
improvement.

## 2. CPI names and denominators

FastSim FST v7 already carries macro boundaries (`kMicroOp` and
`kLastMicroOp`). P0 now reports:

- `cycles_per_user_uop = active scope cycles / committed user trace UOPs`;
- user `perf_like_cpi = user cycles / retired user macro instructions`;
- combined gem5 `perf_like_cpi = active user+kernel cycles / retired
  user+kernel macro instructions`;
- combined FastSim `perf_like_cpi` as a clearly labeled
  `profile-derived-proxy`, because a user-only functional trace has no exact
  kernel instruction stream.

The legacy FastSim `scope_metrics.cpi` remains only as a compatibility alias
for cycles per user UOP. New comparison and summary tools use the explicit
names and report both metrics.

## 3. Implemented FastSim-owned gates

- `tools/merge_kernel_events_oracle_v3.py` merges only dense, consistent v3
  per-core rows and validates the completed document before writing it.
- `tools/validate_kernel_events_oracle.py` rejects v2 by default and enforces
  cycle, macro denominator, PMU class, syscall-profile, memory-UOP,
  line-request, and exactly-once conservation.
- `tools/validate_fs_oracle_identity.py` uses final-config hashes and the
  effective target; request.json comparison is an opt-in diagnostic fallback.
- kernel-event profiles now carry independent memory-UOP, line-request,
  upgrade, remote-supply, merged-miss, unique-fill, and DRAM-transaction
  counts. The old 14-field and transitional 16-field profiles remain readable
  but are non-formal; new generated profiles use 22 fields.
- FastSim reports the parsed kernel profile contract. Formal combined PMU
  comparison requires `kernel_profile_contract=p0-22-field`; maintained legacy
  profiles may still support CPI diagnostics but cannot silently pass this
  gate.
- the patched gem5 wrapper generates the single runtime target manifest;
  formal launchers only validate that immutable artifact and re-merge the v3
  oracle before inference. They never regenerate or overwrite identity after
  simulation.
- the formal dataset builder rejects non-v3 accounting, copies the effective
  target with each case, and overwrites stale copied oracle files on a resumed
  build instead of blessing them through a new index.

## 4. Verification evidence

FastSim-owned code passes:

```bash
cmake --build build -- -j16
./build/fastsim_tests
python3 tests/test_p0_contract.py
```

The seven focused P0 tests prove a 2-memory-UOP/3-line-request example is
accepted; unaccounted UOPs, scope line-request mismatches, missing hierarchy
fields, line expansion incorrectly reused as extra dTLB accesses, and an
unknown committed dTLB outcome are rejected; unavailable controller events
stay out of accuracy aggregation.
The TreePLRU overlay smoke test passes. The cross-repository patch both
dry-runs cleanly and, when applied to project-local copies, produces files
byte-identical to the reviewed overlays.

The final-config generator was also run on the formal C4 Stockfish result. It
read L1D/L1I=`lru`, L2/LLC=`tree_plru`, four cores, and marked the unpatched
runtime unsupported. Pointing the same generator at the patched shared-model
overlay changed only support detection and made the complete identity gate
pass. This is a direct fail-closed demonstration of the baseline mismatch and
its proposed repair.

A 100-macro-instruction FastSim slice retired 206 UOPs. Its report emitted
`cycles_per_user_uop=3.242718447` and macro-denominator
`perf_like_cpi=6.68`, proving the two denominators are distinct in the real
CLI JSON rather than only in a unit-test helper. The identity smoke also
rejected a previously generated manifest immediately after the event
dictionary changed, naming the old/new SHA-256 values, then passed after one
canonical regeneration. This proves dictionary and TaoTrace model-source
hashes are active gates rather than decorative provenance.

The same CLI smoke labeled the maintained historical 14-field combined
profile `legacy-nonformal`, while a 22-field profile was labeled
`p0-22-field`. The formal comparison gate requires the latter.

## 5. P0 activation evidence

P0 was activated on 2026-08-18/19. The producer/runtime, consumer, dTLB
attribution, and per-core pending-attribution patches were applied to the
actual `gem5-fs`, `TCSim`, and `taogen` paths, then gem5 was rebuilt. The
current superseding binary is:

```text
gem5.opt sha256 = 76e7d169f24b0c9ac5afaffa8b840e5a33bf3b6c567cddb832e50f608fc746bf
```

The superseding short strict gate is under
`tmp/p0-percore-c4-gate-20260819/`. It collected C4 zstd, Graph500, SPH, and NAMD
at 100K measurement records per core. Its exit code is zero. All 16 FST files,
1,600,013 measurement records, and 71,354,206 warmup records passed structural,
warmup/measurement-boundary, destination-class, syscall-metadata, and virtual
page-map audits. The four final-config identity reports have zero mismatches;
the effective target records L1D/L1I LRU, private L2/LLC TreePLRU, 4 cores,
8 DRAM channels, and runtime replacement support enabled. Each request hashes
the final gem5 binary above, so this result cannot be a reused pre-fix run.

The aggregate v3 accounting evidence is:

| Workload | Committed memory UOPs | Packet | Fallback | Line requests | Unaccounted | Duplicate |
|---|---:|---:|---:|---:|---:|---:|
| zstd | 1,384,721 | 493,429 | 891,292 | 1,384,725 | 0 | 0 |
| SPH | 253,154 | 99,820 | 153,334 | 253,156 | 0 | 0 |
| Graph500 | 199,777 | 73,619 | 126,158 | 199,777 | 0 | 0 |
| NAMD | 191,635 | 114,814 | 76,821 | 191,650 | 0 | 0 |

Thus every case satisfies committed = packet + fallback, and separately
conserves line requests and PMU scopes. `late_packets_after_fallback` is
nonzero by design: it records a callback that arrived after the functional
fallback was already emitted, but that callback is not counted a second time.

The dTLB repair is independently visible in the same runs:

| Workload | Fallback UOPs | Old combined dTLB misses | New combined-active dTLB misses | Unknown |
|---|---:|---:|---:|---:|
| zstd | 891,292 | 892,025 | 7,439 | 0 |
| SPH | 153,334 | 153,478 | 729 | 0 |
| Graph500 | 126,158 | 126,263 | 185 | 0 |
| NAMD | 76,821 | 77,218 | 466 | 0 |

The old count tracked fallback almost one-for-one; the new count does not.
All four cases also satisfy dTLB access = hit + miss and have zero unknown
committed outcomes. “Combined-active” follows the existing user plus active
kernel PMU scope; idle-class memory UOPs are not silently added to it.

The per-core pending-table A/B used the immediately preceding dTLB-fixed gate
`tmp/p0-dtlb-c4-gate-20260819/` as its control. For zstd, Graph500, SPH, and
NAMD, `diff -q` reports the merged v3 oracle before/after as byte-identical,
including both CPI denominators, all PMU scopes, the exactly-once ledger, and
late-packet counts. This simultaneously proves that the patch is
timing-neutral and rules it out as the explanation of the current four-case
PMU residual.

The real gate also found one previously untested consumer defect:
`validate_gem5_usergate_result.py` still required kernel-events-v2, and the
TCSim CPI summarizer silently ignored v3. The follow-up
`patches/p0-external-baseline-contract-consumers.patch` upgrades the usergate
to the shared v3 completeness check and preserves both explicit CPI
denominators in JSON/CSV. After applying it, the audit-only rerun passed all
four cases with exit code zero. The four `kernel-*.json` reports each state
`formal_pmu_eligible=true`, `memory_coverage_conservation=true`,
`dual_cpi_conservation=true`, and `unknown_ratio=0`.

This closes the superseding P0 activation, including dTLB and per-core
completion correlation. It proves identity and accounting integrity; it does
not prove that the remaining cache path-class proxy counts match native
Ruby/MemCtrl event populations. That accuracy question is the P1 boundary,
documented in `p1-native-pmu-population-audit-2026-08-19.md`.
