# infer — TAO 推理与验证子模块

> 本子模块涉及的 functional 输入边界、ref_sim 拼接输入、单步推理输出、
> driver 契约与 ckpt 兼容口径，统一以
> [global/SCHEMA.md](SCHEMA.md) 为准。
> 若本文档与 `global/SCHEMA.md`、当前生产 ckpt 或实际代码实现冲突，以后者为准。

## 范围

`infer` 负责两类流程：

- 部署侧推理
  - 从 `records.micro` 投影出 `functional.core<N>.parquet`
  - 通过 driver + ref_sim + 模型 ckpt 做端到端预测

- 验证侧推理
  - 将 functional 输入与 ref_sim 输出拼接为 strict 推理输入
  - 运行单步模型推理
  - 与 oracle label 对比并生成报告

## 关键目录

- `functional_trace/`
  - functional / labels 边界定义与投影逻辑
- `driver/`
  - 端到端调度、ref_sim 客户端、reference clock
- `ml/`
  - strict 推理脚本与推理侧模型定义
- `mesi_ref_sim/`
  - C++ ref_sim 实现
- `docs/`
  - 过程性分析与优化记录

## 统一入口

开始阅读本子模块前，建议先看：

- [SCHEMA.md](SCHEMA.md)
- [README.md](docs/00-global-overview.md)
