/* bench_fp_lite (v5 / -O0) — 轻 INT 节流 + 明确 FP 主链的轻浮点负载。
 *
 * 设计目标：
 *   - IPC ≈ 0.65-0.85
 *   - is_fp ≈ 10-20%
 *   - is_store ≈ 8-15%
 *   - n_src ≈ 1.5-1.8
 *
 * v5 关键转向：
 *   - 不再用重 INT hash-mix 模板上撒一点 FP
 *   - 只保留一个数据相关 INT 索引链来压 IPC
 *   - 每次 load 喂出多条 FP 运算，避免 FP 被地址/循环/spill 稀释
 *
 * µop 预算（每次外层迭代）：
 *   - 内层 4 次循环：每次约 2-3 INT 操作 + 4 组 FP（mul+add）+ 偶尔 store
 *   - 单个 INT load 只负责提供轻度不规则索引，FP 链承担主要算术密度
 *
 * buffer：4096 long（32KB）+ 512 double（4KB），INT 链有轻微 L1 压力，FP 数据常驻 L1。
 *
 * scale = 每线程外层迭代次数（×100）。-O0 下 µop 密度大，scale=10-15 达 50w/核。 */

#include "tao_bench.h"

#define FPL_INT_LEN  4096u
#define FPL_INT_MASK (FPL_INT_LEN - 1u)
#define FPL_FP_LEN   512u
#define FPL_FP_MASK  (FPL_FP_LEN - 1u)

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;

    long  *ibuf = (long   *)tao_xaligned(FPL_INT_LEN * sizeof(long));
    double *fbuf = (double *)tao_xaligned(FPL_FP_LEN  * sizeof(double));

    for (unsigned i = 0; i < FPL_INT_LEN; i++) ibuf[i] = (long)(tid * 7 + i * 13);
    for (unsigned i = 0; i < FPL_FP_LEN;  i++) fbuf[i] = 0.25 + (double)(tid + 1) * 1e-3 * (double)i;

    long iters = scale * 100;

    long  x = (long)tid * 0x12345 + 1;
    long  y = (long)tid * 0x67890 + 3;
    double f0 = (double)tid * 0.1 + 0.5;
    double f1 = (double)tid * 0.07 + 1.0;
    double f2 = (double)tid * 0.05 + 1.5;
    double a = 1.0001, b = 0.9997, c = 1.0003, d = 0.9999;
    unsigned ii = (unsigned)tid * 11u;
    unsigned fi = (unsigned)tid * 7u;

    for (long i = 0; i < iters; i++) {
        for (int k = 0; k < 4; k++) {
            /* 单条数据相关 INT 链：保留低 IPC 机制，但不再堆叠大量 INT ALU。 */
            long tag = ibuf[ii];                   /* 1 load + 1 addr */
            ii = (ii + 1u + ((unsigned)tag & 15u)) & FPL_INT_MASK;

            /* 由 INT 链给出轻度不规则 FP 索引。 */
            unsigned fj = (fi + (((unsigned)tag >> 2) & 7u)) & FPL_FP_MASK;
            double fv = fbuf[fj];                  /* 1 load + 1 addr */

            /* 明确的 FP 主链：1 次 load 喂出多组 FP mul/add。 */
            f0 = f0 * a + fv;                      /* 1 FP mul + 1 FP add */
            f1 = f1 * b + f0;                      /* 1 FP mul + 1 FP add */
            f2 = f2 * c + f1;                      /* 1 FP mul + 1 FP add */
            f0 = f0 + f2 * d;                      /* 1 FP mul + 1 FP add */

            /* 周期性 store：每 2 次内层迭代写一次（store 浓度 ~10%） */
            if ((k & 1) == 0) {
                ibuf[ii] = tag ^ (long)(f0 + f2);  /* 1 store */
                fbuf[fj] = f0 + f1;                /* 1 FP add + 1 store */
            }

            fi = (fi + 3u + (((unsigned)tag >> 5) & 3u)) & FPL_FP_MASK;
            x = x + (tag & 7);
            y = y ^ (long)fj;
        }
    }

    volatile long  isink = x + y + ibuf[0] + ibuf[FPL_INT_LEN - 1];
    volatile double fsink = f0 + f1 + f2 + fbuf[0] + fbuf[FPL_FP_LEN - 1];
    (void)isink; (void)fsink;

    free(ibuf);
    free(fbuf);
}

TAO_BENCH_MAIN("fp_lite", kernel, NULL)
