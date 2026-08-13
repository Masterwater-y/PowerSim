# FastSim gem5 overlays

These reproducible overlays target gem5 v25.1.0.1 commit
`c8222cc67a399bfc01e8658dd14b30d5bfd634f9`.

- `reference/` contains the gem5 reference trace producer: TaoTrace,
  BranchEvents, MESI support files, and the run configuration used to emit
  records-only traces for FastSim conversion.
- `dr_converter/` contains the DynamoRIO trace converter target that lowers
  drmemtrace input through gem5's x86 decoder and writes FastSim FST v6.

The reference overlay is the gem5 source of truth. The DR converter overlay is
DR-only and must not change the reference gem5 trace semantics. The repository's
`include/fastsim/fst_format.hpp` is the shared FST wire-format definition; the
DR target includes it through `FASTSIM_INCLUDE_ROOT`.

```bash
# Default applies and builds only the reference gem5 trace producer.
tools/build_gem5.sh

# Apply/build only the DR adapter.
TARGET=dr MODE=apply-and-build tools/build_gem5.sh
```

The apply script verifies the exact gem5 base commit and the selected overlay's
own `SHA256SUMS` before syncing it. `TARGET=mesi` is the default reference
producer path. `TARGET=dr` applies only the DynamoRIO converter overlay, and
`TARGET=all` is an explicit developer path that applies both overlays before
building both gem5 targets.
