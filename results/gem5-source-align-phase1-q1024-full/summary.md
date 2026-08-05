# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / p99 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 23 | 10.211% / 8.585% / 20.017% / 22.857% / 22.931% | 3.644% | 10.211% |
| 8 | 23 | 10.361% / 7.865% / 19.022% / 30.681% / 32.966% | 2.774% | 10.351% |
| 16 | 23 | 11.385% / 9.556% / 21.394% / 28.143% / 28.555% | 5.103% | 11.325% |
| 32 | 23 | 12.896% / 12.382% / 21.364% / 36.944% / 41.003% | 9.518% | 13.189% |

## Acceptance gates

CPI P99 and throughput are gated independently for each core count.

| Cores | CPI P99 (<=10%) | Min UOP/s (>=5M) | Slowest workload | Pass |
|---:|---:|---:|---|:---:|
| 4 | 22.857% | 7.115M | W_v28_memory_random_mlp | FAIL |
| 8 | 30.681% | 6.690M | W_v28_memory_random_mlp | FAIL |
| 16 | 28.143% | 5.991M | W_v28_memory_random_mlp | FAIL |
| 32 | 36.944% | 5.165M | W_v28_memory_random_mlp | FAIL |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Min / P10 / median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 7.115M / 9.192M / 9.720M | 7.644 | 0 | 0 | 0 |
| 8 | 6.690M / 8.905M / 9.470M | 7.335 | 0 | 0 | 0 |
| 16 | 5.991M / 8.225M / 8.903M | 6.908 | 0 | 0 | 0 |
| 32 | 5.165M / 7.352M / 8.119M | 6.250 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 16 | 10.120% / 9.080% / 22.931% | 3.283% |
| 4 | mechanism | 9 | 9.802% / 11.337% / 22.597% | 2.744% |
| 4 | business_base | 7 | 10.528% / 8.459% / 22.931% | 3.977% |
| 4 | heldout | 7 | 10.421% / 8.585% / 20.828% | 4.470% |
| 8 | train_base | 16 | 11.086% / 8.218% / 32.966% | 2.386% |
| 8 | mechanism | 9 | 12.426% / 10.128% / 32.966% | 1.263% |
| 8 | business_base | 7 | 9.363% / 7.865% / 19.363% | 3.830% |
| 8 | heldout | 7 | 8.705% / 7.225% / 17.659% | 3.660% |
| 16 | train_base | 16 | 12.311% / 9.559% / 28.555% | 4.788% |
| 16 | mechanism | 9 | 14.160% / 16.222% / 28.555% | 3.970% |
| 16 | business_base | 7 | 9.933% / 8.984% / 14.323% | 5.841% |
| 16 | heldout | 7 | 9.270% / 8.870% / 12.448% | 5.821% |
| 32 | train_base | 16 | 13.119% / 11.948% / 41.003% | 9.126% |
| 32 | mechanism | 9 | 12.995% / 9.371% / 41.003% | 7.553% |
| 32 | business_base | 7 | 13.278% / 13.100% / 22.029% | 11.150% |
| 32 | heldout | 7 | 12.386% / 12.462% / 18.707% | 10.415% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 6.706% | 0.043% | -0.043% | W_v28_int_alu_dense (42) |
| 4 | private_l2_miss | 11.768% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | cha_llc_lookup | 11.767% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | branch_miss | 2.028% | 0.165% | -0.072% | W_v28_int_alu_dense (64) |
| 4 | dtlb_access | 15.048% | 8.770% | -8.770% | W_v28_int_alu_dense (112) |
| 4 | dtlb_miss | 19.071% | 14.733% | 14.717% | W_v28_pytorch_base (49078) |
| 4 | o3_iq_full | 54.632% | 50.900% | 17.393% | W_v28_pytorch_base (765556) |
| 4 | llc_tag_vs_functional_path | 0.606% | 0.767% | 0.767% | W_v28_marine_heldout (45456) |
| 8 | l1d_miss | 7.491% | 0.048% | -0.048% | W_v28_int_alu_dense (85) |
| 8 | private_l2_miss | 12.008% | 0.266% | -0.266% | W_v28_int_alu_dense (147) |
| 8 | cha_llc_lookup | 12.006% | 0.265% | -0.265% | W_v28_int_alu_dense (147) |
| 8 | branch_miss | 2.007% | 0.170% | -0.060% | W_v28_int_alu_dense (128) |
| 8 | dtlb_access | 15.825% | 8.799% | -8.799% | W_v28_int_alu_dense (230) |
| 8 | dtlb_miss | 18.058% | 14.737% | 14.722% | W_v28_pytorch_base (98185) |
| 8 | o3_iq_full | 1068.769% | 52.440% | 18.878% | W_v28_simd_sse_dense (48) |
| 8 | llc_tag_vs_functional_path | 0.610% | 0.769% | 0.769% | W_v28_marine_heldout (88796) |
| 16 | l1d_miss | 7.769% | 0.052% | -0.051% | W_v28_int_alu_dense (168) |
| 16 | private_l2_miss | 12.133% | 0.283% | -0.283% | W_v28_int_alu_dense (295) |
| 16 | cha_llc_lookup | 12.132% | 0.283% | -0.283% | W_v28_int_alu_dense (295) |
| 16 | branch_miss | 2.010% | 0.160% | -0.057% | W_v28_int_alu_dense (256) |
| 16 | dtlb_access | 16.005% | 8.805% | -8.805% | W_v28_int_alu_dense (456) |
| 16 | dtlb_miss | 17.553% | 14.738% | 14.723% | W_v28_pytorch_base (196355) |
| 16 | o3_iq_full | 761.709% | 54.068% | 20.401% | W_v28_simd_sse_dense (123) |
| 16 | llc_tag_vs_functional_path | 0.606% | 0.760% | 0.760% | W_v28_marine_heldout (170695) |
| 32 | l1d_miss | 8.112% | 0.059% | -0.055% | W_v28_int_alu_dense (336) |
| 32 | private_l2_miss | 12.217% | 0.290% | -0.290% | W_v28_int_alu_dense (592) |
| 32 | cha_llc_lookup | 12.213% | 0.290% | -0.290% | W_v28_int_alu_dense (592) |
| 32 | branch_miss | 1.987% | 0.153% | -0.054% | W_v28_int_alu_dense (512) |
| 32 | dtlb_access | 16.140% | 8.843% | -8.843% | W_v28_int_alu_dense (916) |
| 32 | dtlb_miss | 17.359% | 14.734% | 14.718% | W_v28_pytorch_base (392696) |
| 32 | o3_iq_full | 684.570% | 55.416% | 21.589% | W_v28_simd_sse_dense (285) |
| 32 | llc_tag_vs_functional_path | 0.579% | 0.702% | 0.702% | W_v28_marine_heldout (318738) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 34404 | 2221.53 | 1093.07 | 28672 | 103.09 | 23.232% | 124171 | 62244 | 0/0 | 0 | 424631 |
| 8 | 34901 | 4379.77 | 1093.84 | 57344 | 203.24 | 23.227% | 248119 | 124361 | 0/0 | 0 | 1336504 |
| 16 | 35983 | 8496.22 | 1096.96 | 114688 | 394.26 | 23.226% | 496103 | 247979 | 0/0 | 0 | 4531933 |
| 32 | 38050 | 16069.26 | 1101.64 | 229376 | 745.68 | 23.227% | 986426 | 493723 | 0/0 | 0 | 16169659 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
