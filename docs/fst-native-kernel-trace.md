# Native kernel instructions in FST

Status: implemented in FastSim; gem5/TCSim producer changes are supplied as
cross-repository patches and must be applied and rebuilt before collection.

Measurement-contract revision, 2026-09-11: the common-end rules below supersede
the old per-core target cutoff. Support for the native FST byte format does not
prove that a producer or validator implements this revised measurement policy.

## Goal and selected design

FastSim now supports a mixed functional stream containing committed CPL3 and
active CPL0 UOPs. Kernel UOPs use the same interval core, branch predictor,
DTLB, caches, coherence directory, and DRAM model as user UOPs. This removes
the largest semantic limitation of the previous `user FST + synthetic kernel
profile` path: kernel instruction mix and memory addresses are no longer
invented from an average event profile.

The design keeps FST v7 and the 64-byte hot record unchanged:

```text
user gem5 OpClass N     -> op_class = N
syscall transition      -> op_class = -1
kernel gem5 OpClass N   -> op_class = -(N + 2)
```

Header feature bit 4 declares that at least one privilege-tagged record is
present. Readers reject a negative kernel encoding without the bit and reject
the bit if the stream contains no kernel record. `canonical_op_class()`
restores `N` before FU/latency lookup.

The syscall gateway remains one user-scoped `-1` marker. The actual handler
instructions follow as kernel records. Consequently native replay must set all
synthetic syscall, page-fault, IRQ, and page-fill/state models to zero/off.
FastSim enforces this invariant in `SimulatorConfig::validate()`.

## Measurement boundary and idle policy

The controlling rule is section 3.0 of
[`project-goal-and-semantic-contract.md`](project-goal-and-semantic-contract.md),
`first-core-target-common-end-v1`. The requested per-core user-UOP count is a
first-core stopping threshold, not a minimum for every core. Every participant
keeps recording and updating dependencies, metadata and CPI/PMU until the
fastest core reaches 10M measured user UOPs at its macro boundary. That one
event closes recording and statistics on all participants.
Kernel records increase the mixed FST population but do not consume the user
target. The declared user-UOP/marker counting rule must match the oracle.

Slower cores can have fewer than the requested user count at the common end.
All actual records, their actual retired macroinstructions, active cycles,
and scope-matched PMU belong to the measured region. They must not be excluded
as unscored context or normalized back to the requested per-core count.
Functional warmup remains outside the measurement window.

During functional warmup TaoTrace drops decoded HLT/MWAIT/PAUSE instructions;
in the measured region it excludes records while the existing CPL classifier
is in `idle` and ignores a kernel tail already in flight at the global marker
until that core has an attributable user commit. This prevents `idle=poll`
PAUSE loops from exploding trace size or being misreported as active
user+kernel work. IRQ handlers nested over established idle are still included.
The detector's initial hysteresis remains an explicit approximation and must
retain the existing oracle validation gate.

Some gem5 kernel microcode and pseudo-instructions have no portable x86 macro
length, or enter capture after their first micro-op. They remain in the
dynamic FST. Native mode conservatively omits only those PCs from the optional
`.imap` static companion; an `.imap` anomaly must never abort or truncate the
dynamic mixed-privilege stream. User-only collection retains the existing
strict `.imap` assertions.

The functional-boundary sidecar must record both total and user-only warmup
and actual common-window measurement populations, plus
`trace_scope=user-plus-kernel`, the common-end policy, participant set, target
unit/count, common boundary identity and stop reason. The collection gate must
require the trigger core to reach the target, allow slower cores below it,
compare every core's actual value with the oracle, and use the complete mixed
measurement population for FST slicing. A manifest row must never be shortened
back to the per-core threshold after collection. The added provenance is a
required contract; it is not a claim that the existing sidecar writer has
already been upgraded.

## FastSim mode

Use the maintained overlay:

```bash
./build/fastsim simulate \
  --config configs/gem5-fs-native-kernel.cfg \
  --manifest /path/to/tao_trace/manifest.txt \
  --measurement-scope user-plus-kernel \
  --output /path/to/fastsim-native-kernel.json
```

The stable alias currently selects the v28_4 modeled-I-fetch profile. Use
`configs/gem5-v28_2-fs-native-kernel.cfg` only when an explicit
no-lower-I-fetch control is required.

