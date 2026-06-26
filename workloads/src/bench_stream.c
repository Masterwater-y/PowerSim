/* bench_stream — 复现 McCalpin STREAM Triad: a[i] = b[i] + q*c[i]。
 * 顺序流式访存、带宽受限、prefetch 友好。每线程独立 3 数组。
 * scale = 每线程每数组元素数（×1024 double，即 scale*8KB/数组）。 */
#include "tao_bench.h"

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    size_t n = (size_t)scale * 1024;
    double *a = (double *)tao_xaligned(n * sizeof(double));
    double *b = (double *)tao_xaligned(n * sizeof(double));
    double *c = (double *)tao_xaligned(n * sizeof(double));
    /* seed 入口：seed=0 时 base_b=1.0, base_c=2.0，与旧版完全一致；
     * seed!=0 时 base_b/base_c 在 [1.0, 1.001) / [2.0, 2.001) 微抖。
     * 改的是数值不是访问模式，hot loop 字面不动。 */
    double base_b = 1.0;
    double base_c = 2.0;
    if (g_tao_seed != 0) {
        base_b = 1.0 + (double)(tao_seed_mix(tid, 0) & 0xfff) / 4.096e6;
        base_c = 2.0 + (double)(tao_seed_mix(tid, 1) & 0xfff) / 4.096e6;
    }
    for (size_t i = 0; i < n; i++) { b[i] = base_b + tid; c[i] = base_c; }
    const double q = 3.0;
    /* 多趟，制造稳定带宽流 */
    for (int rep = 0; rep < 4; rep++)
        for (size_t i = 0; i < n; i++)
            a[i] = b[i] + q * c[i];
    volatile double s = a[n - 1]; (void)s;
    free(a); free(b); free(c);
}

TAO_BENCH_MAIN("stream", kernel, NULL)
