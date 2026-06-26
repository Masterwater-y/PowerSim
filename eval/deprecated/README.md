# Deprecated eval entrypoints

These scripts are kept only for historical reference.

- `eval.py`: fixed-window dataset/cache validation. It does not simulate the
  quota-cycle deployment loop.
- `eval_cycles.py`: legacy global CPI aggregation that weights `cpi_uop` by
  `instr_retired`; this is not the current v7 evaluation contract.

For current validation use:

```bash
CKPT=ckpt/v7_c08_absmiss_ddp8 bash scripts/eval_parallel.sh
```

The active entrypoint is `eval/eval_quota_cycles.py`.
