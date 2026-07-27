# TCSim v29 external snapshot

本目录让 v29 复现不再隐式依赖同级的 TSim、LLMSim、taogen 和已修改 gem5 工作树。

```text
gem5_patch/              当前 v29 TaoTrace/BranchEvents/ROI overlay、配置与构建脚本
trace_collection/        v28 business workload 源码、采集器、Parquet 对齐器
llmsim_semantic_adapter/ 可选 LLMSim 语义/LoRA 研究快照
references/              TSim/taogen 的历史设计与对齐报告
PROVENANCE.md            源路径、Git 基线、工作树差异和版本选择
```

从 [v29 部署文档](../../docs/v29/deployment_and_usage.md) 开始使用。生成的 gem5、workload
二进制、raw trace、cache 和 checkpoint 不放在本目录，也不应提交到 Git。
