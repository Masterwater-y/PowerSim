# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 3 | 19.708% / 20.090% / 24.968% / 24.968% | -10.331% | 19.708% |
| 32 | 3 | 22.924% / 4.265% / 63.165% / 63.165% | 19.186% | 27.764% |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 8.379M | 5.803 | 0 | 0 | 0 |
| 32 | 7.063M | 5.007 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 3 | 19.708% / 20.090% / 24.968% | -10.331% |
| 4 | mechanism | 2 | 17.078% / 17.078% / 20.090% | -3.013% |
| 4 | business_base | 1 | 24.968% / 24.968% / 24.968% | -24.968% |
| 32 | train_base | 3 | 22.924% / 4.265% / 63.165% | 19.186% |
| 32 | mechanism | 2 | 32.254% / 32.254% / 63.165% | 30.911% |
| 32 | business_base | 1 | 4.265% / 4.265% / 4.265% | -4.265% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 0.028% | 0.021% | -0.021% | W_v28_pytorch_base (49207) |
| 4 | private_l2_miss | 0.117% | 0.097% | -0.097% | W_v28_pytorch_base (48762) |
| 4 | cha_llc_lookup | 0.117% | 0.097% | -0.097% | W_v28_pytorch_base (48762) |
| 4 | branch_miss | 5.996% | 4.158% | -4.158% | W_v28_memory_random_mlp (61) |
| 4 | dtlb_access | 0.215% | 0.210% | -0.210% | W_v28_pytorch_base (215189) |
| 4 | dtlb_miss | 49.478% | 29.883% | 29.883% | W_v28_pytorch_base (49078) |
| 4 | llc_tag_vs_functional_path | 0.005% | 0.004% | 0.004% | W_v28_memory_random_mlp (91132) |
| 32 | l1d_miss | 0.044% | 0.029% | -0.029% | W_v28_pytorch_base (393856) |
| 32 | private_l2_miss | 0.147% | 0.126% | -0.126% | W_v28_pytorch_base (390287) |
| 32 | cha_llc_lookup | 0.147% | 0.126% | -0.126% | W_v28_pytorch_base (390287) |
| 32 | branch_miss | 5.969% | 3.634% | -3.634% | W_v28_memory_random_mlp (492) |
| 32 | dtlb_access | 0.275% | 0.269% | -0.269% | W_v28_pytorch_base (1724232) |
| 32 | dtlb_miss | 49.377% | 29.909% | 29.909% | W_v28_pytorch_base (392696) |
| 32 | llc_tag_vs_functional_path | 0.021% | 0.010% | 0.010% | W_v28_pytorch_base (266053) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 2362 | 3234.05 | 2731.07 | 23866 | 271.19 | 43.248% | 5146 | 2793 | 0/0 | 0 | 65082 |
| 32 | 4340 | 14080.77 | 2716.87 | 178286 | 1180.74 | 43.237% | 40782 | 22398 | 0/0 | 0 | 3875128 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
