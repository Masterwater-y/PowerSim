/* bench_stencil2d — 复现 5-point 2D stencil（HPC kernel）。邻域复用、cache 局部性。
 * 中等 miss、规则访存、有 spatial+temporal locality。scale = 每线程网格边长。 */
#include "tao_bench.h"

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    size_t dim = (size_t)scale;            /* 网格 dim×dim */
    if (dim < 16) dim = 16;
    size_t n = dim * dim;
    double *cur = (double *)tao_xaligned(n * sizeof(double));
    double *nxt = (double *)tao_xaligned(n * sizeof(double));
    for (size_t i = 0; i < n; i++) cur[i] = (double)((i + tid) & 255);
    for (int rep = 0; rep < 8; rep++) {    /* 多时间步 -> temporal reuse */
        for (size_t y = 1; y < dim - 1; y++)
            for (size_t x = 1; x < dim - 1; x++) {
                size_t k = y * dim + x;
                nxt[k] = 0.2 * (cur[k] + cur[k - 1] + cur[k + 1]
                                + cur[k - dim] + cur[k + dim]);
            }
        double *t = cur; cur = nxt; nxt = t;
    }
    volatile double s = cur[dim + 1]; (void)s;
    free(cur); free(nxt);
}

TAO_BENCH_MAIN("stencil2d", kernel, NULL)
