# SPR 两阶段对齐持续实施记录

依据 `docs/spr-high-throughput-two-stage-alignment-design-20261105.md` 及
`docs/spr-store-prefetch-tbe-two-stage-design-20261105.md`，保持退休功能输入、
原两阶段、高吞吐；gem5 OoO 仅提供独立参考。各分项先微测，再在同源输入上
逐项消融，比较 CPI 绝对与相对误差、perf-like PMU 和吞吐，保留所有失败结果。

身份范围更正：本记录中此前的“同源/身份一致”指同一 manifest、人口以及
旧 `functional_source_identity` 覆盖的文件；该旧策略遗漏 `.sreq/.ffe/.uops/
.core/.archb/.mmuctl/.ixlat` 等新伴随，不能证明它们在运行期间未变。完整
身份策略正在修复，旧实验数字保留为诊断，不事后补称其已有完整身份。

并行任务按文件边界隔离：预取负责 FastSim simulator/配置及预取；原生 PMU
负责 gem5-spr 参考观察；核心资源负责 interval_core 与独立 bank 组件；主线
负责 committed store buffer 摘要与单测、整合与评估。碰到需要同一文件时顺序
合流，避免覆盖既有改动。每条线先做有价值的有限切片，不把单一 unknown 或
长实验当作所有其他工作的阻塞。

## 主线 store 摘要

1. 测试 SQ 和 buffer 共用预算、offload 最大宽度4、严格尾部同线/上下文/flags
合并、已发送不合并、四条不同 line、乱序完成但队头按序回收、跨窗口状态。
2. 实现不逐拍推进的有界摘要；只接收退休功能事实和模型估计的时间/响应，
与现有模拟共享存储资源后再申请模拟器合流。特殊属性/缺失需保留旧路径。
3. 微测通过后再集成阶段二并开展同源消融；单元测试不代表 CPI/PMU 精度。

当前：任务开始，已有工作区包含前次未提交改动；保留所有现状并明确作者边界。

## 2026-11-05：首批并行切片

- 主线：`spr_committed_store_buffer.hpp` 实现共享 SQ/buffer 容量、4 项 offload
  宽度、仅未发送尾项同 line/context/requestor/flags 合并及 byte mask、不同
  line 在途预算、乱序返回后按头部连续回收；`test_spr_committed_store_buffer.py`
  覆盖及复制快照通过。此类接受既有模型产生的有序事件，不执行实际 cache
  服务，未接入 FastSim，不是 CPI 改善证明。注意调用方须按估计事件先后提交，
  不把未来 offload 状态提前应用到更早的完成事件。
- 核心线：L1D bank 独立预约组件及直接抽取 gem5 LSQ 源码对照通过；packet 级
  地址和准入确认后仍需接入第二阶段并保持 rollback。
- 参考线：按需求身份记录 L1D/L2 在途预取碰撞，保守保留 unknown，不改变
  FST；独立 gem5 正按 -j128 构建，须短验证并决定是否重采原生参考。
- 预取线：独立 predictor/stream/filter 与 gem5 规则差分通过；尚须安全接入
  队列、返回、共享流量、preview/rollback；不得将候选立即安装为命中。
- 主线追加 `spr_sequencer_credits.hpp`：普通 demand 与 optional prepare 分账，
  普通预算在同线 alias 检查前约束，alias 仍各占一项；prepare 满时可拒绝而
  不改变 demand，prepare 上限0按 gem5 语义为无限。完成按调用方提供的
  模型事件释放；它不假设同线 follower 均与父请求同时返回，也不冒充 TBE。
  独立测试覆盖容量、同线、prepare、释放、snapshot；尚未接入模拟器。

扩展 `python3 -m unittest discover -s tests -p 'test_spr_*'`：84项中82通过、
1失败1错误。`test_spr_asid_static_map_producer` 的源码抽取 shim 缺
`isReadBarrier`/`macroop`；`test_spr_functional_retirement_capture` 的 shim
缺现有 task-clock/store-facts 接口。当前 gem5-spr `tao_trace.cc` 未提交差异
仅在 `perfRetirementNativeFacts` 的两条观察字段投影，不修改两个失败用例抽取
的方法；这些测试构建桩与本次工作前的当前源码形态不一致。保留失败并安排
修复测试桩，不将扩展回归表述为通过。
- 核心线追加：`spr_macro_fusion.hpp` 静态资格三态组件已对 gem5-spr
  65536 对编码差分；`SprFrontendDelivery` 与 `IntervalTiming` 已携带模型内
  source-batch 标识，旧行为的 6000 宏对照一致。融合尚未启用，不以相邻
  退休宏推断目标 fetch batch。
