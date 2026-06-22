# mt_bench

一个简单的多线程负载，包含计算、访存和线程同步三类行为。

## 结构

```
workloads/mt_bench/
├── mt_bench.c
├── Makefile
└── README.md
```

## 用法

```bash
./mt_bench <iter> <threads>
```

- `iter`：每个线程的循环次数，用于控制运行时间（越大越久）。
- `threads`：线程数量。

程序会为每个线程分配一段私有内存并做按 cacheline (64B) 步长的读-改-写；每轮迭代包含两次 barrier 同步以及一次受互斥锁保护的全局归约更新，用于制造同步/竞争。

## 编译

```bash
cd workloads/mt_bench
make
```

产物：`workloads/mt_bench/mt_bench`（静态链接）。
