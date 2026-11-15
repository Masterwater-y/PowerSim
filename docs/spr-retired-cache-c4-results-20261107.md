# SPR 退休 cache 观察器：四负载 C4 验收

实际采集验收通过 0/4 个负载。验收范围为 workload_task：每核合格退休 load 人口非空、人口 unknown 为零，L1/L2/L3 load miss 的成员 unknown 均为零。

采集采用原 Marian、Graph500、XZ、FLAC C4 输入、10M measured user UOP 目标、500K warmup 和 SPR DDR5/32GiB split-range 配置。身份校验与服务证据审计独立执行；事件按退休宏指令计数。

零 unknown 不表示全部 exact，也不表示 SPR 硬件 PMU 等价认证。转发、合并、预取跟随及部分多 UOP/跨行规则仍保留 estimated；未做真机 perf 采集，也未计算 FastSim PMU 精度、CPI 或吞吐。

| 负载 | 核 | 合格 load | load unknown | L1 miss unknown | L2 miss unknown | L3 miss unknown | 身份 | 验收 |
|---|---:|---:|---:|---:|---:|---:|---|---|
| c04-872.marian_s | 0 | 1701862 | 0 | 0 | 4 | 4 | True | False |
| c04-872.marian_s | 1 | 1718344 | 0 | 0 | 4 | 4 | True | False |
| c04-872.marian_s | 2 | 1811711 | 0 | 1 | 5 | 5 | True | False |
| c04-872.marian_s | 3 | 1917934 | 0 | 0 | 933 | 933 | True | False |
| c04-854.graph500_s | 0 | 3329643 | 0 | 0 | 13 | 13 | True | False |
| c04-854.graph500_s | 1 | 3332809 | 0 | 0 | 8 | 8 | True | False |
| c04-854.graph500_s | 2 | 3331302 | 0 | 0 | 8 | 8 | True | False |
| c04-854.graph500_s | 3 | 3334430 | 0 | 0 | 10 | 10 | True | False |
| c04-801.xz_s | 0 | 385661 | 0 | 0 | 1 | 1 | True | False |
| c04-801.xz_s | 1 | 1686572 | 0 | 4 | 4 | 4 | True | False |
| c04-801.xz_s | 2 | 1912462 | 0 | 1 | 135 | 135 | True | False |
| c04-801.xz_s | 3 | 1922234 | 0 | 7 | 172 | 172 | True | False |
| c04-817.flac_s | 0 | 1487569 | 0 | 0 | 19 | 19 | True | False |
| c04-817.flac_s | 1 | 1548876 | 0 | 22 | 237 | 237 | True | False |
| c04-817.flac_s | 2 | 1556007 | 0 | 0 | 1 | 1 | True | False |
| c04-817.flac_s | 3 | 1391618 | 0 | 6 | 393 | 393 | True | False |

