# scripts/archive

历史诊断/实验脚本归档。这些脚本：

- 当时为了验证某个一次性假设而写，结论已经吸收进主代码或文档
- 量纲（cpi_macro / instr_retired 等）与 v7_cpi_uop 数据集**不兼容**
- 不再维护，import 路径可能已腐烂

需要复用时**先拷回**到 `scripts/` 并按当前 PMU_KEYS / label schema 适配。

| 文件 | 原用途 | 结论去向 |
|------|--------|----------|
| analyze_v6_distribution.py | v6 训练集 35-d 特征分布 | v7 重采后改用 dataset_audit.py |
| diagnose_v6_1_distribution.py | v6.1 self-NN / OOD 维度诊断 | 功能并入 ood_holdout_scan.py |
| compare_warm_state_proxy.py | history-only warm-state proxy 实验 | 已并入 build_windows 主路径 |
| diagnose_cpi_outliers.py | CPI 残差离群窗诊断 | 功能并入 dataset_audit.py 难样本部分 |
| oracle_per_op_diag.py | P0 per-op oracle vs gem5 真值 | 验证完成，结论文档化 |
| oracle_warmup_ab.py | shared_system 真值队列 warmup A/B | 已选定方案并固化 |
| pmu_layers_diag.py | PMU 全栈分层 oracle vs real | 验证完成，结论文档化 |
| verify_opclass_pipeline.py | op_class 解析与 tokenizer 一致性 | 已通过单测固化 |
| eval_addr_features_vs_cpi.py | 地址流特征对窗 CPI 解释力 | 结论已得，cpi_macro 量纲不适配 v7 |
| downsample_workload.py | windows.jsonl 按 workload 下采样 | v7 重采后用不到 |
| _inspect_tstart.py | 一次性查看 t_start/CPI 抽样 | 调试一次性脚本 |
