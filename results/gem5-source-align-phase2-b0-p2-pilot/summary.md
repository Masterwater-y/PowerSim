# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / p99 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 3 | 20.020% / 22.086% / 25.321% / 26.049% / 26.130% | 12.124% | 20.020% |
| 32 | 3 | 50.066% / 24.623% / 99.430% / 116.262% / 118.132% | 45.103% | 54.614% |

## Acceptance gates

CPI P99 and throughput are gated independently for each core count.

| Cores | CPI P99 (<=10%) | Min UOP/s (>=5M) | Slowest workload | Pass |
|---:|---:|---:|---|:---:|
| 4 | 26.049% | 4.193M | W_v28_pytorch_base | FAIL |
| 32 | 116.262% | 2.902M | W_v28_pytorch_base | FAIL |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Min / P10 / median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 4.193M / 4.692M / 6.690M | 5.805 | 0 | 0 | 0 |
| 32 | 2.902M / 3.310M / 4.939M | 4.285 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 3 | 20.020% / 22.086% / 26.130% | 12.124% |
| 4 | mechanism | 2 | 16.965% / 16.965% / 22.086% | 5.121% |
| 4 | business_base | 1 | 26.130% / 26.130% / 26.130% | 26.130% |
| 32 | train_base | 3 | 50.066% / 24.623% / 118.132% | 45.103% |
| 32 | mechanism | 2 | 71.377% / 71.377% / 118.132% | 71.377% |
| 32 | business_base | 1 | 7.445% / 7.445% / 7.445% | -7.445% |

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
| 4 | o3_iq_full | 105.756% | 109.343% | 109.343% | W_v28_pytorch_base (765556) |
| 4 | llc_tag_vs_functional_path | 0.005% | 0.004% | 0.004% | W_v28_memory_random_mlp (91132) |
| 32 | l1d_miss | 0.044% | 0.029% | -0.029% | W_v28_pytorch_base (393856) |
| 32 | private_l2_miss | 0.147% | 0.126% | -0.126% | W_v28_pytorch_base (390287) |
| 32 | cha_llc_lookup | 0.147% | 0.126% | -0.126% | W_v28_pytorch_base (390287) |
| 32 | branch_miss | 5.969% | 3.634% | -3.634% | W_v28_memory_random_mlp (492) |
| 32 | dtlb_access | 0.275% | 0.269% | -0.269% | W_v28_pytorch_base (1724232) |
| 32 | dtlb_miss | 49.377% | 29.909% | 29.909% | W_v28_pytorch_base (392696) |
| 32 | o3_iq_full | 137.390% | 138.345% | 138.345% | W_v28_pytorch_base (4311893) |
| 32 | llc_tag_vs_functional_path | 0.021% | 0.010% | 0.009% | W_v28_pytorch_base (266053) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Shared replay | CA candidates/components | CA replay | CA stable/fallback | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 5750 | 1328.49 | 1382.84 | 11094 | 111.40 | 43.248% | 10059 | 5472 | 0/305 | 0 | 532/558 | 274338 | 227/305 | 27867 |
| 32 | 14244 | 4290.26 | 1367.06 | 73600 | 359.76 | 43.237% | 81256 | 44404 | 0/978 | 0 | 981/4772 | 3444359 | 3/978 | 2109578 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
