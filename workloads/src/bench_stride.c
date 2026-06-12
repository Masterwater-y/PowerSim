/* bench_stride — 跨步访存（stride prefetch 友好 + TLB 压力）。
 * 大跨步触发 hardware prefetcher 和 TLB miss。scale = 数组元素数(×1024)。 */
#include "tao_bench.h"

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    size_t n = (size_t)scale * 1024;
    long *arr = (long *)tao_xaligned(n * sizeof(long));
    /* 多档 stride 扫描：64B / 1KB / 4KB(跨页->TLB) */
    size_t strides[3] = {8, 128, 512};
    long acc = tid;
    for (int rep = 0; rep < 3; rep++) {
        size_t st = strides[rep];
        for (size_t i = 0; i < n; i += st) {
            arr[i] = arr[i] * 3 + acc;
            acc ^= arr[i];
        }
    }
    volatile long s = acc; (void)s;
    free(arr);
}

TAO_BENCH_MAIN("stride", kernel, NULL)
