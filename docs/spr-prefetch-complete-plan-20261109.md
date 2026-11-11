# SPR 预取方案实施与验证

继续原方案，保持两阶段、一次 CPU 响应反馈，不重仲裁反馈前已冻结的窗口。

已接线，待本轮构建与 FST 验证：

- L1/L2 队列、下层供给、在途跟随。L1 同时由队列头与控制器服务/资源释放事件驱动，一次处理所有当前可接纳候选。
- 统一服务资源记录保留 ticket、核/控制器、物理行、origin、issue/response、parent_service、目标 fill 和失效状态。已有 L1 发布记录和 L2 返回队列保存发布动作，引用同一资源/服务身份；不保留完整执行历史。
- demand 和预取共享控制器资源区间。L1 fill=16、L2 fill=48，TBE=256；不占硬件预取的 CPU Sequencer 槽。取消的数据保留流量与资源释放时间。填充释放由本窗口固定服务时间及一次 CPU 反馈修正，不回放本窗口仲裁。
- 同核及跨核同线预取共享已发下层服务，接收核保留独立返回/填充与父 ID。跨批次 demand 服务可给后续候选与需求提供剩余等待。
- 本地 store 预约使用逐行限制；接受的同线写使共享服务失去发布资格。未来响应不提前发布缓存行。
- L2 使用功能请求的 cacheable/nonsecure/not-strict/PT-walk/prefetch 属性判断 GETS/GETX 训练，包含合格 hit、split fragment 和 atomic 请求；L1 stride 继续只使用已知虚拟映射的普通 load。
- L1 脏 victim 走现有 L1→L2 写回和 private eviction 路径。新增 L1 预取替换/写回/未使用行淘汰计数；取消和失效单独计数。缓存逐行标记在首次合格使用后清除。

明确近似边界：控制器 TBE 与 fill 当前共用数据响应作为释放边界；同一窗口尚无固定完成时间的 demand 下层源仍限制候选机会并计数。有限宿主队列有 overflow 计数。只重放已退休功能路径，不生成 wrong-path 流量，不声称硬件等价或 gem5 动态顺序等价。

必要验证：现有 fastsim_tests、单个控制器资源关键检查、当前四份新 FST 的单配置回放，以及维护的 retired-cache/time-accounting 比较函数。没有 ON/OFF 或新增微负载矩阵。Marian native 参考的两条 unknown 不会被改写，三级 miss 保持不可评分。

## 组合回放修复记录

首轮组合回放在 FLAC、XZ、Marian 触发时间回退，结果不用于准确性评分。最终只读审查定位并修订：资源等待后的 shared frontier；L1 发布导致的 L2 victim 延后取消；L2 follower 独立接收侧 L1 fill。另修正 FIFO 队首重试后暴露旧候选 ready 的问题：next_ready 不能早于预测器已处理前沿。

控制器资源检查 tests/test_spr_controller_resources.cpp 已通过。当前正在重建并准备同一四份 FST 的 reviewed 回放。

用户收窄本轮范围为硬件预取：软件预取提前确认保持原配置，不开启、不扩展；后续单配置回放仅用于 L1/L2 硬件预取验证。

## 当前版本的服务时间语义

服务表的数据就绪时间在第二阶段选定后保持固定；CPU 反馈只调整需求响应与资源释放，不重新仲裁当前窗口。模型响应使用固定服务就绪时间和 corrected issue 的本地最小返回时间取最大值。数据未知的预约不能作为父服务；已完成数据只有仍驻留缓存时才能由旧记录供给。请求来源随父链保留，普通 demand 合并不会触发硬件预取续流。

本轮硬件范围内，软件预取的 early-ack 开关保持原默认关闭。功能流中的软件预取指令仍按既有路径执行，不从输入删除。

## 控制器等待的局部性

硬件最终候选版四项回放正常结束，但 CPI MAE 达到 13.831218，Marian CPI 52.983988。诊断表明控制器 fill 等待推进了全局 shared frontier，产生大量跨核连带 clamped_cycles。该结果只作为缺陷证据。

修订为：lookup/事件推进继续使用固定排序的原始模型时间；相关请求单独预约未来控制器 admission 与服务 response，不调用 advance_shared 跳向它的未来 admission。需求可以按协议跟随已经接受并排队的需求请求，但硬件候选不能借用尚未实际发出的服务。服务记录分别保存 accepted、issue、data_ready 与 release。未来 cache tag 不能代替可读数据，需求查询也检查本层在途服务。

## 硬件预取关闭对照

按用户后续要求，使用与 hardware-local 开启版相同的二进制、FST、preview/排序/反馈路径，仅将 L1/L2 suppress_issue 置 true。四案返回 0，硬件预取 issued 均为 0。gem5 参考仍为本批开启预取的采集。

关闭后 CPI MAE 为 2.495542 cycles/macro（开启为 2.465935）。四案包含 Marian unknown 暂定计数的退休 miss 诊断 MAPE：L1 100.26%、L2 319.59%、L3 347.66%（开启分别为 89.86%、298.31%、298.84%）。Marian 关闭 CPI 11.332862，gem5 1.488882，绝对误差 9.843980 cycles/macro。关闭预取没有消除主要 CPI 残差。

完整计数和误差位于 tmp/spr-fst-recapture-20261109/local/prefetch-hardware-off/comparison.md。
