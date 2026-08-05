# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 23 | 15.551% / 14.660% / 22.601% / 59.389% | 1.674% | 15.551% |
| 8 | 23 | 15.509% / 14.147% / 34.895% / 56.296% | 0.790% | 15.499% |
| 16 | 23 | 16.312% / 16.412% / 27.123% / 46.883% | 3.695% | 16.248% |
| 32 | 23 | 17.105% / 16.794% / 31.418% / 36.660% | 9.266% | 17.732% |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 19.904M | 15.517 | 0 | 0 | 0 |
| 8 | 18.701M | 14.556 | 0 | 0 | 0 |
| 16 | 16.796M | 12.928 | 0 | 0 | 0 |
| 32 | 14.443M | 11.066 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 16 | 13.536% / 9.474% / 59.389% | 0.583% |
| 4 | mechanism | 9 | 9.073% / 7.244% / 22.601% | -0.757% |
| 4 | business_base | 7 | 19.273% / 16.126% / 59.389% | 2.305% |
| 4 | heldout | 7 | 20.157% / 17.713% / 55.965% | 4.167% |
| 8 | train_base | 16 | 14.287% / 8.718% / 56.296% | -0.170% |
| 8 | mechanism | 9 | 11.204% / 7.262% / 34.895% | -1.987% |
| 8 | business_base | 7 | 18.251% / 15.628% / 56.296% | 2.166% |
| 8 | heldout | 7 | 18.301% / 15.840% / 53.610% | 2.984% |
| 16 | train_base | 16 | 15.548% / 16.593% / 46.883% | 3.243% |
| 16 | mechanism | 9 | 13.274% / 16.775% / 27.123% | 1.817% |
| 16 | business_base | 7 | 18.472% / 16.412% / 46.883% | 5.077% |
| 16 | heldout | 7 | 18.057% / 15.893% / 45.731% | 4.727% |
| 32 | train_base | 16 | 16.123% / 15.838% / 36.660% | 8.717% |
| 32 | mechanism | 9 | 11.910% / 11.760% / 31.418% | 6.148% |
| 32 | business_base | 7 | 21.541% / 21.617% / 36.660% | 12.019% |
| 32 | heldout | 7 | 19.349% / 20.786% / 30.897% | 10.521% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 6.706% | 0.043% | -0.043% | W_v28_int_alu_dense (42) |
| 4 | private_l2_miss | 11.768% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | cha_llc_lookup | 11.767% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | branch_miss | 2.028% | 0.165% | -0.072% | W_v28_int_alu_dense (64) |
| 4 | dtlb_access | 15.048% | 8.770% | -8.770% | W_v28_int_alu_dense (112) |
| 4 | dtlb_miss | 8.436% | 0.081% | -0.081% | W_v28_int_div_serial (39) |
| 4 | llc_tag_vs_functional_path | 0.606% | 0.767% | 0.767% | W_v28_marine_heldout (45456) |
| 8 | l1d_miss | 7.493% | 0.048% | -0.048% | W_v28_int_alu_dense (85) |
| 8 | private_l2_miss | 12.010% | 0.266% | -0.266% | W_v28_int_alu_dense (147) |
| 8 | cha_llc_lookup | 12.008% | 0.266% | -0.266% | W_v28_int_alu_dense (147) |
| 8 | branch_miss | 2.007% | 0.170% | -0.060% | W_v28_int_alu_dense (128) |
| 8 | dtlb_access | 15.825% | 8.799% | -8.799% | W_v28_int_alu_dense (230) |
| 8 | dtlb_miss | 7.889% | 0.085% | -0.085% | W_v28_int_div_serial (75) |
| 8 | llc_tag_vs_functional_path | 0.610% | 0.769% | 0.769% | W_v28_marine_heldout (88796) |
| 16 | l1d_miss | 7.769% | 0.052% | -0.051% | W_v28_int_alu_dense (168) |
| 16 | private_l2_miss | 12.133% | 0.283% | -0.283% | W_v28_int_alu_dense (295) |
| 16 | cha_llc_lookup | 12.132% | 0.283% | -0.283% | W_v28_int_alu_dense (295) |
| 16 | branch_miss | 2.010% | 0.160% | -0.057% | W_v28_int_alu_dense (256) |
| 16 | dtlb_access | 16.005% | 8.805% | -8.805% | W_v28_int_alu_dense (456) |
| 16 | dtlb_miss | 7.643% | 0.086% | -0.086% | W_v28_int_div_serial (150) |
| 16 | llc_tag_vs_functional_path | 0.606% | 0.760% | 0.760% | W_v28_marine_heldout (170695) |
| 32 | l1d_miss | 8.113% | 0.059% | -0.055% | W_v28_int_alu_dense (336) |
| 32 | private_l2_miss | 12.218% | 0.290% | -0.290% | W_v28_int_alu_dense (592) |
| 32 | cha_llc_lookup | 12.215% | 0.290% | -0.290% | W_v28_int_alu_dense (592) |
| 32 | branch_miss | 1.987% | 0.153% | -0.054% | W_v28_int_alu_dense (512) |
| 32 | dtlb_access | 16.140% | 8.843% | -8.843% | W_v28_int_alu_dense (916) |
| 32 | dtlb_miss | 7.626% | 0.091% | -0.091% | W_v28_int_div_serial (305) |
| 32 | llc_tag_vs_functional_path | 0.579% | 0.702% | 0.702% | W_v28_marine_heldout (318738) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 17265 | 4426.84 | 1789.41 | 57344 | 205.42 | 23.232% | 59860 | 38482 | 0/0 | 0 | 1984318 |
| 8 | 17487 | 8741.25 | 1779.78 | 114688 | 405.63 | 23.227% | 119849 | 77312 | 0/0 | 0 | 3980479 |
| 16 | 18090 | 16899.91 | 1748.65 | 229376 | 784.23 | 23.226% | 245094 | 157018 | 0/0 | 0 | 7750090 |
| 32 | 19276 | 31720.04 | 1729.12 | 458752 | 1471.94 | 23.227% | 497111 | 318253 | 0/0 | 0 | 14770652 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
