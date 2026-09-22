# QEMU-FST gem5 EXTRAS component

This directory is the complete, versioned gem5 EXTRAS component for lowering
QEMU raw traces to FastSim FST v7. It targets the dedicated gem5 v25.1.0.1
checkout without copying files into that repository.

```
config/
  qemu_fst_to_v7.py           gem5 SimObject launch configuration
build_opts/X86_QEMU_FST       isolated gem5 build variant
SConscript                   EXTRAS source registration
arch/x86/
  X86QemuFstLoweringCPU.py   transactional CPU SimObject declaration
  X86QemuFstTransfer.py      per-core worker SimObject declaration
  qemu_record_reader.*       committed record and state-delta reader
  qemu_fst_lowering_cpu.*    gem5-native x86 lowering
  qemu_fst_projector.*       FST v7 projection and companions
  qemu_fst_transfer.*        per-core worker and workload publication
```

The `arch/x86` layout preserves gem5's public include paths and generated
parameter conventions. `python -m tools.fst_pipeline build --component gem5`
supplies this directory through gem5's native `EXTRAS` mechanism.

The public workflow runs workloads serially. For each workload it starts one
gem5 worker process per logical core and publishes the multi-core FST set only
after every worker succeeds. Syscall returns are associated only within the
same shard; migrated, ambiguous, or cutoff returns remain unavailable.

The producer closes every shard when the first vCPU reaches its configured
CPL3 architectural macro target. Each worker consumes the complete committed
record shard through its footer and preserves the actual per-core population
at that common end. New QEMU files always declare complete dependencies, emit
the virtual-page map version required by their actual page-state fields, and
emit a complete operand `.imap` only for a single address space. FST v7 bit 7
and `.fst.io` identify MMIO/PIO without changing the hot record or canonical
OpClass. Both retain core/LSQ lifetime through `trace.allow_mmio_escape` and
bypass the RAM hierarchy.

Raw trace ABI definitions are intentionally not copied into this overlay. The
build receives `QEMU_TRACE_INCLUDE_ROOT` and reads
`qemu_tracer/common/qemu_fst_record.h` directly. `qemu_tracer` is the only
pending macro/event owner and publishes a committed record containing state
delta, access facts and event metadata. `QemuRecordReader` restores the
per-shard architectural state and passes each record directly to the lowerer;
there is no marker-stream transaction assembly stage.

The repository-level status and acceptance boundary, along with the raw
transaction, lowering, FST v7 extension, and FastSim consumption contract,
are documented in
[`docs/qemu_fst/mainline.md`](../../../docs/qemu_fst/mainline.md). The
previous recorded state is preserved in
[`docs/qemu_fst/history.md`](../../../docs/qemu_fst/history.md).
