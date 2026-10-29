# 动态页映射 producer 与压缩 sidecar

用户授权：实现 gem5 producer 的动态叶页/PTE 路径；压缩保存；gem5 重编译使用128核。只增加功能事实观察，不改 TLB/walker/cache 语义，不重采六项大负载，不改 FastSim DTLB 模型。

实际 walker 读取各级 PTE 时把物理地址存入 TlbEntry；实际翻译完成时，将叶大小、虚实页基址、当前 CR3/PCID、原始 walk root、global 标志及 PTE 路径通过 Request extension 复制到 DynInst 的成功 fragment 列表。TLB hit 沿用 entry 中的功能事实，不记录 hit/miss；旧 checkpoint 缺 path 时显式 unknown，不能从最终页表补造路径。支持split fragment及翻译重试；fault/squash不得把其他访问的事实写入本记录。

新增 `.fst.xlat.gz`，不重定义已有 vmap v2（两份边界快照不能表达动态变化），也不改变64字节热记录。feature bit9声明此 companion。公共数据接口 `include/fastsim/fst_translation_facts.hpp`，gem5镜像 `taogen/shared/fst_translation_facts.hh`。Mapping属性：address_space_id,walk_root,virtual_base,physical_base,pte_addresses[8],pcid,page_bits,levels,flags。Fragment属性：virtual_address,physical_address,size,mapping。DynInst字段 `std::vector<fastsim::fst::TranslationFragment> functionalTranslations`。

压缩为两层：mapping字典去重＋token最新关联变化的稀疏记录；外层gzip level1流式压缩。页大小/PTE路径改变即不同字典项，即便VA/PA不变；回到旧字典项也写新的有序binding。token0（跨页等情况）每次明确记录，不假设可从旧token恢复。没有成功翻译事实的memory成员写unknown binding，不能继承旧known状态。未知状态明确保存，审计可要求全覆盖时fail closed。

字节协议（全部整数除显式fixed外均为无符号LEB128，限制64位）：

- Header `<8sIIIIQ>`32字节：magic FSTXLT1\0、version1、header_size32、core_id、flags1、reserved0。
- tag1 definition：mapping_id（从1递增）、address_space_id、walk_root、virtual_base、physical_base、pcid、page_bits(u8)、levels(u8)、flags(u8)、levels个PTE物理地址。mapping_id0保留unknown。
- tag2 binding：ordinal_delta（相对前一binding，初值0，同一记录多fragment允许0）、hot_record_hash固定u32LE、token、fragment_virtual_address、fragment_size、mapping_id。token非0时稳定关联可省略，状态按token缓存；token0强制逐记录写。
- tag255 footer：record_count、memory_records、definition_count、binding_count、unknown_records、hot_stream_hash固定u64LE。hash使用现有mreq完整热流FNV，不含timing或oracle。gzip CRC、footer及EOF完整性均强制审计。

binding覆盖该token未来记录直到下一更新；仅对同token完整single-fragment观察允许省略。多个fragment与token0全部保留。每个mapping的精确fragment physical地址由mapping基址＋VA偏移给出，需与实际翻译fragment一致；无法完整覆盖或地址空间不匹配时unknown。

测试：4KiB→2MiB→4KiB、同VA/PA但路径改变、稳定百万次引用压缩、split/unknown/ASID、损坏gzip/varint/row hash/footer、实际producer编译与小型gem5探针。所有新增上游文件走overlay；已存在上游文件的改动走独立patch。保留当前工作树其他修改，不commit。
