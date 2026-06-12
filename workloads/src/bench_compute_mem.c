/* bench_compute_mem — 计算与访存紧密交织（非分相，而是同一循环内混合）。
 * 模拟 high-IPC 段中突发 cache miss 的真实组合 -> PMU 联合分布。
 * scale = 迭代×1000。每线程私有 buffer 跨 L2。 */
#include "tao_bench.h"

static uint64_t g_sink[TAO_MAX_THREADS];

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    long iters = scale * 1000;
    size_t bn = 256 * 1024;               /* 2MiB，跨 L2/接近 LLC */
    long *buf = (long *)tao_xaligned(bn * sizeof(long));
    for (size_t i = 0; i < bn; i++) buf[i] = (long)(i + tid);
    uint64_t acc = tid + 1, r = (uint64_t)tid * 2654435761ULL + 1;
    for (long i = 0; i < iters; i++) {
        /* 一串 ALU（喂满流水）*/
        acc = acc * 0x9e3779b1L + i;
        acc ^= acc >> 11;
        /* 间歇插入一次不规则访存（偶发 miss，与高 IPC 段交织）*/
        if ((i & 7) == 0) {
            r = r * 6364136223846793005ULL + 1442695040888963407ULL;
            size_t p = (size_t)(r % bn);
            acc += buf[p];
            buf[p] ^= (long)acc;
        }
    }
    g_sink[tid] = acc;
    free(buf);
}

TAO_BENCH_MAIN("compute_mem", kernel, NULL)
