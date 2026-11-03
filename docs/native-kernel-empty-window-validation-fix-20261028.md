# 合法零内核窗口的验证修正（2026-10-28）

FLAC C4当前正式32GiB来源在预热阶段记录了2,450,602条内核微操作，说明内核采集开启。
测量窗口只有0.677766221ms模拟时间，四核合计37,976,981条用户微操作、17,970,837条
用户宏指令，内核指令、内核访存及系统调用/缺页/IRQ入口均为零。
FST边界、gem5 CPL统计与FastSim计数一致，指令数量和原生摘要守恒均通过。
原维护验证器仍因`kernel_records_present`及`kernel_memory_present`要求大于零而拒绝。

## 改动

`tools/run_native_kernel_fastsim_validation.py`改为检查：

- 内核记录数非负；
- 内核访存微操作数在零到内核记录数之间；
- 原有源请求的内核包含声明、user-plus-kernel范围、FastSim原生模式、
  参考/回放宏指令和微操作计数、原生Ruby来源与守恒检查继续执行。

零执行不等于缺失采集能力；同样，有内核执行但无内核访存也允许通过。
参考包含内核指令而回放丢失、禁用原生模式、缺少内核包含声明、非法计数仍失败。
不把未知参考改为零，不放宽退休load-miss的完整分类条件，不改变FastSim模型或任何FST。

## 验证

新增`tests/test_native_kernel_empty_window.py`，五项测试覆盖上述合法/非法情况。
修改前两项合法窗口测试按预期失败，分别报旧非零检查；修改后5/5通过。
相关维护验证器、退休PMU及时间口径回归日志在
`tmp/native-kernel-empty-window-fix-20261028/related-tests.log`。
默认`cmake --build build -- -j16`与`LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests`通过。

完整Python discovery在补齐GCC11库路径后运行606项：596项通过，7项跳过，3项环境错误。
没有未报告的断言失败。错误为：

- `test_python_bindings`：未安装`fastsim_py`模块；
- `test_uarch_common_end_collection.CollectionPlanningTests.test_checkpoint_recovery_receipt_is_strict_and_reusable`；
- `test_uarch_common_end_collection.CollectionPlanningTests.test_checkpoint_recovery_rejects_record_output`。

后两项依赖不存在的外部
`/data00/yinhaolang/TCSim/configs/gem5/x86_fs_kvm_boot_checkpoint_tao.py`。
全量日志为`tmp/native-kernel-empty-window-fix-20261028/python-tests-gcc11.log`，
最初缺GCC11运行库的失败日志另保留，未据此声称全量Python套件通过。

## 已有结果重评

原10案并行回放及补采继续运行，原冻结验证器及历史报告保持原样。
新验证器冻结到`tmp/spec-speed-32g-fastsim-validation-20261028-revalidated/`，
四路维护CLI使用`--source-root`、`--stats-dir`重评同一份模型预测。
每案等待原回放完成，保留原来源、原预测及其SHA256、配置审核和原统计耗时。
重评前后比较源文件属性与预测哈希；重评不产生新的模型吞吐测量。

新目录中的`summary.md`/`summary.json`、逐案`validation.json`及`worker-status.json`
记录修正后的状态；原目录`revalidation-successor.json`指向此重评。
完整身份和参数证据仍来自原采集/回放链，单独的归档重评不认证二进制来源。

FLAC C4真实归档重评现已完成：维护入口返回0、`status=passed`、错误列表为空，
新计数合法性检查和原有来源/指令数量/原生守恒检查均通过。
`source_stat_unchanged=true`且`prediction_unchanged=true`；
原预测SHA256仍为`7d646907e35bb76595adbe0df7b1495afc27516ab9f329b1543afe90a830203e`。
这证明误拒已消除，不是模型预测精度提高。证据位于重评目录
`cases/c04-817.flac_s/worker-status.json`及
`cases/c04-817.flac_s/validation/cases/c04-817.flac_s/validation.json`。
