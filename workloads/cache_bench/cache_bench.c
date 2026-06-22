/*
 * cache_bench.c
 *
 * 单核单线程微基准：用三段访存足迹不同的 kernel，分别热 L1 / L2 / L3；
 * 每段 kernel 内夹带纯 ALU 计算，参数 N 控制每段的重复次数。
 *
 * 编译： gcc -O2 -static -o cache_bench cache_bench.c
 *
 * 使用： ./cache_bench [N]
 *   N 默认 64；越大越慢。三段 kernel 都会跑 N 次。
 *
 * 输出（stdout）：
 *   [L1] elapsed = ... s, checksum = ...
 *   [L2] elapsed = ... s, checksum = ...
 *   [L3] elapsed = ... s, checksum = ...
 *   [TOTAL] elapsed = ... s
 *
 * 说明：在 gem5 SE 模式下 clock_gettime 返回的是仿真时间；在 sniper 消费
 * 由 SiftTracer 生成的 trace 时，这段调用会作为普通指令序列出现，对流水
 * 影响很小，但 elapsed 的“墙钟”语义会变成仿真语义，属于预期行为。
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/*
 * 三档 working set。默认按宿主配置：
 *   L1D = 48 KiB  → L1_BYTES = 16 KiB  (远小于 L1D)
 *   L2  = 2 MiB   → L2_BYTES = 512 KiB (L1 放不下, 落在 L2)
 *   L3  = 96 MiB  → L3_BYTES = 16 MiB  (L2 放不下, 落在 L3)
 *
 * 若你改过 cache 大小，只要保证每档严格大于前一级即可。
 */
#define L1_BYTES  (16  * 1024)
#define L2_BYTES  (512 * 1024)
#define L3_BYTES  (16  * 1024 * 1024)

/* 每个 element 8 字节；步长选 8，保证按顺序访问每个 cache line 都被碰到。 */
typedef uint64_t elem_t;
#define STRIDE_ELEMS 8  /* 8 * 8 = 64B, 正好一个 cache line */

static double time_diff_sec(struct timespec start, struct timespec end)
{
    double s  = (double)(end.tv_sec  - start.tv_sec);
    double ns = (double)(end.tv_nsec - start.tv_nsec);
    return s + ns * 1e-9;
}

/*
 * run_kernel:
 *   - 在 buf[0..nelems-1] 上按 cache-line 步长循环访问；
 *   - 每次访问做一段 ALU 计算（乘 + 异或 + 加），避免被折叠；
 *   - 重复 iters 遍；
 *   - 返回一个 checksum，用来防止整个循环被优化掉。
 */
static uint64_t run_kernel(elem_t *buf, size_t nelems, long iters)
{
    uint64_t acc = 0x9E3779B97F4A7C15ULL; /* 随便选个大素数做种子 */
    for (long it = 0; it < iters; it++) {
        /* 正向扫一遍 */
        for (size_t i = 0; i < nelems; i += STRIDE_ELEMS) {
            uint64_t v = buf[i];
            /* 少量 ALU 夹带：乘法 + 移位 + 异或，pipeline 里有点料 */
            v = v * 0xC6A4A7935BD1E995ULL;
            v ^= v >> 47;
            acc += v + (uint64_t)i;
            buf[i] = v;  /* 写回，形成 read-modify-write 流 */
        }
        /* 防止编译器把 iters 这一层循环吃掉 */
        __asm__ volatile("" :: "r"(acc) : "memory");
    }
    return acc;
}

static int run_level(const char *name, size_t bytes, long iters, double *elapsed_out)
{
    size_t nelems = bytes / sizeof(elem_t);
    elem_t *buf = (elem_t *)malloc(bytes);
    if (!buf) {
        fprintf(stderr, "[%s] malloc(%zu) failed\n", name, bytes);
        return -1;
    }
    /* 触一遍页，避免首轮全是 page fault */
    for (size_t i = 0; i < nelems; i++) {
        buf[i] = (elem_t)(i * 2654435761u);
    }

    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    uint64_t checksum = run_kernel(buf, nelems, iters);
    clock_gettime(CLOCK_MONOTONIC, &t1);

    double elapsed = time_diff_sec(t0, t1);
    printf("[%s] elapsed = %.6f s, checksum = 0x%016llx, bytes=%zu, iters=%ld\n",
           name, elapsed, (unsigned long long)checksum, bytes, iters);
    fflush(stdout);

    free(buf);
    if (elapsed_out) *elapsed_out = elapsed;
    return 0;
}

int main(int argc, char **argv)
{
    long iters = 64;
    if (argc > 1) {
        iters = strtol(argv[1], NULL, 10);
        if (iters <= 0) iters = 64;
    }
    fprintf(stdout, "[cache_bench] iters-per-level = %ld\n", iters);
    fprintf(stdout, "[cache_bench] footprints: L1=%d KiB, L2=%d KiB, L3=%d KiB\n",
            L1_BYTES / 1024, L2_BYTES / 1024, L3_BYTES / 1024);
    fflush(stdout);

    struct timespec total_start, total_end;
    clock_gettime(CLOCK_MONOTONIC, &total_start);

    double e1 = 0.0, e2 = 0.0, e3 = 0.0;
    if (run_level("L1", L1_BYTES, iters, &e1) != 0) return 1;
    if (run_level("L2", L2_BYTES, iters, &e2) != 0) return 1;
    if (run_level("L3", L3_BYTES, iters, &e3) != 0) return 1;

    clock_gettime(CLOCK_MONOTONIC, &total_end);
    double total = time_diff_sec(total_start, total_end);

    printf("[TOTAL] elapsed = %.6f s (L1=%.6f, L2=%.6f, L3=%.6f)\n",
           total, e1, e2, e3);
    return 0;
}
