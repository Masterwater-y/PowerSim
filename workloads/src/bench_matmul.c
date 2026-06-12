/* bench_matmul — 复现分块矩阵乘 GEMM（HPC 经典）。计算+访存混合、cache 分块复用。
 * 每线程独立 N×N 矩阵 C=A*B（ikj 顺序对 cache 友好）。scale = 矩阵边长 N。 */
#include "tao_bench.h"

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    size_t N = (size_t)scale;
    if (N < 16) N = 16;
    if (N > 512) N = 512;                 /* 上限防内存爆炸（每线程 3×N²×8B） */
    double *A = (double *)tao_xaligned(N * N * sizeof(double));
    double *B = (double *)tao_xaligned(N * N * sizeof(double));
    double *C = (double *)tao_xaligned(N * N * sizeof(double));
    for (size_t i = 0; i < N * N; i++) {
        A[i] = (double)((i + tid) & 63) * 0.5;
        B[i] = (double)((i * 3 + tid) & 63) * 0.25;
    }
    /* ikj 顺序：B/C 行连续访问，cache 友好的 GEMM */
    for (size_t i = 0; i < N; i++)
        for (size_t k = 0; k < N; k++) {
            double aik = A[i * N + k];
            for (size_t j = 0; j < N; j++)
                C[i * N + j] += aik * B[k * N + j];
        }
    volatile double s = C[N * N - 1]; (void)s;
    free(A); free(B); free(C);
}

TAO_BENCH_MAIN("matmul", kernel, NULL)
