# 动态翻译消费端与 Marian C4 单项重采

授权：用户要求依次实施上一轮建议1/2/3，注意磁盘空间；不删除旧FST，不重采其余五项，不提交。沿用现有FastSim脏工作树续作，独立tmp输出。

## 方案与范围
采用已有压缩xlat字典/稀疏关联扩展，接入源记录ordinal，再用实际叶页几何驱动共用64项LRU，复用已有hierarchy walk依赖链。放弃固定配置全用2MiB（无法表达混合页和动态拆页）；不做新的gem5计时调参。

## 特殊地址
实际成功翻译分清分页与数据物理地址编码。IO/MSR非分页；APIC MMIO可能实际经过页表，必须保留该叶大小与PTE路径，不能将其DTLB访问抹掉。
公共TranslationMapping新增 `TranslationKind kind` 、`uint64_t request_physical_base`、`uint64_t request_virtual_base`。enum成员kPaged=0,kLocalApic=1,kIo=2,kMsr=3,kPciConfig=4,kInterrupt=5,kM5=6。物理基址physical_base仍是原叶页真实PA，request_physical_base与request_virtual_base仅特殊kind保存实际请求VA/PA对应锚点；PCI配置访问可能改变页内偏移，不能假设VA与PA低12bit相同。nonpaged特殊kind无leaf/path时virtual_base为VA4KiB基址，page_bits/levels/root/flags全部0；它的4KiB仅是关联定位，不是页大小。分页特殊kind仍按真实leaf范围核验，实际请求PA由request_physical_base+(VA-request_virtual_base)恢复，源端默认记录本次实际VA/PA锚点，禁止溢出/越界。
Wire header version2；definition在原有levels个PTE varint之后追加kind(u8)、request_physical_base(varint)、request_virtual_base(varint)。magic仍FSTXLT1\0，v1兼容解码为kPaged/requestbase0/requestvirtualbase0。未知/CRC/hash/人口不合法fail closed，普通RAM缺页大小或PTE不能变成特殊地址。非分页已认证特殊访问可计完整覆盖；分页APIC仍要求真实leaf/path。

## 消费接口
TraceSource新增 `dynamic_translations() const -> bool` 和 `current_translation_fragments() const -> const vector<fst::TranslationFragment>&`；所有包装器按raw ordinal代理。BinaryTraceSource流式解析gzip，绑定hot行hash和完整热流FNV，保留稀疏token状态、覆盖与重试跨页事实；64byte TraceRecord不改。旧trace的旧行为保留；声明新feature但缺sidecar/事实必须报错。
IntervalCore schedule从TraceSource取当前facts。DTLB key加入页大小，实际值为ASID、VA>>pagebits、pagebits；同一LRU混放。动态映射变化需使相交旧geometry/路径的条目及未完成walk失效，不能让旧walk重新填回。跨fragment按页去重翻译，非分页特殊成员不访问DTLB。未知leaf新trace拒绝。
Simulator hierarchy_walk使用当前动态facts的物理PTE路径，3/4级依次发射，保留walker排队/下层服务依赖。只解除有实现和回归支撑的bounded/pending-fill/shared-service互斥；不能删除配置拒绝后绕过响应反馈或重复填充。固定page_walk_latency不调参。

## 单项采集和验收
gem5重编译用-j128。先小探针完整覆盖/回放，再Marian C4同source配置32GiB、10M common-end、500K warmup；新数据配新stats。保留旧所有FST。复用存储guard：实际空闲>=30GB，全局FST400GB/395GB提前停止；单项预算先按旧约3.6GiB+裕量确定，不复制checkpoint或32GiB大文件。原始FST可回放，xlat始终gzip；采集前实际盘点并预留所有旁表和编译占用。维护validator、独立identity、原生Ruby来源与精度分开报告；CPI含绝对误差，不将来源passed称精度通过。