Equivalent explicit configuration requires:

```text
measurement.scope = user-plus-kernel
measurement.native_kernel_trace = true
syscall.service_latency = 0
syscall.restart_latency = 0
syscall.cost_model = false
syscall.event_model = false
page_fault.event_model = false
page_fault.cache_state_model = false
page_fault.syscall_semantic_model = false
page_fault.roi_entry_page_state_model = false
irq.event_model = false
```

Stats retain aggregate functional PMU for the mixed replay. Record, retired
UOP/instruction, memory-UOP, and line-request subsets are input-exact;
branch-predictor and DTLB results are privilege-attributed FastSim model
events. Cache/coherence results cover the combined stream but are not
privilege-partitioned; the user-only cache PMU is therefore reported as
unavailable instead of being
silently mislabeled.

Native FST improves active kernel execution fidelity; it does not turn a
functional trace into a scheduler trace. Descheduled/blocked wall time,
run-queue delay, preemption, and exact interrupt arrival time are still absent.
Do not reintroduce them through syscall timestamp deltas. If a later scheduler
model adds blocked time, it must be a separate non-overlapping domain with its
own validation contract.

## Producer installation

The sibling work trees are read-only in managed FastSim sessions, so the
maintained changes are patches:

```bash
cd /data00/yinhaolang/gem5-fs
patch -p1 < ../FastSim/patches/gem5-taotrace-native-kernel-fst.patch

cd /data00/yinhaolang/TCSim
patch -p1 < ../FastSim/patches/tcsim-native-kernel-fst-plumbing.patch
```

Rebuild gem5 after applying the producer patch. Existing KVM/ROI checkpoints
remain reusable because the change affects only the restored O3 sampling
phase.

A matrix collection uses the existing arguments plus the new trace mode:

The following arguments describe the native format and target size. Historical
producer patches and these flags alone do not implement the revised common
end. Formal collection additionally requires the collector, oracle and
consumer gates to pass the common-end acceptance checks below.

```bash
python3 scripts/run_gem5_fs_cpi_matrix.py \
  --emit-functional-trace \
  --trace-format fst \
  --measure-cpl \
  --functional-include-kernel \
  --roi-target-domain user-fst \
  --warmup-mode source \
  --roi-insts 10000000 \
  ...existing workload, core, image, checkpoint and output arguments...
```

Do not also pass `--functional-user-only`. The TCSim and TaoTrace argument
layers reject that combination.

## Acceptance gates

Before replay, audit the actual promoted FST files:

```bash
python3 tools/audit_fst_privilege.py \
  --trace-dir /path/to/result/tao_trace \
  --require-user --require-kernel \
  --output /path/to/result/tao_trace/privilege-audit.json
```

A collection is acceptable only when:

1. every FST is v7/64-byte and feature bit 4 matches its kernel population;
2. the fastest core reaches the target and all predeclared participants close
   collection/statistics at that same event; slower cores may be below target;
3. total records equal warmup plus the complete common-window measurement
   records, and actual per-core user/mixed populations match the oracle and
   replay; no conversion stage pads or clips cores to an equal target;
4. syscall markers and sparse metadata remain one-to-one;
5. the existing CPL cycle/PMU oracle conserves and has zero unknown class;
6. FastSim uses the native overlay and all synthetic kernel sources are off;
7. the measured FastSim region contains at least one kernel record;
8. CPI cycles, actual macroinstruction denominators and PMU all use the common
   window, using every core's actual population;
9. post-window drain closes eligible pre-end identities under the PMU event
   dictionary, without adding new measured work or silently extending CPI;
10. unequal-progress multicore acceptance demonstrates one core reaching the
    target and another below it, with shared CPI/PMU closing evidence; early EOF
    before any target, interrupted collection and legacy local-cutoff oracles fail.

For throughput, prefer direct in-probe FST over JSONL conversion. Privilege
encoding adds no bytes and no second input stream; the replay hot loop performs
one sign check and canonical OpClass transform. The main storage increase is
the real kernel instruction population. Every core's measured user work is
bounded by the first-core stop event, with possible macro-boundary overshoot
on the trigger core. Warmup remains additional; 10M is not a total-file size
cap. Classified idle exclusion and the user target domain remain unchanged.
