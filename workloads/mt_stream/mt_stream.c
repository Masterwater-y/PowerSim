/*
 * mt_stream.c — STREAM (McCalpin) 4 kernels，pthread 多核版
 *
 * 设计目标：
 *   - 4 个经典 STREAM 内核：Copy / Scale / Add / Triad（FP, double）
 *   - 每个 kernel 用 4-thread 静态切分数组，**ROI 包裹整段 4-kernel**
 *   - **完全删除原版 OpenMP / gettimeofday / verify 段**，仅保留计算骨架
 *   - 数组大小 4M doubles × 3 = 96 MB，远超 LLC，纯 DRAM bandwidth 负载
 *
 * ROI 内**严禁同步**：
 *   - 4 thread 切片不重叠
 *   - kernel 之间不在 ROI 内做 barrier；改为 pthread_join + 重 spawn 模型不可用，
 *     因此 4 个 kernel 在每个 worker 内顺序串行调用 → 同 thread 内串行天然同步
 *
 * 参考：
 *   - Copy:  c[j] = a[j]
 *   - Scale: b[j] = scalar * c[j]
 *   - Add:   c[j] = a[j] + b[j]
 *   - Triad: a[j] = b[j] + scalar * c[j]
 *
 * 用法：./mt_stream <nthreads> <iter>
 *   推荐：./mt_stream 4 2
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
/* 8K doubles 64KB/数组 × 3 = 192KB（全部 L2 内）；用作 5min smoke。
   对 ROI 内的 BR_COND/FP/LD/ST 分布已经足够采样；DRAM-bandwidth 角色由 mt_chase_dram + mt_stride_pf 承担。 */
#define ARRAY_SIZE   (8L * 1024)

typedef double STREAM_T;

/* 全局三数组，main 中分配并 first-touch */
static STREAM_T *a, *b, *c;

static int  g_nthreads = 4;
static long g_iter     = 1;

static const STREAM_T SCALAR = 3.0;

static void stream_copy(long lo, long hi)
{
    for (long j = lo; j < hi; j++) c[j] = a[j];
}
static void stream_scale(long lo, long hi)
{
    for (long j = lo; j < hi; j++) b[j] = SCALAR * c[j];
}
static void stream_add(long lo, long hi)
{
    for (long j = lo; j < hi; j++) c[j] = a[j] + b[j];
}
static void stream_triad(long lo, long hi)
{
    for (long j = lo; j < hi; j++) a[j] = b[j] + SCALAR * c[j];
}

typedef struct { int tid; long lo; long hi; long iter; } warg_t;

static void *worker(void *p)
{
    warg_t *w = (warg_t *)p;
    m5_work_begin_inline();
    for (long it = 0; it < w->iter; it++) {
        stream_copy(w->lo, w->hi);
        stream_scale(w->lo, w->hi);
        stream_add(w->lo, w->hi);
        stream_triad(w->lo, w->hi);
    }
    m5_work_end_inline();
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc >= 2) g_nthreads = atoi(argv[1]);
    if (argc >= 3) g_iter     = atol(argv[2]);
    if (g_nthreads <= 0 || g_nthreads > MAX_THREADS) g_nthreads = 4;
    if (g_iter    <= 0)                              g_iter    = 1;

    /* ROI 外分配并 first-touch，确保所有 page 已映射（避免 ROI 内 page-fault syscall） */
    size_t bytes = (size_t)ARRAY_SIZE * sizeof(STREAM_T);
    a = aligned_alloc(LINE_BYTES, bytes);
    b = aligned_alloc(LINE_BYTES, bytes);
    c = aligned_alloc(LINE_BYTES, bytes);
    if (!a || !b || !c) { fprintf(stderr, "alloc fail\n"); return 1; }
    for (long j = 0; j < ARRAY_SIZE; j++) { a[j] = 1.0; b[j] = 2.0; c[j] = 0.0; }

    fprintf(stderr, "mt_stream: nthreads=%d iter=%ld arr=%ldM doubles (%.1f MB total)\n",
            g_nthreads, g_iter, ARRAY_SIZE >> 20,
            (3.0 * (double)bytes) / (1024.0 * 1024.0));

    /* 切片：均分 ARRAY_SIZE */
    pthread_t ths[MAX_THREADS];
    warg_t   args[MAX_THREADS];
    long chunk = (ARRAY_SIZE + g_nthreads - 1) / g_nthreads;
    for (int i = 0; i < g_nthreads; i++) {
        args[i].tid  = i;
        args[i].lo   = (long)i * chunk;
        args[i].hi   = args[i].lo + chunk;
        if (args[i].hi > ARRAY_SIZE) args[i].hi = ARRAY_SIZE;
        args[i].iter = g_iter;
        pthread_create(&ths[i], NULL, worker, &args[i]);
    }
    for (int i = 0; i < g_nthreads; i++) pthread_join(ths[i], NULL);

    /* 防 DCE */
    fprintf(stderr, "mt_stream: done. a[0]=%f c[%ld]=%f\n",
            a[0], ARRAY_SIZE - 1, c[ARRAY_SIZE - 1]);
    return 0;
}
