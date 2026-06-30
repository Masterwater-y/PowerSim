# t_hat_start Training Design

## Goal

LLMSim deployment cannot observe gem5 `commit_tick`. Any timing input used by the
model must be available from functional trace plus online model state. The timing
anchor should therefore be an estimated per-core start time:

```text
t_hat_start[c]
```

Deployment updates this state after each predicted window:

```text
t_hat_start[c] += pred_cpi_uop[c] * uops[c]
t_start_rel[c] = t_hat_start[c] - min_c(t_hat_start[c])
```

The training-side input should match this semantics as closely as possible.

## Old Behavior

The existing TQ windows stored:

```text
t_start_rel[c] = real_commit_start[c] - min_c(real_commit_start[c])
```

This is useful as an oracle timing-anchor ablation, but it is not deployment
pure: deployment does not know the current window's true start cycle.

## First-Version Training Behavior

For TQ training windows, use teacher-forced `t_hat_start`:

```text
initialize:
  t_hat_start[c] = 0

for accepted training windows in chronological TQ order:
  t_start_hat_rel[c] = t_hat_start[c] - min_c(t_hat_start[c])
  t_end_hat[c] = t_hat_start[c] + label_cpi_uop[c] * uops[c]
  t_end_hat_rel[c] = t_end_hat[c] - min_c(t_end_hat[c])

  write model input:
    t_start_rel = t_start_hat_rel
    timing attention/side features derived from t_start_rel

  update teacher-forced state:
    t_hat_start[c] = t_end_hat[c]
```

This still uses labels to advance time, but only through the same recurrence
shape used at deployment. It avoids feeding the current window's true
`commit_tick` start directly as model input.

`t_end_hat_rel` is written only for diagnostics in this first version. It is not
used as a model feature because this implementation derives it from the current
window label CPI, which would leak the current window latency. A later version
may add a deployment-available end/span estimate from the planner.

The raw oracle values are still retained for diagnostics:

```text
t_start_real_rel
t_end_real_rel
t_start_hat_rel
t_end_hat_rel
tstart_source
```

## Modes

`data/build_windows.py` supports:

```text
--tstart-source teacher_forced  # default for new TQ datasets
--tstart-source commit_tick     # old oracle-relative behavior
--tstart-source zero            # ablation; equivalent to no timing variation
```

`t_start_rel` is no longer fed through a standalone `tstart_proj`. It is
materialized into attention-visible feature tokens and side-tensor calibration
features:

```text
global attention:
  GF_TIME_START_SKEW

per-core attention:
  CF_TIME_START_REL
  CF_TIME_LAG_TO_LEADER
  CF_TIME_RANK

per-core side tensor:
  log1p_t_start_rel
  log1p_t_start_skew
  log1p_t_lag_to_leader
  t_start_rank
```

The old `--use-tstart/--no-use-tstart` training flags are compatibility no-ops.
For ablation, rebuild the dataset with:

```text
--tstart-source zero
```

## Limitations

The first version computes teacher-forced state over the accepted sampled TQ
windows. This is much closer to deployment than direct `commit_tick`, but it is
not yet full scheduled sampling. A later version can mix in model predictions:

```text
t_hat_start += mix(label_cpi_uop, pred_cpi_uop) * uops
```

It can also add deployment-available end/overlap estimates later if the planner
passes a pre-window span estimate that does not use the current window label.
