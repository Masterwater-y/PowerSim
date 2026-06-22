# cache_bench

一个单核、单线程的微基准，用来在 gem5 → SIFT → Sniper 的流水线里
产生可观测的多级缓存行为。

## 结构

```
workloads/cache_bench/
├── cache_bench.c   # 源码
├── Makefile        # build & run helpers
└── README.md       # 本文件
```

## 设计目标

- **单核单线程**：方便 gem5 SE 模式 + SIFT 单 trace 输出。
- **计算 + 访存**：kernel 里夹带整数乘/异或/移位，并对每条 cache line 做读-改-写。
- **显式多级缓存行为**：三段 kernel 的 working set 分别设定为：
  - `L1`: 16 KiB → 命中 L1D (48 KiB)
  - `L2`: 512 KiB → L1 放不下, 命中 L2 (2 MiB)
  - `L3`: 16 MiB → L2 放不下, 命中 L3 (96 MiB)
- **参数控制时间**：唯一的命令行整数 `N` 控制每段 kernel 的循环次数。
- **输出运行时间**：每段和总计都用 `clock_gettime(CLOCK_MONOTONIC)` 打印 elapsed。

## 编译

```bash
cd workloads/cache_bench
make
```

产物: `workloads/cache_bench/cache_bench` (静态链接)。

## 直接在宿主机上跑 (健康检查)

```bash
./cache_bench 8
```

输出形如:
```
[cache_bench] iters-per-level = 8
[cache_bench] footprints: L1=16 KiB, L2=512 KiB, L3=16384 KiB
[L1] elapsed = 0.000123 s, checksum = 0x... , bytes=16384,   iters=8
[L2] elapsed = 0.001234 s, checksum = 0x... , bytes=524288,  iters=8
[L3] elapsed = 0.032145 s, checksum = 0x... , bytes=16777216,iters=8
[TOTAL] elapsed = 0.033502 s (L1=..., L2=..., L3=...)
```

## 经过 gem5 产出 SIFT trace

从仓库根目录 `/data00/yinhaolang/simulators`:

```bash
cd gem5
./build/X86/gem5.opt \
  --outdir=out/cache_bench \
  configs/example/gem5_library/x86-se-xeon8457c-o3.py \
  --num-cores=1 \
  --sift=out/cache_bench/trace.sift \
  --cmd=/data00/yinhaolang/simulators/workloads/cache_bench/cache_bench \
  -- 8
```

跑完产物:

| 文件 | 作用 |
|---|---|
| `gem5/out/cache_bench/simout.txt` | workload 自身 stdout (含每段 elapsed) |
| `gem5/out/cache_bench/stats.txt`  | gem5 自己的统计 |
| `gem5/out/cache_bench/trace.app0.th0.sift` | 喂给 Sniper 的 trace |

或者直接用 Makefile:

```bash
cd workloads/cache_bench
make run-gem5
```

## 喂给 Sniper

```bash
cd /data00/yinhaolang/simulators/snipersim
./run-sniper --traces=/data00/yinhaolang/simulators/gem5/out/cache_bench/trace.app0.th0.sift
```

## 调节运行时间

唯一参数是 `iters`:

| iters | 三段总访存次数 | 预期 gem5 O3 仿真耗时 (参考) |
|------:|------:|---:|
| 1   | ~2M     | 秒级 |
| 8   | ~16M    | 十秒~分钟 |
| 64  | ~130M   | 分钟级 |
| 256 | ~520M   | 接近小时 |

gem5 O3 本身很慢, 建议先从小 `iters` 起验证 trace 可用, 再加大做实验。

## 备注

- `clock_gettime` 在 gem5 SE 下返回仿真时间, 所以 `elapsed` 代表仿真语义的时间;
  这对 sniper 消费 trace 做性能分析没有影响, 仅是为了 workload 自己能输出时间。
- 编译强制 `-static` 是为了避开 SE 模式下无法加载 ld.so 的问题。
- 访存步长固定 64 B (一个 cache line), 编译器不要用 `-O3 -funroll-loops` 过度
  展开, 默认 `-O2` 已足够并且与 sniper 官方 recorder 行为接近。
