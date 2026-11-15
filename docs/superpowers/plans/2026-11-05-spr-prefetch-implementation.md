# SPR 硬件预取实施计划

依据 docs/spr-store-prefetch-tbe-two-stage-design-20261105.md；不改变两阶段框架、不消耗 OoO 事实。

1. 先独立移植 gem5-spr L1 `observeDataAccess` 的 PC/context/secure 四路组相联步长规则；测试首次访问、重复地址、正负步长、步长改变、替换、页边界、物理映射与复位。该组件只产候选，不修改 cache；不能声称预取已接通。
2. 将 PC、虚拟地址、上下文及功能准入属性绑定到阶段二事件；验证 warmup、分片、ASID 和未知字段。预测器状态参与 preview/rollback，候选仅在接受后提交。
3. 候选进有界异步队列，接入既有 cache/shared hierarchy 与带宽/fill 资源：未返回不可提前命中，跨 chunk 保留、去重且不污染退休 PMU；完成 L1 后再依 gem5 规则接 L2 miss/filter/相邻行。验证跨核交错和需求合流。
4. 同一冻结二进制与 FST 做 OFF/ON FLAC 及四案 CPI（含绝对误差）、退休 PMU 与吞吐比较；先后运行避免竞争；native unknown 如实保留。
