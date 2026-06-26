/* bench_compute_int — ALU-bound 纯整数长依赖链。低 CPI / 高 IPC / 几乎无 miss。
 * scale = 每线程内核迭代次数（×1000）。
 *
 * §12 phase 切换：N_PHASE=8 段稳态拼接，每段切换 (K1, K2, shift)。
 * 与 seed 完全解耦（不读 g_tao_seed）。 */
#include "tao_bench.h"

#define COMPUTE_INT_N_PHASE 8

static long g_priv[TAO_MAX_THREADS][8] __attribute__((aligned(TAO_LINE)));

static const long PH_K1[COMPUTE_INT_N_PHASE] = {
    0x9e3779b1L, 0x517cc1b7L, 0xc2b2ae3dL, 0xbf58476dL,
    0x94d049bbL, 0x85ebca6bL, 0xa3b195f3L, 0xd1b54a32L,
};
static const long PH_K2[COMPUTE_INT_N_PHASE] = {
    0x12345, 0x67890, 0xabcdef, 0x13579b,
    0x2468ac, 0x369cf0, 0x5a5a5a, 0xa5a5a5,
};
static const int  PH_SHIFT[COMPUTE_INT_N_PHASE] = {13, 17, 11, 19, 7, 23, 13, 17};

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    long iters = scale * 1000;
    long iters_per_phase = iters / COMPUTE_INT_N_PHASE;
    if (iters_per_phase < 1) iters_per_phase = 1;
    long *a = g_priv[tid];
    long x = (long)tao_seed_or(tid, 0, (uint64_t)((long)tid * 0x12345 + 1));
    long y = (long)tao_seed_or(tid, 1, (uint64_t)((long)tid * 0x67890 + 3));
    long z = (long)tao_seed_or(tid, 2, (uint64_t)((long)tid * 0xabcde + 5));
    long w = (long)tao_seed_or(tid, 3, (uint64_t)((long)tid * 0xf0f0f + 7));
    for (int ph = 0; ph < COMPUTE_INT_N_PHASE; ph++) {
        long k1 = PH_K1[ph], k2 = PH_K2[ph];
        int s = PH_SHIFT[ph];
        for (long i = 0; i < iters_per_phase; i++) {
            x = x * k1 + k2;
            y = (y ^ (y >> s)) + i;
            z = z + (z << 5) - x;
            w = (w * 5 + y) ^ (z >> 7);
            x = x + w;
        }
    }
    a[0] = x; a[1] = y; a[2] = z; a[3] = w;
}

TAO_BENCH_MAIN("compute_int", kernel, NULL)
