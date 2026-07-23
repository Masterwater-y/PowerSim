# TSim v27.0-cold16 workload set

This is the initial fixed-mechanism workload set for the TCSim functional
chunk model.  It deliberately models a **cold-start O3/Ruby ROI** and does
not model atomic, lock, barrier, spin, or happens-before semantics.

Build:

```bash
make -C workloads/v27
```

The collector ABI is unchanged:

```bash
NUM_CORES=8 OUT_BASE=data/raw_v27_0_cold16_seed0_c08 \
  FF_ATOMIC=1 bash scripts/collect_v27_workloads.sh train
```

`FF_ATOMIC=1` runs program setup through AtomicSimpleCPU with a non-caching
path, then switches to O3+Ruby at the first `m5_work_begin`.  The target body
therefore starts with a cold Ruby hierarchy.  This exact recipe must be used
for train, validation and heldout collection.

Workload sets:

- train: 16 mechanism workloads
- heldout: `phase_ws_shrink`, `phase_coh_decay`
- all: train + heldout
- smoke: one representative workload from compute, memory, coherence and phase

The 16 train workloads are grouped as compute (4), memory (4), ordinary
load/store coherence (4), phase (2), and cross-core skew (2).  Detailed
contracts and train/validation split are in
`/data00/yinhaolang/TCSim/docs/v27_0_cold16_workload_contract.md`.