- 主线开始把 load bank 接入第二阶段，明确实验限制：仅单遍 time-epoch、
  单行且普通的 load；属性不明、跨行、转发走原路径并计数，关闭默认值；
  bank 摘要随既有 TimingFeedback 复制并在接受后提交，使用 dispatch
  单调下界回收。现处于构建和集成测试，未宣称准确率或性能收益。
- 预取线新增仅到期且 coherence 许可仍有效时发布 private fill 的接口，
  覆盖 L1/L2、脏 victim、回滚和 demand 计数隔离的独立测试；预取事务尚未
  接入共享目录和实际模型，不构成端到端预取。
- bank 只读评审发现未过滤非物理地址和功能标记的 split；原第一次 FLAC/四案
  10 个 bank 对照在输出完成前终止，不能用于统计。现已补 `kPhysicalAddress`、
  功能事实存在、非 split、单行单 fragment 的资格及单测；不能正确归因 bank
  的 attribution/frontier 模式暂时显式互斥，重新构建和回放后再报告结果。
- 先前两个 SPR 测试桩经修复后扩展回归 87/87 通过；新增功能单测另行运行。
  前端 batch 标识 integration 测试通过，保留前端仅模型代理的说明。
- bank v2 四案同二进制 OFF/ON 并行中；历史批次 FLAC/Graph500 已完成并保持
  原 CPI，但两者全部 bank 候选走 fallback（分别 5693585、13329354），不能
  用作 bank 效果评估。核查旧 FLAC 的 FST 没有 `.sreq`，而新 FLAC 四核都有；
  最新完整伴随 FLAC 的对照还在运行。旧结果可验证缺事实时不改变行为，
  不能说明真实 workload 不产生 bank 冲突。需检查另外两案是否同样缺伴随。
- store/Sequencer 组件经独立审查修复未来 offload 提前生效、同 line follower
  乱序释放、rollback ID 复用；用共享非回滚 ID 源，store `send_next/complete`
  显式传单调模型 frontier，遇未来 offload 返回 deferred 而不提交。跨 line
  ordered store 顺序由另一个单遍下界组件提供；真实 target park/reissue 未建，
  外层处理 SQ 队首、地址/数据就绪、send port 与特殊请求。目标性能尚未测。
- 源码复核发现 bank 延后可能超总 load-port，停止 bank v2 的未完 FLAC 对照。
  v3 共同预约总端口和已知 bank，unknown bank 不猜但仍占总端口；显式限制
  sparse repair 等事后移动模型。微测和主测试通过，10 个同二进制回放进行中。
- 原生 smoke 证明部分 unknown 与 L1/L2 在途预取碰撞重叠；另一组 core1
  样本为设备 bypass 而非 prefetch。短 ROI 的一核缺 user 退休导致 common
  clock 不可用，不做严格 CPI 对照；新旧功能身份和新版观察字段已验证。
- FLAC、XZ、Graph500、Marian 四个完整新独立 C4 捕获已并行启动，保留旧数据；
  记录和守卫位于 tmp/spr-native-prefetch-observation-20261105，完成后由
  独立 watcher 按案 prepare、identity 及 native 审计，失败不阻塞其他案。

## 后续集成与 FLAC 消融

- bank v4 在共同预约 load port / bank 时仅纳入已证明物理、非 split、单行
  单 fragment 的功能请求；未知请求计入 fallback，不占已认证的联合日历，
  因而混合输入的端口竞争仍可能低估。这修正了上文 v3 对 unknown 占端口的
  描述；后置重排等不兼容模式显式互斥。构建、主测试与负例测试通过。
