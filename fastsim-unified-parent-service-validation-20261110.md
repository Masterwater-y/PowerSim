# FastSim 统一父服务记录：隔离实施与验证

日期：2026-11-10。**状态：CPU 私有侧 S0/S1 已实施并通过回归；35 次回放全部成功。S0 中性、重复确定性及 S1 人口守恒通过，但 S1 功能结果守恒门禁未通过。按用户要求已停止本轮 native 审计和评分，直接基于保存的 CPI 参考给出误差；完整设计尚未验收。**

隔离目录为 `tmp/parent-service-exclusive-20261110/`。正式源码未写入本轮实现，没有提交或推送。

## 已实现范围

- `core.unified_parent_service = off | observe | wait`，默认 `off`。
- 一份父记录持有已认证普通读的数据就绪时间与两层 MSHR lease。行、释放及到期索引引用槽号/代次 ID。
- `observe` 迁移核响应侧资源记录；`wait` 以 `max(original_response, parent.data_ready)` 约束已认证 follower，保留每请求 token、回调与退休。
- 固定容量、到期引用压缩、dispatch 回收前沿、lease pin 和候选快照隔离已接入。
- 同线本核/跨核写、atomic 和 page-fault 修改撤销读资格；未认证来源不猜测数据就绪时间。

## 已完成验证

基线及候选最终全量构建与 `fastsim_tests` 通过。`fastsim_parent_service_tests` 和补充 hazard 测试通过，覆盖提前响应与消费者传播、跨窗口、缺失元数据、源 issue 反转、事务拒绝、代次复用、固定容量、不同层释放、同周期完成、回收前沿倒退和同线写撤销资格。

五案独立硬件身份验证：5/5 有效、0 mismatch。冻结 baseline 的 661 文件再次复核为零漂移，最终八文件补丁与隔离源码哈希一致。

## 回放结果与 CPI 直接误差

五案为 Zstd、XZ、FLAC、Graph500、Marian，各四核、同一完整冻结功能输入。r0 使用 baseline/off/observe/wait，r1 使用 wait/observe/off，共 35 次回放。回放二进制和配置 include 链已冻结。

35/35 次回放已经成功完成，无运行失败。全模型字段比较确认 baseline/off 中性、off/observe 中性、两次交错重复确定性通过；S1 的逐核宏指令、UOP、内存成员和退休任务人口一致。

S1 功能结果守恒门禁失败：五案的 L1/L2 汇总计数均有小幅变化。具体字段与逐核变化已保存于 `results/comparison.json`，没有放宽门禁或删除差异。汇总如下；这些是模型内部结果，尚不能据此确认底层 Tag 状态变化的原因。

| 案 | wait 次数 | L1 misses 总变化 | L2 misses 总变化 |
| --- | --- | --- | --- |
| Zstd | 65,258 | -62 | -40 |
| XZ | 1,512 | -6 | -9 |
| FLAC | 2,532 | +3 | 0 |
| Graph500 | 1,290,880 | +3 | -5 |
| Marian | 17,998 | +2 | -3 |

按用户后续明确要求，本轮 native 来源预检、三批评分及 `finish_validation.py`
已停止，核对后没有本轮相关 Python 审计进程残留。35 次回放没有重跑。

直接复用 `tmp/spr-no-prefetch-c4-fastsim-20261109/validation/summary.json`
已保存的维护 validator gem5 CPI 参考；轻量 JSON 检查确认当前各模式与原始
baseline 的 task/core 人口、active scope 和退休终点政策一致，off CPI 与原评分相同。
没有重新扫描 FST 或执行 native 来源审计。

| 案 | gem5 CPI | off CPI | off 绝对误差 | off 相对误差 | wait CPI | wait 绝对误差 | wait 相对误差 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Zstd | 1.023967 | 1.174084 | 0.150118 | +14.66% | 1.193666 | 0.169700 | +16.57% |
| XZ | 0.609912 | 0.651903 | 0.041991 | +6.88% | 0.643894 | 0.033982 | +5.57% |
| FLAC | 0.284157 | 0.317938 | 0.033781 | +11.89% | 0.321143 | 0.036987 | +13.02% |
| Graph500 | 0.966058 | 0.753662 | 0.212395 | −21.99% | 0.834105 | 0.131952 | −13.66% |
| Marian | 2.359792 | 3.023056 | 0.663264 | +28.11% | 3.069147 | 0.709355 | +30.06% |

绝对误差单位为 cycles/macro；CPI 使用 workload-task active cycles /
实际退休 task 宏指令。observe 与 off 一致。

| 模式 | 案数 | CPI MAE cycles/macro | CPI MAPE | max APE |
| --- | --- | --- | --- | --- |
| off / observe | 5 | 0.220310 | 16.71% | 28.11% |
| wait | 5 | 0.216395 | 15.78% | 30.06% |

wait 的 MAE 降低 1.78%，MAPE 降低 0.93 个百分点；Graph500、XZ 改善，
Zstd、FLAC、Marian 变差。最大误差增大，聚合改善不足以推广默认开启。

[直接 CPI 误差报告](fastsim-parent-service-cpi-errors-20261110.md)
及同名 JSON/CSV 保留未舍入数值、实际分母与引用哈希。这是基于已有参考
的误差计算，不宣称本轮重新完成 native 来源认证或真机 perf 认证。
五案并行且宿主有其他任务，吞吐只作诊断；不可用的 end-to-end user-UOP/s
不改名为已认证指标。

## 未完成边界

本轮保留 checkpoint-local MSHR 日历近似与 bounded pool snapshot。LLC/预取 owner、slot journal、S2 下层 unique-service 合并及 S3 延迟 Tag 发布尚未迁移，merge/publish 配置被明确拒绝。完整统一父服务设计尚未验收，保持默认关闭。

## 可审查产物

- [最终八文件补丁](tmp/parent-service-exclusive-20261110/s0-s1-private-final.patch)
- [实施记录](tmp/parent-service-exclusive-20261110/IMPLEMENTATION.md)
- [实际所有权与未迁移路径](tmp/parent-service-exclusive-20261110/OWNERSHIP.md)
- [回放冻结身份](tmp/parent-service-exclusive-20261110/inputs/replay-freeze.json)
- [独立硬件身份结果](tmp/parent-service-exclusive-20261110/results/identity.json)
