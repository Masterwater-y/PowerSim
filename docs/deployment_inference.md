# TCSim fixed-chunk deployment inference

`scripts/infer_deployment.py` is the deployment/free-running evaluator.  It is
separate from `scripts/eval_mvp.py`: the latter replays oracle-selected training
contexts, while deployment inference never reads `rollout.jsonl`.

## Runtime contract

For each scheduler step the runner loads at most one current fixed-K chunk per
active core and performs one full-QKVR forward over the complete active-core
context.  Model outputs are latched only for newly loaded chunks:

- `delta_hat = pred_cpi * n_uops`
- `branch_miss_hat = pred_branch_miss_prob * n_retired_branch`
- `E_pred = T_pred + delta_hat`

A resident chunk remains in later cross-core contexts, but later forwards do
not overwrite its latched output.  Its cycles and branch misses are accumulated
exactly once, when epsilon scheduling commits that chunk.  A scheduler-only
step with no newly loaded chunks does not call the model because no output can
legally be relatched.

The model receives functional packed fields, masks, summaries, current-context
relations and uarch features.  It does not receive true ticks, labels, predicted
cycles, resident flags or exposure counts.

## Cache semantics

The last two UOP fields (`xcore_role`, `xcore_fanout`) depend on the current
resident context.  Static tokens are therefore cached with a cross-core field
signature in addition to trace/core/chunk/uarch/checkpoint identity.  This is a
bounded exact cache; a chunk is never reused under a different functional
context signature.  Dynamic Q/K/V/R projections are recomputed because deeper
layer states depend on the current peer chunks.

## Seed1 workflow

If seed1 has not yet been collected, prepare its 16 deployment workloads at
c01/c04/c08/c16/c32 and build their packed caches:

```bash
bash scripts/prepare_v28_seed1_deployment_cache.sh
```

Then run all 80 seed1 traces over eight local GPUs:

```bash
bash scripts/run_v28_seed1_deployment_eval.sh
```

The launcher exports a 382 MiB inference-only checkpoint from the 1.2 GiB
training checkpoint, assigns independent traces to GPUs, and merges shard
reports.  The final report includes chunk CPI MAPE, aggregate/prefix/endpoint/
makespan cycle error, exact-once counts, branch-miss error, resident exposure,
and predicted-vs-oracle scheduler disagreement.

## Metric and logging levels

The deployment log uses explicit names because a scheduler window contains a
variable number of committed per-core chunks:

- `chunk CPI MAPE`: one error per committed per-core fixed-K chunk; resident
  repetitions are not counted again.
- `scheduler-window CPI MAPE`: aggregate predicted/label cycles divided by
  aggregate UOPs for the chunks committed in one epsilon scheduler step.
- `per-core ROI UOP-CPI error`: complete per-core predicted/label cycle sums
  divided by that core's ROI micro-ops; the JSON report stores every core
  separately for diagnosis.
- `ROI UOP-CPI error`: sum of cycle advances over all cores divided by the sum
  of micro-ops over all cores.  Cores are pooled before division; this is not
  an equal-core average.  Macro-instruction CPI uses the same cycle numerator
  with the count of macro-instruction heads as its denominator.
- `branch miss rate`: total predicted/label misses over all retired control-flow
  instructions divided by all retired control-flow instructions. Conditional,
  direct/indirect, call and return branches share this denominator. Predicted
  misses are latched as probability times retired-branch opportunities and
  counted exactly once at commit.
  Count-relative and rate-relative errors are mathematically identical because
  they share the same opportunity denominator.  New JSON uses the canonical
  `branch_miss_relative_error`; the old count/rate-relative names are retained
  as exact compatibility aliases. Reports use one branch-relative
  error and retain counts, rates, and absolute percentage-point delta.

Every 200 scheduler windows by default, logs show running ROI CPI, progress,
UOP/s, active/new/committed/resident rows and average model-forward latency.
Every completed workload prints its full metric block, throughput, timing,
cache statistics and CUDA peak memory.  `<report>.traces.jsonl` is fsynced after
each workload; `--resume` skips only rows produced by the same checkpoint and
same trace length, so a killed shard does not lose completed workloads.

For a bounded smoke test, set `MAX_TRACES` and `MAX_CHUNKS_PER_CORE`; both are
zero by default and therefore do not truncate full evaluation.
