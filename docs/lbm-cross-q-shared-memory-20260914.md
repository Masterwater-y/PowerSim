# LBM 跨 Q：共享内存服务子路径接入与核心阶段拆分

2026-09-14，继续此前批准的跨 Q 服务修复；默认不变，未提交或推送。

## 当前结论与范围

SharedSystem 的 **已分类唯一 LLC miss 的内存服务子路径** 现在实际使用
MixedDramController、未决 MSHR owner、未来 fill 发布和真实 LLC dirty victim。
定向测试经过真实缓存与控制器，不是另一个独立控制器实现。

但目前该新子路径仍由定向测试调用，Simulator 的上层循环尚未使用它。
四个既有核心阶段的计算已抽成显式依赖的可调用操作，但仍立即执行，引用的
状态仍属于当前调用；没有跨 Q 保留或恢复闭包。**完整 LBM 修复未完成**。
非 off 模式继续在构造 Simulator 时明确拒绝，不能用组件通过解除该保护。

## 本次实际改动

- `submit_pending_memory` 保存原始 core/sequence/fragment 与不复用 generation；
  完成时间未定时维持 optional 未决值。普通 store 的行获取仍是 RD。
- `advance_pending_memory` 将外部已知到达前沿、最早可能的新响应、已选 fill
  回调组合起来推进控制器。选出的响应可以晚于 Q/F，但不能提前安装缓存。
  真实 fill 引发的脏淘汰在回调时进入 WB 队列，不能隐藏在已发布前沿之后。
- 每 CHA 的 MSHR 归属保留到 fill；每 core 的服务数量受其既有 L2 MSHR 归属
  约束。越界在 generation、map 和 PMU 修改前拒绝，不增加拟合容量或伪延迟。
  调用者必须在 miss 分类前完成上游资源准入。
- 事务覆盖 controller、未决服务、MSHR、每 core 计数、frontier、cache/directory
  undo、PMU 和 expiry。回调消费 expiry，避免按 ROI 累积完成历史。
- 新增显式 DRAM 写时序解析、校验与 JSON 输出；CWL/RCD_WR 缺省继承 CL/RCD，
  保留现有周期单位和 burst，不导入测试夹具的22周期缺省值。
- 既有 scalar feedback 的 dispatch、issue/fragments、completion/WB、
  retirement/store-drain 计算已实际抽出，阶段间数据放入 frame，四个操作都有
  显式捕获列表。它们并不是已可恢复的核心，也没有增加全前缀第三遍反馈。

核心实现见 `src/simulator.cpp`；配置转换见 `src/mixed_dram.cpp`；
定向测试为 `tests/test_shared_mixed_service.cpp`、`tests/test_cross_q_config.cpp`。

## 失败先行与修正

1. 写时序键被忽略的反例先失败：`DRAM write timing must not be ignored: t_cwl`。
2. 真实旧共享路径先失败：`shared demand fabricated a response before controller selection`。
3. 审查发现 pre-admission map 无界，新增反例先失败；按已有上游 L2 MSHR 归属修正。
4. 修正数量约束后，指令服务排队 PMU 泄入 data scope 的反例失败；新子路径改为
   instruction/data 及对应 context 分开记账。旧 off 路径未改。
5. 核心第一版仅有阶段 setter，审查拒绝；之后移动实际计算。构建发现 audit 使用
   的 base_fetch 越过作用域，改为 frame 显式输出；去除3个未用别名警告。

最终定向测试的字面值：首个 fill1512、外部 response1517，均可晚于 Q1024；
fill前 LLC 不可见、相等时可见；第二个请求仍被单 MSHR owner 阻挡。
两次唯一 miss 对应2个 RD；真实脏 LLC 淘汰对应1个 WB，未把 store 直接当 WB。
重复同一 frontier 不重复发布；拆分/整段推进的 RD 身份和时序一致；事务回滚
恢复缓存、未决时间、MSHR/上游计数和 PMU。指令-only 夹具的 instruction/context
排队均为700，data/context均为0。

## 最终验证与全输入默认回归

- `cmake --build build -- -j16`：session80604，exit0；只有既有 serial-LTRANS
  链接提示，无新增编译警告。
- shared mixed、cross-Q config、runtime guard 三个定向可执行文件：exit0。
- `./build/fastsim_tests`：session20940，exit0，`all FastSim tests passed`。
- 共享子路径、阶段计算提取、显式捕获分别经过范围独立审查；修正后无未解决
  Critical/Important 实现问题。审查通过范围不包含未实现的核心恢复。
- 配置目录与本轮入口快照完全相同；`git diff --check` 通过。

全输入是已有 common-end `formal-32c-782.lbm_r`，user-plus-kernel，功能预热
只建状态、不计入测量。实际宏指令240,123,783，user UOP308,016,035；身份、
oracle、共同结束和实际计数门禁通过。此次是维护默认兼容性回归，不是候选收益。

| 配置 | FastSim CPI | gem5 CPI | 有符号相对误差 | CPI绝对误差（cycles/macro） |
|---|---:|---:|---:|---:|
| LBM C32，维护默认 off | 4.200373 | 3.906000 | +7.536430% | 0.294373 |

32个 core 未加权 CPI MAE 为0.321020，与旧版一致。总周期1,008,609,550；
scope_metrics 排除 host throughput 后及所有 threads 逐项等价，既有配置值无差异。
新增7个写时序字段为0（CWL/RCD_WR表示继承），cross-Q仍off。
运行伴随正常回归检查，未进行安静主机 ABBA，不作吞吐收益判断。

冻结二进制 SHA256：
`afef6853a16196965d303756337108e7f163f51184aa81893a4e8772475ca2b6`。
产物目录：`tmp/lbm-cross-q-repair-20260914.2r2LRW/continuation.HBC39M/`；
`validation-inventory.json` 保存配置/二进制指纹，`default-c32-status.json`
保存完整命令、输入门禁、计数、误差和等价比较；没有覆盖旧结果。

## 生产门禁与下一处必要接入

用当前库重新链接的两核生产门禁仍为 RED：

- off：普通 store 不满足实际 admission >= commit+2。
- combined：在执行前报 `cross-Q core continuation is not integrated`。

这证明本次没有改变默认精度，也没有完成315个 DRAM store /430个 SQ owner 的
LBM 关键覆盖。不能扩到 LBM C4–C32 或40项矩阵来宣称修复。

接下来需要把四阶段引用的状态放进有生命周期保障的 per-core live context，
保存独立 dispatch/issue/retire 游标和响应 owner；未决 load 只能阻塞其 RAW 或
容量依赖者，不能隐藏年轻独立请求。再把真实 private/shared lookup、同线父服务、
实际 store send 与上述内存服务子路径接到同一循环，以所有请求来源的下界证明
F，而不是把 Q 或 producer H 当成证明。只有通过两核生产门禁、再完成全 C32
C13及C1见证覆盖后，才能评估三组干预的 CPI/P99。
