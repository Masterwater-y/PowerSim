/* bench_indirect — 间接跳转/虚派发（function pointer table）。间接分支误预测。
 * 模拟解释器 dispatch / 虚函数调用。scale = 迭代×1000。 */
#include "tao_bench.h"

static uint64_t g_sink[TAO_MAX_THREADS];

static uint64_t op_add(uint64_t a, uint64_t b) { return a + b; }
static uint64_t op_xor(uint64_t a, uint64_t b) { return a ^ b; }
static uint64_t op_mul(uint64_t a, uint64_t b) { return a * (b | 1); }
static uint64_t op_rot(uint64_t a, uint64_t b) { return ((a << 7) | (a >> 57)) ^ b; }
static uint64_t op_sub(uint64_t a, uint64_t b) { return a - b; }
static uint64_t op_or (uint64_t a, uint64_t b) { return a | (b << 3); }

typedef uint64_t (*op_fn)(uint64_t, uint64_t);

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    long iters = scale * 1000;
    op_fn tab[6] = {op_add, op_xor, op_mul, op_rot, op_sub, op_or};
    uint64_t r = tao_seed_or(tid, 0,
        (uint64_t)tid * 0x9e3779b97f4a7c15ULL + 7);
    uint64_t acc = tao_seed_or(tid, 1, (uint64_t)(tid + 1));
    for (long i = 0; i < iters; i++) {
        r = r * 6364136223846793005ULL + 1442695040888963407ULL;
        int sel = (int)((r >> 40) % 6);   /* 数据决定的间接目标 -> 难预测 */
        acc = tab[sel](acc, r);
    }
    g_sink[tid] = acc;
}

TAO_BENCH_MAIN("indirect", kernel, NULL)
