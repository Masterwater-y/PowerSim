# TCSim

> **当前稳定主线（2026-07-27）：v29 E0 packed3。** 完整部署、gem5 patch、workload、
> 数据采集、cache、模型、训练与推理说明见
> [`docs/v29/README.md`](docs/v29/README.md)。外部 TSim/taogen/LLMSim 依赖的源码快照已
> 归档到 [`vendor/v29/`](vendor/v29/PROVENANCE.md)。以下内容保留为 v27.2 历史说明。

# v27.2 Oracle-First Fixed-Chunk MVP（历史）

> **Implementation status (2026-07-13):** the code implements the v27.2
> oracle-context / functional-only contract.  The active workload contract is
> **v27.0-cold16**; superseded raw-v27 data has been removed.

This repository targets the plan described in
`/data00/yinhaolang/TSim/docs/v27_fixed_chunk_resident_mvp_plan.md`.

The current oracle-first training contract, functional-only model interface,
and corrected per-core loss design are specified in
[`docs/v27_2_oracle_first_functional_training_and_per_core_loss.md`](docs/v27_2_oracle_first_functional_training_and_per_core_loss.md).
The active workload and collection contract is in
[`docs/v27_0_cold16_workload_contract.md`](docs/v27_0_cold16_workload_contract.md).
The older raw-v27 audit remains historical context only.

**Key semantics (see plan §0):**

- Fixed functional chunk of `K` UOPs (default `K=256`).
- Each active core emits its current chunk; slow cores may stay **resident**.
- Scheduling decision uses predicted end-time skew:
  `E_pred[c] > E_min + epsilon` → resident; else fast.
- Cursor / cycle / loss are committed **exactly once** per chunk.
- Chunk boundaries are decided by functional UOP index only — never by
  real or predicted time.
- Main supervision is `delta_cycles`; `log_cpi = log(delta_cycles / n_uops)`
  is preserved as a compatible head.
- Oracle timing is serialized only as `audit_*` scheduler metadata and is not
  accepted by the model forward path.
- Functionally indistinguishable cores are grouped before centered/listwise
  losses, so loss cannot invent a core identity.

**Repo layout:**

```
tcsim/            Python package
  chunker/        Phase 0 — fixed-K functional chunk builder
  scheduler/      Phase 1 — epsilon resident scheduler
  dataset/        Phase 2 — rollout generator, label join, torch dataset
  model/          Phase 3 — static encoder + dynamic interact + heads
  train/          Phase 3 — losses + training loop
  eval/           Eval driver
  utils/          io + logging helpers
scripts/          Runnable one-shot scripts
tests/            unit + smoke tests
configs/          YAML/JSON knobs for K, epsilon, uarch, tokenizer
data/             generated artifacts (chunks.parquet, rollout.jsonl, labels.parquet, ...)
docs/             design notes (mirrors TSim plan for offline reading)
```

**Quick smoke test:**

```bash
python3 scripts/smoke_end_to_end.py
```

Generates a tiny synthetic trace, runs Phase 0 → Phase 3 in ≈10 s and
prints headline metrics. This is the fastest way to verify the full
pipeline is wired end-to-end.

**Real run (once TSim raw traces are present):**

```bash
bash scripts/run_mvp.sh \
    --raw /data00/yinhaolang/TSim/data/raw_v27_0_cold16_seed0_c04 \
    --workloads W_chase_DRAM,W_stream_seq_DRAM \
    --out /data00/yinhaolang/TCSim/data/mvp_run
```

**Audit and strict cold16 dataset plan:**

```bash
python3 scripts/audit_v27_raw_dataset.py \
  --sample-regions 9 --sample-uops-per-core 8192 \
  --out data/v27_raw_audit.json

python3 scripts/build_v27_dataset.py \
  --raw-root-glob '/data00/yinhaolang/TSim/data/raw_v27_0_cold16_seed*_c*' \
  --out data/v27_0_cold16_dataset \
  --input-format aligned \
  --audit-report data/v27_raw_audit.json
```

The second command writes a trace-level manifest.  `--build` materializes
mmap-friendly packed caches, but strict mode refuses to build while audit
blockers remain.  Training accepts this explicit manifest via
`scripts/train_mvp.py --manifest ...`; it does not make a random validation
split.

**MVP scope (this repo):**

- Phase 0 — fixed-K chunk builder → packed mmap cache (Parquet remains a small-run compatibility format).
- Phase 1 — epsilon resident scheduler (`E_min + epsilon`).
- Phase 2 — rollout cache (`rollout.jsonl`) joined with `labels.parquet`.
- Phase 3 — static chunk encoder + functional cross-core interaction + log-CPI
  head + identifiable absolute/centered/listwise losses.

**MVP explicitly out of scope** (see plan §6 Phase 4): full temporal
graph, precise MESI / atomic solvers, chunk-level query transformer,
multi-fast-chunk microbatch readout.
