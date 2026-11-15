# 统一父服务 v2：隔离实现与机制验证

日期：2026-11-10。状态：第二轮候选已构建并通过回归；按用户要求已停止后台回放，保留 34/35 次验证和 10/30 次直接吞吐对照的成功结果。完整 v2 尚未验收，最终批次报告未生成。

实现位于 `tmp/parent-service-v2-exclusive-20261110/attempt2/source/`，补丁为该隔离目录内的 `parent-service-v2.patch`。主源码未合并，未执行 commit/push。候选二进制 SHA-256：

`009f3b8cb9cb358af6d0f179c72a5567bf5c774bd68833b8decdf8ca8ce9adf5`

prepare 全程开启，统一父服务默认 `off`。保持退休功能输入、原并行两阶段与一次反馈；LLC/预取迁移、广泛合并及延迟 Tag 发布未开放。

## 已实现

| 改动 | 实际范围 |
| --- | --- |
| 服务事实 | 捕获功能访问前驻留/权限及已选服务动作，区分 I GETX、S UPGRADE、E/M 无新事务和未知来源 |
| 唯一记录 | origin 与 consumer 分开；store 接管保留 service ID，prepare credit 仅保存服务引用和请求资源归还边 |
| 完成边 | data、permission、controller、write 与 request callback 独立；有资格的 prepare 使用模型已选 lower return，不使用 gem5 OoO 时间 |
| load 响应 | 数据/控制器门后执行一次既有本地路径；更早的 read 不继承未来 store 的内容边，callback 不直接充当整行数据门 |
| 资源 | 独立 TBE/fill lease 存于服务记录；释放堆只存 ID；S 升级不分配 fill，接管不再分配已有资源 |
| 资源资格 | 只接受安全 watermark 下单调、容量可容纳的冻结源；不匹配时保留完整请求并计 source/resource fallback，不事后移动下层服务 |
| 事务 | 服务记录按 32 槽块 COW，槽代次保护旧引用；行/expiry/释放索引仍做有界快照 |

旧 checkpoint-local 两层 MSHR 租约仍按原合同释放；新的服务 TBE/fill 租约独立存在。它们不是完整统一容量反馈已完成的声明。

## 已完成的检查

- 隔离 Release 构建，`FASTSIM_LTO_JOBS=16`、`cmake --build build-candidate -- -j16`：通过。
- `fastsim_tests`：通过。
- `fastsim_parent_service_tests`：通过，包括真实两阶段普通 load 反例。
- 独立池/prepare/拆边/资源/回滚反例：通过。
- 真实两阶段 prepare 源资格反例：通过。
- 五案独立硬件 identity：5/5。
- 已保存 CPI 参考与冻结基线的逐核人口/active scope 核对：5/5。

运行环境使用 `LD_LIBRARY_PATH=/opt/gcc-11/lib64`；未修改全局环境配置。

关键日志位于隔离目录 `results/build.log`、`regression.log`、`parent-tests.log`、`identity.json` 和 `runtime-prepare-witness.json`。

## 已确认的来源覆盖限制

真实两阶段反例包含两个普通 store 和同线不同字节的 load；输入只有退休功能记录。

| Store 序号 | 已选下层 issue | 实际 retire | 最早 canWB | permission 诊断 |
| --- | --- | --- | --- | --- |
| 0 | 5 | 6 | 8 | 0（direct 分支未建立 optional permission） |
| 2 | 5 | 6 | 8 | 220 |

本反例产生 1 个 prepare GETX、0 个可认证拆边服务、2 次 source fallback，32 个退休 UOP 保留。

`selected=5 < canWB=8` 表明目前下层服务先于最终提示准入。该记录不能认证为 load 的父源。放松相等条件或把原响应后移，会在保留旧共享/DRAM 排队结果时改变来源因果关系，因此本候选没有这样处理。

扩展这一范围需要在共享拥有者提交之前取得 prepare 准入上界和源资格，或通过现有有界事务完整撤销相关依赖组件。当前接口不足以在提交后只改一条父记录安全闭合它；不能恢复全窗口收敛或逐周期运行时。

## 回放与报告

按用户最新要求，Zstd、XZ、FLAC、Graph500、Marian 五案并行，每案内三轮样本串行，共 35 次回放。每案使用独立的 12 个 CPU：128–139、140–151、152–163、164–175、176–187。

首轮比较 `off / v2-observe / v2-wait`，后两轮交错比较 `off / v2-wait`。中断的串行产物保存在 `results/interrupted-serial-*`。冻结输入与源码保持不变，实际并发配置另存 `inputs/execution-plan.json`。

原入口设计在全部完成后自动生成根目录 `fastsim-unified-parent-service-v2-validation-20261110.md/json`。本批已按用户要求中止，未运行最终汇总。原始结果保留人口、L1/L2 delta、逐核 active cycles、源/资源覆盖及吞吐。CPI 参考仍为已有 gem5 active 数据，未来报告逐案同时列 AE（cycles/macro）和相对误差，聚合列 MAE 与 MAPE；本轮不恢复 native 来源审计。

并行样本竞争共享缓存、内存带宽和 I/O。测得吞吐属于并行争用诊断，不能单独认证 v2 的独占宿主开销。

## 尚未完成的验收项

完整统一容量准入、增量索引 journal、全域 Q16/clock epoch 资格、普通读物理 fill 与 callback 拆分，以及 prepare 的同窗口源覆盖仍不完整。完整 v2、native PMU 和硬件 perf 精度均不据此宣称已验收。中性、人口、功能结果、事务资源、精度与吞吐分别判断，不能用 CPI 改善豁免未闭合机制。
