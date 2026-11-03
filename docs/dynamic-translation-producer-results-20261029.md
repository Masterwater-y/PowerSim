# 动态页映射 producer 与压缩保存（2026-10-29）

本轮实现 gem5 FS producer 的实际叶页大小、各级 PTE 物理路径和动态关联记录。只增加功能事实采集，没有修改 FastSim DTLB/PTE 模型，也没有重新采集六项工作负载。gem5 编译按用户指定使用 **128核**。

## 1. 从实际翻译取事实

walker 在读取 PTE 时记录物理地址，随 TlbEntry 保存；TLB 命中继续携带该 entry 的叶大小与原始 walk 路径。成功翻译经 Request extension、LSQ 按请求顺序组成 DynInst fragment，producer 在写对应热记录时消费。fault、squash、重试替换和跨 fragment 均保持独立责任；不在提交时重新读取可能已经变化的页表。

记录当前 CR3/PCID 与原始 walk root 两种身份，以及 global 标志。二者可能不同，不强制把过去 walk 的 root 改成当前 root。旧 checkpoint 没有这部分观察元数据时保留真实叶页几何，但 PTE 路径明确unknown，不从最终页表猜测。

本轮不扩展 gem5 原生页大小能力：其现有 walker 对 LongPDP 的行为保留，不能将sidecar支持的30bit编码误认为本轮给gem5增加1GiB页翻译。协议兼容4KiB/2MiB、既有legacy4MiB及将来的1GiB事实。

## 2. 压缩与动态变化

新增 `<trace>.fst.xlat.gz`，magic `FSTXLT1`，FST feature bit9。64byte 热记录和旧vmap v1保持原布局；不把只含初始/ROI入口快照的vmap v2冒充动态时间线。

- 映射字典去重保存ASID、walk root、虚实叶基址、PCID、页大小、PTE路径。
- 非零token的最新关联稳定时不重复保存；页大小/PTE路径改变即写新关联。同VA/PA仍会记录变化，返回旧映射也写新的关联事件。
- token0与多个fragment保留逐记录关联；多fragment之后重新声明下一次单fragment状态，避免继承不完整覆盖。
- 缺失或不匹配的事实显式写unknown；不能把unknown解释为4KiB。关联记录用ordinal delta、字典ID和LEB128整数编码，外层gzip level1流式压缩。
- footer保存完整热流FNV、人口及字典/关联/unknown计数。关联记录另绑hot-record hash。gzip CRC/EOF、footer、源FST与人口守恒分别验证。

压缩fixture：1,000,005条memory记录，含4KiB→2MiB→4KiB、同VA/PA但PTE路径改变、unknown及split转移，只需3个定义、7个关联，gzip文件138bytes。该数字是高度稳定的合成压缩见证，不是实际工作负载的普遍压缩率。

## 3. 审计与严格模式

`tools/fst_dynamic_translations.py`提供独立decoder；`tools/audit_fst_dynamic_translations.py SIDECAR --fst SOURCE`验证全热流hash、逐关联hash、token固定身份、fragment物理对应及完整人口。`--require-complete`额外要求所有memory成员均有叶页和PTE路径证据；不给FST时只解码，不认证coverage。

token固定身份为 `(ASID, VA4KiB, PA4KiB)`；同token允许改变页大小/路径，不允许改成另一个ASID或虚实4KiB页。unknown与多fragment不会清掉这一不可变身份校验。字典的PathKnown必须具有非零walk root和路径；没有PathKnown不能带伪造的root/levels。

**初版消费端边界（后续已更新）：当时FastSim reader会明确拒绝bit9。** 本轮范围是producer与独立审计器，P1/P2混页DTLB/PTE消费者仍待实现；不通过忽略新的必要事实让旧4KiB模型继续宣称支持。旧FST仍可由旧reader回放。明确拒绝已经实际验证，见 `tmp/dynamic-translation-producer-20261029/legacy-reader-rejection.log`。

## 4. 测试和构建

- capture helper C++测试：实际路径保存、Request clone、ASID/walk root分离、4KiB/2MiB重用清理、部分fault前缀、重试替换、缺路径unknown。
- writer C++测试及跨语言全FNV审计：百万次稳定引用与动态转移、unknown、split回到single。
- Python targeted测试19项：损坏gzip/CRC/EOF、varint、header、row hash、stream hash、footer、人口、token身份与路径资格。
- 独立审核发现token身份与路径资格问题，均先复现失败再修复；新feature与旧reader的拒绝是本轮明确保留的接口边界，不声称P1消费者已经完成。
- `cmake --build build -- -j16` 和 `LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests` 通过，保持既有FastSim回归。

gem5命令从 `gem5-fs/` 运行：

