# TCSim v28.1 seed0 C4/C8 FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 4 | 27.027% / 28.977% / 50.033% / 50.033% | -27.027% | 27.027% |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 4 | 27.027% / 28.977% / 50.033% | -27.027% |
| 4 | mechanism | 3 | 27.325% / 31.823% / 50.033% | -27.325% |
| 4 | business_base | 1 | 26.131% / 26.131% / 26.131% | -26.131% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 16.091% | 0.029% | -0.029% | W_v28_int_alu_dense (42) |
| 4 | private_l2_miss | 19.969% | 0.123% | -0.123% | W_v28_int_alu_dense (73) |
| 4 | cha_llc_lookup | 19.969% | 0.122% | -0.122% | W_v28_int_alu_dense (73) |
| 4 | branch_miss | 6.862% | 0.362% | -0.197% | W_v28_int_alu_dense (64) |
| 4 | llc_tag_vs_functional_path | 0.502% | 0.244% | 0.244% | W_v28_gofeed_base (30398) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | Memory events | Mean/step | Max batch | Reordered pairs | Same-line pairs | Zero-progress steps |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 37308 | 624388 | 16.74 | 176 | 734925 | 300 | 0 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
