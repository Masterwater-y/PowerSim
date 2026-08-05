# TCSim v28.1 seed0 C4/C8 FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 23 | 33.858% / 29.947% / 81.088% / 94.128% | 4.570% | 33.858% |
| 8 | 23 | 30.703% / 22.956% / 74.240% / 84.756% | 0.111% | 30.722% |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 16 | 36.624% / 32.489% / 94.128% | 5.552% |
| 4 | mechanism | 9 | 46.461% / 43.585% / 94.111% | 5.558% |
| 4 | business_base | 7 | 23.978% / 4.619% / 94.128% | 5.545% |
| 4 | heldout | 7 | 27.535% / 22.061% / 81.088% | 2.323% |
| 8 | train_base | 16 | 32.483% / 26.440% / 84.756% | 0.664% |
| 8 | mechanism | 9 | 40.307% / 35.044% / 84.756% | -0.528% |
| 8 | business_base | 7 | 22.425% / 7.794% / 84.466% | 2.196% |
| 8 | heldout | 7 | 26.635% / 22.861% / 72.575% | -1.153% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 6.706% | 0.043% | -0.043% | W_v28_int_alu_dense (42) |
| 4 | private_l2_miss | 11.768% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | cha_llc_lookup | 11.767% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | branch_miss | 2.028% | 0.165% | -0.072% | W_v28_int_alu_dense (64) |
| 4 | llc_tag_vs_functional_path | 0.606% | 0.767% | 0.767% | W_v28_marine_heldout (45456) |
| 8 | l1d_miss | 7.257% | 0.048% | -0.048% | W_v28_int_alu_dense (85) |
| 8 | private_l2_miss | 11.862% | 0.266% | -0.266% | W_v28_int_alu_dense (147) |
| 8 | cha_llc_lookup | 11.860% | 0.265% | -0.265% | W_v28_int_alu_dense (147) |
| 8 | branch_miss | 2.007% | 0.170% | -0.060% | W_v28_int_alu_dense (128) |
| 8 | llc_tag_vs_functional_path | 0.610% | 0.769% | 0.769% | W_v28_marine_heldout (88796) |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
