# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / p99 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 3 | 16.732% / 14.543% / 21.253% / 22.763% / 22.931% | -8.250% | 16.732% |
| 8 | 3 | 23.263% / 19.363% / 30.246% / 32.694% / 32.966% | -11.624% | 23.182% |
| 16 | 3 | 23.186% / 26.681% / 28.181% / 28.518% / 28.555% | -5.399% | 22.724% |
| 32 | 3 | 18.524% / 7.448% / 34.292% / 40.332% / 41.003% | 8.812% | 20.769% |

## Acceptance gates

CPI P99 and throughput are gated independently for each core count.

| Cores | CPI P99 (<=10%) | Min UOP/s (>=5M) | Slowest workload | Pass |
|---:|---:|---:|---|:---:|
| 4 | 22.763% | 7.103M | W_v28_memory_random_mlp | FAIL |
| 8 | 32.694% | 6.709M | W_v28_memory_random_mlp | FAIL |
| 16 | 28.518% | 6.024M | W_v28_memory_random_mlp | FAIL |
| 32 | 40.332% | 5.342M | W_v28_memory_random_mlp | FAIL |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Min / P10 / median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 7.103M / 7.518M / 9.180M | 6.163 | 0 | 0 | 0 |
| 8 | 6.709M / 7.151M / 8.921M | 5.979 | 0 | 0 | 0 |
| 16 | 6.024M / 6.472M / 8.266M | 5.644 | 0 | 0 | 0 |
| 32 | 5.342M / 5.624M / 6.751M | 5.208 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 3 | 16.732% / 14.543% / 22.931% | -8.250% |
| 4 | mechanism | 2 | 13.633% / 13.633% / 14.543% | -0.910% |
| 4 | business_base | 1 | 22.931% / 22.931% / 22.931% | -22.931% |
| 8 | train_base | 3 | 23.263% / 19.363% / 32.966% | -11.624% |
| 8 | mechanism | 2 | 25.212% / 25.212% / 32.966% | -7.754% |
| 8 | business_base | 1 | 19.363% / 19.363% / 19.363% | -19.363% |
| 16 | train_base | 3 | 23.186% / 26.681% / 28.555% | -5.399% |
| 16 | mechanism | 2 | 27.618% / 27.618% / 28.555% | -0.937% |
| 16 | business_base | 1 | 14.323% / 14.323% / 14.323% | -14.323% |
| 32 | train_base | 3 | 18.524% / 7.448% / 41.003% | 8.812% |
| 32 | mechanism | 2 | 24.062% / 24.062% / 41.003% | 16.941% |
| 32 | business_base | 1 | 7.448% / 7.448% / 7.448% | -7.448% |

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
| 4 | o3_iq_full | 103.251% | 106.141% | 106.141% | W_v28_pytorch_base (765556) |
| 4 | llc_tag_vs_functional_path | 0.005% | 0.004% | 0.004% | W_v28_memory_random_mlp (91132) |
| 8 | l1d_miss | 0.031% | 0.023% | -0.023% | W_v28_pytorch_base (98419) |
| 8 | private_l2_miss | 0.141% | 0.123% | -0.123% | W_v28_pytorch_base (97544) |
| 8 | cha_llc_lookup | 0.141% | 0.123% | -0.123% | W_v28_pytorch_base (97543) |
| 8 | branch_miss | 5.827% | 3.777% | -3.777% | W_v28_memory_random_mlp (122) |
| 8 | dtlb_access | 0.229% | 0.225% | -0.225% | W_v28_pytorch_base (430523) |
| 8 | dtlb_miss | 49.422% | 29.912% | 29.912% | W_v28_pytorch_base (98185) |
| 8 | o3_iq_full | 112.352% | 116.405% | 116.405% | W_v28_pytorch_base (1410217) |
| 8 | llc_tag_vs_functional_path | 0.005% | 0.004% | 0.004% | W_v28_memory_random_mlp (182085) |
| 16 | l1d_miss | 0.039% | 0.026% | -0.026% | W_v28_pytorch_base (196885) |
| 16 | private_l2_miss | 0.145% | 0.126% | -0.126% | W_v28_pytorch_base (195086) |
| 16 | cha_llc_lookup | 0.145% | 0.126% | -0.126% | W_v28_pytorch_base (195085) |
| 16 | branch_miss | 6.037% | 4.016% | -4.016% | W_v28_memory_random_mlp (245) |
| 16 | dtlb_access | 0.257% | 0.251% | -0.251% | W_v28_pytorch_base (861713) |
| 16 | dtlb_miss | 49.407% | 29.917% | 29.917% | W_v28_pytorch_base (196355) |
| 16 | o3_iq_full | 123.999% | 128.066% | 128.066% | W_v28_pytorch_base (2485022) |
| 16 | llc_tag_vs_functional_path | 0.013% | 0.008% | 0.008% | W_v28_pytorch_base (159620) |
| 32 | l1d_miss | 0.044% | 0.029% | -0.029% | W_v28_pytorch_base (393856) |
| 32 | private_l2_miss | 0.147% | 0.126% | -0.126% | W_v28_pytorch_base (390287) |
| 32 | cha_llc_lookup | 0.147% | 0.126% | -0.126% | W_v28_pytorch_base (390287) |
| 32 | branch_miss | 5.969% | 3.634% | -3.634% | W_v28_memory_random_mlp (492) |
| 32 | dtlb_access | 0.275% | 0.269% | -0.269% | W_v28_pytorch_base (1724232) |
| 32 | dtlb_miss | 49.377% | 29.909% | 29.909% | W_v28_pytorch_base (392696) |
| 32 | o3_iq_full | 138.225% | 139.237% | 139.237% | W_v28_pytorch_base (4311893) |
| 32 | llc_tag_vs_functional_path | 0.021% | 0.010% | 0.010% | W_v28_pytorch_base (266053) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 4884 | 1564.05 | 1382.59 | 12429 | 131.15 | 43.248% | 9679 | 5473 | 0/0 | 0 | 33619 |
| 8 | 5357 | 2851.90 | 1367.74 | 22714 | 239.15 | 43.240% | 20035 | 11076 | 0/0 | 0 | 140378 |
| 16 | 6187 | 4938.62 | 1366.76 | 36754 | 414.13 | 43.235% | 40590 | 22217 | 0/0 | 0 | 536488 |
| 32 | 7790 | 7844.74 | 1361.37 | 73600 | 657.82 | 43.237% | 81918 | 44597 | 0/0 | 0 | 2108393 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
