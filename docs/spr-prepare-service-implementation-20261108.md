# SPR store-prepare follower 活关联实施记录

目标与门禁见 [结果链修订方案](superpowers/specs/2026-11-08-spr-retired-cache-result-path-repair-design.md)。本记录为进行中工作，不能据构建或单元通过宣布最终验收。

在 L0 的 S/I 到 SP_IE 转换创建有界活服务，合并的读请求持有实际服务引用。只有 `u_writeDataToCache` 观察到数据返回后，完成回调才能转换为既定的权限或非驻留 follower 估计。TBE 释放后，仍活的 follower 持有服务；失效、逐地址新代替换使旧服务未知；最后引用销毁时自动清除活索引。服务索引受容量门禁保护，退休时仍只归并常数大小结果。`Data_Stale` 为独立属性，不覆盖已有 miss 位。未改变功能 trace 或 FastSim 时序输入。

生产回归首先复现缺失服务结果，随后通过：两种入口、数据尚未完成、TBE 释放后 follower 完成、失效、新代替换、多个消费者、stale 和 70,000 个提前释放请求。SLICC action 顺序以实际状态机条目驱动测试，覆盖 prepare 先完成和 store 随 data 返回唤醒两种路径。FastSim 默认构建、`fastsim_tests`、退休事件专项及 22 项退休缓存契约回归通过。gem5 gold/-j128 首次因 NodeID 声明错误失败并恢复旧二进制，修正后构建通过。

第一轮真实 C4 小 FS：三个模式均到 common-end，capture 返回码为 0，独立目标身份有效，native response 守恒和 drain 通过。mode 0 每核人口和三个 miss 事件 unknown 均为 0；mode 1 每核人口 unknown=0，每个 miss 的核 0–3 unknown=6/4/1/2；mode 2 每核人口 unknown=0，每个 miss 的核 0–3 unknown=22/27/14/24。mode 2 相比旧运行 181/154/106/90 有减少，但不同运行的调度和人口并不完全相同，不能把差值解释为同人口精度或严格因果量化。mode 1 的代表性未知是内核普通内存读改写指令，例如 `0xffffffff81077eea`，其 Sequencer 类型属于读改写入口；原 SLICC admission 只接纳 LD/Load_Linked。活 prepare 服务统计为全模拟器累积快照，不是退休人口：mode 1 begun=652196、linked=87、TBE closed=652196、destroyed=652196、live=0、peak=20；mode 2 begun=65901、linked=67113、TBE closed=65900、destroyed=65899、live=2、live TBE=1、peak=18。

已为 RMW_Read 和 Locked_RMW_Read 补实际 admission 条件回归，回归先失败；增加读取类型后通过。驻留读改写权限路径标记估计，普通 store/取指仍不视为读请求。增加未完成 prepare 服务的逐请求 data/invalidated/TBE 状态，供剩余未知逐类诊断，不改变 fail-closed 判断。新二进制正在构建，仍需重跑三个小 FS、同起点中性、同源回放和四负载；硬件等价未验证。

第二轮的 gem5 `gem5-rmw-build.log` 显示构建成功。C4 三模式真实采集均到 common-end、capture 返回 0、guard 未触发；三个独立目标身份有效、native response 守恒和 drain 通过。维护退休审计无 evidence gaps。mode 0 和 mode 1 的四核退休 load 人口及三个 miss 事件 unknown 均为零；mode 2 的每核人口 unknown 为零，但每个 miss 事件的核 0–3 unknown 为 27/17/34/15。三模式总体门禁未通过。

mode 2 代表样本的状态为 `dataComplete=true` 且 `invalidated=true`（status=3）；既有标记不能区分失效在数据返回之前还是之后。同一全局快照中准备服务 begun=63649、linked=63314、data completed=63649、TBE closed=63649、destroyed=63648、live=1、live TBE=0、peak=18；live 计数为 core finalization 时快照，不能自动解释为泄漏或全部请求已清空。为区分失效顺序及新代替换，增加每样本诊断位，未改变分类；生产测试先失败后通过。诊断版 gem5 已构建并开始新的 mode 2 采集。零 unknown、真实路径覆盖、同起点中性、同源回放和四负载仍未全部完成。

失效顺序诊断采集 `order-diagnostic-mode2/` 到 common-end，返回码 0。代表性逐核 unknown 样本同时出现 status=11（数据完成、失效发生在数据之前）、status=19（数据完成、失效发生在数据之后）、status=51/59（还发生同地址新代替换）；每核 native 诊断最多保留 8 个样本，这些不是全部未知的分类比例。全局快照 prepare 服务 begun=62367、linked=61590、data completed=62367、TBE closed=62367、destroyed=62366、live=1、live TBE=0、peak=18。新代和真实失效不能统一清除。增加失效来源位区分协议 invalidation ack、CPU eviction 通知和数据块释放；该改动仅为诊断，生产组件测试先失败后通过。后续短窗口用于定位，不能替代正式三模式验收。

