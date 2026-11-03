# Marian/XZ 访存服务修复实施计划

> 执行方式：当前会话依次完成；依据前一轮机制报告及用户“优先排查和解决 marian 和 xz”的指令推进，不重复请求方案批准。

**目标：**找到本批 Marian/XZ 的关键误差链，并交付经过反例、真实 ROI 和反向控制验证的最小通用修复。

**框架：**保留 Bound–Weave、Q=1024、现有 sparse feedback；先观察，后单机制修改。不以工作负载/PC/核数或误差符号分派，不拟合延迟，不增加全流事件求解器。

**依据：**[机制审查](../../spec-speed32g-two-stage-error-mechanisms-20261028.md)。

## 约束及工作区

- 原正式预测/FST 及后台采集、审计保持原样；总 FST 400GB、停止395GB、空闲底线30GB。
- 当前工作树已有大量未提交工作。涉及源码先保存当前内容和 SHA，构建/实验写独立 tmp 子目录；不 reset、不以 HEAD 覆盖当前模型，不提交无关文件。
- 读取并遵守精度合同0/8；native cache 真值只取维护验证器选择的 Ruby/SLICC native summaries。
- 若需要重编 gem5 使用用户指定 -j128；优先复用原二进制和已有诊断接口。
- 原快照9项继续作为基线，不用后续完成项替换原聚合。

## 验证重点

1. 观察开关严格中性；配置加载时的校验早于 CLI 覆盖，观察资格写入 leaf cfg。
2. 自身 canWB、已有其他请求授予权限、prepare/head demand 不能混为同一角色。
3. 同 set 不等于驱逐；填充/权限/资源释放必须保留真实先后关系。
4. 未来预约不能阻挡较早独立请求；跨 Q 和测量 barrier 保留活跃状态。
5. 正误差减小不能掩盖 Graph500/FLAC 退化或吞吐损失。

## Task 1：有界关键链观察

- [x] 核对工作树、磁盘、运行任务和原二进制/输入。
- [x] 用原二进制对 XZ C4 core0、Marian C8 core2 输出稀疏 frontier；核对与原输出中性。
- [x] 输出有界稠密样本，定位 clear_page_rep 与 memcpy/memmove store 服务；尚未完成全 prepare/权限链配对。
- [x] 检查原生诊断接口并固定配额；新自然窗口与旧来源不同，未冒充原快照时序。
- [x] 记录明确的 callback/publish 分歧、配额中断及不可用 pipeline 窗口。

产物：tmp/marian-xz-service-repair-20261028/，记录配置、命令、退出/完整输出证据、输入身份及对账。

## Task 2：最小机制修复

- [x] 选择独立 ordinary-store callback/publish；观察参数未消费及 WB 重复计费两个 RED。
- [x] 保存涉及文件/原二进制 SHA；修复由独立默认关闭开关控制。
- [x] 回归包含 SQ release、WB 压力、generic/fast、load/atomic 中性及不支持组合的拒绝；保留既有跨 Q/容量/干扰测试。
- [x] 最终默认构建及完整 fastsim_tests 通过。

首选范围仍为 store 权限/服务与 SQ release；若证据否定，记录原因并沿实际前驱检查，不为满足计划强行修改该处。

## Task 3：真实窗口与吞吐验收

- [x] 同源 XZ C4/C8、Marian C4/C8 单机制 A/B；四项维护验证器均通过来源并给出 CPI。
- [x] Graph500 C16 改善、FLAC C4 不变；六项均报告 CPI、相对/绝对误差和完整聚合。
- [x] XZ C4 真实 ROI generic/fast 目标一致；逐核指令/来源/PMU unknown 单独保留。
- [x] 固定 NUMA、串行交错三个对照，单独报告预热、测量/进程 wall 和 Peak RSS；保留 XZ 首组波动，不宣称正式吞吐准入。
- [x] 自查本轮差分与各收据，更新 curated docs；开关默认关闭，不宣称完整泛化/吞吐准入。

## 执行记录

- 初次稀疏观察在配置阶段被拒绝，未开始回放；原因是 audit 条件在 CLI 的 cpi_attribution 覆盖前校验。将相关观察开关写入 leaf cfg 后重启，未改变模型参数。
- 已交付独立 callback 修复；结果与限制见 [修复记录](../../marian-xz-private-store-callback-repair-20261028.md)。
- Ruling: 不把完整 prepare 生命周期重构与本片混合；当前最小可证伪修复是消费现有协议服务参数，并去掉 publish 中重复 WB 等待。代价是 store miss/prepare 残差仍未解决。
- Ruling: 明确拒绝仍以 load wakeup 反推 callback 的 Sequencer 实验组合；本批关闭这些实验。代价是新开关暂不能与这些组合叠加，旧配置关闭新开关不受影响。
- 独立审查的 Important 已处理；两个测试补强及配置注释也已修正。最终 self-review 以 task-only.diff 和 RED/GREEN 日志为依据，不重新启动第二轮相同审查。

- 聚合只收集十二项已完成的维护验证报告；停止仅为汇总而再次扫描全部 FST 的串行聚合 CLI，未中断任何未完成的单项验证。来源、报告 SHA 和退出收据均保留。