```bash
LD_LIBRARY_PATH=/opt/gcc-11/lib64:/root/.pyenv/versions/3.11.14/lib \
TAOGEN_SHARED="$PWD/taogen/shared" \
PYTHON_CONFIG=/root/.pyenv/versions/3.11.14/bin/python3-config \
/root/.pyenv/versions/3.11.14/bin/python3 /root/.local/bin/scons \
  build/X86_MESI_Three_Level/gem5.opt -j128
```

源文件、编译和测试收据在 `tmp/dynamic-translation-producer-20261029/`。新增文件通过overlay分发，既有上游六文件通过 `gem5-collection/patches/0004-functional-translation-capture.patch` 分发；该patch对本轮保存的采集hook前状态做了dry-run核验，不将其夸大为整套历史bootstrap已经重新认证。没有commit。

最终128核构建退出码0，日志末尾为 `scons: done building targets.`；二进制SHA256为 `ad976e751f27918cb7792796d0e6dc79b6913dbca9a3a31509a30a465169bb4d`。完整命令、工作目录、并行度和二进制大小保存在 `tmp/dynamic-translation-producer-20261029/build-receipt.json`。新增头和TaoTrace的6组生产树/overlay镜像逐字一致，manifest中的新增文件与patch哈希已核对。

## 5. 首个实际FS探针

小型静态程序在guest中申请对齐2MiB区域、MADV_HUGEPAGE并预触页，随后KVM→O3，记录10000条测量user记录。实际全热流（含warmup/kernel）1,205,406条，memory 298,228条；33个字典定义、577条关联，压缩sidecar约5.7KiB。独立审计通过热流hash及人口守恒，实际记录到12/21bit叶页，包含用户区2MiB映射。

全流保留3392条unknown，来自两个关联的编码APIC/IO特殊地址；没有伪造普通RAM的PTE路径。故不声称该探针通过 `--require-complete`。后续P1/P2还需要明确区分无需普通页表翻译的架构地址空间；本轮保留未知而不把它们清零或删除后宣称全量通过。

## 6. 实际动态拆页：全部512对通过

维护的探针为 `tests/workloads/dynamic_translation_probe.c`：guest先MADV_HUGEPAGE并预触2MiB区域，smaps实际显示 `AnonHugePages: 2048 kB`；切换至O3后读取整片区域，再对其中一个4KiB范围执行mprotect，触发大页拆分，随后再次读取。

实际捕获 **512个不同token的21bit→12bit转移**。逐对核验ASID、实际VA和PA一致；原3级PTE物理路径是新4级路径的前缀。首对token120在ordinal1,437,114使用21bit/3级路径，在ordinal1,705,989使用12bit/4级路径。这里验证的是实际翻译携带的变化，不是把物理连续性或THP配置当成叶页证据。

完整热流2,025,534条，memory464,861条；586个映射定义、1195条关联，压缩sidecar **19,577bytes**。全流FNV、关联hash和人口守恒均通过；461,045条完整known、3816条unknown，保留严格完整覆盖边界。收据和全部512对见 `tmp/dynamic-translation-producer-20261029/fs-dynamic-sleep-audit.json`，运行日志为同目录 `fs-dynamic-sleep.log`。

这3816条unknown对应3个显式关联：编码LocalAPIC地址 `0x20000000000000b0`、IO地址 `0x80000000000003fa`，以及MSR内部地址 `0x1600000800`（`translateInt()`直接保留该地址，使用local accessor，不经过普通页表）。严格模式已实测拒绝该全流；本轮没有删除这些成员后宣称全覆盖。

探针使用默认1ms KVM quantum，checkpoint之后增加10ms guest sleep，确保短测试的拆页发生在O3采集阶段。此前没有sleep的短探针在切换前已经拆页，只验证了拆分后的映射；10µs quantum探针在切换后停止推进，已终止该单独进程，未将不完整输出计为通过。未因此修改模拟器调度机制。

复现：先用gcc静态编译上述源文件，include路径为 `workloads/spec_roi_integration`，然后运行已有探针runner：

```bash
LD_LIBRARY_PATH=/opt/gcc-11/lib64:/root/.pyenv/versions/3.11.14/lib \
gem5-fs/build/X86_MESI_Three_Level/gem5.opt --outdir=<OUT> \
  scripts/run_gem5_sync_probe.py --workload-binary <STATIC_PROBE> \
  --omp-threads 1 --functional-user-target 500000 \
  --mem-size 3GiB --mem-channels 1 --warmup-insts 0 \
  --max-ticks 100000000000000
python3 tools/audit_fst_dynamic_translations.py <SIDECAR> --fst <FST>
```

本轮完成producer、压缩协议与审计验证；尚未完成FastSim混页DTLB/PTE消费端，未据此报告CPI改善或六项重采结果。

后续进展：同日已实现特殊地址v2和FastSim混页DTLB/PTE消费端，当前能力与单项重采验证见 [消费端实施记录](dynamic-translation-consumer-results-20261029.md)。上文初版reader拒绝及unknown探针结果保留为历史证据。
