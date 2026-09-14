# LBM 跨 Q 修复：基础状态已落地，生产闭环未完成

状态：**部分实施，不是 LBM 修复完成**。原设计见
[跨 Q 服务设计](superpowers/specs/2026-09-14-lbm-cross-q-service-design.md)，
剩余任务见[实施计划](superpowers/plans/2026-09-14-lbm-cross-q-services.md)。

## 已实施

- 增加显式 `core.cross_q_service_mode=off|controller|admission|combined`，
  默认 `off`；校验冲突配置并输出配置身份。尚未接通完整恢复核心，因此非 off
  模式在执行 trace 前明确拒绝，不会把旧引擎结果标成新模式结果。
- `SharedFill` 区分未决与已选中响应，保留 generation 和响应下界。未决 fill
  不能当作可见数据、数值 merge 或 expiry 事件。真实 `SharedSystem` 增加
  未决归属、不可变响应发布及事务撤回接口，不重复请求 PMU；旧 generation
  回调不能修改替代请求。generation 继续沿用原先回滚后也不复用的规则。
- 增加有界 `PendingResourcePool` 与 `DetachedStoreGroup`。未知资源释放保留
  owner，不能被一个可能更晚的已知时间错误越过。store 退休独立于响应，SQ
  等待全部分片的最终响应，同一 store 内分片不互相等待响应。
- 现有 shared-service/pending-fill **已知响应**的普通 store 分片循环已经使用
  `DetachedStoreGroup` 汇总真实分片准入/响应和 SQ 释放。此处仍是闭合数值路径，
  并不等于完整跨 Q store 已接入。

没有改变 ordinary-load=3、response-to-ready=1、Q=1024、维护配置别名或历史配置；
没有引入负载/PC 补偿，没有提交或推送。

## 测试与边界

`cmake --build build -- -j16`、两个配置/运行门禁测试及 `./build/fastsim_tests`
通过。保留既有 LTRANS 串行链接提示，无 C++ 编译诊断。独立审查通过了配置、
owner 组件及本轮部分共享层/闭合 store 接入，未批准完整跨 Q 或精度结论。

关键 RED/GREEN：未决 fill 的原判定会把未选中响应当成可见数据，修正后通过；
新增模式在旧库中会静默接受并使用旧引擎，增加运行门禁后通过。真实 SharedSystem
测试检查 pending → selected → rollback、200-cycle fill 对应 205-cycle NoC 返回、
PMU 恢复及旧/重复/冲突回调。owner 测试通过优化构建与 ASan/UBSan；ptrace 环境
不支持 LeakSanitizer，未声称泄漏检查通过。

两核生产验收 fixture 仍为 **RED**：

- `off`：在普通 store 的 commit+2 准入边检查失败；
- `combined`：在“cross-Q core continuation is not integrated”门禁失败。

这与单元测试通过并不矛盾。后者证明基础接口与旧路径兼容，前者证明完整修复仍
未完成。不得以此扩大精度矩阵或宣称关键 LBM 请求已覆盖。

## 完整输入默认回归

只运行 1 个已用于诊断的 LBM C32 配置（不是 held-out 泛化验证），32 条完整 trace。
使用 `tmp/first-core-common-end-20260911/source/formal-32c-782.lbm_r/` 的现有
first-core-target-common-end-v1 输入；user-plus-kernel、功能 warmup 排除。
输入身份/common-end/population 门禁通过，拒绝 0 项。实际宏指令 240,123,783，
用户 UOP 308,016,035；未截取冷窗口、未重采 gem5。

| 默认 C32 | FastSim CPI | gem5 CPI | 相对误差 | CPI 绝对误差 | 32 核 CPI MAE（不加权） |
|---|---:|---:|---:|---:|---:|
| 本轮基础改造后 | 4.200373 | 3.906000 | +7.5364% | 0.294373 | 0.321020 |

CPI 绝对误差单位为 cycles/macro-instruction。这个单配置样本的 case-level CPI
MAE 为 0.294373。总周期 1,008,609,550，scope（排除 host throughput）及所有线程
结果与之前冻结默认逐项一致；32 核误差仍全部为正。**没有 LBM 精度改善**。
本轮不构成 quiet-host 吞吐验证，未运行候选模式、C4–C32 或 40 项/P99 矩阵。

验证二进制 SHA256：
`a414794c20700bc0c3948e77ae55d256168f2a5a94f8cce190e8cd8e684a625a`。
当前 `build/fastsim` 与冻结副本一致。

产物在 `tmp/lbm-cross-q-repair-20260914.2r2LRW/`：
`progress.md`、`task-2a-report.md`、`task-3a-report.md`、
`cross_q_production_gate.cpp`、`validation-inventory.json`、
`default-c32-status.json`、`default-c32.json`；保留 task-entry 源码快照和审查 diff。

## 必须继续完成的接入

1. 按 `core-seam.md` 的源码边界拆出 dispatch、issue、completion、retirement
   四阶段，保留有界 ROB live window 和退休后未响应的 store。不能在旧标量
   UOP 循环遇到未知 load 后整体停住，否则会漏掉更年轻独立请求。
2. 将上述 owner 接到真实 Sequencer/MSHR/SQ 和 mixed RD/WB 服务，保留 dirty
   LLC WB 身份及一次性副作用。`PendingResourcePool` 只支持时间序准入，不能
   直接代替允许向过去插空的 WB/FU 日历。当前混合控制器仍没有生产调用者。
3. 分离 producer 覆盖 H、全来源 exclusive 服务 frontier F 与退休游标，允许
   空 UOP epoch 推进未决服务，再把两核生产验收修到通过。
4. 在完整 C32 中验证 C13 的 315 个 DRAM store、430 个 SQ owner，以及 C1
   正/负对照；覆盖通过后才做 controller/admission/combined 消融和精度矩阵。

非阻断测试覆盖缺口：本轮共享层 hook 尚未直接覆盖 reserve 回滚后重申请、
低于下界的响应拒绝以及恢复后的旧 expiry 推进；已有代码审查未发现相应实现缺陷。
