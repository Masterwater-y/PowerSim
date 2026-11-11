# SPR-M1 源码与 native 补丁分发

2026-11-11。SPR-M1 的 FastSim 源码、功能 companions、父服务组件、
退休 store prepare、退休 load miss 预测与测试随 Codebase `fastsim-spr`
提交。里程碑程序的身份仍由
`configs/spr-m1-retired-prepare.json` 的二进制和配置 SHA-256 定义。
新机器重新构建后的二进制可能因编译器或路径而产生不同哈希；
源码提交不能替代已经冻结的程序身份。

## Native 子仓库的分发方式

`gem5-spr` 保留已公开可获取的基底 gitlink：
`02119d168de477a2782f33d14ad229484ad17de4`。
完整源码差异在父仓库
[`patches/spr-gem5-source-spr-m1-20261111.patch`](../patches/spr-gem5-source-spr-m1-20261111.patch)。
该补丁相对上述基底生成，包含既有功能隔离、采集事实、native cache
观察器，以及 SPR-M1 硬件预取和退休前 early prepare 的默认关闭设置。
它包含所需新增头文件，可以独立应用，不需要先叠加旧日期补丁。

在新 checkout 中，从父仓库根目录执行：

```bash
source scripts/project_tmp.sh
git submodule update --init gem5-spr
git -C gem5-spr rev-parse HEAD
git -C gem5-spr apply --check ../patches/spr-gem5-source-spr-m1-20261111.patch
git -C gem5-spr apply ../patches/spr-gem5-source-spr-m1-20261111.patch
```

应用前确认子仓库 HEAD 是上述基底且没有待保留的本地修改。
当前开发工作区已经具有补丁中的源码，无需重复应用。
父仓库不将未发布的 native 子提交写入 gitlink，不隐式推送
`shown-net/gem5`。`spr-framework` 的本地改动与本次 SPR-M1 分发独立。

补丁通过以基底建立临时 Git index 的应用检查，并核对应用后的受控源码
与当前工作区一致。该检查不重新运行 native 采集，也不重新认证所有
可选实验功能的 native 执行、中性或硬件等价。
未验收的可选功能保持原默认关闭状态。

## 构建与使用

FastSim 标准开发验证：

```bash
source scripts/project_tmp.sh
cmake --build build -- -j16
./build/fastsim_tests
```

首次构建前按 README 配置 CMake。固定 SPR-M1 对照使用保留的冻结程序，
并选择 `configs/spr-m1-retired-prepare.cfg`；后续评测默认入口是
`configs/spr-evaluation-default.cfg`。
实际运行、输入绑定、四负载 CPI 与两类 PMU 误差见
[SPR-M1 使用说明与精度报告](../fastsim-spr-m1-user-guide-and-accuracy-20261111.md)。

源码、测试、配置、工具、脚本、文档和源码补丁进入提交；
构建产物、实验 source、`tmp/` 和批量结果保留在本地。
