# FastSim v2：服务时间接口隔离准备

日期：2026-11-10。状态：固定服务分量组件与定向测试已实现，尚未接入模拟器生产者/消费者；R0/R1 中性及吞吐门禁仍在进行。完整 v2 未验收。

隔离目录：`tmp/parent-service-v2-time-exclusive-20261110/source/`，复制自 R0/R1 候选。原 R0/R1 回放源码和二进制保持冻结。新增文件为 `include/fastsim/service_time_model.hpp`、`tests/test_service_time_model.cpp`；另在隔离 CMake 中注册组件测试目标。

## 已实现的接口

`ServiceTimeModel::Descriptor` 是固定大小、可直接复制的反馈值，分为三部分：

- 功能身份：事务 ID、内容代次、权限代次、controller、前状态及动作，各项有效性独立于数值。
- 配置分量：private request、lookup、lower request、数据/权限服务、返回、fill、controller，各分量使用带域和 epoch 的 Q16 时钟。
- 冻结队列摘要：适用准入区间及 lookup、lower、ack、fill port 四个资源前沿。私有 TBE/fill 槽位准入由唯一容量拥有者另行负责，摘要禁止再次包含该等待。

求值只执行固定深度的 `max` 和加法，不推进共享事件，不扫描 ROI，不重复收敛，不读取旧 absolute response 或旧请求 stall。lower 队列使用窗口前冻结的前沿，实际准入晚到时相应减少剩余等待。

GETX 使用独立数据和权限分支，controller 在两者完成后归约。S→UPGRADE 保留旧驻留数据，只分配 TBE，不分配 data-fill 槽。结果边标注 `estimated`，不会升级成 selected/final 或上界。

未知身份/代次、错误状态动作、不可隔离的共享副作用、跨时钟 epoch、超出摘要适用范围、重复容量收费、未知名义分量及溢出均返回互斥拒绝原因；纯求值失败不修改描述或已接受状态。

## 已完成的验证

组件测试与隔离 CMake 目标 `fastsim_service_time_model_tests` 均通过，另以 `-Wall -Wextra -Werror` 编译通过：

- 对实际准入=8，GETX 从配置分量重算完成边；旧共享 issue=5 不存在于该时间接口的输入中。
- 冻结 lower 前沿下，准入=8 和准入=20 可以得到相同完成时刻，验证没有把旧排队 stall 平移为固定延迟。
- S 升级不生成 fill，数据仍驻留，权限/controller 分开。
- 缺失代次、跨 epoch、超出摘要范围、重复容量收费及溢出均拒绝。
- 放弃一次失败求值后，已接受描述重新求值得到相同结果。

测试的名义参数仅用于构造机制反例，不代表 SPR 配置映射或精度评估。日志为隔离目录 `configure.log` 和 `component-build.log`。

## 仍需完成的接入与验收

当前 shared descriptor 有 LLC fill generation，但并未普遍提供私有内容和权限代次；不能拿 fill generation 或源序号替代这些硬事实。现有 cache 的 set mutation generation 也不能直接当作内容/权限代次。

下一步先完成 R0/R1 性能证据审查，随后由原共享拥有者产生冻结摘要和配置路径分量，在现有一次反馈中按实际准入计算。还须提供真实功能身份与代次有效性、唯一时间拥有者和有限组件回退。

真实 prepare 接管、store send/write/callback 拆分、跨窗口父引用、资源满后推迟准入、半开资源守恒及旧约束逐项切换尚未实现或验收。E/M 无事务确认、兼容 transient 及 unknown 路径当前继续由原实现处理。本片组件测试不证明以上运行时机制已经完成。

prepare 保持开启；新增 estimated 路径没有配置开关或运行时覆盖，LLC/预取迁移与延迟 Tag 发布继续关闭。未合并主源码，未 commit/push。
