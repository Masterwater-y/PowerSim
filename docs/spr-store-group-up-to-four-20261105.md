# 单遍两阶段 store 组扩至最多四成员：接线设计

目标是在现有功能退休流、两阶段和单次前向近似内，把当前一个 owner、一个 follower 的候选扩大至最多四条同核同 line 普通 store。**四条是本切片的固定成员上限，不是新增或扩大目标 store buffer、SQ、TBE 或端口容量。** 首版已实现且默认关闭；真实 FLAC 中观察到的 88/99 个三/四条 pair 形状可能重复，也尚未通过完整身份、信用和最终可见性检查，不能当作可合并组数或预期收益。

## 首版状态与验证范围

同版本头文件和库的 Simulator 专项已通过 3/4 成员、line-shape、C4、FR-FCFS 测试。3/4 成员 fixture 验证每条退休 store 与功能访存人口守恒、层级请求减少 N−1、所有成员共享组边且各自信用不提前释放；覆盖 FIFO/预算不足、第五条、跨 producer chunk、同线 load、跨核同线、unknown/context、MMIO、atomic、fence、异常边界，以及 FR 预测成功后水位失败并恢复后续同线请求。测试不等同于完整应用效果验证，统一主测试和完整 FLAC 对照仍待执行。

输出新增 `committed_members`、`size2`、`size3`、`size4`，只在实际提交后计数；功能形状计数仍可重叠且不代表真实合组。ON 路径亦取消旧双成员 follower 的独立 store 服务估计，改为整个组的一次模型服务；因此与旧二进制相比 ON 不只改变组长度，任何准确度和吞吐比较都应使用同一新二进制的 OFF/ON。legacy clean prepare 和 head demand 尚未完整区分。

## 成员及一次性预检

每个接受 Q 最多一个组。以全局 batch 中第一个合法 line 事件为 owner，仅选此后全局 batch 连续、同核、同一 producer chunk 的二至四条 store；每条都要有物理地址、完整未掩码 `.sreq` 功能事实，flags、context、requestor 和 line 全员一致。相邻成员之间只能有无访存、无 MMIO/NoAccess、无软件预取、无 fence/serialize/atomic/hardware-entry/precise-fault 的普通功能 UOP。候选前已有同核 UOP 或不同 line store 时，沿当前前缀一次前向汇总的最坏占用执行预检；其他核在本 Q 的特殊边界、任何其他同 line 访问或中间全局 memory 请求仍截断。若该 line 在本 Q 超过四条，或者存在无法覆盖的同 line 额外事件，整组不合并，不把它拆成未经可见性证明的多组。

预检对已有有效信用、该核早于 owner 的所有潜在 store 和每个组员保守计容，检查 FIFO SQ 中相应的所有原有槽位和单一共享预算，绝不增加目标容量。预检失败使用现有完整请求路径；成功仅冻结输入事实，不能宣称成员已经准入、退休或发送。

## 阶段二与层级人口

候选用固定 `std::array` 保存不超过四个 `(core, sequence, event, batch rank)` 及功能 facts，不创建逐 UOP 堆对象；同一 Q 至多一组。canonical 按已接受次序真实访问一个 owner clean permission prepare；其余成员标记为 group follower，显式从 FR 的 shared/DRAM 请求列表移除，不能伪造为 Sequencer 合并或复制 owner cache outcome。其他全部内存请求按原路径处理，owner 仍可能未获得可用 permission。

反馈逐条完成真实模型 dispatch、SQ/预算准入、执行完成和有序退休。固定数组保存所有成员的独立 ticket 和 SQ 槽；不足四条时不创建或占用额外资源。每个成员尚未完成前保留原信用，不让 younger follower 提前释放。所有成员完成后才按 `max(成员退休、成员数据/地址完成、组前 FIFO drain、预算时间水位)+commit-to-IEW` 求模型 offload；唯一 callback 不早于 offload 加非零本地服务和 owner 权限准备完成。每个 follower 的共享信用最早在 offload 释放，owner 到 callback；独立 SQ 槽及 FIFO 前沿分别更新。group 槽位、信用票据和共享计数必须覆盖全部组员而非只覆盖前两个。

现有两成员路径会先执行 follower 的普通 store retire 计算、随后修正组回调；扩大至多成员时，中间 follower 不能制造额外的虚拟 send、request 或 TSO drain 更新。应在其 retire 时只保存真实完成/退休边与信用和 SQ 槽，最后一个成员才建立一次组级 send/callback；诊断输出须标出实际组边，不能把提前临时边作为真实请求。需定向检查这条旁路对当前两成员和非组路径的人口与统计，避免用伪造的 follower 请求填补数值。

现有 `SprSharedStoreCredits::schedule_group(initializer_list)` 虽接受二至四个固定参数，真实接线必须提供固定数组加 count 的有界接口，或显式分派二/三/四成员；不能按条重建其他信用表。现有 `SprPendingStoreGroup` 的 `hold` 只接受退休边，需要补充独立 completion 下界并检查它的 predecessor drain 和 follower 信用语义后方可复用；不能把它孤立出来当作完成真实接线。

FR-FCFS 对显式剔除所有 follower 的真实请求表至多预测一次；最终 response 导出唯一 feedback 后，使用最终组 callback 检查全部活跃核的未消费事件水位。cache/目录/DRAM/MSHR、各核 totals 和稀疏 FB shadow 与 credit/SQ feedback 作为一次事务提交。任一成员准入、权限、line 身份、回调或最终水位失败，至多恢复一次并以完整原始人口走旧路径；重算被拒分支的不同请求人口是一次失败回退，不是多轮收敛。被丢弃的 FR pass 和实际提交 pass 分开统计。当前 legacy 仍把 clean prepare 与 dirty publication 作为一个模型 hierarchy 访问；本切片不会因此假称已经实现 gem5 独立 prepare hint 和真实 head demand，后续 prepare/head 统一工作须共同接入事务。

## 可执行验收

- 真实 Simulator 在 FCFS、单遍 FR-FCFS 下分别执行 3/4 条同线 store：每条宏/UOP 与退休 store 人口不减，hierarchy/Sequencer 请求分别减少 2/3；所有成员同一组 ID，offload 晚于全部实际退休与完成，owner/follower 信用释放符合上述边界。
- 覆盖组前普通 UOP 和不同 line store、组内安全非访存 UOP 及依赖、同周期、前序 drain、SQ/预算分别不足 `N` 的保守回退；保留每核独立预算，跨 chunk/Q 和 warmup boundary 不跨界组装。
- 逐一插入同线 load、其他核同线访问、不同 line 插入的 global batch memory、fence、atomic、MMIO、NoAccess、hardware-entry、precise-fault、未知或不同 flags/context/requestor、第五条同线 store；请求和退休人口必须守恒，不能误组。
- 覆盖 FR 预测成功后最终水位失败、MSHR 饱和、非 replayable 请求、permission 失效和后续同线读取；对比 OFF 的 cache/目录/DRAM、FB shadow、逐核人口及 FR 提交计数。额外确认 3/4 成员、失败回退与既有两成员的分配没有逐周期调度或反复求解。
- 最后对同二进制、同配置、同源 FST 的 FLAC C4 做开关对照，分别报告实际候选/成功/回退和请求人口、CPI 绝对/相对误差、受控模型吞吐与端到端耗时；C4 参与核、预热、thread-state、MMIO、v2 身份及有限原生证据不得被省略。
