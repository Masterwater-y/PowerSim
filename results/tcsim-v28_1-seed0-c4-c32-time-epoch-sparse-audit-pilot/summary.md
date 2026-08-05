# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 1 | 10.450% / 10.450% / 10.450% / 10.450% | 10.450% | 10.450% |
| 8 | 1 | 10.914% / 10.914% / 10.914% / 10.914% | 10.914% | 10.914% |
| 16 | 1 | 14.092% / 14.092% / 14.092% / 14.092% | 14.092% | 14.091% |
| 32 | 1 | 22.544% / 22.544% / 22.544% / 22.544% | 22.544% | 22.544% |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 1 | 10.450% / 10.450% / 10.450% | 10.450% |
| 4 | business_base | 1 | 10.450% / 10.450% / 10.450% | 10.450% |
| 8 | train_base | 1 | 10.914% / 10.914% / 10.914% | 10.914% |
| 8 | business_base | 1 | 10.914% / 10.914% / 10.914% | 10.914% |
| 16 | train_base | 1 | 14.092% / 14.092% / 14.092% | 14.092% |
| 16 | business_base | 1 | 14.092% / 14.092% / 14.092% | 14.092% |
| 32 | train_base | 1 | 22.544% / 22.544% / 22.544% | 22.544% |
| 32 | business_base | 1 | 22.544% / 22.544% / 22.544% | 22.544% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 0.048% | 0.048% | -0.048% | W_v28_gofeed_base (49644) |
| 4 | private_l2_miss | 0.276% | 0.276% | -0.276% | W_v28_gofeed_base (34036) |
| 4 | cha_llc_lookup | 0.276% | 0.276% | -0.276% | W_v28_gofeed_base (34036) |
| 4 | branch_miss | 0.085% | 0.085% | 0.085% | W_v28_gofeed_base (5896) |
| 4 | llc_tag_vs_functional_path | 1.997% | 1.997% | 1.997% | W_v28_gofeed_base (30398) |
| 8 | l1d_miss | 0.060% | 0.060% | -0.060% | W_v28_gofeed_base (99304) |
| 8 | private_l2_miss | 0.317% | 0.317% | -0.317% | W_v28_gofeed_base (68108) |
| 8 | cha_llc_lookup | 0.317% | 0.317% | -0.317% | W_v28_gofeed_base (68108) |
| 8 | branch_miss | 0.091% | 0.091% | 0.091% | W_v28_gofeed_base (12142) |
| 8 | llc_tag_vs_functional_path | 1.897% | 1.897% | 1.897% | W_v28_gofeed_base (54512) |
| 16 | l1d_miss | 0.065% | 0.065% | -0.065% | W_v28_gofeed_base (198623) |
| 16 | private_l2_miss | 0.343% | 0.343% | -0.343% | W_v28_gofeed_base (136273) |
| 16 | cha_llc_lookup | 0.343% | 0.343% | -0.343% | W_v28_gofeed_base (136273) |
| 16 | branch_miss | 0.074% | 0.074% | 0.074% | W_v28_gofeed_base (24353) |
| 16 | llc_tag_vs_functional_path | 1.766% | 1.766% | 1.766% | W_v28_gofeed_base (88567) |
| 32 | l1d_miss | 0.068% | 0.068% | -0.068% | W_v28_gofeed_base (397257) |
| 32 | private_l2_miss | 0.337% | 0.337% | -0.337% | W_v28_gofeed_base (272654) |
| 32 | cha_llc_lookup | 0.337% | 0.337% | -0.337% | W_v28_gofeed_base (272654) |
| 32 | branch_miss | 0.070% | 0.070% | 0.070% | W_v28_gofeed_base (48788) |
| 32 | llc_tag_vs_functional_path | 1.912% | 1.912% | 1.912% | W_v28_gofeed_base (126271) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | In-flight mem UOP | Horizon failures | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 1033 | 3168.03 | 1944.49 | 12806 | 191.91 | 4379 | 1682 | 4601 |
| 8 | 982 | 6665.58 | 1918.97 | 25599 | 403.75 | 8931 | 3406 | 8987 |
| 16 | 894 | 14643.45 | 1928.87 | 50918 | 886.98 | 17682 | 6778 | 17660 |
| 32 | 791 | 33100.87 | 1985.80 | 102182 | 2004.96 | 34648 | 13156 | 35528 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
