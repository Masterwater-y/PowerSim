# TCSim v28.1 seed0 C4/C8 FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 23 | 23.682% / 16.687% / 74.059% / 82.811% | 12.090% | 23.682% |
| 8 | 23 | 20.180% / 16.699% / 65.854% / 74.401% | 7.665% | 20.198% |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 16 | 23.108% / 12.565% / 82.811% | 14.722% |
| 4 | mechanism | 9 | 24.736% / 16.156% / 82.811% | 20.327% |
| 4 | business_base | 7 | 21.015% / 8.974% / 79.167% | 7.515% |
| 4 | heldout | 7 | 24.994% / 21.327% / 74.059% | 6.075% |
| 8 | train_base | 16 | 18.490% / 11.080% / 74.401% | 9.903% |
| 8 | mechanism | 9 | 18.838% / 16.144% / 74.401% | 14.345% |
| 8 | business_base | 7 | 18.043% / 6.015% / 69.963% | 4.193% |
| 8 | heldout | 7 | 24.043% / 19.210% / 65.854% | 2.548% |

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
