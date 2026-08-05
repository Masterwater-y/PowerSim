# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 2 | 19.517% / 19.517% / 24.968% / 24.968% | -5.452% | 19.517% |
| 32 | 2 | 33.715% / 33.715% / 63.165% / 63.165% | 29.450% | 33.716% |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 7.148M | 5.423 | 0 | 0 | 0 |
| 32 | 6.083M | 4.577 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 2 | 19.517% / 19.517% / 24.968% | -5.452% |
| 4 | mechanism | 1 | 14.065% / 14.065% / 14.065% | 14.065% |
| 4 | business_base | 1 | 24.968% / 24.968% / 24.968% | -24.968% |
| 32 | train_base | 2 | 33.715% / 33.715% / 63.165% | 29.450% |
| 32 | mechanism | 1 | 63.165% / 63.165% / 63.165% | 63.165% |
| 32 | business_base | 1 | 4.265% / 4.265% / 4.265% | -4.265% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 0.036% | 0.028% | -0.028% | W_v28_pytorch_base (49207) |
| 4 | private_l2_miss | 0.136% | 0.113% | -0.113% | W_v28_pytorch_base (48762) |
| 4 | cha_llc_lookup | 0.135% | 0.112% | -0.112% | W_v28_pytorch_base (48762) |
| 4 | branch_miss | 5.661% | 3.820% | -3.820% | W_v28_memory_random_mlp (61) |
| 4 | dtlb_access | 0.241% | 0.223% | -0.223% | W_v28_pytorch_base (215189) |
| 4 | dtlb_miss | 49.708% | 29.641% | 29.641% | W_v28_pytorch_base (49078) |
| 4 | llc_tag_vs_functional_path | 0.008% | 0.009% | 0.009% | W_v28_memory_random_mlp (91132) |
| 32 | l1d_miss | 0.059% | 0.039% | -0.039% | W_v28_pytorch_base (393856) |
| 32 | private_l2_miss | 0.173% | 0.154% | -0.154% | W_v28_pytorch_base (390287) |
| 32 | cha_llc_lookup | 0.173% | 0.154% | -0.154% | W_v28_pytorch_base (390287) |
| 32 | branch_miss | 5.621% | 3.222% | -3.222% | W_v28_memory_random_mlp (492) |
| 32 | dtlb_access | 0.321% | 0.291% | -0.291% | W_v28_pytorch_base (1724232) |
| 32 | dtlb_miss | 49.707% | 29.669% | 29.669% | W_v28_pytorch_base (392696) |
| 32 | llc_tag_vs_functional_path | 0.031% | 0.020% | 0.020% | W_v28_pytorch_base (266053) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 1325 | 4083.33 | 2915.09 | 23866 | 384.46 | 28.646% | 4565 | 1852 | 0/0 | 0 | 65051 |
| 32 | 2388 | 18125.33 | 2900.25 | 178286 | 1706.57 | 28.631% | 36473 | 14870 | 0/0 | 0 | 3873781 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
