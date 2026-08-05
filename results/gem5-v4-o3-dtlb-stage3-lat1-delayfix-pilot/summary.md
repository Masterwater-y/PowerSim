# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 4 | 17.518% / 1.219% / 67.526% / 67.526% | -16.466% | 17.518% |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 23.092M | 17.908 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 4 | 17.518% / 1.219% / 67.526% | -16.466% |
| 4 | mechanism | 3 | 0.849% / 0.335% / 2.104% | 0.554% |
| 4 | business_base | 1 | 67.526% / 67.526% / 67.526% | -67.526% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 22.541% | 0.047% | -0.047% | W_v28_int_alu_dense (42) |
| 4 | private_l2_miss | 34.283% | 0.174% | -0.174% | W_v28_int_alu_dense (73) |
| 4 | cha_llc_lookup | 34.283% | 0.173% | -0.173% | W_v28_int_alu_dense (73) |
| 4 | branch_miss | 7.622% | 5.097% | -3.691% | W_v28_int_alu_dense (64) |
| 4 | dtlb_access | 35.886% | 0.262% | -0.262% | W_v28_int_alu_dense (112) |
| 4 | dtlb_miss | 44.330% | 29.649% | 29.632% | W_v28_pytorch_base (49078) |
| 4 | llc_tag_vs_functional_path | 0.004% | 0.009% | 0.009% | W_v28_memory_random_mlp (91132) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 1417 | 8848.19 | 4008.28 | 57344 | 359.56 | 28.650% | 4120 | 1565 | 0/0 | 0 | 108708 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
