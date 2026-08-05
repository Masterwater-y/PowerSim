# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / p99 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 3 | 16.746% / 14.065% / 22.788% / 24.750% / 24.968% | 0.100% | 16.746% |
| 32 | 3 | 25.570% / 9.279% / 52.388% / 62.087% / 63.165% | 22.726% | 25.570% |

## Acceptance gates

CPI P99 and throughput are gated independently for each core count.

| Cores | CPI P99 (<=10%) | Min UOP/s (>=5M) | Slowest workload | Pass |
|---:|---:|---:|---|:---:|
| 4 | 24.750% | 4.877M | W_v28_memory_random_mlp | FAIL |
| 32 | 62.087% | 4.864M | W_v28_memory_random_mlp | FAIL |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Min / P10 / median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 4.877M / 5.408M / 7.533M | 5.049 | 0 | 0 | 0 |
| 32 | 4.864M / 5.419M / 7.635M | 5.118 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 3 | 16.746% / 14.065% / 24.968% | 0.100% |
| 4 | mechanism | 2 | 12.634% / 12.634% / 14.065% | 12.634% |
| 4 | business_base | 1 | 24.968% / 24.968% / 24.968% | -24.968% |
| 32 | train_base | 3 | 25.570% / 9.279% / 63.165% | 22.726% |
| 32 | mechanism | 2 | 36.222% / 36.222% / 63.165% | 36.222% |
| 32 | business_base | 1 | 4.265% / 4.265% / 4.265% | -4.265% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 0.028% | 0.024% | -0.024% | W_v28_pytorch_base (49207) |
| 4 | private_l2_miss | 0.244% | 0.132% | -0.132% | W_v28_coh_readmostly_sparse (8257) |
| 4 | cha_llc_lookup | 0.244% | 0.131% | -0.131% | W_v28_coh_readmostly_sparse (8257) |
| 4 | branch_miss | 4.573% | 2.790% | -2.790% | W_v28_memory_random_mlp (61) |
| 4 | dtlb_access | 0.195% | 0.209% | -0.209% | W_v28_pytorch_base (215189) |
| 4 | dtlb_miss | 34.046% | 29.617% | 29.617% | W_v28_pytorch_base (49078) |
| 4 | o3_iq_full | 96.886% | 88.683% | 88.683% | W_v28_pytorch_base (765556) |
| 4 | llc_tag_vs_functional_path | 0.005% | 0.009% | 0.009% | W_v28_memory_random_mlp (91132) |
| 32 | l1d_miss | 0.045% | 0.033% | -0.033% | W_v28_pytorch_base (393856) |
| 32 | private_l2_miss | 0.300% | 0.176% | -0.176% | W_v28_coh_readmostly_sparse (66122) |
| 32 | cha_llc_lookup | 0.300% | 0.176% | -0.176% | W_v28_coh_readmostly_sparse (66122) |
| 32 | branch_miss | 4.297% | 2.084% | -2.084% | W_v28_memory_random_mlp (492) |
| 32 | dtlb_access | 0.254% | 0.271% | -0.271% | W_v28_pytorch_base (1724232) |
| 32 | dtlb_miss | 33.359% | 29.643% | 29.643% | W_v28_pytorch_base (392696) |
| 32 | o3_iq_full | 135.673% | 112.498% | 112.498% | W_v28_pytorch_base (4311893) |
| 32 | llc_tag_vs_functional_path | 0.021% | 0.020% | 0.020% | W_v28_pytorch_base (266053) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 1607 | 5325.04 | 2902.76 | 23866 | 358.44 | 26.761% | 6662 | 2940 | 1/0 | 0 | 65693 |
| 32 | 2671 | 25630.36 | 2881.62 | 178286 | 1725.26 | 26.748% | 53426 | 23675 | 6/0 | 0 | 3945664 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
