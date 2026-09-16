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
  qemu_transaction_assembler.*  framed raw transaction assembly
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

Each worker stops FST emission after reaching its configured CPL3 UOP lower
bound at the end of the current complete macro, then continues parsing its raw
shard through the validated footer without advancing FST state. New QEMU files
always declare complete dependencies, emit the virtual-page map version
required by its actual page-state fields, and emit a complete operand `.imap`
only for a single address space. FST v7 bit 7 and `.fst.io` identify MMIO/PIO
without changing the hot record or canonical OpClass. Both retain core/LSQ
lifetime through `trace.allow_mmio_escape` and bypass the RAM hierarchy.

Raw trace ABI definitions are intentionally not copied into this overlay. The
build receives `QEMU_TRACE_INCLUDE_ROOT` and reads
`qemu_tracer/common/trace_entry_extensions.h` directly, so producer and
consumer use the same marker definitions. The transaction assembler requires
the QEMU-FST raw ABI v1 declaration and its mandatory capabilities once per
shard.

The repository-level status and acceptance boundary, along with the raw
transaction, lowering, FST v7 extension, and FastSim consumption contract,
are documented in
[`docs/qemu_fst/mainline.md`](../../../docs/qemu_fst/mainline.md). The
previous recorded state is preserved in
[`docs/qemu_fst/history.md`](../../../docs/qemu_fst/history.md).
