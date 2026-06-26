/* bench_chase_dram — pointer-chasing：随机置换链表遍历，制造 DRAM 随机访问。
 * 高 LLC miss、高 CPI、无法 prefetch。每线程独立大数组（> LLC 才有效）。
 * scale = 每线程节点数（×1024）。建议 scale 足够大使工作集 > LLC(2MiB)。 */
#include "tao_bench.h"

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    size_t n = (size_t)scale * 1024;
    /* 每节点占 1 cacheline，next 指针在行首 */
    size_t *idx = (size_t *)tao_xaligned(n * sizeof(size_t));
    volatile size_t *next = (size_t *)tao_xaligned(n * TAO_LINE);
    for (size_t i = 0; i < n; i++) idx[i] = i;
    /* Fisher-Yates 随机置换 -> 随机访问链 */
    uint64_t r = tao_seed_or(tid, 0,
        (uint64_t)tid * 0x9e3779b97f4a7c15ULL + 1);
    for (size_t i = n - 1; i > 0; i--) {
        r = r * 6364136223846793005ULL + 1442695040888963407ULL;
        size_t j = (size_t)(r % (i + 1));
        size_t t = idx[i]; idx[i] = idx[j]; idx[j] = t;
    }
    size_t stride = TAO_LINE / sizeof(size_t);
    for (size_t i = 0; i < n - 1; i++)
        next[idx[i] * stride] = idx[i + 1] * stride;
    next[idx[n - 1] * stride] = idx[0] * stride;
    /* 遍历：每步依赖上一步的 load 结果，完全串行 latency-bound */
    size_t p = 0, hops = n * 3;
    for (size_t h = 0; h < hops; h++) p = next[p];
    volatile size_t s = p; (void)s;
    free(idx); free((void *)next);
}

TAO_BENCH_MAIN("chase_dram", kernel, NULL)
