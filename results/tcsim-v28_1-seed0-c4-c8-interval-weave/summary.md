# TCSim v28.1 seed0 C4/C8 FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 23 | 20.219% / 21.788% / 31.128% / 50.033% | -17.784% | 20.223% |
| 8 | 23 | 20.042% / 21.556% / 29.811% / 62.067% | -16.672% | 20.037% |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 16 | 18.582% / 19.222% / 50.033% | -15.081% |
| 4 | mechanism | 9 | 14.793% / 11.901% / 50.033% | -8.569% |
| 4 | business_base | 7 | 23.453% / 25.730% / 28.383% | -23.453% |
| 4 | heldout | 7 | 23.961% / 24.805% / 31.128% | -23.961% |
| 8 | train_base | 16 | 18.752% / 19.786% / 62.067% | -13.907% |
| 8 | mechanism | 9 | 17.190% / 16.094% / 62.067% | -8.576% |
| 8 | business_base | 7 | 20.762% / 23.293% / 28.765% | -20.762% |
| 8 | heldout | 7 | 22.991% / 23.988% / 31.775% | -22.991% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 6.706% | 0.043% | -0.043% | W_v28_int_alu_dense (42) |
| 4 | private_l2_miss | 11.768% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | cha_llc_lookup | 11.767% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | branch_miss | 2.028% | 0.165% | -0.072% | W_v28_int_alu_dense (64) |
| 4 | llc_tag_vs_functional_path | 0.606% | 0.767% | 0.767% | W_v28_marine_heldout (45456) |
| 8 | l1d_miss | 7.491% | 0.048% | -0.048% | W_v28_int_alu_dense (85) |
| 8 | private_l2_miss | 12.009% | 0.266% | -0.266% | W_v28_int_alu_dense (147) |
| 8 | cha_llc_lookup | 12.006% | 0.265% | -0.265% | W_v28_int_alu_dense (147) |
| 8 | branch_miss | 2.007% | 0.170% | -0.060% | W_v28_int_alu_dense (128) |
| 8 | llc_tag_vs_functional_path | 0.610% | 0.769% | 0.769% | W_v28_marine_heldout (88796) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | Memory events | Mean/step | Max batch | Reordered pairs | Same-line pairs | Zero-progress steps |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 273888 | 3546652 | 12.95 | 184 | 2141576 | 99558 | 0 |
| 8 | 546633 | 7093255 | 12.98 | 368 | 3576206 | 108800 | 0 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
