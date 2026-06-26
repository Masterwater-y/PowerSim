/* bench_fp_compute_dense (v4 / -O0) — FP-dense 计算密集型负载，覆盖 ads_ctr / feed_ranking 形态。
 *
 * 设计目标：
 *   - IPC ≈ 0.55-0.75
 *   - is_fp ≈ 25-40%
 *   - is_store ≈ 10-18%
 *   - n_src ≈ 1.5-2.0
 *
 * v4 关键转向：用 -O0 编译，让源码与 µop 字面对应。
 *   不需要 volatile / restrict / prime stride / 依赖链注入——编译器不会消除任何东西。
 *
 * µop 预算（每次外层迭代，理想字面映射）：
 *   - 6 条显式 FP（acc 链） * 4 内层 = 24 FP
 *   - 内层 4 次：每次 ~5 INT (循环计数 + 索引计算)
 *   - 周期性 store：每内层迭代触发 1 store，但 -O0 还会因 spill 多产 INT-store
 *   - 每外层迭代约 35-50 µop，FP 占比 ~40%（实际会被 -O0 的 spill 稀释到 ~30%）
 *
 * buffer 大小：32KB（恰好 L1D），制造温和访存延迟，IPC 落在 0.6 附近。
 *
 * scale = 每线程外层迭代次数（×100，不是 ×1000）。-O0 下 µop 密度比 -O2 高 3-4 倍，
 * scale=1 大概产 4-6w µop/核，scale=10-15 即可达 50w/核。 */

#include "tao_bench.h"

#define FPD_BUF_LEN  8192u                  /* 64KB / thread, mild L1 pressure */
#define FPD_BUF_MASK (FPD_BUF_LEN - 1u)

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;

    double *buf = (double *)tao_xaligned(FPD_BUF_LEN * sizeof(double));
    for (unsigned i = 0; i < FPD_BUF_LEN; i++) {
        buf[i] = 0.5 + (double)(tid + 1) * 1e-3 * (double)i;
    }

    long iters = scale * 100;

    /* seed 入口：seed=0 时各值与旧版 bit-equal；seed!=0 时 a/b/c/d 在
     * [0.9990, 1.0010] 内微抖（量级 ~3e-5，远小于 0.0010 容差），其余在
     * 合理范围派生。 */
    double acc, a, b, c, d;
    unsigned idx;
    if (g_tao_seed == 0) {
        acc = (double)tid * 0.1 + 1.0;
        a = 1.0001; b = 0.9999; c = 1.0003; d = 0.9997;
        idx = (unsigned)tid * 17u;
    } else {
        acc = 1.0 + (double)(tao_seed_mix(tid, 0) & 0xfff) / 4096.0;  /* [1.0, 2.0) */
        a = 1.0001 + (double)((int64_t)(tao_seed_mix(tid, 1) & 0xff) - 128) / 4.096e6;
        b = 0.9999 + (double)((int64_t)(tao_seed_mix(tid, 2) & 0xff) - 128) / 4.096e6;
        c = 1.0003 + (double)((int64_t)(tao_seed_mix(tid, 3) & 0xff) - 128) / 4.096e6;
        d = 0.9997 + (double)((int64_t)(tao_seed_mix(tid, 4) & 0xff) - 128) / 4.096e6;
        idx = (unsigned)(tao_seed_mix(tid, 5) & FPD_BUF_MASK);
    }
    double sum = 0.0;

    for (long i = 0; i < iters; i++) {
        /* 内层 6 次小循环：略增 store 频率，并用温和的数据相关步长压低 IPC */
        for (int k = 0; k < 6; k++) {
            double v = buf[idx];                  /* 1 load + 1 INT-addr */

            acc = acc * a + v;                    /* FP mul + FP add */
            acc = acc * b + v;                    /* FP mul + FP add */
            acc = acc * c + v;                    /* FP mul + FP add */
            /* 至此 6 FP µop */

            sum = sum + acc * d;                  /* 1 FP mul + 1 FP add */

            /* store 控比例：每 3 次内层迭代写一次 buf */
            if ((k % 3) == 0) {
                buf[idx] = sum;                   /* 1 store + 1 INT-addr */
            }

            idx = (idx + 7u + (((unsigned long long)sum >> 2) & 1u)) & FPD_BUF_MASK;
        }
    }

    volatile double sink = acc + sum + buf[0] + buf[FPD_BUF_LEN - 1];
    (void)sink;

    free(buf);
}

TAO_BENCH_MAIN("fp_compute_dense", kernel, NULL)
