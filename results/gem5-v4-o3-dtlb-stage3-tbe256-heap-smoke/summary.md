# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 32 | 1 | 43.537% / 43.537% / 43.537% / 43.537% | 43.537% | 43.539% |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 32 | 7.989M | 6.932 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 32 | train_base | 1 | 43.537% / 43.537% / 43.537% | 43.537% |
| 32 | mechanism | 1 | 43.537% / 43.537% / 43.537% | 43.537% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 32 | l1d_miss | 0.017% | 0.017% | -0.017% | W_v28_memory_random_mlp (1099389) |
| 32 | private_l2_miss | 0.116% | 0.116% | -0.116% | W_v28_memory_random_mlp (778304) |
| 32 | cha_llc_lookup | 0.116% | 0.116% | -0.116% | W_v28_memory_random_mlp (778304) |
| 32 | branch_miss | 8.943% | 8.943% | -8.943% | W_v28_memory_random_mlp (492) |
| 32 | dtlb_access | 0.129% | 0.129% | -0.129% | W_v28_memory_random_mlp (2362945) |
| 32 | dtlb_miss | 0.149% | 0.149% | -0.149% | W_v28_memory_random_mlp (923383) |
| 32 | llc_tag_vs_functional_path | 0.006% | 0.006% | 0.006% | W_v28_memory_random_mlp (728474) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 32 | 1654 | 10778.41 | 2378.58 | 145152 | 1426.79 | 32.942% | 21335 | 7479 | 0/0 | 0 | 2993 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
