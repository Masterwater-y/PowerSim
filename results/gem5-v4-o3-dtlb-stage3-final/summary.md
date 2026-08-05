# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 23 | 12.018% / 7.221% / 22.597% / 67.526% | -3.659% | 12.018% |
| 8 | 23 | 11.763% / 6.867% / 22.580% / 55.252% | -2.047% | 11.754% |
| 16 | 23 | 12.562% / 8.635% / 22.543% / 74.089% | 7.880% | 12.799% |
| 32 | 23 | 25.798% / 17.841% / 38.829% / 169.056% | 23.449% | 26.078% |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 19.724M | 15.305 | 0 | 0 | 0 |
| 8 | 18.734M | 14.246 | 0 | 0 | 0 |
| 16 | 16.851M | 13.159 | 0 | 0 | 0 |
| 32 | 14.555M | 11.338 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 16 | 11.157% / 7.120% / 67.526% | -3.254% |
| 4 | mechanism | 9 | 8.256% / 7.221% / 22.597% | -0.517% |
| 4 | business_base | 7 | 14.887% / 7.018% / 67.526% | -6.773% |
| 4 | heldout | 7 | 13.987% / 7.720% / 59.332% | -4.583% |
| 8 | train_base | 16 | 11.167% / 7.057% / 42.763% | -0.723% |
| 8 | mechanism | 9 | 11.109% / 7.794% / 22.580% | 1.382% |
| 8 | business_base | 7 | 11.241% / 6.622% / 42.763% | -3.429% |
| 8 | heldout | 7 | 13.125% / 6.609% / 55.252% | -5.072% |
| 16 | train_base | 16 | 13.921% / 7.913% / 74.089% | 10.369% |
| 16 | mechanism | 9 | 19.438% / 16.796% / 74.089% | 13.989% |
| 16 | business_base | 7 | 6.827% / 5.202% / 15.545% | 5.716% |
| 16 | heldout | 7 | 9.455% / 9.072% / 16.798% | 2.189% |
| 32 | train_base | 16 | 30.099% / 17.329% / 169.056% | 27.036% |
| 32 | mechanism | 9 | 41.122% / 22.553% / 169.056% | 35.677% |
| 32 | business_base | 7 | 15.927% / 13.443% / 27.980% | 15.927% |
| 32 | heldout | 7 | 15.968% / 17.896% / 24.837% | 15.249% |

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
| 16 | l1d_miss | 7.770% | 0.052% | -0.051% | W_v28_int_alu_dense (168) |
| 16 | private_l2_miss | 12.134% | 0.283% | -0.283% | W_v28_int_alu_dense (295) |
| 16 | cha_llc_lookup | 12.133% | 0.283% | -0.283% | W_v28_int_alu_dense (295) |
| 16 | branch_miss | 2.010% | 0.160% | -0.057% | W_v28_int_alu_dense (256) |
| 16 | dtlb_access | 16.005% | 8.805% | -8.805% | W_v28_int_alu_dense (456) |
| 16 | dtlb_miss | 7.634% | 0.056% | -0.056% | W_v28_int_div_serial (150) |
| 16 | llc_tag_vs_functional_path | 0.606% | 0.760% | 0.760% | W_v28_marine_heldout (170695) |
| 32 | l1d_miss | 8.113% | 0.059% | -0.055% | W_v28_int_alu_dense (336) |
| 32 | private_l2_miss | 12.218% | 0.290% | -0.290% | W_v28_int_alu_dense (592) |
| 32 | cha_llc_lookup | 12.214% | 0.290% | -0.290% | W_v28_int_alu_dense (592) |
| 32 | branch_miss | 1.987% | 0.153% | -0.054% | W_v28_int_alu_dense (512) |
| 32 | dtlb_access | 16.140% | 8.843% | -8.843% | W_v28_int_alu_dense (916) |
| 32 | dtlb_miss | 7.617% | 0.061% | -0.061% | W_v28_int_div_serial (305) |
| 32 | llc_tag_vs_functional_path | 0.579% | 0.702% | 0.702% | W_v28_marine_heldout (318738) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 16159 | 4729.84 | 2249.25 | 57344 | 219.48 | 23.232% | 60160 | 29674 | 0/0 | 0 | 2387113 |
| 8 | 16963 | 9011.28 | 2223.20 | 114688 | 418.16 | 23.227% | 122816 | 60106 | 0/0 | 0 | 4804754 |
| 16 | 19060 | 16039.84 | 2200.88 | 229376 | 744.32 | 23.226% | 249528 | 122334 | 0/0 | 0 | 9663371 |
| 32 | 22928 | 26667.63 | 2198.91 | 458752 | 1237.49 | 23.227% | 498378 | 244412 | 0/0 | 0 | 19372819 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
