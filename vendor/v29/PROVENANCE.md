# v29 外部源码归档来源

归档日期：2026-07-27。这里保存的是 TCSim v29 复现所需的外部源码快照，不保存
raw trace、tensor cache、checkpoint、模型权重或 workload 二进制。

| 归档 | 原路径 | Git 基线 | 说明 |
|---|---|---|---|
| `gem5_patch/overlay` | `/data00/yinhaolang/gem5` | gem5 `v25.1.0.1`，`c8222cc67a399bfc01e8658dd14b30d5bfd634f9` | 取自实际工作树；包含未提交的 TCSim trace patch |
| `gem5_patch/config`、`shared` | `/data00/yinhaolang/taogen` | `c469efe2abc803456fd7f2b0c8f90a4e9ae022b9` | `run_mt_mvp.py` 与共享地址/缓存解码头 |
| `trace_collection`、`references/tsim` | `/data00/yinhaolang/TSim` | `a73275cd107fedac16b2ee52c445ced659678b3e` | v28 workload、采集/对齐脚本和早期设计 |
| `references/taogen` | `/data00/yinhaolang/taogen` | `c469efe2abc803456fd7f2b0c8f90a4e9ae022b9` | gem5/PMU 对齐记录 |
| `llmsim_semantic_adapter` | `/data00/yinhaolang/LLMSim` | `bd6d42b3e17d9137bc2a9ee672e05c0d1a3ee466` | 工作树快照；可选语义适配实验，不是基线 v29 |

重要差异：taogen 仓库中已提交的 `gem5_patches/tao_trace.*` 早于当前训练数据合同，
缺少逐核 ROI gate 和真实 committed `branch_taken/target/next_pc/history`。因此本归档的
gem5 overlay 取自当前实际 gem5 工作树，并用 `gem5_patch/SHA256SUMS` 固定内容。

外部仓库均可能包含本表 Git 基线之后的未提交修改。归档按文件内容复现，不把 Git
commit 错写为完整工作树版本。重新同步时应先比较 SHA 和合同版本，不能静默覆盖。