- 最新 FLAC 同一二进制、同一功能源 OFF/ON，仅 bank 开关不同：任务宏指令
  均为 31,226,613，模型 CPI 0.320469 / 0.320861；同源独立 gem5 参考
  0.282346，绝对误差 0.038123 / 0.038515 cycles/macro，相对误差
  +13.502% / +13.641%。模型端检测到 4,877,514 个 bank packet、75 次
  bank 等待，795,794 次 bank fallback；load-port 4,893,910 个 packet、
  33,034 次等待、794,086 次 fallback。粒度是请求/模型事件，不可等同
  宏指令；起点不严格匹配，结果仅为已允许的参考对照。PMU 正用维护的原生
  validator 单独重评分；并行回放不用于吞吐判断。重评分完成后，工作负载
  退休 L1 load miss 估计 7,918、Ruby 参考 4,016，绝对差 3,902、相对
  97.161%；ISA/服务资格等证据仍不完整。L2/L3 native unknown 为
  1,080/1,094，各自误差不可评分。validator 的 passed 不是精度达标。
- 实验性 L1 预取只支持目标 line 已存在本核 L2 的子域，默认关闭；缺失、
  secure、split 等未证实输入不训练，FCFS、禁止 I-fetch 下层与 preview
  等限制均显式检查。此子域不是完整 SPR 配置，现有 OFF/ON FLAC 仅用于
  评估该子机制；共享层 miss、I-fetch 和 FR-FCFS 尚待合流。
- 该受限子域 FLAC 同二进制 OFF/ON 同源对照已结束，配置唯一差异为预取
  开关、逐核任务宏人口一致（合计 31,226,613）；模型活动 CPI
  0.321724 → 0.319658，模型内部 perf-like 退休 load L1 miss 计数
  8,858 → 5,377。ON 训练 4,877,514、发出并完成 3,896，完成包含到期时
  已在 L1 的情况，不等同于 3,896 次实际新填充；未支持的 L2
  miss 候选 2,485，输入不合格 795,794。上述 miss 是模型内部数值，未与
  native PMU 逐事件评分；禁用 I-fetch 下层、preview 和 FR-FCFS 的设置
  不等于目标 SPR 配置。两进程并发，墙钟不用于吞吐结论。后续检查发现
  相同退休流下 `perf_retired_stores` 为 511,540 / 513,181，人口不守恒；
  定位并修复前不能用这组结果证明 PMU 精度或把 miss 变化归因于预取。
  审查确认这一不守恒属于旧 `scope_metrics.pmu` 的 memory-event 分类；
  新 `scope_metrics.perf_retirement` 的 workload-task 退休宏人口在 OFF/ON
  均为 store 971,895、load 5,968,072，模型 L1 miss 为 7,926 / 4,445。
  宏口径一致本身不证明 PMU 精度。
  已完成独立 native Ruby 逐事件评分：OFF/ON L1 miss 7,926/4,445，
  参考 4,016，绝对差 3,910/429，相对 97.361%/10.682%；事件资格等
  证据仍不完整。L2/L3 参考 unknown 分别 1,080/1,094，不算误差；
  同一非严格起点诊断的 gem5 CPI 0.282346，FastSim OFF/ON
  0.321724/0.319658，绝对误差 0.039378/0.037312 cycles/macro、
  相对误差 13.947%/13.215%。这些数字仅适用于受限 FCFS 子域。
- 旧 store 事件人口漏计已由单条 store 同输入复现：serial 为 1，legacy
  buffer 为 0。原因是 prepare 分类跳过计数，而 legacy 提交没有补回；
  修复仅在该分支成功 publish 后补事件计数，并排除软件预取；新的宏退休
  命名空间不从此处计数。新增 serial、lifecycle、legacy 的 1/2 条普通
  store 负例，正在构建验证，不将它扩展声称为拆分或宏指令认证。
  统一构建、主测试通过后，新旧相同功能源的受限预取 OFF/ON 全流重跑：
  旧事件 store 计数均为 1,046,372；新宏 store 人口均为 971,895；活动
  cycles 仍为 10,046,345 / 9,981,844，与修复前一致。该事件数不等于宏数。
- 共享层 miss 的有界 line 事务/去重/延迟发布和失效代次校验已形成独立
  组件及测试；真实共享目录、写回、等待者和跨 Q 水位仍未接入。
