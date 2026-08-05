# TCSim v28.1 seed0 C4/C8 FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 23 | 11.853% / 12.107% / 19.098% / 30.733% | 8.949% | 11.853% |
| 8 | 23 | 14.990% / 13.984% / 33.666% / 47.376% | 10.531% | 14.980% |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 16 | 10.961% / 10.756% / 30.733% | 6.787% |
| 4 | mechanism | 9 | 9.223% / 10.976% / 16.802% | 1.801% |
| 4 | business_base | 7 | 13.196% / 10.537% / 30.733% | 13.196% |
| 4 | heldout | 7 | 13.892% / 13.480% / 23.472% | 13.892% |
| 8 | train_base | 16 | 14.649% / 13.722% / 47.376% | 8.239% |
| 8 | mechanism | 9 | 12.838% / 16.103% / 33.942% | 1.442% |
| 8 | business_base | 7 | 16.978% / 13.192% / 47.376% | 16.978% |
| 8 | heldout | 7 | 15.770% / 13.984% / 33.666% | 15.770% |

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
| 8 | private_l2_miss | 12.008% | 0.266% | -0.266% | W_v28_int_alu_dense (147) |
| 8 | cha_llc_lookup | 12.006% | 0.265% | -0.265% | W_v28_int_alu_dense (147) |
| 8 | branch_miss | 2.007% | 0.170% | -0.060% | W_v28_int_alu_dense (128) |
| 8 | llc_tag_vs_functional_path | 0.610% | 0.769% | 0.769% | W_v28_marine_heldout (88796) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | Memory events | Mean/step | Max batch | Reordered pairs | Same-line pairs | Zero-progress steps |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 276481 | 3546652 | 12.83 | 184 | 2120007 | 127426 | 4 |
| 8 | 557462 | 7093255 | 12.72 | 368 | 4085651 | 153761 | 5 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
