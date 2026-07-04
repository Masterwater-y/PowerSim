# Recent v17-v21 experiment state and results

Date: 2026-07-04

This file records the recent experiment state that is not represented by
tracked logs or checkpoints. Raw logs, tensor caches, and checkpoints remain
ignored by git because they are large.

## Checkpoint and log pointers

- v16 baseline:
  - checkpoint: `ckpt/v16_v9core_tail_local_delta_rank_8gpu_8000/step_005000`
  - c04/c16 eval log: `logs/eval_v16_tail_local_step5000_c04_c16_seedB_full.nohup.log`
  - c08 eval log: `logs/eval_v16_tail_local_step5000_c08_seedB_full.nohup.log`
- v17 baseline:
  - checkpoint: `ckpt/v17_bc_split_heads_nophase_8gpu_8000_resume500/step_008000`
  - c08 eval log: `logs/eval_v17_bc_split_step_008000_c08_seedB_full_ctx32768.nohup.log`
- local-core CPI-only direct:
  - checkpoint: `ckpt/local_core_cpi_only_direct_scratch_8gpu_12000`
  - c08 eval log: `logs/eval_cpi_only_best_c08_full_20260704_112757.nohup.log`
  - hidden-capacity log dir:
    `logs/hidden_capacity_W_ads_ranking_proxy_local_core_cpi_only_direct_scratch_8gpu_12000_20260704_110448`
- v21 local-core direct cycles-aux:
  - checkpoint: `ckpt/v21_local_core_direct_cycles_aux_scratch_8gpu_12000`
  - c08 eval log: `logs/eval_v21_direct_cycles_aux_best_c08_full_parallel_20260704_205458.nohup.log`
  - c04/c16/c32 driver log: `logs/eval_v21_c04_c16_c32_driver.nohup.log`

## Deployment CPI results

Mean `pred vs ROI` CPI error, full 17 workloads unless noted:

| version | c04 | c08 | c16 | c32 |
|---|---:|---:|---:|---:|
| v16 tail-local delta-rank | 5.61% | 6.33% | 10.85% | - |
| v17 split-head no-phase | - | 6.63% on 16 workloads | - | - |
| local-core CPI-only direct | - | 15.17% | - | - |
| v21 local-core direct cycles-aux | 8.33% | 9.35% | 13.81% | 31.96% partial, 15/17 done |

The v21 c32 run was not complete when recorded:

- completed 15/17 workloads
- missing `Summary`: `W_search_index_proxy`, `W_stream`
- partial worst errors: `W_phased_mix` 95.32%, `W_chase_dram` 84.61%,
  `W_false_sharing` 50.91%, `W_ads_ranking_proxy` 48.52%

## Main conclusions

- v16 remains the strongest deployed multi-core baseline among complete
  comparable runs.
- v17 is competitive on c08 but lacks c04/c16/c32 evidence in the local logs.
- local-core CPI-only direct converged in validation but performed poorly in
  deployment c08 full eval.
- v21 improved over local-core CPI-only direct on c08, but still did not beat
  v16/v17 and degraded sharply with higher core counts.
- Hidden-state probes showed useful information exists in local-core hidden
  states, but the local-core readout and closed-loop planner behavior were not
  reliable enough.