- Store 集成定位见 `docs/spr-store-summary-stage2-integration-20261105.md`：
  当前阶段二逐 UOP 处理顺序不等于事件时间顺序，直接执行未来 offload 会
  破坏更早 dispatch 的信用账。采用有界稀疏未来事件摘要继续推进，最终
  callback、特殊请求和功能字段完整性证明以前不接生产路径。
- 端口选择新增默认关闭 `core.spr_shared_fu_round_robin`：按模型操作类别
  保存小型轮询游标，基于现有可用端口表选择；保留原固定选择作为 OFF 路径。
  组件正反例、IntervalCore 中竞争端口场景和完整主测试通过，同源 FLAC
  OFF/ON 在跑。与 gem5 的 FU 类别循环队列方向一致，但阶段一按退休流
  处理、候选掩码和事件到达不等同真实 OoO 仲裁，属于有界近似。审查后
  修复 generic FUPool 的 branch 在完整能力队列上轮询的错误，并增加实际
  IntervalCore 负例。SPR 还有独立 integer execution-family 仲裁路径，
  当前按 OpClass 的游标未把它与 generic 路径分开，不能称完整端口对齐。
  修复后既有 FLAC 同二进制 OFF/ON 身份与逐核人口一致，模型 CPI
  0.320469/0.321243，独立 gem5 参考 0.282346；绝对误差
  0.038123/0.038897 cycles/macro、相对误差 13.502%/13.776%。起点非严格
  匹配，仅作诊断，模型结果稍变差，保持默认 OFF；并行运行不评吞吐。
- 新增默认 OFF 的 integer-family 阶段一增量：读取策略一致的静态 `.uops`
  family/候选端口，与 generic 使用同一物理端口账；未知、zero、move、潜在
  fusion 回退并单独计数，不读取 gem5 实际 grant。目标 profile 对新 FLAC
  的整数资源形状判定 true，四核 `.uops.gz` 策略摘要均匹配；这是目标配置
  和静态事实核对，不是 OoO 决策或 Atomic/QEMU 独立产出认证。完整构建、
  主测试、integer-family 定向回归、branch 与边界集成测试通过；新 FLAC
  同源 OFF/ON 已完成，逐核 task 人口一致、配置唯一差异为该开关。gem5
  CPI 0.281872、模型 0.321041/0.318890，绝对误差 0.039169/0.037018
  cycles/macro，相对 13.896%/13.133%，起点非严格匹配；尚无隔离吞吐。
  ON 阶段一计数 managed 1,138,703、generic 25,554,024、unknown 804、
  zero/move/fusion 回退 172,305/3,615,364/4,448,834，六项合计
  34,930,034，与计分退休 UOP 数一致。原生 PMU 评分完成：L1 miss
  估计 7,691、Ruby 参考 3,898，差 3,793（97.306%），与 OFF 的 7,690
  基本相同；L2/L3 native unknown 963/971，不计算误差。
- 受限 L1 预取的配置保护漏排除了 `ruby.sequencer_load_admission`，已用
  先失败的负例复现并补互斥；已完成的 FLAC OFF/ON 此开关均为 false。
- 四案新采集均正常退出。Graph500 已准备并通过目标 identity，独立 retirement
  与 task PMU 审核仍在进行；FLAC、Marian 正准备。XZ 准备遇到 `MMU controls:
  boundary partial prefix ffe mismatch`，未放宽检查，正在定位。首次启动的
  Graph500 bank 和修复后 FLAC 预取对照漏写 `--perf-cache-input-identity true`，
  已中止无身份的运行并加身份记录重启；早先已报告的对照均带身份。
- XZ 前读原因：archb 可在消费下一 member 前先读到 terminal，相应 FFE
  前缀尚未到达。reader 改为暂存待核对前缀，到对应 FFE 行时仍执行匹配，
  结束时检查 pending 清空；篡改前缀并重算 hash 的负例及 integration 测试
  通过，原始采集未改，prepare 已重启。首次失败日志保留。
- 完整主测试通过；修正 branch 候选能力队列后的独立 IntervalCore 回归
  通过。新 FLAC 的 source/identity 已检查并启动 bank OFF/ON；其 native
  retirement/task PMU 审核尚在进行，功能翻译 leaf-only 和 core unknown
  仍需按输入限制报告。
