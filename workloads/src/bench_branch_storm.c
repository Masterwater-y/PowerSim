/* bench_branch_storm — 数据依赖的难预测分支风暴。高 branch misprediction。
 * 修正：
 *   1) 放大每个 scale 的有效 work，避免 ROI 过短。
 *   2) 保留数据依赖和多路条件，避免编译器把热循环收缩过头。
 * scale = 迭代×12000。
 *
 * §12 phase 切换：N_PHASE=8 段，每段切换 4 个偏斜阈值 (m1, m2, thr_hi, thr_lo)
 * → branch predictor 在每个 phase 内有不同稳态命中率。与 seed 完全解耦。 */
#include "tao_bench.h"

#define BS_N_PHASE 8

static uint64_t g_sink[TAO_MAX_THREADS];

static const unsigned PH_M1[BS_N_PHASE]      = {12, 6, 14, 12, 6, 14, 10, 12};
static const unsigned PH_M2[BS_N_PHASE]      = {7,  3, 15, 7,  3, 15, 11, 7};
static const unsigned PH_THR_HIGH[BS_N_PHASE] = {4, 2, 6,  4,  2, 6,  0,  5};
static const unsigned PH_THR_LOW[BS_N_PHASE]  = {1, 3, 5,  1,  3, 5,  7,  1};

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    long iters = scale * 12000;
    long iters_per_phase = iters / BS_N_PHASE;
    if (iters_per_phase < 1) iters_per_phase = 1;
    uint64_t r = tao_seed_or(tid, 0,
        (uint64_t)tid * 0x9e3779b97f4a7c15ULL + 12345);
    uint64_t a = 0, b = 0, c = 0;
    for (int ph = 0; ph < BS_N_PHASE; ph++) {
        unsigned m1 = PH_M1[ph], m2 = PH_M2[ph];
        unsigned thr_hi = PH_THR_HIGH[ph], thr_lo = PH_THR_LOW[ph];
        for (long i = 0; i < iters_per_phase; i++) {
            r = r * 6364136223846793005ULL + 1442695040888963407ULL;
            unsigned bits = (unsigned)(r >> 33);
            if (bits & 1) a += i; else b -= i;
            if (bits & 2) c ^= a; else c += b;
            if ((bits & m1) == 0) a = (a << 1) | 1;
            if (((bits >> 4) & m2) > thr_hi) b ^= c; else a += c;
            r ^= a + (b << 1) + (c << 3);
            bits = (unsigned)(r >> 29);
            if (bits & 1) c += r; else c ^= (a + b);
            if ((bits & 6) == thr_lo) a ^= c; else b += a;
            asm volatile("" : "+r"(r), "+r"(a), "+r"(b), "+r"(c) :: "memory");
        }
    }
    g_sink[tid] = a ^ b ^ c;
}

TAO_BENCH_MAIN("branch_storm", kernel, NULL)
