# 动态页映射 producer 实施计划

设计：../specs/2026-10-29-dynamic-translation-producer-design.md。使用测试驱动、执行计划和完成前验证技能。原工作树续作，无commit。恢复账本：tmp/dynamic-translation-producer-20261029/progress.md。

1. 低层实际 walker/TLB→Request→DynInst功能事实采集，保存上游增量patch与新增文件overlay。
2. producer gzip字典与稀疏binding writer；与旧FST/mreq流身份绑定，明确unknown和跨页fragment。
3. 独立Python decoder/auditor和损坏/动态变化/压缩测试。
4. 正确gem5-fs目录、Python3.11、128核编译；小型实际gem5测试＋必要FastSim兼容回归。无需大规模重采。
5. 独立审核，修复重要问题，写结果和限制、128核编译收据。