- 新批次 FLAC 和 Graph500 均已通过 native source/identity/retirement/task
  检查并完成同二进制 bank OFF/ON；使用已审核的 native 活跃周期与逐核人口
  比较，FLAC gem5 0.281872、模型 0.321041/0.319261，CPI 绝对误差
  0.039169/0.037389 cycles/macro、相对 13.896%/13.265%；Graph500
  gem5 0.652711、模型 0.754031/0.753857，绝对误差 0.101320/0.101146、
  相对 15.523%/15.496%。起点不严格匹配，maintained validator 正完整
  重评分；不可将这些数字与前一采集直接做精度改善结论。Graph500 ON 的
  bank packet 13,329,970、等待 73,667、fallback 256；新 FLAC ON 的
  packet 4,897,965、等待 66、fallback 796,856，均为模型事件单位。
  Graph500 ON 的 maintained validator 已完成：退休 L1 load miss 估计
  556,356、Ruby 参考 276,155，相差 280,201（101.465%），资格证据仍有
  缺口；L2/L3 native unknown 169,751/179,114，不计误差。
  新 FLAC bank OFF/ON 的 maintained validator 已完成：退休 L1 load miss
  估计 7,690/7,696、Ruby 参考 3,898，差 3,792/3,798（97.281%/97.435%），资格证据仍有
  缺口；L2/L3 unknown 963/971，不计误差。它与上述受限预取实验的 FLAC
  为不同采集窗口，不能直接比较误差推断改善。
- 新 Marian 通过相同 native gate，bank OFF/ON 逐核人口一致，gem5 CPI
  1.428304、模型 2.934776/2.913619，绝对误差 1.506472/1.485315
  cycles/macro，相对误差 105.473%/103.991%；是非严格起点参考对照，
  不能认为 bank 已解释较大 CPI 差距。ON bank packet 3,407,670、等待
  11,712、fallback 11,500；完整 native validator 重评分进行中。
  OFF/ON 评分完成：L1/L2/L3 原生 unknown 分别 4/1,856/2,692，三项均
  不可计算可信误差；L1 已知参考 43,387、模型估计 81,710/81,709。
- 新 XZ 经边界读取器修复后通过 source/identity/retirement/task PMU 审核，
  bank OFF/ON gem5 0.603524、模型 0.647965/0.644478，绝对误差
  0.044441/0.040955 cycles/macro，相对误差 7.364%/6.786%；ON 的
  bank packet 3,065,997、等待 23,645、fallback 17,943。逐核人口一致但
  起点非严格匹配，maintained validator 正在评分。
  OFF/ON 的 maintained validator 已完成：L1/L2/L3 native unknown
  27/578/690，三项不可计算可信误差；L1 已知参考 23,616、模型估计
  60,492/60,499。
- 新 FLAC 同源 fill 16/48 OFF/ON：模型 CPI 均为 0.321041，gem5
  0.281872，绝对误差均为 0.039169 cycles/macro、相对 13.896%。ON
  普通 load 候选 8,442、未认证 fallback 180、L1/L2 等待均 0；只说明
  此受限模型未产生等待，不等于完整 TBE 无瓶颈。Graph500 fill ON
  有 556,290 候选、95 回退、L1/L2 等待均 0；OFF/ON 模型 CPI 均
  0.754031，对 gem5 0.652711 的绝对误差均 0.101320 cycles/macro，
  相对 15.523%。两案 OFF/ON 的模型 `retired_cache` 和 `perf_retirement`
  数据完全相同，不能将这项开关称作 PMU 改善。
- 集成后 `fastsim_tests`、branch、integer-family、边界集成测试通过；
  扩展 `test_spr_*` 当前 111 项通过。
