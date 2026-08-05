# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / p99 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 32 | 1 | 7.448% / 7.448% / 7.448% / 7.448% / 7.448% | -7.448% | 7.447% |

## Acceptance gates

CPI P99 and throughput are gated independently for each core count.

| Cores | CPI P99 (<=10%) | Min UOP/s (>=5M) | Slowest workload | Pass |
|---:|---:|---:|---|:---:|
| 32 | 7.448% | 7.868M | W_v28_pytorch_base | PASS |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Min / P10 / median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 32 | 7.868M / 7.868M / 7.868M | 5.274 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 32 | train_base | 1 | 7.448% / 7.448% / 7.448% | -7.448% |
| 32 | business_base | 1 | 7.448% / 7.448% / 7.448% | -7.448% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 32 | l1d_miss | 0.101% | 0.101% | -0.101% | W_v28_pytorch_base (393856) |
| 32 | private_l2_miss | 0.231% | 0.231% | -0.231% | W_v28_pytorch_base (390287) |
| 32 | cha_llc_lookup | 0.231% | 0.231% | -0.231% | W_v28_pytorch_base (390287) |
| 32 | branch_miss | 2.298% | 2.298% | -2.298% | W_v28_pytorch_base (3046) |
| 32 | dtlb_access | 0.513% | 0.513% | -0.513% | W_v28_pytorch_base (1724232) |
| 32 | dtlb_miss | 99.399% | 99.399% | 99.399% | W_v28_pytorch_base (392696) |
| 32 | o3_iq_full | 232.248% | 232.248% | 232.248% | W_v28_pytorch_base (4311893) |
| 32 | llc_tag_vs_functional_path | 0.055% | 0.055% | 0.055% | W_v28_pytorch_base (266053) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 32 | 985 | 25843.46 | 1760.43 | 66732 | 1741.51 | 22.701% | 34073 | 14354 | 0/0 | 0 | 2093896 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
