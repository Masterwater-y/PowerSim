# TCSim v29 部署与使用文档

本目录是 TCSim 当前稳定主线的入口。版本选择固定为 **v29 E0 packed3**：

```text
config      configs/v29_100m.yaml
dataset     data/v29_global_time_dataset/manifest.json
checkpoint  ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt
best step   59000
schema      tcsim-v29-checkpoint-3
```

截至 2026-07-27，long-history E1 和 frozen-memory E2 是诊断实验，整体结果没有稳定超过
E0 packed3，不能替换本文默认 checkpoint。v30 branch replay 仍是后续实验，也不属于
本部署合同。

推荐阅读顺序：

1. [部署与使用](deployment_and_usage.md)：从 gem5、workload、采集、cache、训练到推理的完整命令。
2. [系统与模型设计](architecture.md)：数据合同、特征、QKVR、loss、全局时钟 rollout。
3. [运维与排障](operations.md)：目录、检查点、恢复、验收、常见故障和版本边界。
4. [外部源码来源](../../vendor/v29/PROVENANCE.md)：从 TSim、taogen、gem5、LLMSim 复制的快照。

现有深入设计文档仍保留在 `docs/`：

- `v29_global_time_prefix_progress_design.md`
- `v28_1_functional_feature_and_trace_contract.md`
- `inference_framework_standard.md`
- `v29_packed3_checkpoint_evaluation_report.md`
- `v29_context_optimization_plan.md`
- `v29_single_trace_multi_gpu_window_parallel.md`

快速检查已有环境：

```bash
cd /data00/yinhaolang/TCSim
/data00/yinhaolang/infer/.venv/bin/python -m pytest \
  tests/test_v29.py tests/test_v29_inference.py -q

CKPT=ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt \
MANIFEST=data/v29_global_time_dataset/manifest.json \
SPLITS=seed0_inference MODE=free CORE_COUNTS=4 \
MAX_FREE_STEPS=10 GPUS=0 \
bash scripts/run_v29_eval_8gpu.sh
```
