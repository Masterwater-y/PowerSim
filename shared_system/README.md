# LLMSim Shared System

This directory contains the MTAO MESI/cache shared-system component copied into
LLMSim for deploy-side PMU generation.

## Recommended Workflow (Offline)

1. Run `eval/eval_quota_cycles.py` with `--emit-mem-events-dir DIR`. It writes
   one `<workload>.mem_events.jsonl` per workload, which is a serial global
   memory event stream stamped with predicted cycle times. `window_end`
   markers are appended after each LLMSim window so per-window cumulative PMU
   snapshots can be reproduced offline. The main inference loop is not
   coupled to the simulator at all.

2. After eval finishes, run the C++ shared system in batch mode:

   ```bash
   cd /data00/yinhaolang/LLMSim
   python3 shared_system/run_shared_system.py \
     --build \
     --events-dir logs/shared_system/mem_events \
     --out-dir    logs/shared_system/pmu \
     --snapshot-interval 200000
   ```

   This processes every `*.mem_events.jsonl` file with a fresh simulator
   instance (state still persists across LLMSim windows within one workload),
   writing `<workload>.shared_pmu.jsonl` next to it.

3. Each output line is a cumulative `pmu_snapshot`. Use the last snapshot
   per workload for end-of-trace PMU comparison versus gem5/ROI.

## Input Contract

Input is a JSONL global memory sequence sorted by predicted memory time. Minimal
fields:

```json
{"event_type":"mem","seq":0,"core_id":0,"thread_id":0,"paddr":4096,"cacheline_paddr":4096,"is_load":1,"is_store":0,"is_atomic":0,"size":8}
```

Accepted address fields, in priority order:

- `paddr`
- `cacheline_paddr`
- `cacheline_addr`

The simulator does not use label-only fields such as `coh_oracle`,
`path_class`, or `d_mshr_depth` as input.

To force a PMU snapshot without resetting state, insert:

```json
{"event_type":"window_end"}
```

or:

```json
{"event_type":"snapshot"}
```

## State And PMU Semantics

One simulator instance processes the whole stream, so L1/L2/LLC, MESI
directory, TLB, page-walker, MSHR, and uncore counters naturally persist across
LLMSim windows.

Each output line is a cumulative `pmu_snapshot` containing:

- `cache`: cache hit/miss counters
- `uncore`: remote hits, writeback-required, invalidation fanout
- `tlb`: TLB and walker counters
- `pmu`: PMU-style raw counts
- `rates`: derived rates such as `mr_llc`, `mr_l1d_ld`, `mr_l1d_st`

## Single-File Run

```bash
python3 shared_system/run_shared_system.py \
  --build \
  --events /path/to/global_mem_events.jsonl \
  --out    /path/to/pmu_snapshots.jsonl \
  --snapshot-interval 100000
```

## In-Process Online Mode (experimental)

`eval_quota_cycles.py --shared-system` will spawn `llmsim_shared_system` as a
child process and stream events through its stdin while the main inference loop
runs. This currently uses a synchronous pipe write, so a slow simulator can
back-pressure the LLM forward path. Use the offline workflow above by default;
switch to this only when an online simulator feedback loop is actually needed.

## Default uarch profile

```text
config/uarch_profile_arch_A.json
```

Derived from `config/uarch_configs.yaml` `arch_A`.
