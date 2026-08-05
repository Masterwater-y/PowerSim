# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 23 | 12.263% / 7.221% / 22.597% / 67.598% | -4.086% | 12.263% |
| 8 | 23 | 12.637% / 6.616% / 34.279% / 63.507% | -4.407% | 12.626% |
| 16 | 23 | 13.436% / 8.540% / 26.659% / 51.286% | -0.914% | 13.376% |
| 32 | 23 | 14.639% / 12.564% / 36.044% / 43.537% | 5.191% | 15.281% |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 19.981M | 15.578 | 0 | 0 | 0 |
| 8 | 18.748M | 14.491 | 0 | 0 | 0 |
| 16 | 17.083M | 12.975 | 0 | 0 | 0 |
| 32 | 14.485M | 10.979 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 16 | 11.509% / 7.120% / 67.598% | -3.868% |
| 4 | mechanism | 9 | 8.873% / 7.221% / 22.597% | -1.601% |
| 4 | business_base | 7 | 14.898% / 7.018% / 67.598% | -6.784% |
| 4 | heldout | 7 | 13.987% / 7.720% / 59.332% | -4.583% |
| 8 | train_base | 16 | 12.336% / 6.716% / 63.507% | -4.042% |
| 8 | mechanism | 9 | 10.871% / 6.827% / 34.279% | -2.194% |
| 8 | business_base | 7 | 14.220% / 6.605% / 63.507% | -6.419% |
| 8 | heldout | 7 | 13.324% / 6.616% / 56.540% | -5.241% |
| 16 | train_base | 16 | 13.314% / 8.262% / 51.286% | -0.111% |
| 16 | mechanism | 9 | 13.442% / 16.796% / 26.659% | 2.069% |
| 16 | business_base | 7 | 13.149% / 6.662% / 51.286% | -2.915% |
| 16 | heldout | 7 | 13.713% / 8.955% / 48.487% | -2.749% |
| 32 | train_base | 16 | 14.282% / 11.960% / 43.537% | 5.947% |
| 32 | mechanism | 9 | 13.395% / 12.564% / 43.537% | 7.743% |
| 32 | business_base | 7 | 15.422% / 11.356% / 41.248% | 3.637% |
| 32 | heldout | 7 | 15.455% / 14.344% / 36.044% | 3.464% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 6.706% | 0.043% | -0.043% | W_v28_int_alu_dense (42) |
| 4 | private_l2_miss | 11.767% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | cha_llc_lookup | 11.767% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | branch_miss | 2.028% | 0.165% | -0.072% | W_v28_int_alu_dense (64) |
| 4 | dtlb_access | 15.048% | 8.770% | -8.770% | W_v28_int_alu_dense (112) |
| 4 | dtlb_miss | 8.428% | 0.051% | -0.051% | W_v28_int_div_serial (39) |
| 4 | llc_tag_vs_functional_path | 0.606% | 0.767% | 0.767% | W_v28_marine_heldout (45456) |
| 8 | l1d_miss | 7.491% | 0.048% | -0.048% | W_v28_int_alu_dense (85) |
| 8 | private_l2_miss | 12.008% | 0.266% | -0.266% | W_v28_int_alu_dense (147) |
| 8 | cha_llc_lookup | 12.006% | 0.265% | -0.265% | W_v28_int_alu_dense (147) |
| 8 | branch_miss | 2.007% | 0.170% | -0.060% | W_v28_int_alu_dense (128) |
| 8 | dtlb_access | 15.825% | 8.799% | -8.799% | W_v28_int_alu_dense (230) |
| 8 | dtlb_miss | 7.880% | 0.055% | -0.055% | W_v28_int_div_serial (75) |
| 8 | llc_tag_vs_functional_path | 0.610% | 0.769% | 0.769% | W_v28_marine_heldout (88796) |
| 16 | l1d_miss | 7.769% | 0.052% | -0.051% | W_v28_int_alu_dense (168) |
| 16 | private_l2_miss | 12.133% | 0.283% | -0.283% | W_v28_int_alu_dense (295) |
| 16 | cha_llc_lookup | 12.132% | 0.283% | -0.283% | W_v28_int_alu_dense (295) |
| 16 | branch_miss | 2.010% | 0.160% | -0.057% | W_v28_int_alu_dense (256) |
| 16 | dtlb_access | 16.005% | 8.805% | -8.805% | W_v28_int_alu_dense (456) |
| 16 | dtlb_miss | 7.634% | 0.056% | -0.056% | W_v28_int_div_serial (150) |
| 16 | llc_tag_vs_functional_path | 0.606% | 0.760% | 0.760% | W_v28_marine_heldout (170695) |
| 32 | l1d_miss | 8.113% | 0.059% | -0.055% | W_v28_int_alu_dense (336) |
| 32 | private_l2_miss | 12.218% | 0.290% | -0.290% | W_v28_int_alu_dense (592) |
| 32 | cha_llc_lookup | 12.215% | 0.290% | -0.290% | W_v28_int_alu_dense (592) |
| 32 | branch_miss | 1.987% | 0.153% | -0.054% | W_v28_int_alu_dense (512) |
| 32 | dtlb_access | 16.140% | 8.843% | -8.843% | W_v28_int_alu_dense (916) |
| 32 | dtlb_miss | 7.617% | 0.061% | -0.061% | W_v28_int_div_serial (305) |
| 32 | llc_tag_vs_functional_path | 0.579% | 0.702% | 0.702% | W_v28_marine_heldout (318738) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 16042 | 4764.33 | 2249.65 | 57344 | 221.09 | 23.232% | 60067 | 29666 | 0/0 | 0 | 2264828 |
| 8 | 16367 | 9339.42 | 2232.26 | 114688 | 433.39 | 23.227% | 122157 | 59841 | 0/0 | 0 | 4522953 |
| 16 | 17123 | 17854.31 | 2200.89 | 229376 | 828.52 | 23.226% | 249120 | 121888 | 0/0 | 0 | 9059690 |
| 32 | 18491 | 33066.65 | 2191.61 | 458752 | 1534.43 | 23.227% | 501067 | 245077 | 0/0 | 0 | 18191795 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