- 新 FLAC bank 性能同二进制、同源、CPU 112–119 绑定，顺序 OFF/ON/ON/OFF：
  进程端到端分别 347.66/348.01/348.97/348.16 秒，最大 RSS
  1,536,672/1,519,484/1,526,996/1,497,916 KiB；测量阶段分别
  57.567/57.242/57.657/57.689 秒，预热 8.318/8.931/8.959/8.420 秒。
  OFF/ON 端到端中位数 347.91/348.49 秒（吞吐比 99.834%），测量阶段
  中位数 57.628/57.450 秒（吞吐比 100.310%）。四份输入身份和退休人口
  一致。测试期间有原生评分、gem5 构建或短采集等背景负载，每侧两次，
  只作初步诊断，不作为隔离吞吐验收或四案总体吞吐结论；进程端到端墙钟
  包含输入读取/初始化/身份校验，不能用模型内部 wall_time 代替。

### 四案 store 组、取指信用与预取反馈续记

- 最多 4 成员 store 组已接入默认关闭的现有两阶段路径；真实 Simulator
  专项与主回归通过。相同二进制、逐案相同 v2 身份和逐核退休人口下，
  FLAC/Graph500/Marian/XZ ON 实际提交 68/0/105/28 组，合并层级请求
  123/0/110/29。受限配置的四案 CPI MAE OFF/ON 为 0.426294/0.412621
  cycles/macro，MAPE 为 35.86%/34.77%；开关同时改变其他 store 预算
  路径，不能将变化全数归因于合组。四案 OFF/ON 均通过 native 来源及
  退休人口验证；unknown 与硬件事件资格限制仍在。逐案绝对误差、PMU、
  源及单次 FLAC 串行吞吐见 `docs/spr-two-stage-alignment-results-20261105.md`。
- I-fetch 需求信用首版的四案 MAE 为 0.422851→0.433866 cycles/macro，
  MAPE 为 35.56%→38.59%；大量夹高来自数据请求，开关保持关闭。
  区间账隔离原型未证反馈冲突回退，不合入。具体对照和限制见
  `docs/spr-ifetch-sequencer-credit-20261105.md`。
- L1 ready guard 初版被真实 Simulator 依赖链证明会在最终模型响应之前
  误发，已撤回；撤回后二进制 SHA 与 store 冻结版一致，统一构建、
  主测试和 14 个相关回归均再次通过。初版三组 serial TSO FLAC 实验为
  历史诊断，不能证明正式预取修复；详见
  `docs/spr-prefetch-components-20261105.md`。
- 独立 prepare/head 的真实 Simulator 混合 RED 已复现，隔离接线首轮仍
  失败：跨 Q 的 hint 提前安装 cache 状态，而后续普通请求缺少同线
  未完成事务约束；该原型已停止且未合入。L1 最终 callback/事务的
  隔离原型尚未验证旧 pending 回滚，亦未合入；跨宏融合与严格事件时间
  I/D STLB 仍未完成。所有这些均
  不借原生 OoO 动态时间或错路径，也未把隔离原型当作完成。

### 退休顺序 I/D STLB 与四案联合评价

- 默认关闭的 I/D、同 ASID global 和 2M/4M/1G 大页近似已接入，并通过
  统一构建、主测试及 15 个相关专项；只依赖功能事实和模型自己的时间。
  这不是 gem5 动态 TLB 或完整 MMU/walker。大页 ON 后 FLAC 剩余 70,399
  个模型 unknown 全来自 core 0，首因均为历史 walk root 与退休 ASID
  不符；有限样本缺当前 root 对应功能映射。受控 walker 只读候选已在
  隔离 gem5 测试，但未接入 FST，也未证明有在途 Ruby 消息时 baseline
  中性。具体证据和四案单项结果见 `docs/spr-shared-stlb-increment-20261105.md`。
- 四案联合开启 bank、store 窗口和 I/D STLB（global+大页），与同一
  二进制 canonical OFF 对照：等权 CPI MAE 0.422851→0.371427
  cycles/macro，MAPE 35.56%→31.14%；Marian 仍有 91.54% 相对误差，
  XZ 从高估转为低估且绝对误差增大。模型源身份、task ID、逐核退休
  人口一致；原生参考复用维护 validator 已验证的同源 Ruby/SLICC 数据，
  新组合未独立重评分。保留 ROI 不严格、原生 PMU unknown 和功能 producer
  未完成等边界；逐案、PMU 和配置见 `docs/spr-two-stage-alignment-results-20261105.md`。
  联合 FLAC 同核串行吞吐初测正在运行。
