# FastSim gem5 reference overlay

This reproducible overlay targets gem5 v25.1.0.1 commit
`c8222cc67a399bfc01e8658dd14b30d5bfd634f9`.

`reference/` contains the gem5 TaoTrace reference producer, BranchEvents,
MESI support files, and the run configuration used to emit records-only traces
for FastSim conversion.

```bash
tools/build_gem5.sh
```

The apply script verifies the exact gem5 base commit and the reference
overlay's `SHA256SUMS` before syncing it.
