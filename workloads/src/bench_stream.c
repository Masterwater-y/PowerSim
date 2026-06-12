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
    for (size_t i = 0; i < n; i++) { b[i] = 1.0 + tid; c[i] = 2.0; }
    const double q = 3.0;
    /* 多趟，制造稳定带宽流 */
    for (int rep = 0; rep < 4; rep++)
        for (size_t i = 0; i < n; i++)
            a[i] = b[i] + q * c[i];
    volatile double s = a[n - 1]; (void)s;
    free(a); free(b); free(c);
}

TAO_BENCH_MAIN("stream", kernel, NULL)
