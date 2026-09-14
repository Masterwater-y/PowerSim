# 两阶段访存服务复用：填充身份与可见性校验

日期：2026-09-09。接续 [load 返回与写回修复](two-stage-response-completion-20260909.md)。

## 修复范围

本轮修复已有 timing-only replay 的错误认证：请求顺序未变，不代表旧 hit/merge 路径
仍有效。它完成的是服务复用条件和拒绝后的事务回退；**没有完成普通 feedback 移动
arrival 后的通用 hit/merge/miss 路径重算**。回退仍保留 canonical 近似，不能计为该次
路径误差已经修好。也未开启 `causal_read`、额外全量 proposal 或新的生产开关。

五条指令的反例：core 0 读取 X；core 1 读取 Y，然后依赖 Y 读取 X，各接一条消费者。
候选时刻的 X 合并到 core 0 的填充。Y 的真实模型返回推迟了 core 1 的 X，使它到达时
填充已结束。旧 response-retime 路径仍将此候选标为 stable；新路径检测到 merge→hit
边界变化，在发布候选 timing state 前回退。它不会在此前的 cache/replacement 副作用
已经提交后，只改 response 或 PMU 标签来冒充完整路径切换。

## 实现

- LLC 活跃填充保存 `ready` 和单调递增的 `generation`；每个 unique miss 分配身份，
  合并请求继承该身份。回滚不重用身份。填充时间变化不改变身份，同 line／同 timestamp
  的不同代不能混用。
- `SharedTimingDescriptor` 保存填充身份。共享 timing replay 在修改 CHA、MSHR、DRAM
  预约前检查父事务及可见性。父事务缺失、代次替换、跨填充边界分别计数；移除找不到
  父事务时静默复用旧 `canonical_fill_completion` 的逻辑。
- 可见区间按 `tag_ready < fill_ready` 合并；同拍 fill 先于 lookup。resident hit 被延迟
  的 fill 重新覆盖时也会失效，新的 miss 不能覆盖另一笔尚未结束的 fill。
- FR-FCFS 使用已有候选结果，对相关共享请求校验上述关系，然后才更新 feedback／
  安装队列状态。仍属于同一填充的 follower 按 generation 更新等待、返回时刻和填充
  元数据，跨 Q 的父事务从 epoch-entry state 读取。请求代次取代旧 `(line, completion)`
  关联；不会把“时间刚好相同”当作事务相同。
- timing transaction 安装时重建活动填充的 expiry，回调同时匹配 generation 和 ready；
  旧回调不删除新代次，retime 后的新回调也不会因旧时间不匹配而永久遗留。
- 原可选 functional reweave 不再仅凭第二轮的顺序稳定提交，仍需对应 arrival 稳定。
  不增加重试上限。可选 suffix-carry 暂不携带缺少持久功能回滚证据的 LLC hit/merge；
  使用其既有 conservative epoch 回退。维护配置的这些开关保持关闭。

计数位于 JSON `causal_frontier.service_fill_*`，是候选工作量／失效诊断，不是 gem5 PMU。
`ChunkUopBound`、`UopIndex`、`ChunkMemoryEvent` 和 FST 不变；共享服务描述符新增一个
64-bit 身份，活动填充值由一个变为两个 64-bit 字段，expiry 项由两个变为三个。
不是给每条 UOP 添加新的事件或扩大依赖槽。

该校验只证明填充关系，依赖原路径对 cache/set/coherence 顺序的约束，不能替代完整
set/victim/owner 版本证明。普通相对延迟 feedback、提前私有 tag、producer clamp 和
跨核最终 arrival 闭合仍是后续问题。

## 验证

`cmake --build build -- -j16` 和 `./build/fastsim_tests` 通过。
新增检查覆盖 fill 前、同拍、之后，缺失／替换父事务，相同 timestamp 的不同代，
fill 延迟导致旧 hit 失效，以及五 UOP 反例的候选回退：cycles、CHA queue、cache PMU、
unique DRAM request 均等于 canonical 对照，没有泄漏部分已计算的候选反馈。
两处旧测试的“第二轮顺序稳定即成功”／“hit/merge 可无条件 carry”断言改为上述回退合同；
保留功能人口和计数守恒检查，没有要求候选必须提高 CPI。

额外的 8-cycle Q、15 UOP 机制检查中，父请求 issue=5、follower issue=17，
跨 Q 仍关联 fill=101／response=113：3 次共享请求，2 次 unique fill、1 次 merge、
2 次 DRAM read；两次 FR-FCFS 候选均通过代次检查。准备该检查时的第一份次序变体
得到的是 fill 后 hit，也保留在产物中；不将它冒称跨 Q 合并覆盖。

工作负载只跑已有 TeaLeaf C4，NUMA node 0 固定 CPU／内存，模拟串行执行且不与
构建或测试重叠：长输入 before/after 各一次，完整依赖短输入 generic/fast 各一次。
没有重采集或重新跑十负载矩阵。

| 长输入指标 | 修改前 | 修改后 |
|---|---:|---:|
| 合计 core cycles | 25,871,081 | 25,871,081 |
| cycles / user UOP | 0.646777 | 0.646777 |
| macro CPI | 1.022661 | 1.022661 |
| 对冻结 gem5 CPI 的误差 | −5.4176% | −5.4176% |
| 用户 UOP 吞吐 | 7.0750 M/s | 7.0328 M/s |
| 全程 wall time | 6.1837 s | 6.2025 s |

本轮 **CPI／PMU 没有改善**。吞吐单组对照 −0.60%，不足以判断微小差异是否稳定；
不重复跑数来放大这一结论。本轮固定 NUMA，不能把它与上一轮未固定亲和性的吞吐
直接比较并宣称加速。macro CPI 参考沿用既有冻结值 1.081237829，未新增 gem5 基准。

PMU 前后严格相同：branch miss=4,541（+3.7232%），L1D tag miss=465,983
（−5.3407%），L2 tag miss=394,238（+0.0231%），LLC tag miss=231,847（+0.0328%）。
测量段 40,000,002 user UOP／169,753 kernel UOP，反馈调用均 6,370 次。
完整依赖短输入 generic/fast 的 core、scope、CHA 结果一致：38,157 cycles，
cycles/user-UOP=0.953925，反馈均 16 次；与上一轮相同。

维护 C4 的 FR-FCFS topology selection window 为 1，走原 FCFS bypass，response-retime
关闭，因此本轮真实负载 `service_fill_checks=0` 是预期结果。这次交付是错误复用的
机制修复及兼容性验证，**不是主路径 arrival 闭合已经接入并改善精度的证明**。

原始产物位于 `tmp/two-stage-service-validity-20260909/`，保留修复前二进制、配置、
输入身份、摘要、构建／测试日志和最终源码／二进制指纹。热 UOP 描述符逐字验证不变。

## 下一步边界

要改善主路径，仍需在功能缓存副作用提交前处理真正的 hit/merge 切换，覆盖受影响
cache set 的 replacement 和相连事务。当前拒绝分支提供明确入口，但缺少这段
局部功能重算时，不能通过开启更大 FR-FCFS 窗口、增加全量反馈轮次、补延迟或修改
PMU 标签来制造收益。下一项应先证明一个有界 set 范围能够恢复并重新提交，超出该
范围的情况继续明确回退，且保持原阶段并行和吞吐约束。