短诊断 `invalidation-short-mode2/` 使用单独的 50K warmup/10K target，只用于定位且不作为正式评价。捕获到的代表性数据前失效来自协议 invalidation ACK；数据后失效来自 CPU eviction 通知和数据块释放。对未被新代替换的同一活 TBE，后续实际数据写回可恢复此前 ACK 后的服务有效性，但 resident 入口还要求 follower 合并时的驻留证据有效；新代替换、数据后的真实失效及缺少证据继续 unknown。以实际 SLICC action 顺序执行的生产回归覆盖先 ACK 后真实数据、ACK 前/后 admission、无数据、数据后失效和新代替换。

对于旧 prepare 数据返回后又被真实逐出的情况，在同一 Sequencer line callback 内借用当前 leader 的活查询结果：只有实际 L1 resident/permission 观察，或实际 L1 miss 且本次 SLICC 数据块写入后专用 callback 提供数据完成证据，才能为该 follower 产生估计结果。只使用本层实际事实按既定 follower 规则给出三位，不复制 leader 的下层 miss；无实际查询、无数据完成证据、缺少关联仍 unknown。`writeCallbackData` 仅从 `u_writeDataToCache` 后的 `hhx_store_hit` 调用，保持原 callback 的时序与参数含义；普通最终 hit 布尔值不是数据证据。生产回归先失败，修订后覆盖 miss 缺数据、miss 有数据、resident、缺失查询及下层父 miss 不被复制；新的 gem5 正在构建和后续 FS 验证，尚不能宣称收敛。

后续审查发现 `SP_IE, Store, IM` 原有 merged-miss action 同时覆盖非驻留填充和驻留权限升级，不能直接把它一律解释为数据 miss。之前启动的 `rebind-mode*` 采集因此主动中断，没有完成回执，不能用于验收。现由同一活 prepare generation 在 merged-miss action 之前标记实际入口：非驻留仍按数据填充处理；有效驻留入口保留权限估计；缺少服务或入口证据失效时保持 unknown。重新关联 follower 仅使用 leader 当前查询，不继承先前 attempt 的 sticky miss。生产组件和 SLICC 顺序回归通过；修订后 gem5 构建与三模式真实验证尚在进行。

`guard-mode*` 三个 C4 采集均正常完成，独立身份、native 守恒及 drain 通过，维护审计无证据缺口。mode 0、1 四核的退休 load 人口和三级 miss unknown 均为 0；mode 2 人口 unknown 均为 0，三级 miss 每个事件的四核 unknown 均为 16/2/7/8，故三模式零 unknown 门禁未通过。mode 2 代表样本为 SP_IE 的 resident prepare 先收到协议失效 ACK，读 follower 随后进入，当前 generation 之后实际写回数据；采样上限和不同人口禁止将其推广为所有未知的比例。针对该顺序增加生产回归，先失败；实现仅当 follower 在 ACK 后、该活 generation 数据返回前合并，且失效来源仅为该 ACK 时记录待数据填充，实际写入完成后按 follower 规则给估计结果；无数据、真实逐出、新代替换仍保持 unknown。回归通过，新的二进制和真实采集待重新验证，不能将修复等同于验收。

ACK 后规则重建成功；`post-ack-mode0/1/2` 三个 C4/32GiB/500K warmup/50K target 均退出 0、guard 未触发，独立目标身份有效、native 守恒和 drain 通过。维护审计无证据缺口，三模式四核的 load 人口均非空，人口及 L1/L2/L3 miss 的 unknown 均为 0。实际退休路径包括 data、转发、demand/prefetch follower、软件预取、权限、remote、stale、split/retry；partial forward、device、LLC follower 和 multiple consumers 这些计数在三个模式中为零，不能声称真实 FS 已覆盖。组件回归包含部分转发、拆分及多消费者，但不能替代缺失的真实路径覆盖。

使用 `guard-mode*` 与 `post-ack-mode*` 的严格同起点比较全部不通过：三对 staged checkpoint 和目标配置相同，但功能输入、退休人口、窗口和执行计数不同。该比较只涉及本次最后 ACK 修订前后，不能替代完整旧观察器基线，也不能单凭差异断定观察器改变了功能流；有界未知原因未被豁免。同一新二进制复跑及更早保存的旧二进制对照已从相同检查点启动，用于区分重复性与完整变更影响；严格中性、同源回放、四负载及硬件等价均尚未通过。

两组补充控制均正常采集并完成准备：`neutrality-repeat-test.json` 比较同一新 gem5 二进制的两次运行，`neutrality-old-new-test.json` 使用更早保存的旧二进制对比新二进制。两组的 checkpoint、目标及参与核均相同，证据完整；功能输入哈希、退休人口、窗口、执行计数及旧 native 摘要仍不同，因此严格比较均不通过。特别是同二进制复跑也不一致，表明仅用现有 staged checkpoint 不能建立该实验所需的同功能流前提，但这不能证明不同二进制的观察器中性。现有 checkpoint 是 launcher fork 前的 KVM 状态，之后仍有 KVM 阶段及工作线程初始化；它们是可研究的重复性差异来源，不视为已证实根因。下一步需建立并验证能固定实际功能输入和窗口的对照起点，再重新比较；不得放宽现有门禁。
