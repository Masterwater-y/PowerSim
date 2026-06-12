/* bench_branch_storm — 数据依赖的难预测分支风暴。高 branch misprediction。
 * 修正：
 *   1) 放大每个 scale 的有效 work，避免 ROI 过短。
 *   2) 保留数据依赖和多路条件，避免编译器把热循环收缩过头。
 * scale = 迭代×12000。 */
#include "tao_bench.h"

static uint64_t g_sink[TAO_MAX_THREADS];

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    long iters = scale * 12000;
    uint64_t r = (uint64_t)tid * 0x9e3779b97f4a7c15ULL + 12345;
    uint64_t a = 0, b = 0, c = 0;
    for (long i = 0; i < iters; i++) {
        r = r * 6364136223846793005ULL + 1442695040888963407ULL;
        unsigned bits = (unsigned)(r >> 33);
        if (bits & 1) a += i; else b -= i;          /* 不可预测 */
        if (bits & 2) c ^= a; else c += b;
        if ((bits & 12) == 0) a = (a << 1) | 1;     /* 偏斜分支 */
        if (((bits >> 4) & 7) > 4) b ^= c; else a += c;
        r ^= a + (b << 1) + (c << 3);
        bits = (unsigned)(r >> 29);
        if (bits & 1) c += r; else c ^= (a + b);
        if ((bits & 6) == 4) a ^= c; else b += a;
        asm volatile("" : "+r"(r), "+r"(a), "+r"(b), "+r"(c) :: "memory");
    }
    g_sink[tid] = a ^ b ^ c;
}

TAO_BENCH_MAIN("branch_storm", kernel, NULL)
