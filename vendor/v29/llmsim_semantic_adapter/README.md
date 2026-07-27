# LLMSim × TCSim v29 语义适配实验快照

本目录归档 LLMSim 中直接依赖 `tcsim.v29` 的语义 sidecar、B2/E2、LoRA-v29 训练和部署
物化代码，避免设计与实现只存在于项目外。它是可选研究扩展，不是 TCSim v29 packed3
基线的必需依赖，也没有被搬进 `tcsim/` Python 包。

选择基线的理由：截至 2026-07-24，原始 E0 packed3 `best.pt@59000` 仍是稳定主线。
long-history 全局残差明显损害多数 workload；frozen memory probe 在 120 traces 上只把
平均相对误差从 6.253% 改到 6.193%，不足以替换基线。LLMSim 语义/LoRA 路径还依赖本地
Qwen 权重、semantic cache 和 LLMSim 的运行环境，应单独验收。

归档内容保留原 LLMSim import 布局。若要运行，应把本目录作为一个 LLMSim 风格根目录，
补齐对应 Qwen 权重和配置，并把 TCSim 项目根加入 `PYTHONPATH`。不要直接在标准 v29
checkpoint 上调用这些入口。

设计与实验结论：

- `docs/llmsim_tcsim_v29_semantic_adapter_design.md`
- `docs/llm_simulation_experiment_retrospective_20260723.md`

代码入口：

- `data/build_tcsim_v29_lorav29_semantic_cache.py`
- `scripts/train_tcsim_v29_b2e2.py`
- `scripts/train_tcsim_v29_lora_v29.py`
- `scripts/infer_tcsim_v29_b2e2.py`
- `scripts/materialize_tcsim_v29_lorav29_deployment.py`

这些文件是工作树内容快照，来源说明见 `../PROVENANCE.md`。
