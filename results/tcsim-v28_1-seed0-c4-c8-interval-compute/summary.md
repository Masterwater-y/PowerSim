# TCSim v28.1 seed0 C4/C8 FastSim validation

All errors below are workload-equal absolute relative errors.

| Cores | Workloads | UOP CPI mean / median / p90 / max | signed bias | per-core CPI MAPE |
|---:|---:|---:|---:|---:|
| 4 | 4 | 8.272% / 8.156% / 16.687% / 16.687% | -0.194% | 8.272% |
| 8 | 4 | 8.281% / 8.161% / 16.699% / 16.699% | -0.208% | 8.281% |

## CPI by domain

| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |
|---:|---|---:|---:|---:|
| 4 | train_base | 4 | 8.272% / 8.156% / 16.687% | -0.194% |
| 4 | mechanism | 4 | 8.272% / 8.156% / 16.687% | -0.194% |
| 8 | train_base | 4 | 8.281% / 8.161% / 16.699% | -0.208% |
| 8 | mechanism | 4 | 8.281% / 8.161% / 16.699% | -0.208% |

## PMU count error

Trace-equal MAPE exposes low-count workloads; WAPE is total absolute count error divided by the total gem5 count.

| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |
|---:|---|---:|---:|---:|---|
| 4 | l1d_miss | 38.135% | 40.299% | -40.299% | W_v28_int_alu_dense (42) |
| 4 | private_l2_miss | 65.465% | 66.527% | -66.527% | W_v28_int_alu_dense (73) |
| 4 | cha_llc_lookup | 65.465% | 66.527% | -66.527% | W_v28_int_alu_dense (73) |
| 4 | branch_miss | 4.792% | 0.024% | -0.008% | W_v28_int_alu_dense (64) |
| 4 | llc_tag_vs_functional_path | 0.000% | 0.000% | 0.000% | W_v28_int_alu_dense (5) |
| 8 | l1d_miss | 41.293% | 42.612% | -42.612% | W_v28_int_alu_dense (85) |
| 8 | private_l2_miss | 65.873% | 66.600% | -66.600% | W_v28_int_alu_dense (147) |
| 8 | cha_llc_lookup | 65.873% | 66.600% | -66.600% | W_v28_int_alu_dense (147) |
| 8 | branch_miss | 4.792% | 0.024% | -0.008% | W_v28_int_alu_dense (128) |
| 8 | llc_tag_vs_functional_path | 0.000% | 0.000% | 0.000% | W_v28_int_alu_dense (7) |

Per-workload values and signed errors are in `summary.csv` and `summary.json`. LLC tag miss versus Ruby protocol demand miss is diagnostic, not an accepted tag-state accuracy metric.
