/* bench_scan_reduce — 类 OLAP 分析负载：顺序扫描大表 + 条件过滤(分支) + 归约。
 * 顺序访存(prefetch友好) + 数据依赖分支 + 累加的真实混合，常见于数据库/分析。
 * scale = 每线程表元素数(×1024)。 */
#include "tao_bench.h"

static uint64_t g_sink[TAO_MAX_THREADS];

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    size_t n = (size_t)scale * 1024;
    int *key = (int *)tao_xaligned(n * sizeof(int));
    long *val = (long *)tao_xaligned(n * sizeof(long));
    uint64_t r = (uint64_t)tid * 0x9e3779b97f4a7c15ULL + 9;
    for (size_t i = 0; i < n; i++) {
        r = r * 6364136223846793005ULL + 1442695040888963407ULL;
        key[i] = (int)(r % 100);          /* 0..99 选择度 */
        val[i] = (long)(r >> 20);
    }
    long sum = 0, cnt = 0; long groups[8] = {0};
    for (int rep = 0; rep < 4; rep++)
        for (size_t i = 0; i < n; i++) {
            if (key[i] < 50) {            /* ~50% 选择度过滤（中等可预测）*/
                sum += val[i];
                cnt++;
                groups[key[i] & 7] += val[i];   /* group-by 归约 */
            }
        }
    g_sink[tid] = (uint64_t)(sum ^ cnt ^ groups[0] ^ groups[7]);
    free(key); free(val);
}

TAO_BENCH_MAIN("scan_reduce", kernel, NULL)
