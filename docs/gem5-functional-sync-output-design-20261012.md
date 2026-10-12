# gem5 功能同步输出：首片设计与验收

日期：2026-10-12。状态：待审阅的设计；未实现、未宣称验证通过。

## 1. 目标与范围

用户要求先整理讨论，再只实施 gem5 侧信息输出并验证效果。
讨论背景见 [跨采集器同步协议](functional-synchronization-trace-discussion-20261012.md)。
使用本仓库 `gem5-fs/`，不使用旧 `/data00/yinhaolang/gem5`。

首片支持 Linux x86-64、GCC 11.5 libgomp、静态非 PIE 二进制、TaoTrace direct FST。
输出 parallel begin/end、每个 implicit task begin/end、team barrier arrive/depart。
语义来自 runtime，gem5 只提供提交观察和 FST 锚定。QEMU/DR 适配器将来可复用相同
marker payload；本次不实现它们，也不改 FastSim 的模型或 FST 热记录。

本片不声称覆盖任务依赖、取消、锁、condvar、准确 spin/sleep 切分、OS 调度和完整
线程创建/退出。线程 ID 是 runtime 生成的 trace-local 软件线程身份，不冒充 OS TID。
未观测字段明确 unknown。unsupported 路径需要能力声明和拒绝误认证。

## 2. 方案比较与选择

1. **runtime 语义 marker + gem5 commit observer（选用）**：身份、团队复用和 barrier
   轮次在原始 runtime 状态处确定；静态 binary 的内部路径也能覆盖。代价是要构建一份
   实验 libgomp，并评估插桩扰动。
2. **仅 gem5 按 GOMP 函数 PC 识别**：侵入 workload 较小，但入口/返回不等于所有
   隐式同步点，成员/TLS/结构布局受 ABI 和编译版本约束，不作为完整首片。
3. **仅 syscall/futex 推断**：无法覆盖用户态自旋及团队语义，不采用猜测分类。

保持现有编译器、libgomp 安装和 workload 原 binary。源副本及独立产物放在项目 `tmp/`；
维护的 transport header、runtime patch/build 工具、collector patch、验证器和小工作负载
放在 source/tools/patches/tests 范围。不得覆盖原实验数据。

## 3. transport 与 runtime 采集点

transport 为可定位符号的 no-op marker，使用六个 x86-64 ABI 寄存器携带：

```text
event_kind, logical_thread_id, region_instance,
sync_object_id, generation, packed(member_index, team_size)
```

marker 指令不修改同步对象或模拟器状态。函数地址从实际链接 binary 的符号表获得，
冻结 SHA256、编译参数、marker 地址与 runtime patch。静态非 PIE 限制要校验，不能猜
地址；动态加载/ASLR 后续通过模块相对位置与加载映射扩展。

logical_thread_id 为 runtime 单调分配、线程本地保存的非零 ID。团队每次实例化分配
新的 region_instance，不能把可复用 team 指针当成唯一实例。barrier 记录实际对象
地址、runtime generation、成员索引与总数；不是由 gem5 时间排序推断轮次。

GCC 11.5 的采集点：

- `gomp_new_team`：分配 region 实例身份，覆盖 team 结构复用。
- `gomp_team_start`：在线程放行前发 parallel_begin；在主线程进入 outlined work
  前发 implicit_task_begin，覆盖单线程和多线程路径。
- `gomp_thread_start`：每次调用 local_fn 前后发 worker implicit_task_begin/end，
  覆盖首次启动、线程池复用与 nested 路径。
- `gomp_team_end`：主线程工作结束与 join 完成事件，必须在 team 状态恢复/释放前
  捕获原身份。
- Linux `gomp_team_barrier_wait` / `gomp_team_barrier_wait_final`：实际 barrier 状态
  操作前发 arrive，完成后发 depart；生成号在到达前取稳定轮次，取消另列 unsupported。

arrive/depart 描述 barrier 同步区间，不等于精确 wait_begin/end。实际是否等待、自旋
多少次或睡眠多久尚未观测，均不能由区间长度或 syscall 邻接猜测。

## 4. TaoTrace 输出

增加默认关闭的 marker PC 参数。参数开启时必须验证 x86/direct FST/functional 模式。
在 marker 成功提交后读架构 payload，禁止使用 execute-time 的可被 squash 事件。
保存提交前 FST record ordinal，并确认 marker 对应的记录实际被功能流保留；
observer 不插入/删除/修改原 FST 记录。

