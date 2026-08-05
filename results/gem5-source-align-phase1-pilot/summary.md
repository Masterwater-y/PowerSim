# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 6 | 66.849% / 43.743% / 141.151% / 153.410% | 39.627% | 66.849% |
| 8 | 6 | 64.851% / 37.478% / 120.968% / 169.750% | 41.797% | 64.955% |
| 16 | 6 | 76.784% / 27.795% / 156.134% / 224.519% | 60.170% | 77.609% |
| 32 | 6 | 107.970% / 19.685% / 265.249% / 320.923% | 99.063% | 109.875% |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 21.819M | 12.071 | 0 | 0 | 0 |
| 8 | 21.743M | 11.743 | 0 | 0 | 0 |
| 16 | 19.948M | 10.716 | 0 | 0 | 0 |
| 32 | 16.887M | 8.566 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 6 | 66.849% / 43.743% / 153.410% | 39.627% |
| 4 | mechanism | 5 | 67.241% / 22.597% / 153.410% | 60.530% |
| 4 | business_base | 1 | 64.889% / 64.889% / 64.889% | -64.889% |
| 8 | train_base | 6 | 64.851% / 37.478% / 169.750% | 41.797% |
| 8 | mechanism | 5 | 67.346% / 22.580% / 169.750% | 60.631% |
| 8 | business_base | 1 | 52.375% / 52.375% / 52.375% | -52.375% |
| 16 | train_base | 6 | 76.784% / 27.795% / 224.519% | 60.170% |
| 16 | mechanism | 5 | 85.532% / 22.543% / 224.519% | 78.813% |
| 16 | business_base | 1 | 33.047% / 33.047% / 33.047% | -33.047% |
| 32 | train_base | 6 | 107.970% / 19.685% / 320.923% | 99.063% |
| 32 | mechanism | 5 | 127.583% / 22.552% / 320.923% | 120.856% |
| 32 | business_base | 1 | 9.902% / 9.902% / 9.902% | -9.902% |

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
| 8 | private_l2_miss | 22.066% | 0.165% | -0.165% | W_v28_int_div_serial (115) |
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
| 32 | l1d_miss | 13.880% | 0.041% | -0.041% | W_v28_simd_sse_dense (327) |
| 32 | private_l2_miss | 22.545% | 0.169% | -0.169% | W_v28_int_div_serial (488) |
| 32 | cha_llc_lookup | 22.532% | 0.169% | -0.169% | W_v28_int_div_serial (488) |
| 32 | branch_miss | 3.384% | 0.090% | -0.090% | W_v28_memory_random_mlp (492) |
| 32 | dtlb_access | 17.899% | 8.040% | -8.040% | W_v28_simd_sse_dense (1466) |
| 32 | dtlb_miss | 36.512% | 29.904% | 29.879% | W_v28_pytorch_base (392696) |
| 32 | llc_tag_vs_functional_path | 0.010% | 0.010% | 0.009% | W_v28_pytorch_base (266053) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 6393 | 2872.63 | 3143.03 | 36056 | 121.04 | 35.944% | 5104 | 3228 | 0/0 | 0 | 51434 |
| 8 | 7701 | 4769.44 | 3015.56 | 72104 | 200.97 | 35.937% | 11609 | 6946 | 0/0 | 0 | 253978 |
| 16 | 9578 | 7669.83 | 3052.89 | 144198 | 323.17 | 35.933% | 22552 | 13855 | 0/0 | 0 | 1221533 |
| 32 | 13493 | 10888.77 | 3035.65 | 288410 | 458.80 | 35.934% | 46092 | 27952 | 0/0 | 0 | 3591850 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
