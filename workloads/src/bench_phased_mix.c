/* bench_phased_mix — 相位混合：每个 worker 在外层循环里轮换执行
 *   [ALU 计算相 -> 私有随机读访存相 -> 难预测分支相]，制造真实程序的
 *   相位过渡与 PMU 联合分布（microbench 缺失的关键）。
 *
 * 修正历史：
 *   1) 访存相改成私有只读主导，避免随机写回在 gem5 Ruby 下 panic。
 *   2) 每个 scale 对应更多轮次，保证每相都有足够窗口样本。
 *   3) 改用 TAO_BENCH_MAIN_KERNEL_ROI：alloc + perm 初始化在 ROI 之外，
 *      避免 init 阶段的 sequential store 污染 trace。
 * scale = 外层轮数 / 8。 */
#include "tao_bench.h"

static uint64_t g_sink[TAO_MAX_THREADS];

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;
    long rounds = scale * 6;
    if (rounds < 6) rounds = 6;
    /* 每线程私有访存 buffer（512KiB，跨 L2；私有只读主导） */
    size_t bn = 64 * 1024;
    uint64_t *buf = (uint64_t *)tao_xaligned(bn * sizeof(uint64_t));
    size_t *perm = (size_t *)tao_xaligned(bn * sizeof(size_t));
    uint64_t r = (uint64_t)tid * 0x9e3779b97f4a7c15ULL + 1;
    for (size_t i = 0; i < bn; i++) {
        perm[i] = i;
        buf[i] = (uint64_t)(i * 1315423911u + tid * 17 + 1);
    }
    for (size_t i = bn - 1; i > 0; i--) {
        r = r * 6364136223846793005ULL + 1442695040888963407ULL;
        size_t j = (size_t)(r % (i + 1));
        size_t t = perm[i]; perm[i] = perm[j]; perm[j] = t;
    }

    /* init 完成 -> 全员到齐 -> 进 ROI，开始计相位 hot loop */
    tao_phase_sync();
    tao_roi_begin();

    uint64_t acc = tid + 1;
    for (long rd = 0; rd < rounds; rd++) {
        /* 相 1：ALU 计算（高 IPC、低 miss） */
        for (int i = 0; i < 2000; i++) {
            acc = acc * 0x9e3779b1L + 0x12345;
            acc ^= acc >> 13;
        }
        asm volatile("" : "+r"(acc) :: "memory");
        /* 相 2：私有随机读访存（高 LLC miss、高 CPI） */
        size_t p = acc % bn;
        for (int i = 0; i < 2500; i++) {
            p = perm[p];
            acc += buf[p];
            acc ^= (uint64_t)p << ((i & 7) + 1);
        }
        asm volatile("" : "+r"(acc), "+r"(p) :: "memory");
        /* 相 3：难预测分支（高 branch miss） */
        for (int i = 0; i < 2000; i++) {
            r = r * 6364136223846793005ULL + 1442695040888963407ULL;
            if ((r >> 33) & 1) acc += i; else acc -= i;
            if ((r >> 40) & 1) acc ^= r; else acc += r;
        }
        asm volatile("" : "+r"(acc), "+r"(r) :: "memory");
    }

    tao_roi_end();

    g_sink[tid] = acc;
    free(buf); free(perm);
}

TAO_BENCH_MAIN_KERNEL_ROI("phased_mix", kernel, NULL)
