# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 6 | 22.050% / 18.949% / 22.597% / 65.591% | -13.761% | 22.050% |
| 8 | 6 | 23.217% / 19.684% / 33.786% / 56.381% | -12.435% | 23.183% |
| 16 | 6 | 22.115% / 21.206% / 26.440% / 39.275% | -5.388% | 21.871% |
| 32 | 6 | 20.085% / 19.685% / 23.748% / 44.175% | 6.204% | 22.543% |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 22.398M | 13.483 | 0 | 0 | 0 |
| 8 | 22.629M | 12.973 | 0 | 0 | 0 |
| 16 | 18.895M | 11.752 | 0 | 0 | 0 |
| 32 | 17.112M | 10.008 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 6 | 22.050% / 18.949% / 65.591% | -13.761% |
| 4 | mechanism | 5 | 13.342% / 16.776% / 22.597% | -3.395% |
| 4 | business_base | 1 | 65.591% / 65.591% / 65.591% | -65.591% |
| 8 | train_base | 6 | 23.217% / 19.684% / 56.381% | -12.435% |
| 8 | mechanism | 5 | 16.584% / 16.788% / 33.786% | -3.646% |
| 8 | business_base | 1 | 56.381% / 56.381% / 56.381% | -56.381% |
| 16 | train_base | 6 | 22.115% / 21.206% / 39.275% | -5.388% |
| 16 | mechanism | 5 | 18.683% / 19.869% / 26.440% | 1.389% |
| 16 | business_base | 1 | 39.275% / 39.275% / 39.275% | -39.275% |
| 32 | train_base | 6 | 20.085% / 19.685% / 44.175% | 6.204% |
| 32 | mechanism | 5 | 19.353% / 16.817% / 44.175% | 12.195% |
| 32 | business_base | 1 | 23.748% / 23.748% / 23.748% | -23.748% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 10.564% | 0.030% | -0.030% | W_v28_int_div_serial (28) |
| 4 | private_l2_miss | 21.520% | 0.137% | -0.137% | W_v28_int_div_serial (56) |
| 4 | cha_llc_lookup | 21.520% | 0.136% | -0.136% | W_v28_int_div_serial (56) |
| 4 | branch_miss | 3.398% | 0.096% | -0.096% | W_v28_memory_random_mlp (61) |
| 4 | dtlb_access | 15.133% | 8.014% | -8.014% | W_v28_int_div_serial (131459) |
| 4 | dtlb_miss | 41.462% | 29.885% | 29.853% | W_v28_pytorch_base (49078) |
| 4 | llc_tag_vs_functional_path | 0.003% | 0.004% | 0.004% | W_v28_memory_random_mlp (91132) |
| 8 | l1d_miss | 12.199% | 0.033% | -0.033% | W_v28_simd_sse_dense (77) |
| 8 | private_l2_miss | 22.065% | 0.164% | -0.164% | W_v28_int_div_serial (115) |
| 8 | cha_llc_lookup | 22.058% | 0.164% | -0.164% | W_v28_int_div_serial (115) |
| 8 | branch_miss | 3.313% | 0.092% | -0.092% | W_v28_memory_random_mlp (122) |
| 8 | dtlb_access | 17.193% | 8.007% | -8.007% | W_v28_simd_sse_dense (335) |
| 8 | dtlb_miss | 38.677% | 29.911% | 29.884% | W_v28_pytorch_base (98185) |
| 8 | llc_tag_vs_functional_path | 0.003% | 0.004% | 0.004% | W_v28_memory_random_mlp (182085) |
| 16 | l1d_miss | 12.819% | 0.037% | -0.037% | W_v28_simd_sse_dense (159) |
| 16 | private_l2_miss | 22.310% | 0.169% | -0.169% | W_v28_int_div_serial (233) |
| 16 | cha_llc_lookup | 22.306% | 0.169% | -0.169% | W_v28_int_div_serial (233) |
| 16 | branch_miss | 3.418% | 0.094% | -0.094% | W_v28_memory_random_mlp (245) |
| 16 | dtlb_access | 17.616% | 8.014% | -8.014% | W_v28_simd_sse_dense (707) |
| 16 | dtlb_miss | 37.044% | 29.913% | 29.888% | W_v28_pytorch_base (196355) |
| 16 | llc_tag_vs_functional_path | 0.007% | 0.008% | 0.008% | W_v28_pytorch_base (159620) |
| 32 | l1d_miss | 13.882% | 0.041% | -0.041% | W_v28_simd_sse_dense (327) |
| 32 | private_l2_miss | 22.547% | 0.170% | -0.170% | W_v28_int_div_serial (488) |
| 32 | cha_llc_lookup | 22.534% | 0.169% | -0.169% | W_v28_int_div_serial (488) |
| 32 | branch_miss | 3.384% | 0.090% | -0.090% | W_v28_memory_random_mlp (492) |
| 32 | dtlb_access | 17.899% | 8.040% | -8.040% | W_v28_simd_sse_dense (1466) |
| 32 | dtlb_miss | 36.512% | 29.904% | 29.879% | W_v28_pytorch_base (392696) |
| 32 | llc_tag_vs_functional_path | 0.011% | 0.010% | 0.010% | W_v28_pytorch_base (266053) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 2840 | 6466.45 | 3157.62 | 36056 | 272.47 | 35.944% | 5232 | 3202 | 0/0 | 0 | 9342 |
| 8 | 3212 | 11435.08 | 3040.52 | 72104 | 481.83 | 35.937% | 11529 | 6848 | 0/0 | 0 | 51458 |
| 16 | 3818 | 19240.88 | 3019.88 | 144198 | 810.71 | 35.933% | 22984 | 14101 | 0/0 | 0 | 339047 |
| 32 | 4870 | 30168.83 | 3047.80 | 288410 | 1271.17 | 35.934% | 46535 | 28261 | 0/0 | 0 | 2282207 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
