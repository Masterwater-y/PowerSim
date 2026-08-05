# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / p99 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 32 | 1 | 41.003% / 41.003% / 41.003% / 41.003% / 41.003% | 41.003% | 41.004% |

## Acceptance gates

CPI P99 and throughput are gated independently for each core count.

| Cores | CPI P99 (<=10%) | Min UOP/s (>=5M) | Slowest workload | Pass |
|---:|---:|---:|---|:---:|
| 32 | 41.003% | 5.286M | W_v28_memory_random_mlp | FAIL |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Min / P10 / median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 32 | 5.286M / 5.286M / 5.286M | 4.587 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 32 | train_base | 1 | 41.003% / 41.003% / 41.003% | 41.003% |
| 32 | mechanism | 1 | 41.003% / 41.003% / 41.003% | 41.003% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 32 | l1d_miss | 0.017% | 0.017% | -0.017% | W_v28_memory_random_mlp (1099389) |
| 32 | private_l2_miss | 0.116% | 0.116% | -0.116% | W_v28_memory_random_mlp (778304) |
| 32 | cha_llc_lookup | 0.116% | 0.116% | -0.116% | W_v28_memory_random_mlp (778304) |
| 32 | branch_miss | 8.943% | 8.943% | -8.943% | W_v28_memory_random_mlp (492) |
| 32 | dtlb_access | 0.129% | 0.129% | -0.129% | W_v28_memory_random_mlp (2362945) |
| 32 | dtlb_miss | 0.015% | 0.015% | 0.015% | W_v28_memory_random_mlp (923383) |
| 32 | o3_iq_full | 97.624% | 97.624% | 97.624% | W_v28_memory_random_mlp (4468702) |
| 32 | llc_tag_vs_functional_path | 0.007% | 0.007% | 0.007% | W_v28_memory_random_mlp (728474) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 32 | 3205 | 5562.40 | 1162.99 | 69070 | 736.32 | 32.942% | 38999 | 15296 | 0/0 | 0 | 13154 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
