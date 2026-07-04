# v20 local-core direct CPI + fixed loss

## Motivation

v19 local-core made the backbone see per-core local sequences, but the final
CPI outputs still collapsed toward the window mean.

Observed on `ckpt/v19_local_core_delta_8gpu_8000/step_005000` for c8
`W_ads_ranking_proxy`:

- local backbone query cosine mean: about `0.868`
- pre-adapter cosine mean: about `0.994`
- post-adapter cosine mean: about `0.984`
- label CPI CV mean: about `0.50`
- predicted CPI CV mean: about `0.15`

This means the local encoder is not the primary failure point. It already
separates cores better than the v18 global-tail query path. The failure is that
projection, cross-core adapter, CPI head, and loss let the output shrink toward
the per-window mean.

## Scope

v20 is a focused ablation. It does not add uops-weighted CPI loss.

Changes:

1. Use direct CPI head.
2. Disable rank, slowest, and fastest training losses.
3. Use fixed task weights instead of uncertainty weighting.
4. Replace hard-gated spread loss with soft always-on spread calibration.

## CPI Head

Use direct per-core CPI prediction:

```text
pred_log_cpi_i = head(h_i)
```

Do not use the v19 delta form:

```text
base = head(mean(h_all))
delta_i = head(h_i) - mean(head(h_all))
pred_log_cpi_i = base + delta_i
```

The delta form makes it too easy to learn a good window-level base and an
underpowered per-core delta. Direct CPI head is the cleanest test of whether the
local-core hidden state contains enough per-core information.

## Loss

Use fixed weights:

```text
L =
  lambda_cpi_abs       * L_abs_log_cpi
+ lambda_delta         * L_delta_log_cpi
+ lambda_spread        * L_spread
+ lambda_cycles_window * L_window_cycles
+ lambda_aux_pmu       * L_aux_pmu
+ lambda_inv           * L_inv
+ lambda_phys          * L_phys
```

Default v20 weights:

```text
lambda_cpi_abs       = 1.0
lambda_delta         = 4.0
lambda_spread        = 0.8
lambda_cycles_window = 0.5
lambda_aux_pmu       = 0.05
lambda_rank          = 0.0
lambda_slowest       = 0.0
lambda_fastest       = 0.0
```

`L_aux_pmu` is the mean of non-CPI PMU losses. It remains an auxiliary signal,
not the main objective.

## Delta Loss

`L_delta` supervises each core's relative CPI offset inside the window:

```text
p_i = pred_log_cpi_i
y_i = log(label_cpi_i)

p_delta_i = p_i - mean_active(p)
y_delta_i = y_i - mean_active(y)

L_delta = Huber(p_delta_i - y_delta_i)
```

This directly targets the v19 failure mode where the average CPI is acceptable
but the per-core spread is too small.

## Soft Spread Loss

Use an always-on spread loss over active cores:

```text
std_p = std_active(pred_log_cpi)
std_y = std_active(label_log_cpi)

L_spread = SmoothL1(log(std_p + eps), log(std_y + eps))
```

Window weight:

```text
w = clamp(std_y / spread_ref, spread_weight_min, spread_weight_max)
```

Default:

```text
spread_ref        = 0.10
spread_weight_min = 0.25
spread_weight_max = 3.0
```

Low-spread windows still constrain the model not to invent large differences.
High-spread windows get more weight so the model cannot keep predicting a
near-uniform CPI vector.

## Metrics To Watch

Do not judge v20 by total training loss alone. Track:

```text
pred_cpi_cv / label_cpi_cv
pred_cpi_range_rel / label_cpi_range_rel
per-core CPI relative error
slowest_hit_rate
fastest_hit_rate
window_cycles_relerr
online alignment agg_relerr
```

Expected sign of improvement:

- `pred_cpi_cv / label_cpi_cv` should move upward from v19's roughly `0.3`.
- Online pred planner should stop treating all cores as nearly equal.
- Fastest/slowest hit rate should improve even though those are no longer
  training losses.

## Non-goals

- No uops-weighted CPI loss in this version.
- No increase in adapter depth as the first fix.
- No hard fastest/slowest classification loss.
- No learned uncertainty weights for CPI/cycles in this ablation.
