# TSim

TSim is the standalone v25a self-trained Transformer experiment derived from
LLMSim.  It follows `docs/pre_v25a_self_trained_transformer_11m.md`: replace the
Qwen3-0.6B + LoRA backbone with an 8-layer, 320-wide, RoPE causal Transformer,
while keeping the v22 tokenizer, UopEncoder, PMU heads, loss, dataset cache, and
deployment eval path.

This tree is intentionally separate from `/data00/yinhaolang/LLMSim`.  Large
training and eval inputs under `data/` are symlinks to the existing LLMSim data
so the experiment can run without duplicating TB-scale traces.

## Environment

Use the existing Python 3.11 environment:

```bash
/data00/yinhaolang/infer/.venv/bin/python --version
```

The system `/usr/bin/python3` is Python 3.7 and does not have the required
`torch`, `transformers`, and `peft` packages.

## Smoke Check

```bash
cd /data00/yinhaolang/TSim
/data00/yinhaolang/infer/.venv/bin/python -m py_compile \
  model/tiny_transformer.py model/llm_wrapper.py train/train_lora.py \
  eval/eval_quota_cycles.py
```

## Train v25a

```bash
cd /data00/yinhaolang/TSim
bash scripts/run_v25a_tiny_transformer_11m.sh
```

The default run writes:

- `ckpt/v25a_tiny_transformer_8l320_8gpu_8000/`
- `logs/train_v25a.log`

## Eval v25a

```bash
cd /data00/yinhaolang/TSim
bash scripts/run_v25a_seedB_full_eval.sh
```

By default this evaluates c04/c08/c16 seedB.  To include c32:

```bash
CORES="04 08 16 32" bash scripts/run_v25a_seedB_full_eval.sh
```
