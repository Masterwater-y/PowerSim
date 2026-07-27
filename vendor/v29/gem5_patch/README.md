# TCSim v29 gem5 patch bundle

本目录把 v29 采集所需的 gem5 修改收敛为一个可校验 overlay。目标基线固定为 gem5
`v25.1.0.1`（commit `c8222cc67a399bfc01e8658dd14b30d5bfd634f9`）。

主要修改：

- `TaoTrace` 输出 functional records、oracle labels、memory events 与逐核 ROI boundary；
- branch 字段来自退休后的真实控制流，不使用 predictor speculative GHR；
- `m5_work_begin/end` 在早退出前通知 TaoTrace，逐核打开/关闭 emit gate；
- `BranchEvents` 提供 predictor 事件探针；
- MESI Three-Level 的两个 L0/L1 message buffer 关闭全局 `ordered` 限制；
- build option 固定 X86 + Ruby + `MESI_Three_Level`；
- `run_mt_mvp.py` 生成 schema-v2 `uarch_profile.json`，支持 Atomic fast-forward、
  O3+Ruby 切换、8-bank LLC、8-channel DDR4 和完成核 quiesce。

准备原始 gem5：

```bash
git clone --branch v25.1.0.1 --depth 1 https://github.com/gem5/gem5.git ../gem5
```

应用并构建：

```bash
JOBS=64 PYTHON=/usr/bin/python3.11 \
  bash vendor/v29/gem5_patch/apply_and_build.sh ../gem5
```

只应用或只构建：

```bash
MODE=apply bash vendor/v29/gem5_patch/apply_and_build.sh ../gem5
MODE=build bash vendor/v29/gem5_patch/apply_and_build.sh ../gem5
```

脚本先核对 gem5 commit 和 `SHA256SUMS`，再复制 overlay。overlay 会覆盖目标中的同名
文件，所以只应对专门用于 TCSim 的精确基线 checkout 使用。重复执行是幂等的。

构建产物：

```text
../gem5/build/X86_MESI_Three_Level/gem5.opt
```

最小启动测试见 [部署与使用文档](../../../docs/v29/deployment_and_usage.md)。
