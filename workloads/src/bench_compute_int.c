/* bench_compute_int — ALU-bound 纯整数长依赖链。低 CPI / 高 IPC / 几乎无 miss。
 * scale = 每线程内核迭代次数（×1000）。 */
#include "tao_bench.h"

static long g_priv[TAO_MAX_THREADS][8] __attribute__((aligned(TAO_LINE)));

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    long iters = scale * 1000;
    long *a = g_priv[tid];
    long x = (long)tid * 0x12345 + 1, y = (long)tid * 0x67890 + 3;
    long z = (long)tid * 0xabcde + 5, w = (long)tid * 0xf0f0f + 7;
    for (long i = 0; i < iters; i++) {
        x = x * 0x9e3779b1L + 0x12345;
        y = (y ^ (y >> 13)) + i;
        z = z + (z << 5) - x;
        w = (w * 5 + y) ^ (z >> 7);
        x = x + w;
    }
    a[0] = x; a[1] = y; a[2] = z; a[3] = w;
}

TAO_BENCH_MAIN("compute_int", kernel, NULL)
