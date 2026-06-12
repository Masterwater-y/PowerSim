/* bench_branch_sm — 规律状态机分支（高度可预测）。低 branch miss、考验 BTB/TAGE 学习。
 * 周期性模式 -> 预测器应能学到。与 branch_storm 形成对照。scale = 迭代×1000。 */
#include "tao_bench.h"

static uint64_t g_sink[TAO_MAX_THREADS];

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    long iters = scale * 1000;
    int state = tid % 4;
    uint64_t acc = 0;
    for (long i = 0; i < iters; i++) {
        switch (state) {                 /* 确定性状态转移 -> 规律分支 */
            case 0: acc += i;     state = 1; break;
            case 1: acc ^= acc>>3;state = 2; break;
            case 2: acc += acc<<2;state = 3; break;
            default:acc -= i;     state = 0; break;
        }
        if ((i % 8) < 5) acc += 1;       /* 固定周期模式，可预测 */
    }
    g_sink[tid] = acc;
}

TAO_BENCH_MAIN("branch_sm", kernel, NULL)
