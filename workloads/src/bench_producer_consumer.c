/* bench_producer_consumer — 真共享：线程间通过共享数组传递数据（MESI 转移）。
 * 邻居环形：tid 写自己的 slot，读上游 slot -> 真 sharing、remote-hit。
 * scale = 迭代×1000。shared = 每线程 1 cacheline。 */
#include "tao_bench.h"

static size_t shbytes(int nthreads, long scale)
{
    (void)scale;
    return (size_t)nthreads * TAO_LINE;
}

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    long iters = scale * 1000;
    size_t stride = TAO_LINE / sizeof(long);
    volatile long *buf = (volatile long *)shared;
    int up = (tid + nthreads - 1) % nthreads;   /* 上游邻居 */
    long acc = tid + 1;
    for (long i = 0; i < iters; i++) {
        buf[tid * stride] = acc;                 /* 写自己（被下游读 -> 触发 inval） */
        acc += buf[up * stride];                 /* 读上游（remote hit -> coherence） */
        acc ^= (acc << 5) + i;
    }
    /* 防止整轮被优化 */
    buf[tid * stride] = acc;
}

TAO_BENCH_MAIN("producer_consumer", kernel, shbytes)
