# TCSim v28.1 multicore FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / p99 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 23 | 10.485% / 8.536% / 21.022% / 24.447% / 24.968% | 3.221% | 10.484% |
| 8 | 23 | 10.877% / 8.098% / 22.250% / 32.449% / 35.232% | 2.749% | 10.866% |
| 16 | 23 | 12.244% / 9.908% / 21.919% / 35.490% / 38.172% | 6.205% | 12.179% |
| 32 | 23 | 14.775% / 14.208% / 22.275% / 54.478% / 63.165% | 12.588% | 15.406% |

## Acceptance gates

CPI P99 and throughput are gated independently for each core count.

| Cores | CPI P99 (<=10%) | Min UOP/s (>=5M) | Slowest workload | Pass |
|---:|---:|---:|---|:---:|
| 4 | 24.447% | 7.258M | W_v28_memory_random_mlp | FAIL |
| 8 | 32.449% | 6.846M | W_v28_memory_random_mlp | FAIL |
| 16 | 35.490% | 6.185M | W_v28_memory_random_mlp | FAIL |
| 32 | 54.478% | 5.564M | W_v28_memory_random_mlp | FAIL |

## Throughput and conservation

Throughput is simulator-only wall time. Mismatch columns count failed cases.

| Cores | Min / P10 / median UOP/s | Median MIPS | UOP mismatch | Memory-event mismatch | Private/escape mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 7.258M / 9.384M / 10.026M | 7.929 | 0 | 0 | 0 |
| 8 | 6.846M / 9.132M / 9.628M | 7.614 | 0 | 0 | 0 |
| 16 | 6.185M / 8.643M / 9.117M | 7.142 | 0 | 0 | 0 |
| 32 | 5.564M / 7.775M / 8.434M | 6.582 | 0 | 0 | 0 |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 16 | 10.606% / 9.083% / 24.968% | 2.821% |
| 4 | mechanism | 9 | 10.538% / 11.203% / 22.597% | 2.247% |
| 4 | business_base | 7 | 10.693% / 8.368% / 24.968% | 3.559% |
| 4 | heldout | 7 | 10.208% / 8.536% / 21.254% | 4.135% |
| 8 | train_base | 16 | 11.700% / 8.368% / 35.232% | 2.498% |
| 8 | mechanism | 9 | 13.273% / 9.991% / 35.232% | 1.607% |
| 8 | business_base | 7 | 9.678% / 8.098% / 21.123% | 3.643% |
| 8 | heldout | 7 | 8.996% / 7.307% / 19.858% | 3.322% |
| 16 | train_base | 16 | 13.281% / 10.416% / 38.172% | 6.287% |
| 16 | mechanism | 9 | 15.640% / 16.796% / 38.172% | 6.022% |
| 16 | business_base | 7 | 10.246% / 9.908% / 14.691% | 6.628% |
| 16 | heldout | 7 | 9.876% / 9.634% / 13.510% | 6.016% |
| 32 | train_base | 16 | 15.356% / 13.914% / 63.165% | 12.484% |
| 32 | mechanism | 9 | 15.948% / 12.279% / 63.165% | 11.790% |
| 32 | business_base | 7 | 14.595% / 15.178% / 23.678% | 13.377% |
| 32 | heldout | 7 | 13.445% / 14.208% / 21.165% | 12.826% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 6.706% | 0.043% | -0.043% | W_v28_int_alu_dense (42) |
| 4 | private_l2_miss | 11.767% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | cha_llc_lookup | 11.767% | 0.239% | -0.239% | W_v28_int_alu_dense (73) |
| 4 | branch_miss | 2.028% | 0.165% | -0.072% | W_v28_int_alu_dense (64) |
| 4 | dtlb_access | 15.048% | 8.770% | -8.770% | W_v28_int_alu_dense (112) |
| 4 | dtlb_miss | 19.071% | 14.733% | 14.717% | W_v28_pytorch_base (49078) |
| 4 | o3_iq_full | 59.489% | 55.325% | 25.500% | W_v28_pytorch_base (765556) |
| 4 | llc_tag_vs_functional_path | 0.606% | 0.767% | 0.767% | W_v28_marine_heldout (45456) |
| 8 | l1d_miss | 7.491% | 0.048% | -0.048% | W_v28_int_alu_dense (85) |
| 8 | private_l2_miss | 12.008% | 0.266% | -0.266% | W_v28_int_alu_dense (147) |
| 8 | cha_llc_lookup | 12.006% | 0.266% | -0.266% | W_v28_int_alu_dense (147) |
| 8 | branch_miss | 2.007% | 0.170% | -0.060% | W_v28_int_alu_dense (128) |
| 8 | dtlb_access | 15.825% | 8.799% | -8.799% | W_v28_int_alu_dense (230) |
| 8 | dtlb_miss | 18.058% | 14.737% | 14.722% | W_v28_pytorch_base (98185) |
| 8 | o3_iq_full | 2451.440% | 57.315% | 26.658% | W_v28_simd_sse_dense (48) |
| 8 | llc_tag_vs_functional_path | 0.610% | 0.769% | 0.769% | W_v28_marine_heldout (88796) |
| 16 | l1d_miss | 7.769% | 0.052% | -0.051% | W_v28_int_alu_dense (168) |
| 16 | private_l2_miss | 12.133% | 0.283% | -0.283% | W_v28_int_alu_dense (295) |
| 16 | cha_llc_lookup | 12.132% | 0.283% | -0.283% | W_v28_int_alu_dense (295) |
| 16 | branch_miss | 2.010% | 0.160% | -0.057% | W_v28_int_alu_dense (256) |
| 16 | dtlb_access | 16.005% | 8.805% | -8.805% | W_v28_int_alu_dense (456) |
| 16 | dtlb_miss | 17.553% | 14.738% | 14.723% | W_v28_pytorch_base (196355) |
| 16 | o3_iq_full | 1732.442% | 58.655% | 28.726% | W_v28_simd_sse_dense (123) |
| 16 | llc_tag_vs_functional_path | 0.606% | 0.760% | 0.760% | W_v28_marine_heldout (170695) |
| 32 | l1d_miss | 8.113% | 0.059% | -0.055% | W_v28_int_alu_dense (336) |
| 32 | private_l2_miss | 12.218% | 0.290% | -0.290% | W_v28_int_alu_dense (592) |
| 32 | cha_llc_lookup | 12.215% | 0.290% | -0.290% | W_v28_int_alu_dense (592) |
| 32 | branch_miss | 1.987% | 0.153% | -0.054% | W_v28_int_alu_dense (512) |
| 32 | dtlb_access | 16.140% | 8.843% | -8.843% | W_v28_int_alu_dense (916) |
| 32 | dtlb_miss | 17.359% | 14.734% | 14.718% | W_v28_pytorch_base (392696) |
| 32 | o3_iq_full | 1531.843% | 60.300% | 29.850% | W_v28_simd_sse_dense (285) |
| 32 | llc_tag_vs_functional_path | 0.579% | 0.702% | 0.702% | W_v28_marine_heldout (318738) |

## Interval/order audit

Reordered pairs compare lower-bound weave order with the dependency-feedback order. Same-line pairs are potential path-changing conflicts and are not certified as exact.

| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | Memory events/step | Escape % | In-flight mem UOP | Horizon failures | State/timing cert failures | Replay events | Same-line pairs |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 17096 | 4470.60 | 2208.62 | 57344 | 207.46 | 23.232% | 61514 | 31324 | 0/0 | 0 | 596674 |
| 8 | 17448 | 8760.79 | 2201.78 | 114688 | 406.54 | 23.227% | 124215 | 62817 | 0/0 | 0 | 2074569 |
| 16 | 18284 | 16720.59 | 2184.68 | 229376 | 775.91 | 23.226% | 250931 | 126728 | 0/0 | 0 | 7562556 |
| 32 | 19778 | 30914.93 | 2182.08 | 458752 | 1434.58 | 23.227% | 501452 | 253647 | 0/0 | 0 | 28008922 |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
