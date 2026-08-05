# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 3 | 39.874% / 55.965% / 59.389% / 59.389% | -37.028% | 39.874% |
| 8 | 3 | 39.070% / 53.610% / 56.296% / 56.296% | -34.201% | 39.070% |
| 16 | 3 | 36.510% / 45.731% / 46.883% / 46.883% | -25.233% | 36.510% |
| 32 | 3 | 31.881% / 31.418% / 33.327% / 33.327% | -10.935% | 31.881% |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 18.415M | 12.343 | 0 | 0 | 0 |
| 8 | 17.705M | 11.867 | 0 | 0 | 0 |
| 16 | 16.280M | 10.912 | 0 | 0 | 0 |
| 32 | 14.086M | 9.158 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 2 | 31.829% / 31.829% / 59.389% | -27.560% |
| 4 | mechanism | 1 | 4.269% / 4.269% / 4.269% | 4.269% |
| 4 | business_base | 1 | 59.389% / 59.389% / 59.389% | -59.389% |
| 4 | heldout | 1 | 55.965% / 55.965% / 55.965% | -55.965% |
| 8 | train_base | 2 | 31.800% / 31.800% / 56.296% | -24.496% |
| 8 | mechanism | 1 | 7.303% / 7.303% / 7.303% | 7.303% |
| 8 | business_base | 1 | 56.296% / 56.296% / 56.296% | -56.296% |
| 8 | heldout | 1 | 53.610% / 53.610% / 53.610% | -53.610% |
| 16 | train_base | 2 | 31.899% / 31.899% / 46.883% | -14.984% |
| 16 | mechanism | 1 | 16.915% / 16.915% / 16.915% | 16.915% |
| 16 | business_base | 1 | 46.883% / 46.883% / 46.883% | -46.883% |
| 16 | heldout | 1 | 45.731% / 45.731% / 45.731% | -45.731% |
| 32 | train_base | 2 | 32.372% / 32.372% / 33.327% | -0.954% |
| 32 | mechanism | 1 | 31.418% / 31.418% / 31.418% | 31.418% |
| 32 | business_base | 1 | 33.327% / 33.327% / 33.327% | -33.327% |
| 32 | heldout | 1 | 30.897% / 30.897% / 30.897% | -30.897% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 0.045% | 0.035% | -0.035% | W_v28_pytorch_heldout (43088) |
| 4 | private_l2_miss | 0.168% | 0.140% | -0.140% | W_v28_pytorch_heldout (42978) |
| 4 | cha_llc_lookup | 0.168% | 0.140% | -0.140% | W_v28_pytorch_heldout (42978) |
| 4 | branch_miss | 3.814% | 0.555% | -0.555% | W_v28_memory_random_mlp (61) |
| 4 | dtlb_access | 0.675% | 0.558% | -0.558% | W_v28_pytorch_heldout (173293) |
| 4 | dtlb_miss | 0.117% | 0.172% | -0.172% | W_v28_memory_random_mlp (115550) |
| 4 | llc_tag_vs_functional_path | 0.007% | 0.008% | 0.008% | W_v28_memory_random_mlp (91132) |
| 8 | l1d_miss | 0.050% | 0.038% | -0.038% | W_v28_pytorch_heldout (86173) |
| 8 | private_l2_miss | 0.190% | 0.173% | -0.173% | W_v28_pytorch_heldout (85931) |
| 8 | cha_llc_lookup | 0.189% | 0.172% | -0.172% | W_v28_pytorch_heldout (85930) |
| 8 | branch_miss | 3.644% | 0.498% | -0.498% | W_v28_memory_random_mlp (122) |
| 8 | dtlb_access | 0.692% | 0.573% | -0.573% | W_v28_pytorch_heldout (346611) |
| 8 | dtlb_miss | 0.118% | 0.175% | -0.175% | W_v28_memory_random_mlp (230892) |
| 8 | llc_tag_vs_functional_path | 0.008% | 0.009% | 0.009% | W_v28_memory_random_mlp (182085) |
| 16 | l1d_miss | 0.060% | 0.043% | -0.043% | W_v28_pytorch_base (196885) |
| 16 | private_l2_miss | 0.196% | 0.176% | -0.176% | W_v28_pytorch_heldout (171891) |
| 16 | cha_llc_lookup | 0.196% | 0.176% | -0.176% | W_v28_pytorch_heldout (171891) |
| 16 | branch_miss | 3.829% | 0.466% | -0.466% | W_v28_memory_random_mlp (245) |
| 16 | dtlb_access | 0.732% | 0.608% | -0.608% | W_v28_pytorch_heldout (693528) |
| 16 | dtlb_miss | 0.118% | 0.174% | -0.174% | W_v28_memory_random_mlp (461715) |
| 16 | llc_tag_vs_functional_path | 0.020% | 0.017% | 0.017% | W_v28_pytorch_base (159620) |
| 32 | l1d_miss | 0.078% | 0.054% | -0.054% | W_v28_pytorch_heldout (344858) |
| 32 | private_l2_miss | 0.200% | 0.177% | -0.177% | W_v28_pytorch_heldout (343783) |
| 32 | cha_llc_lookup | 0.200% | 0.177% | -0.177% | W_v28_pytorch_heldout (343783) |
| 32 | branch_miss | 3.759% | 0.408% | -0.408% | W_v28_memory_random_mlp (492) |
| 32 | dtlb_access | 0.797% | 0.661% | -0.661% | W_v28_pytorch_heldout (1389106) |
| 32 | dtlb_miss | 0.123% | 0.182% | -0.182% | W_v28_memory_random_mlp (923383) |
| 32 | llc_tag_vs_functional_path | 0.043% | 0.030% | 0.030% | W_v28_pytorch_heldout (280624) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 1280 | 6596.10 | 2530.88 | 17458 | 531.27 | 27.765% | 5235 | 3317 | 0/0 | 0 | 664 |
| 8 | 1409 | 11984.27 | 2413.99 | 34413 | 965.25 | 27.755% | 10846 | 6965 | 0/0 | 0 | 1419 |
| 16 | 1710 | 19749.39 | 2128.54 | 68746 | 1590.68 | 27.751% | 24256 | 15784 | 0/0 | 0 | 3607 |
| 32 | 2257 | 29925.91 | 1895.94 | 163730 | 2410.32 | 27.752% | 54795 | 35438 | 0/0 | 0 | 11574 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
