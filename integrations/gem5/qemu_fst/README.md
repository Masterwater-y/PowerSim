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
  X86QemuUserFstLowerer.py   SimObject declaration
  qemu_*.{cc,hh}             raw transport, lowering, state and FST emission
```

The `arch/x86` layout preserves gem5's public include paths and generated
parameter conventions. `python -m tools.qemu_fst build` supplies this directory
through gem5's native `EXTRAS` mechanism.

Raw trace ABI definitions are intentionally not copied into this overlay. The
build receives `QEMU_TRACE_INCLUDE_ROOT` and reads
`qemu_tracer/common/trace_entry_extensions.h` directly, so producer and
consumer use the same marker definitions.
