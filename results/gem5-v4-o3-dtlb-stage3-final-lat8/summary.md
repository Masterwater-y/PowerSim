# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 23 | 12.746% / 8.019% / 22.598% / 65.549% | -2.972% | 12.746% |
| 8 | 23 | 13.029% / 7.643% / 34.899% / 61.408% | -3.378% | 13.021% |
| 16 | 23 | 13.898% / 10.165% / 26.944% / 49.683% | -0.007% | 13.833% |
| 32 | 23 | 14.860% / 13.398% / 33.777% / 38.875% | 5.955% | 15.503% |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 19.921M | 15.407 | 0 | 0 | 0 |
| 8 | 18.679M | 14.420 | 0 | 0 | 0 |
| 16 | 16.753M | 13.042 | 0 | 0 | 0 |
| 32 | 14.516M | 11.133 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 16 | 11.697% / 7.619% / 65.549% | -3.065% |
| 4 | mechanism | 9 | 8.984% / 7.220% / 22.598% | -1.676% |
| 4 | business_base | 7 | 15.184% / 9.191% / 65.549% | -4.851% |
| 4 | heldout | 7 | 15.145% / 9.751% / 58.964% | -2.759% |
| 8 | train_base | 16 | 12.418% / 7.098% / 61.408% | -3.328% |
| 8 | mechanism | 9 | 10.879% / 6.928% / 34.899% | -2.325% |
| 8 | business_base | 7 | 14.398% / 8.638% / 61.408% | -4.618% |
| 8 | heldout | 7 | 14.425% / 8.110% / 56.289% | -3.492% |
| 16 | train_base | 16 | 13.568% / 9.622% / 49.683% | 0.504% |
| 16 | mechanism | 9 | 13.321% / 16.792% / 26.944% | 1.888% |
| 16 | business_base | 7 | 13.886% / 9.079% / 49.683% | -1.274% |
| 16 | heldout | 7 | 14.652% / 10.623% / 47.915% | -1.174% |
| 32 | train_base | 16 | 14.367% / 12.979% / 38.875% | 6.332% |
| 32 | mechanism | 9 | 12.870% / 12.560% / 38.875% | 7.219% |
| 32 | business_base | 7 | 16.291% / 13.398% / 38.848% | 5.192% |
| 32 | heldout | 7 | 15.988% / 16.058% / 33.777% | 5.093% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 6.706% | 0.043% | -0.043% | W_v28_int_alu_dense (42) |
| 4 | private_l2_miss | 11.768% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | cha_llc_lookup | 11.767% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | branch_miss | 2.028% | 0.165% | -0.072% | W_v28_int_alu_dense (64) |
| 4 | dtlb_access | 15.048% | 8.770% | -8.770% | W_v28_int_alu_dense (112) |
| 4 | dtlb_miss | 8.434% | 0.073% | -0.073% | W_v28_int_div_serial (39) |
| 4 | llc_tag_vs_functional_path | 0.606% | 0.767% | 0.767% | W_v28_marine_heldout (45456) |
| 8 | l1d_miss | 7.491% | 0.048% | -0.048% | W_v28_int_alu_dense (85) |
| 8 | private_l2_miss | 12.008% | 0.266% | -0.266% | W_v28_int_alu_dense (147) |
| 8 | cha_llc_lookup | 12.006% | 0.265% | -0.265% | W_v28_int_alu_dense (147) |
| 8 | branch_miss | 2.007% | 0.170% | -0.060% | W_v28_int_alu_dense (128) |
| 8 | dtlb_access | 15.825% | 8.799% | -8.799% | W_v28_int_alu_dense (230) |
| 8 | dtlb_miss | 7.887% | 0.076% | -0.076% | W_v28_int_div_serial (75) |
| 8 | llc_tag_vs_functional_path | 0.610% | 0.769% | 0.769% | W_v28_marine_heldout (88796) |
| 16 | l1d_miss | 7.769% | 0.052% | -0.051% | W_v28_int_alu_dense (168) |
| 16 | private_l2_miss | 12.133% | 0.283% | -0.283% | W_v28_int_alu_dense (295) |
| 16 | cha_llc_lookup | 12.132% | 0.283% | -0.283% | W_v28_int_alu_dense (295) |
| 16 | branch_miss | 2.010% | 0.160% | -0.057% | W_v28_int_alu_dense (256) |
| 16 | dtlb_access | 16.005% | 8.805% | -8.805% | W_v28_int_alu_dense (456) |
| 16 | dtlb_miss | 7.641% | 0.077% | -0.077% | W_v28_int_div_serial (150) |
| 16 | llc_tag_vs_functional_path | 0.606% | 0.760% | 0.760% | W_v28_marine_heldout (170695) |
| 32 | l1d_miss | 8.113% | 0.059% | -0.055% | W_v28_int_alu_dense (336) |
| 32 | private_l2_miss | 12.217% | 0.290% | -0.290% | W_v28_int_alu_dense (592) |
| 32 | cha_llc_lookup | 12.214% | 0.290% | -0.290% | W_v28_int_alu_dense (592) |
| 32 | branch_miss | 1.987% | 0.153% | -0.054% | W_v28_int_alu_dense (512) |
| 32 | dtlb_access | 16.140% | 8.843% | -8.843% | W_v28_int_alu_dense (916) |
| 32 | dtlb_miss | 7.624% | 0.082% | -0.082% | W_v28_int_div_serial (305) |
| 32 | llc_tag_vs_functional_path | 0.579% | 0.702% | 0.702% | W_v28_marine_heldout (318738) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 16262 | 4699.88 | 2118.45 | 57344 | 218.09 | 23.232% | 60522 | 32012 | 0/0 | 0 | 2213877 |
| 8 | 16596 | 9210.55 | 2106.65 | 114688 | 427.41 | 23.227% | 121654 | 63839 | 0/0 | 0 | 4393895 |
| 16 | 17297 | 17674.70 | 2076.25 | 229376 | 820.18 | 23.226% | 249816 | 130537 | 0/0 | 0 | 8672949 |
| 32 | 18625 | 32828.75 | 2066.29 | 458752 | 1523.39 | 23.227% | 502465 | 262130 | 0/0 | 0 | 17116003 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
