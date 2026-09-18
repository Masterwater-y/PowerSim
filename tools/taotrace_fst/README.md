# gem5 TaoTrace reference producer

This producer restores the FastSim-branch gem5 FS orchestration for
cross-producer comparison. It is intentionally separate from the QEMU
production chain.

The comparison uses a dual-ELF contract:

- QEMU executes the workload ELF prepared under `var/qemu_fst/workloads/`.
- gem5 restores the established TaoTrace ROI checkpoint and executes the
  corresponding `*.gem5-x86-linux` workload from its auxiliary disk.
- ELF virtual addresses are producer-local. Cross-producer comparison does
  not require equal PCs, branch targets, or load/store addresses.
- Both producers must still emit canonical FST v7 with complete dependencies,
  C4 topology, user-plus-kernel scope, and measured CPL3 macro population.

The current maintained collection is Stockfish C4 with a 1M measured-user-macro
target under `first-core-macro-common-end`. The first gem5 core to reach the
target closes all four streams at one event; slower cores are not padded or
extended.

```bash
source /data00/xuhaoen/.agent_cli_auth/env.sh

"$PYTHON" -m tools.fst_pipeline taotrace \
  --run-id production \
  --workload 706.stockfish_r \
  --user-macro-target 1000000
```

The default inputs are the frozen, read-only FastSim common-end gem5 binary,
TCSim checkpoint, resource directory, auxiliary disk, and workload ELF under
`/data00/yinhaolang`. The collector copies the ELF for provenance but never
modifies those inputs. All generated files are written below
`var/qemu_fst/runs/<run-id>/gem5/c04/706.stockfish_r/` and
`var/taotrace_fst/assets/`.

Preview the exact command without creating an output directory:

```bash
"$PYTHON" -m tools.fst_pipeline taotrace \
  --run-id preview \
  --workload 706.stockfish_r \
  --dry-run
```

After collection, emit the cross-producer FST feature JSON:

```bash
"$PYTHON" -m tools.fst_producer_report \
  --workload 706.stockfish_r \
  --gem5-fst-dir var/qemu_fst/runs/production/gem5/c04/706.stockfish_r \
  --qemu-fst-dir var/qemu_fst/runs/production/qemu/c04/706.stockfish_r/fst
```

The JSON reports each producer's own measurement population, per-core and
aggregate functional FST features, and numeric deltas. It is not a gate and
does not compare PC, address, timestamps, or record sequence across ELFs.
