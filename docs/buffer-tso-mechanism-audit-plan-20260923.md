# Buffer TSO 机制排查与实施计划

> 执行方式：superpowers:executing-plans，在当前会话逐项实施。用户已明确授权设计并实施。

**目标：** 建立 gem5/FastSim 同事件的证据链，按首个机制分歧修复，不以 CPI 接近程度代替正确性。

**设计：** 复用有界原生采集与 FastSim frontier audit；保留两端原始身份和各自时钟，先做输入与边界门，再比较生命周期。native 缺失的 prepare 状态标记 unavailable，不用推测补齐。物理时序修改必须有事件见证，审计本身不得改变预测。

**实现技术：** C++17 现有模拟器审计字段、Python 标准库采集与配对工具、原生 Ruby sideband 和同次 config.ini。

**依据：** 用户要求机制与 gem5 对齐，即使 CPI 退化也不否决正确机制；当前范围和已知近似见 `docs/buffer-tso-head-demand-only-20260923.md`。

## 约束和验收

- 不重新引入已撤销的混合请求调度或整 epoch 串行回退；资源差异先取证。
- 不增加可调 publish/drain 常数，不按负载/PC 特判；gem5 时间仅用于离线审计。
- 当前原始 8 案例只有 native 汇总，没有逐请求日志；重采必须使用新目录。
- 当前 gem5 二进制散列不同于原采集版本；显式声明 diagnostic，并验证目标身份和功能前缀。
- 不用 store 执行完成时间冒充 SQ 释放；不把估算的 prepare response 标成真实 admission。
- 完整准确性与本次短窗口分开；CPI 报告沿用维护 validator，必须包含绝对误差/MAE。

## Task 1：补齐现代冻结案例的有界采集入口

文件：`tools/collect_tail_timing.py`、`tests/test_tail_timing_collection.py`。

- [x] 先测试 `command.json` argv、相对 manifest 路径、原 `run.log` 回退、路径缺失拒绝。
- [x] 新增 `--source-case`，保留原 inventory 接口；集中解析源目录和冻结命令。
- [x] 修复插入 debug 参数后配置脚本索引可能过期的问题，用已保存的脚本路径计算指纹。
- [x] 以 NAMD/Graph500/TeaLeaf C4 各 100k user UOP 做 diagnostic 采集，开启 native response JSONL；保存命令、二进制和脚本散列。

## Task 2：只增加 FastSim 生命周期观测

文件：`include/fastsim/types.hpp`、`src/simulator.cpp`、`src/main.cpp`、`tests/test_main.cpp`。

- [x] 在已有 frontier sample 内增加 buffer role（非优化/原队头/在途接管/额外本地 demand）、canonical prepare issue/response、估算 permission-ready 及原服务时长。
- [x] 复用已有 actual_retire、store_send、store_service、SQ release 和前序 TSO owner，不另建调度器。
- [x] 测试三种角色、响应关系、审计 ON/OFF 预测一致及最终提交不重复。
- [x] 原始代码快照、失败测试、最终构建/完整测试结果写入 `tmp/buffer-tso-mechanism-audit-20260923/`。

## Task 3：运行首轮配对并选择有证据的修正

文件：新增 `tools/audit_buffer_tso_lifecycle.py` 及配对测试；结果写入本计划同目录的实施报告。

- [x] 核对 native 单次 admission/response、完整 line 身份、时钟周期；缺失 prepare 事件明确拒绝完整协议认证。
- [x] 以功能序号/PC/地址/类型对齐，不以时间相近推断同一事件；先比较同条请求的时长，再比较同源时间位移。
- [x] 在原始完整 ROI 上采 FastSim 有界窗口，证明 audit 不改变完整预测；重采窗口须另做功能一致性判断。
- [x] 对已确认的普通 store callback 边验证 `mandatory_queue_latency → hh_store_hit → same-tick response → completeStore`，检查实际日志后才修改时延来源。
- [x] 若缺失原生 prepare 身份或输入前缀不一致，完成采集/审计工具并给出阻断证据；不编造根因、不据此改物理模型。
- [x] 最终独立代码复查，报告完成项、未验证环节和下一条应采集的原生事件。

## 重点反例

1. 分裂/atomic/终止请求不能当作普通单次 demand。
2. 原生缺字段与合法零周期必须区分。
3. 不同二进制的相同几何不代表功能输入相同。
4. 审计试算不能输出成多次已提交事件。
5. 同组缓存状态与资源身份尚未对齐时，不因 CPI 改善宣布机制通过。

## 本轮执行结果

首轮计划执行完成，结果见 [buffer-tso-mechanism-audit-20260923.md](buffer-tso-mechanism-audit-20260923.md)。完成的是有来源门禁的诊断与观测实现，不是完整协议认证。配对确认 commit→admission 及 store callback 的阶段差异，Graph500 另有同条 store 的层级选择差异。native 缺逐 store prepare 身份，按本计划的条件保留物理模型，不单独把 L1 服务从 2 改为 1。额外修复了真实 pending-store 日志触发的 maintained native auditor 误判，并用合法/非法终态反例验证。
