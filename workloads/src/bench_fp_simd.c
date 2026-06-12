/* bench_fp_simd — SSE2 128-bit FP/SIMD 长依赖链（mul/add/div）。
 * 覆盖 FP 执行单元延迟、SIMD path。scale = 迭代×1000。 */
#include "tao_bench.h"
#include <emmintrin.h>

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    long iters = scale * 1000;
    __m128d a = _mm_set1_pd(1.0 + tid * 0.001);
    __m128d b = _mm_set1_pd(1.0000001);
    __m128d c = _mm_set1_pd(0.9999999);
    volatile double sink;
    for (long i = 0; i < iters; i++) {
        a = _mm_mul_pd(a, b);      /* 长依赖：mul -> add -> div 串行 */
        a = _mm_add_pd(a, c);
        a = _mm_div_pd(a, b);
        if ((i & 0xFFFF) == 0) a = _mm_set1_pd(1.0 + tid * 0.001);
    }
    sink = _mm_cvtsd_f64(a);
    (void)sink;
}

TAO_BENCH_MAIN("fp_simd", kernel, NULL)
