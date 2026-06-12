/* bench_false_sharing — false sharing：各线程写同一 cacheline 的不同字。
 * 制造 coherence invalidation 风暴（M->I 反复弹跳）。高 inv_recv/inv_send。
 * scale = 迭代×1000。shared = 1 个 cacheline 共所有线程争抢。 */
#include "tao_bench.h"

static size_t shbytes(int nthreads, long scale) { (void)nthreads; (void)scale; return TAO_LINE * 4; }

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    long iters = scale * 1000;
    volatile long *line = (volatile long *)shared;
    /* 所有线程写同一 cacheline 内相邻 long（false sharing） */
    int slot = tid % 8;
    for (long i = 0; i < iters; i++) {
        line[slot] += i;                 /* 各自的字，但同行 -> coherence 弹跳 */
        line[slot] ^= line[(slot + 1) % (nthreads > 8 ? 8 : nthreads)];
    }
}

TAO_BENCH_MAIN("false_sharing", kernel, shbytes)
