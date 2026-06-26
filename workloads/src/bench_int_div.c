/* bench_int_div — 整数除法/取模长延迟单元压力。低 IPC、execution stall。
 * 修正：
 *   1) 每个 scale 对应更多动态 work，避免 probe 过短。
 *   2) 用编译器 barrier 保留真实依赖链，避免 O2 下被过度收缩。
 * scale = 迭代×8000，每轮做 4 组依赖 div/mod。
 *
 * §12 phase 切换：N_PHASE=8 段，每段切换 (KX, CX) LCG 系数 → x 的值域稳态
 * 不同 → divide 平均周期跨 phase 差异。与 seed 完全解耦。 */
#include "tao_bench.h"

#define INT_DIV_N_PHASE 8

static uint64_t g_sink[TAO_MAX_THREADS];

static const uint64_t PH_KX[INT_DIV_N_PHASE] = {
    6364136223846793005ULL, 2862933555777941757ULL,
    6906969069ULL,          1103515245ULL,
    134775813ULL,           3935559000370003845ULL,
    2685821657736338717ULL, 3037000493ULL,
};
static const uint64_t PH_CX[INT_DIV_N_PHASE] = {
    1442695040888963407ULL, 3037000493ULL,
    1ULL,                   12345ULL,
    1ULL,                   2891336453ULL,
    1ULL,                   12820163ULL,
};

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    long iters = scale * 8000;
    long iters_per_phase = iters / INT_DIV_N_PHASE;
    if (iters_per_phase < 1) iters_per_phase = 1;
    uint64_t x = tao_seed_or(tid, 0,
        (uint64_t)tid * 0x9e3779b97f4a7c15ULL + 1);
    uint64_t acc = tao_seed_or(tid, 1, 0);
    for (int ph = 0; ph < INT_DIV_N_PHASE; ph++) {
        uint64_t kx = PH_KX[ph], cx = PH_CX[ph];
        for (long i = 0; i < iters_per_phase; i++) {
            uint64_t d0 = ((uint64_t)(i * 4 + 1) * 2654435761ULL) | 1ULL;
            uint64_t d1 = ((uint64_t)(i * 4 + 3) * 2246822519ULL) | 1ULL;
            uint64_t d2 = ((uint64_t)(i * 4 + 5) * 3266489917ULL) | 1ULL;
            uint64_t d3 = ((uint64_t)(i * 4 + 7) * 668265263ULL)  | 1ULL;
            acc += x / d0;
            x = x * kx + acc + cx;
            acc ^= x % d1;
            x ^= acc / d2;
            acc += x % d3;
            asm volatile("" : "+r"(x), "+r"(acc) :: "memory");
        }
    }
    g_sink[tid] = acc;
}

TAO_BENCH_MAIN("int_div", kernel, NULL)
