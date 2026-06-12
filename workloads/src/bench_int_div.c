/* bench_int_div — 整数除法/取模长延迟单元压力。低 IPC、execution stall。
 * 修正：
 *   1) 每个 scale 对应更多动态 work，避免 probe 过短。
 *   2) 用编译器 barrier 保留真实依赖链，避免 O2 下被过度收缩。
 * scale = 迭代×8000，每轮做 4 组依赖 div/mod。 */
#include "tao_bench.h"

static uint64_t g_sink[TAO_MAX_THREADS];

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    long iters = scale * 8000;
    uint64_t x = (uint64_t)tid * 0x9e3779b97f4a7c15ULL + 1;
    uint64_t acc = 0;
    for (long i = 0; i < iters; i++) {
        uint64_t d0 = ((uint64_t)(i * 4 + 1) * 2654435761ULL) | 1ULL;
        uint64_t d1 = ((uint64_t)(i * 4 + 3) * 2246822519ULL) | 1ULL;
        uint64_t d2 = ((uint64_t)(i * 4 + 5) * 3266489917ULL) | 1ULL;
        uint64_t d3 = ((uint64_t)(i * 4 + 7) * 668265263ULL)  | 1ULL;
        acc += x / d0;
        x = x * 6364136223846793005ULL + acc + 1442695040888963407ULL;
        acc ^= x % d1;
        x ^= acc / d2;
        acc += x % d3;
        asm volatile("" : "+r"(x), "+r"(acc) :: "memory");
    }
    g_sink[tid] = acc;
}

TAO_BENCH_MAIN("int_div", kernel, NULL)
