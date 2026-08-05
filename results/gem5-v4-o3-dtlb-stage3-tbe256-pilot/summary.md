# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 4 | 8.204% / 5.330% / 19.872% / 19.872% | -3.142% | 8.203% |
| 8 | 4 | 12.686% / 6.049% / 34.279% / 34.279% | -4.453% | 12.625% |
| 16 | 4 | 18.479% / 19.636% / 26.659% / 26.659% | 5.149% | 18.134% |
| 32 | 4 | 18.371% / 14.514% / 43.537% / 43.537% | 17.912% | 22.065% |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 17.405M | 14.157 | 0 | 0 | 0 |
| 8 | 17.609M | 14.304 | 0 | 0 | 0 |
| 16 | 15.780M | 12.828 | 0 | 0 | 0 |
| 32 | 13.260M | 10.797 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 4 | 8.204% / 5.330% / 19.872% | -3.142% |
| 4 | mechanism | 4 | 8.204% / 5.330% / 19.872% | -3.142% |
| 8 | train_base | 4 | 12.686% / 6.049% / 34.279% | -4.453% |
| 8 | mechanism | 4 | 12.686% / 6.049% / 34.279% | -4.453% |
| 16 | train_base | 4 | 18.479% / 19.636% / 26.659% | 5.149% |
| 16 | mechanism | 4 | 18.479% / 19.636% / 26.659% | 5.149% |
| 32 | train_base | 4 | 18.371% / 14.514% / 43.537% | 17.912% |
| 32 | mechanism | 4 | 18.371% / 14.514% / 43.537% | 17.912% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 0.225% | 0.018% | -0.018% | W_v28_cache_L1_mixed (1056) |
| 4 | private_l2_miss | 1.118% | 0.108% | -0.108% | W_v28_cache_L1_mixed (1088) |
| 4 | cha_llc_lookup | 1.118% | 0.108% | -0.108% | W_v28_cache_L1_mixed (1088) |
| 4 | branch_miss | 4.914% | 2.646% | -2.646% | W_v28_memory_random_mlp (61) |
| 4 | dtlb_access | 0.159% | 0.146% | -0.146% | W_v28_cache_L1_mixed (67750) |
| 4 | dtlb_miss | 2.628% | 0.149% | -0.149% | W_v28_cache_L1_mixed (38) |
| 4 | llc_tag_vs_functional_path | 0.003% | 0.004% | 0.004% | W_v28_memory_random_mlp (91132) |
| 8 | l1d_miss | 0.214% | 0.019% | -0.019% | W_v28_cache_L1_mixed (2112) |
| 8 | private_l2_miss | 1.169% | 0.139% | -0.139% | W_v28_cache_L1_mixed (2179) |
| 8 | cha_llc_lookup | 1.156% | 0.139% | -0.139% | W_v28_cache_L1_mixed (2178) |
| 8 | branch_miss | 4.914% | 2.646% | -2.646% | W_v28_memory_random_mlp (122) |
| 8 | dtlb_access | 0.164% | 0.151% | -0.151% | W_v28_cache_L2_mixed (135510) |
| 8 | dtlb_miss | 2.402% | 0.152% | -0.152% | W_v28_cache_L1_mixed (76) |
| 8 | llc_tag_vs_functional_path | 0.002% | 0.004% | 0.004% | W_v28_memory_random_mlp (182085) |
| 16 | l1d_miss | 0.230% | 0.018% | -0.018% | W_v28_cache_L1_mixed (4226) |
| 16 | private_l2_miss | 1.182% | 0.140% | -0.140% | W_v28_cache_L1_mixed (4361) |
| 16 | cha_llc_lookup | 1.176% | 0.140% | -0.140% | W_v28_cache_L1_mixed (4360) |
| 16 | branch_miss | 5.008% | 2.655% | -2.655% | W_v28_memory_random_mlp (245) |
| 16 | dtlb_access | 0.164% | 0.151% | -0.151% | W_v28_memory_seq_moderate (525493) |
| 16 | dtlb_miss | 2.567% | 0.148% | -0.148% | W_v28_cache_L1_mixed (155) |
| 16 | llc_tag_vs_functional_path | 0.002% | 0.003% | 0.003% | W_v28_memory_random_mlp (364103) |
| 32 | l1d_miss | 0.234% | 0.019% | -0.019% | W_v28_cache_L1_mixed (8452) |
| 32 | private_l2_miss | 1.157% | 0.139% | -0.139% | W_v28_cache_L1_mixed (8707) |
| 32 | cha_llc_lookup | 1.156% | 0.139% | -0.139% | W_v28_cache_L1_mixed (8707) |
| 32 | branch_miss | 5.101% | 2.665% | -2.665% | W_v28_memory_random_mlp (492) |
| 32 | dtlb_access | 0.167% | 0.153% | -0.153% | W_v28_cache_L2_mixed (542053) |
| 32 | dtlb_miss | 2.753% | 0.163% | -0.163% | W_v28_cache_L1_mixed (311) |
| 32 | llc_tag_vs_functional_path | 0.002% | 0.003% | 0.003% | W_v28_memory_random_mlp (728474) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 2640 | 4272.38 | 2637.15 | 18460 | 212.65 | 42.355% | 3360 | 3257 | 0/0 | 0 | 233 |
| 8 | 2922 | 7720.10 | 2631.30 | 36918 | 384.25 | 42.344% | 6862 | 6538 | 0/0 | 0 | 620 |
| 16 | 3491 | 12923.60 | 2636.84 | 73834 | 643.24 | 42.339% | 13807 | 13268 | 0/0 | 0 | 1985 |
| 32 | 4509 | 20011.66 | 2627.09 | 147666 | 996.04 | 42.339% | 29054 | 26901 | 0/0 | 0 | 7390 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
