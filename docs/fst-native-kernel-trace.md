# Native kernel instructions in FST

Status: implemented in FastSim; gem5/TCSim producer changes are supplied as
cross-repository patches and must be applied and rebuilt before collection.

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

Adding kernel records must not change the user work represented by an ROI.
The producer therefore continues to stop each core after the requested number
of CPL3 FST records. Kernel records between those CPL3 records increase total
FST records but do not consume the user target.

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

The functional-boundary sidecar records both total and user-only warmup and
measurement populations, plus `trace_scope=user-plus-kernel`. TCSim compares
the oracle and `user-fst` target with `measurement_user_records`, uses the total
population for FST slicing, and reports both populations in its CPI summary.

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
2. every core reaches the requested `measurement_user_records` target;
3. total records equal warmup plus measurement records;
4. syscall markers and sparse metadata remain one-to-one;
5. the existing CPL cycle/PMU oracle conserves and has zero unknown class;
6. FastSim uses the native overlay and all synthetic kernel sources are off;
7. the measured FastSim region contains at least one kernel record.

For throughput, prefer direct in-probe FST over JSONL conversion. Privilege
encoding adds no bytes and no second input stream; the replay hot loop performs
one sign check and canonical OpClass transform. The main storage increase is
the real kernel instruction population, bounded by excluding classified idle
execution and retaining the CPL3 target domain.
