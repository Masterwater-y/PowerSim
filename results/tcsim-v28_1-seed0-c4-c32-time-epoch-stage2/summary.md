# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 23 | 11.907% / 7.176% / 20.585% / 67.512% | -4.650% | 11.907% |
| 8 | 23 | 12.118% / 6.532% / 34.380% / 59.411% | -4.711% | 12.108% |
| 16 | 23 | 12.495% / 8.075% / 25.233% / 46.273% | -0.755% | 12.432% |
| 32 | 23 | 13.307% / 13.120% / 23.888% / 44.644% | 6.012% | 13.939% |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 16 | 11.118% / 7.007% / 67.512% | -4.466% |
| 4 | mechanism | 9 | 8.271% / 7.236% / 20.585% | -2.446% |
| 4 | business_base | 7 | 14.777% / 6.779% / 67.512% | -7.063% |
| 4 | heldout | 7 | 13.711% / 7.176% / 59.578% | -5.070% |
| 8 | train_base | 16 | 11.678% / 6.559% / 59.411% | -4.274% |
| 8 | mechanism | 9 | 10.298% / 6.587% / 34.380% | -2.815% |
| 8 | business_base | 7 | 13.451% / 6.532% / 59.411% | -6.151% |
| 8 | heldout | 7 | 13.124% / 6.212% / 57.067% | -5.710% |
| 16 | train_base | 16 | 12.273% / 8.245% / 41.579% | 0.209% |
| 16 | mechanism | 9 | 12.873% / 16.075% / 25.233% | 1.796% |
| 16 | business_base | 7 | 11.502% / 6.126% / 41.579% | -1.831% |
| 16 | heldout | 7 | 13.002% / 8.046% / 46.273% | -2.959% |
| 32 | train_base | 16 | 12.930% / 11.989% / 44.644% | 6.709% |
| 32 | mechanism | 9 | 13.044% / 13.120% / 44.644% | 7.323% |
| 32 | business_base | 7 | 12.783% / 10.858% / 23.888% | 5.919% |
| 32 | heldout | 7 | 14.170% / 13.988% / 27.907% | 4.420% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 6.706% | 0.043% | -0.043% | W_v28_int_alu_dense (42) |
| 4 | private_l2_miss | 11.767% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | cha_llc_lookup | 11.767% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | branch_miss | 2.028% | 0.165% | -0.072% | W_v28_int_alu_dense (64) |
| 4 | llc_tag_vs_functional_path | 0.606% | 0.767% | 0.767% | W_v28_marine_heldout (45456) |
| 8 | l1d_miss | 7.491% | 0.048% | -0.048% | W_v28_int_alu_dense (85) |
| 8 | private_l2_miss | 12.009% | 0.266% | -0.266% | W_v28_int_alu_dense (147) |
| 8 | cha_llc_lookup | 12.006% | 0.265% | -0.265% | W_v28_int_alu_dense (147) |
| 8 | branch_miss | 2.007% | 0.170% | -0.060% | W_v28_int_alu_dense (128) |
| 8 | llc_tag_vs_functional_path | 0.610% | 0.769% | 0.769% | W_v28_marine_heldout (88796) |
| 16 | l1d_miss | 7.770% | 0.052% | -0.051% | W_v28_int_alu_dense (168) |
| 16 | private_l2_miss | 12.134% | 0.283% | -0.283% | W_v28_int_alu_dense (295) |
| 16 | cha_llc_lookup | 12.133% | 0.283% | -0.283% | W_v28_int_alu_dense (295) |
| 16 | branch_miss | 2.010% | 0.160% | -0.057% | W_v28_int_alu_dense (256) |
| 16 | llc_tag_vs_functional_path | 0.606% | 0.760% | 0.760% | W_v28_marine_heldout (170695) |
| 32 | l1d_miss | 8.112% | 0.059% | -0.055% | W_v28_int_alu_dense (336) |
| 32 | private_l2_miss | 12.217% | 0.290% | -0.290% | W_v28_int_alu_dense (592) |
| 32 | cha_llc_lookup | 12.213% | 0.290% | -0.290% | W_v28_int_alu_dense (592) |
| 32 | branch_miss | 1.987% | 0.153% | -0.054% | W_v28_int_alu_dense (512) |
| 32 | llc_tag_vs_functional_path | 0.579% | 0.702% | 0.702% | W_v28_marine_heldout (318738) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 15958 | 4789.41 | 2291.81 | 57344 | 222.25 | 23.232% | 61059 | 29699 | 0/0 | 0 | 2419146 |
| 8 | 16325 | 9363.45 | 2264.13 | 114688 | 434.50 | 23.227% | 121920 | 59149 | 0/0 | 0 | 4836407 |
| 16 | 17136 | 17840.76 | 2235.92 | 229376 | 827.89 | 23.226% | 251301 | 120667 | 0/0 | 0 | 9737969 |
| 32 | 18571 | 32924.20 | 2229.94 | 458752 | 1527.82 | 23.227% | 503785 | 241769 | 0/0 | 0 | 19547268 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