| 负载 | 核 | 事件 | 成员 exact | 成员 estimated | 非成员 exact | 非成员 estimated | unknown |
|---|---:|---|---:|---:|---:|---:|---:|
| c04-872.marian_s | 0 | L1 miss | 8049 | 0 | 543491 | 1150322 | 0 |
| c04-872.marian_s | 0 | L2 miss | 4402 | 0 | 411706 | 1285750 | 4 |
| c04-872.marian_s | 0 | L3 miss | 3963 | 216 | 411920 | 1285759 | 4 |
| c04-872.marian_s | 1 | L1 miss | 8169 | 5 | 550384 | 1159786 | 0 |
| c04-872.marian_s | 1 | L2 miss | 4362 | 2 | 419997 | 1293979 | 4 |
| c04-872.marian_s | 1 | L3 miss | 3940 | 214 | 420183 | 1294003 | 4 |
| c04-872.marian_s | 2 | L1 miss | 8552 | 0 | 598438 | 1204720 | 1 |
| c04-872.marian_s | 2 | L2 miss | 4513 | 111 | 462704 | 1344378 | 5 |
| c04-872.marian_s | 2 | L3 miss | 3696 | 317 | 463304 | 1344389 | 5 |
| c04-872.marian_s | 3 | L1 miss | 19541 | 1 | 660539 | 1237853 | 0 |
| c04-872.marian_s | 3 | L2 miss | 4208 | 813 | 543394 | 1368586 | 933 |
| c04-872.marian_s | 3 | L3 miss | 3380 | 1013 | 543999 | 1368609 | 933 |
| c04-854.graph500_s | 0 | L1 miss | 68861 | 0 | 1109259 | 2151523 | 0 |
| c04-854.graph500_s | 0 | L2 miss | 15549 | 42317 | 1120235 | 2151529 | 13 |
| c04-854.graph500_s | 0 | L3 miss | 13110 | 44618 | 1120373 | 2151529 | 13 |
| c04-854.graph500_s | 1 | L1 miss | 69185 | 0 | 1109235 | 2154389 | 0 |
| c04-854.graph500_s | 1 | L2 miss | 15739 | 42216 | 1120457 | 2154389 | 8 |
| c04-854.graph500_s | 1 | L3 miss | 13269 | 44573 | 1120554 | 2154405 | 8 |
| c04-854.graph500_s | 2 | L1 miss | 69214 | 0 | 1108029 | 2154059 | 0 |
| c04-854.graph500_s | 2 | L2 miss | 15523 | 42654 | 1119058 | 2154059 | 8 |
| c04-854.graph500_s | 2 | L3 miss | 13045 | 45006 | 1119180 | 2154063 | 8 |
| c04-854.graph500_s | 3 | L1 miss | 68548 | 0 | 1114316 | 2151566 | 0 |
| c04-854.graph500_s | 3 | L2 miss | 15932 | 41668 | 1125254 | 2151566 | 10 |
| c04-854.graph500_s | 3 | L3 miss | 13506 | 43969 | 1125377 | 2151568 | 10 |
| c04-801.xz_s | 0 | L1 miss | 1528 | 0 | 197169 | 186964 | 0 |
| c04-801.xz_s | 0 | L2 miss | 1098 | 160 | 86023 | 298379 | 1 |
| c04-801.xz_s | 0 | L3 miss | 832 | 266 | 86176 | 298386 | 1 |
| c04-801.xz_s | 1 | L1 miss | 4555 | 0 | 962571 | 719442 | 4 |
| c04-801.xz_s | 1 | L2 miss | 62 | 1 | 967051 | 719454 | 4 |
| c04-801.xz_s | 1 | L3 miss | 39 | 2 | 967061 | 719466 | 4 |
| c04-801.xz_s | 2 | L1 miss | 10201 | 0 | 1112977 | 789283 | 1 |
| c04-801.xz_s | 2 | L2 miss | 113 | 0 | 1122904 | 789310 | 135 |
| c04-801.xz_s | 2 | L3 miss | 84 | 0 | 1122915 | 789328 | 135 |
| c04-801.xz_s | 3 | L1 miss | 6636 | 0 | 1103026 | 812565 | 7 |
| c04-801.xz_s | 3 | L2 miss | 143 | 1 | 1109334 | 812584 | 172 |
| c04-801.xz_s | 3 | L3 miss | 106 | 1 | 1109353 | 812602 | 172 |
| c04-817.flac_s | 0 | L1 miss | 1565 | 9 | 858953 | 627042 | 0 |
| c04-817.flac_s | 0 | L2 miss | 100 | 18 | 846080 | 641352 | 19 |
| c04-817.flac_s | 0 | L3 miss | 69 | 15 | 846099 | 641367 | 19 |
| c04-817.flac_s | 1 | L1 miss | 211 | 380 | 831117 | 717146 | 22 |
| c04-817.flac_s | 1 | L2 miss | 38 | 0 | 831289 | 717312 | 237 |
| c04-817.flac_s | 1 | L3 miss | 7 | 1 | 831291 | 717340 | 237 |
| c04-817.flac_s | 2 | L1 miss | 195 | 150 | 877956 | 677706 | 0 |
| c04-817.flac_s | 2 | L2 miss | 12 | 0 | 878138 | 677856 | 1 |
| c04-817.flac_s | 2 | L3 miss | 4 | 0 | 878146 | 677856 | 1 |
| c04-817.flac_s | 3 | L1 miss | 961 | 453 | 709527 | 680671 | 6 |
| c04-817.flac_s | 3 | L2 miss | 125 | 364 | 709456 | 681280 | 393 |
| c04-817.flac_s | 3 | L3 miss | 20 | 1 | 709501 | 681703 | 393 |

逐事件 reason 直方图与全部 scope 保留在独立 audit JSON 中。

- [c04-872.marian_s 独立服务审计](../tmp/spr-retired-membership-c4-20261107/audits/c04-872.marian_s.json)

- [c04-854.graph500_s 独立服务审计](../tmp/spr-retired-membership-c4-20261107/audits/c04-854.graph500_s.json)

- [c04-801.xz_s 独立服务审计](../tmp/spr-retired-membership-c4-20261107/audits/c04-801.xz_s.json)

- [c04-817.flac_s 独立服务审计](../tmp/spr-retired-membership-c4-20261107/audits/c04-817.flac_s.json)

[完整验收回执](../tmp/spr-retired-membership-c4-20261107/completion.json)、[配置命令](../tmp/spr-retired-membership-c4-20261107/plan.json)、[冻结身份](../tmp/spr-retired-membership-c4-20261107/freeze.json)。
