# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 23 | 11.814% / 7.176% / 20.585% / 65.569% | -4.549% | 11.814% |
| 8 | 23 | 12.015% / 6.532% / 34.380% / 59.668% | -4.729% | 12.005% |
| 16 | 23 | 12.514% / 8.429% / 25.233% / 45.774% | -0.802% | 12.451% |
| 32 | 23 | 13.349% / 13.477% / 25.338% / 45.301% | 5.922% | 13.974% |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 16 | 11.034% / 7.053% / 65.569% | -4.391% |
| 4 | mechanism | 9 | 8.342% / 7.327% / 20.585% | -2.555% |
| 4 | business_base | 7 | 14.497% / 6.779% / 65.569% | -6.752% |
| 4 | heldout | 7 | 13.597% / 7.176% / 58.914% | -4.909% |
| 8 | train_base | 16 | 11.617% / 6.619% / 59.668% | -4.345% |
| 8 | mechanism | 9 | 10.185% / 6.706% / 34.380% | -2.932% |
| 8 | business_base | 7 | 13.458% / 6.532% / 59.668% | -6.162% |
| 8 | heldout | 7 | 12.924% / 6.163% / 56.344% | -5.605% |
| 16 | train_base | 16 | 12.282% / 8.252% / 43.236% | 0.035% |
| 16 | mechanism | 9 | 12.745% / 16.075% / 25.233% | 1.667% |
| 16 | business_base | 7 | 11.685% / 6.252% / 43.236% | -2.063% |
| 16 | heldout | 7 | 13.045% / 8.622% / 45.774% | -2.715% |
| 32 | train_base | 16 | 12.976% / 11.962% / 45.301% | 6.572% |
| 32 | mechanism | 9 | 13.050% / 13.477% / 45.301% | 7.295% |
| 32 | business_base | 7 | 12.882% / 10.446% / 25.338% | 5.642% |
| 32 | heldout | 7 | 14.200% / 14.075% / 28.020% | 4.437% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 6.706% | 0.043% | -0.043% | W_v28_int_alu_dense (42) |
| 4 | private_l2_miss | 11.768% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | cha_llc_lookup | 11.767% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | branch_miss | 2.028% | 0.165% | -0.072% | W_v28_int_alu_dense (64) |
| 4 | llc_tag_vs_functional_path | 0.606% | 0.767% | 0.767% | W_v28_marine_heldout (45456) |
| 8 | l1d_miss | 7.493% | 0.048% | -0.048% | W_v28_int_alu_dense (85) |
| 8 | private_l2_miss | 12.010% | 0.266% | -0.266% | W_v28_int_alu_dense (147) |
| 8 | cha_llc_lookup | 12.008% | 0.265% | -0.265% | W_v28_int_alu_dense (147) |
| 8 | branch_miss | 2.007% | 0.170% | -0.060% | W_v28_int_alu_dense (128) |
| 8 | llc_tag_vs_functional_path | 0.610% | 0.769% | 0.769% | W_v28_marine_heldout (88796) |
| 16 | l1d_miss | 7.770% | 0.052% | -0.051% | W_v28_int_alu_dense (168) |
| 16 | private_l2_miss | 12.134% | 0.283% | -0.283% | W_v28_int_alu_dense (295) |
| 16 | cha_llc_lookup | 12.133% | 0.283% | -0.283% | W_v28_int_alu_dense (295) |
| 16 | branch_miss | 2.010% | 0.160% | -0.057% | W_v28_int_alu_dense (256) |
| 16 | llc_tag_vs_functional_path | 0.606% | 0.760% | 0.760% | W_v28_marine_heldout (170695) |
| 32 | l1d_miss | 8.112% | 0.059% | -0.055% | W_v28_int_alu_dense (336) |
| 32 | private_l2_miss | 12.216% | 0.290% | -0.290% | W_v28_int_alu_dense (592) |
| 32 | cha_llc_lookup | 12.213% | 0.290% | -0.290% | W_v28_int_alu_dense (592) |
| 32 | branch_miss | 1.987% | 0.153% | -0.054% | W_v28_int_alu_dense (512) |
| 32 | llc_tag_vs_functional_path | 0.579% | 0.702% | 0.702% | W_v28_marine_heldout (318738) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 15967 | 4786.71 | 2293.11 | 57344 | 222.12 | 23.232% | 48327 | 29414 | 2393340 |
| 8 | 16328 | 9361.73 | 2267.52 | 114688 | 434.42 | 23.227% | 99136 | 59017 | 4813224 |
| 16 | 17139 | 17837.64 | 2227.90 | 229376 | 827.74 | 23.226% | 202003 | 121183 | 9675647 |
| 32 | 18561 | 32941.94 | 2222.14 | 458752 | 1528.65 | 23.227% | 402151 | 241969 | 19359408 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