每核缓冲写 `functional-sync-coreN.jsonl`，事件包含 schema、producer、stream/core、
record_ordinal、boundary=after_record、address-space、logical thread、event kind、
region/object/generation/member/team size 和 warmup/measurement phase。
另写 `oracle/functional-sync-observation.jsonl`，用事件键 `(stream, record_ordinal)`
关联全局提交观察序号和 commit tick。验证器可据此检查实际观察中是否提前 depart；
不能仅按任意 per-core 文件的读取顺序判断跨核先后。观察序号/tick 仅用于离线验证，
功能 sidecar 无时间答案；未来 consumer 不能把观察总顺序强加给独立工作。

结束时输出 metadata：能力范围、源记录数、事件数、marker 参数与共同测量边界。
保持 first-core common-end，关闭后不得补出未观测的 barrier 完成或 worker 记录。
同步未闭合可能是合法边界截断，必须与中途丢事件区分。

## 5. ROI 初始状态与能力门禁

第一组正向机制测试在同步上下文为空的边界启动，逐轮完整执行。随后增加在 barrier
中途结束的测试，检查声明的合法截断。若真实 workload 在 KVM 阶段已建团队且切换
时上下文不为空，现有 O3 observer 不能凭空恢复前史：先通过 warmup 中的已观测
事件建立状态；无法建立时将覆盖判为 partial，不宣称完整 scheduler 输入。

生产级 ROI 初始同步 snapshot 是后续独立扩展。当前 TeaLeaf 验证必须报告是否
捕获到完整 warmup/measurement 上下文，以及不能满足的门禁。

## 6. 验证器

流式检查 schema/字段范围、source population、ordinal 递增及 marker PC；按实际 FST
读取锚点，不仅检查 JSON 可解析。按 region/generation 聚合，检查：

- begin/end 关系、固定 team size、成员唯一性、线程身份稳定；
- barrier 同一轮参与者不重复，到齐后才可有 depart；
- 所有 implicit tasks 到达 final barrier 后才能有 parallel_end；
- 完整窗口中的缺失/未知、unsupported 不能被判为完整；
- declared boundary 的开区间单列 truncated，不伪造事件；
- 记录人口与原始 FST/功能边界匹配；关系来自 event/participant/generation，
  observation oracle 仅检查该关系在采集执行中成立，不提供 consumer 的时间。

需要 negative fixtures：错轮次、重复到达、提前离开、非法 ordinal、错 PC、缺失
参与者和未声明的中途 EOF。验证器不能以补事件修复这些问题。

## 7. 实验顺序与效果定义

1. transport 与 libgomp 独立构建；host 小工作负载检查计算结果和线程数。
2. gem5 SE 小工作负载检查两轮/多轮 parallel、显式 barrier、final barrier、线程池
   复用和不等工作量；输出 on/off 使用相同插桩 binary。
3. gem5 FS 小工作负载验证真实 guest 线程和 kernel 混合流，保持 buffer-114、LRU
   及共同终点；再尝试 TeaLeaf 有限规模诊断，不能因只有一个活跃核就算多核成功。
4. 输出 on/off 记录 source counts、目标执行结果与模拟周期，验证 observer 不改变
   目标人口和行为。KVM boot 差异时应共享 checkpoint，或明确不可逐值比较。
5. 单独报告 sidecar bytes、事件密度、宿主 elapsed time（重复交替运行）；对照相同
   binary 仅衡量 observer，不能据此宣称 runtime 插桩没有扰动。

不进行 FastSim CPI/PMU gain 或吞吐测量，因为 consumer 未实现。
若 later replay，只能使用维护 validator、独立 identity gate 和 CPI 绝对误差列。
默认 FastSim build/tests 为必要回归；gem5 使用当前项目构建环境和约 80% CPU 并行度。

## 8. 首片完成条件

文档、版本化协议、可复现 runtime/collector patch、启用/关闭参数、验证器和运行证据
齐备；正向机制输出通过，负向样本拒绝；至少一组真实 gem5 多线程采集证明各成员
与 barrier 轮次可识别。所有未通过的 FS/TeaLeaf 覆盖或成本测量如实列出，不把机制
测试通过写成完整 OpenMP/OS 调度支持或精度提升。
