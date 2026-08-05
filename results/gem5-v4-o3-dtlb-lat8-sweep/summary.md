# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 3 | 42.184% / 58.964% / 65.549% / 65.549% | -42.184% | 42.184% |
| 8 | 3 | 40.520% / 56.289% / 61.408% / 61.408% | -37.944% | 40.520% |
| 16 | 3 | 38.584% / 47.915% / 49.683% / 49.683% | -26.481% | 38.584% |
| 32 | 3 | 37.167% / 38.848% / 38.875% / 38.875% | -11.250% | 37.166% |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 18.000M | 12.065 | 0 | 0 | 0 |
| 8 | 17.367M | 11.640 | 0 | 0 | 0 |
| 16 | 16.153M | 10.826 | 0 | 0 | 0 |
| 32 | 14.086M | 9.216 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 2 | 33.795% / 33.795% / 65.549% | -33.795% |
| 4 | mechanism | 1 | 2.041% / 2.041% / 2.041% | -2.041% |
| 4 | business_base | 1 | 65.549% / 65.549% / 65.549% | -65.549% |
| 4 | heldout | 1 | 58.964% / 58.964% / 58.964% | -58.964% |
| 8 | train_base | 2 | 32.636% / 32.636% / 61.408% | -28.772% |
| 8 | mechanism | 1 | 3.864% / 3.864% / 3.864% | 3.864% |
| 8 | business_base | 1 | 61.408% / 61.408% / 61.408% | -61.408% |
| 8 | heldout | 1 | 56.289% / 56.289% / 56.289% | -56.289% |
| 16 | train_base | 2 | 33.919% / 33.919% / 49.683% | -15.764% |
| 16 | mechanism | 1 | 18.155% / 18.155% / 18.155% | 18.155% |
| 16 | business_base | 1 | 49.683% / 49.683% / 49.683% | -49.683% |
| 16 | heldout | 1 | 47.915% / 47.915% / 47.915% | -47.915% |
| 32 | train_base | 2 | 38.862% / 38.862% / 38.875% | 0.014% |
| 32 | mechanism | 1 | 38.875% / 38.875% / 38.875% | 38.875% |
| 32 | business_base | 1 | 38.848% / 38.848% / 38.848% | -38.848% |
| 32 | heldout | 1 | 33.777% / 33.777% / 33.777% | -33.777% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 0.045% | 0.035% | -0.035% | W_v28_pytorch_heldout (43088) |
| 4 | private_l2_miss | 0.168% | 0.140% | -0.140% | W_v28_pytorch_heldout (42978) |
| 4 | cha_llc_lookup | 0.168% | 0.140% | -0.140% | W_v28_pytorch_heldout (42978) |
| 4 | branch_miss | 3.814% | 0.555% | -0.555% | W_v28_memory_random_mlp (61) |
| 4 | dtlb_access | 0.675% | 0.558% | -0.558% | W_v28_pytorch_heldout (173293) |
| 4 | dtlb_miss | 0.101% | 0.148% | -0.148% | W_v28_memory_random_mlp (115550) |
| 4 | llc_tag_vs_functional_path | 0.007% | 0.008% | 0.008% | W_v28_memory_random_mlp (91132) |
| 8 | l1d_miss | 0.050% | 0.038% | -0.038% | W_v28_pytorch_heldout (86173) |
| 8 | private_l2_miss | 0.190% | 0.173% | -0.173% | W_v28_pytorch_heldout (85931) |
| 8 | cha_llc_lookup | 0.189% | 0.172% | -0.172% | W_v28_pytorch_heldout (85930) |
| 8 | branch_miss | 3.644% | 0.498% | -0.498% | W_v28_memory_random_mlp (122) |
| 8 | dtlb_access | 0.692% | 0.573% | -0.573% | W_v28_pytorch_heldout (346611) |
| 8 | dtlb_miss | 0.100% | 0.146% | -0.146% | W_v28_memory_random_mlp (230892) |
| 8 | llc_tag_vs_functional_path | 0.008% | 0.009% | 0.009% | W_v28_memory_random_mlp (182085) |
| 16 | l1d_miss | 0.060% | 0.043% | -0.043% | W_v28_pytorch_base (196885) |
| 16 | private_l2_miss | 0.196% | 0.177% | -0.177% | W_v28_pytorch_heldout (171891) |
| 16 | cha_llc_lookup | 0.196% | 0.177% | -0.177% | W_v28_pytorch_heldout (171891) |
| 16 | branch_miss | 3.829% | 0.466% | -0.466% | W_v28_memory_random_mlp (245) |
| 16 | dtlb_access | 0.732% | 0.608% | -0.608% | W_v28_pytorch_heldout (693528) |
| 16 | dtlb_miss | 0.099% | 0.145% | -0.145% | W_v28_memory_random_mlp (461715) |
| 16 | llc_tag_vs_functional_path | 0.020% | 0.017% | 0.017% | W_v28_pytorch_base (159620) |
| 32 | l1d_miss | 0.078% | 0.054% | -0.054% | W_v28_pytorch_heldout (344858) |
| 32 | private_l2_miss | 0.200% | 0.177% | -0.177% | W_v28_pytorch_heldout (343783) |
| 32 | cha_llc_lookup | 0.200% | 0.177% | -0.177% | W_v28_pytorch_heldout (343783) |
| 32 | branch_miss | 3.759% | 0.408% | -0.408% | W_v28_memory_random_mlp (492) |
| 32 | dtlb_access | 0.797% | 0.661% | -0.661% | W_v28_pytorch_heldout (1389106) |
| 32 | dtlb_miss | 0.104% | 0.152% | -0.152% | W_v28_memory_random_mlp (923383) |
| 32 | llc_tag_vs_functional_path | 0.043% | 0.030% | 0.030% | W_v28_pytorch_heldout (280624) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 1185 | 7124.90 | 3335.84 | 20661 | 573.86 | 27.765% | 5233 | 2521 | 0/0 | 0 | 700 |
| 8 | 1344 | 12563.86 | 3100.02 | 40267 | 1011.93 | 27.755% | 10984 | 5428 | 0/0 | 0 | 1504 |
| 16 | 1707 | 19784.10 | 2620.38 | 80523 | 1593.47 | 27.751% | 26730 | 12853 | 0/0 | 0 | 3916 |
| 32 | 2306 | 29290.02 | 2438.72 | 197087 | 2359.11 | 27.752% | 57201 | 27570 | 0/0 | 0 | 12416 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
