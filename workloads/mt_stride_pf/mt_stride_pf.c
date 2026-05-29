/*
 * mt_stride_pf.c — stream prefetcher friendly 大跨步扫描
 *
 * 设计目标：
 *   - per-thread 8MB private buffer（栈外，主线程预先分配）
 *   - stride = 64B（一行一行扫），单调递增 → 触发 L2 stream prefetcher
 *   - 期望 path_class 中出现高比例 PF_HIT，与 mt_chase_dram 的纯 miss 形成对比
 *
 * ROI 内**严禁同步**：4 thread 各自扫自己的 buffer。
 *
 * 工作集大小折中：
 *   - 8MB > L2 (512KB) → 必经 L3 / DRAM 路径
 *   - 4 thread × 8MB = 32MB，gem5 SE 模式可承受
 *
 * 用法：./mt_stride_pf <nthreads> <iter_unit>
 *   推荐：./mt_stride_pf 4 4
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static inline void m5_work_begin_inline(void)
{
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005a" : : : "memory");
}
static inline void m5_work_end_inline(void)
{
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005b" : : : "memory");
}

#define MAX_THREADS  16
#define LINE_BYTES   64
#define BUF_BYTES    (1L * 1024 * 1024)        /* 1 MB / thread（gem5 仿真预算 ≤5min） */
#define BUF_LINES    (BUF_BYTES / LINE_BYTES)  /* 16384 lines */

static int  g_nthreads = 4;
static long g_iter     = 1;   /* 整 buffer 扫几遍 */

/* per-thread buffer 指针（main 里 malloc，不进 ROI） */
static char *g_buf[MAX_THREADS];
static volatile long g_sink[MAX_THREADS][8] __attribute__((aligned(LINE_BYTES)));

static long stride_pf_kernel(int tid, long passes)
{
    char *p = g_buf[tid];
    long sum = 0;
    for (long pass = 0; pass < passes; pass++) {
        /* 64B stride 顺序扫，每行只读一个 long → stream prefetcher 完美匹配 */
        for (long i = 0; i < BUF_LINES; i++) {
            sum += *(volatile long *)(p + i * LINE_BYTES);
        }
    }
    g_sink[tid][0] = sum;
    return sum;
}

typedef struct { int tid; long cs; } warg_t;

static void *worker(void *p)
{
    warg_t *a = (warg_t *)p;
    m5_work_begin_inline();
    a->cs = stride_pf_kernel(a->tid, g_iter);
    m5_work_end_inline();
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc >= 2) g_nthreads = atoi(argv[1]);
    if (argc >= 3) g_iter     = atol(argv[2]);
    if (g_nthreads <= 0 || g_nthreads > MAX_THREADS) g_nthreads = 4;
    if (g_iter    <= 0)                              g_iter    = 1;

    /* ROI 外预分配并触摸 buffer，确保 page mapping 已就位 */
    for (int i = 0; i < g_nthreads; i++) {
        g_buf[i] = aligned_alloc(LINE_BYTES, BUF_BYTES);
        if (!g_buf[i]) { fprintf(stderr, "alloc failed\n"); return 1; }
        /* 用 tid pattern 写一遍，避免 zero-page COW */
        memset(g_buf[i], (i * 17) & 0xff, BUF_BYTES);
    }

    fprintf(stderr, "mt_stride_pf: nthreads=%d passes=%ld buf=%ldMB\n",
            g_nthreads, g_iter, BUF_BYTES >> 20);

    pthread_t ths[MAX_THREADS];
    warg_t   args[MAX_THREADS];
    for (int i = 0; i < g_nthreads; i++) {
        args[i].tid = i; args[i].cs = 0;
        pthread_create(&ths[i], NULL, worker, &args[i]);
    }
    long total = 0;
    for (int i = 0; i < g_nthreads; i++) {
        pthread_join(ths[i], NULL);
        total ^= args[i].cs;
    }
    fprintf(stderr, "mt_stride_pf: done. total=%ld\n", total);
    return 0;
}
