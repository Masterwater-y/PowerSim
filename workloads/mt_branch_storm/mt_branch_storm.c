/*
 * mt_branch_storm.c — 难预测条件分支多核负载
 *
 * 设计目标：
 *   - 用 XOR-shift PRNG 产生伪随机比特流，喂给 4 层嵌套 if/else
 *   - 强制不可学习的 BR_COND 模式 → 拉高 mispred 长尾
 *   - 内核完全 ALU 化（无 mem 依赖），确保 mispred 是分支预测器本身的极限
 *
 * 关键：volatile sink 阻止编译器把 if 折叠成 cmov / mux。
 *
 * ROI 内**严禁同步**：4 thread 各自跑独立 PRNG seed，无共享。
 *
 * 用法：./mt_branch_storm <nthreads> <iter_unit>
 *   推荐：./mt_branch_storm 4 800
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static inline void m5_work_begin_inline(void)
{
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005a" : : : "memory");
}
static inline void m5_work_end_inline(void)
{
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005b" : : : "memory");
}

#define MAX_THREADS 16
#define LINE_BYTES  64

static int  g_nthreads = 4;
static long g_iter     = 800;

/* per-thread PRNG state，独占 cacheline 避免 false-sharing */
static uint64_t g_seed[MAX_THREADS][8] __attribute__((aligned(LINE_BYTES)));

/* volatile sink：阻止编译器把 if 优化成 cmov */
static volatile long g_sink[MAX_THREADS][8] __attribute__((aligned(LINE_BYTES)));

/* XOR-shift 64：每次产生一个看起来"随机"的 64-bit */
static inline uint64_t xs64(uint64_t *s)
{
    uint64_t x = *s;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    *s = x;
    return x;
}

/* 四层嵌套 if，每层用 PRNG 不同 bit 决定走向，避免被 BTB/2-bit predictor 学习 */
static long branch_storm_kernel(int tid, long iters)
{
    uint64_t s = g_seed[tid][0];
    long acc = 0;
    for (long i = 0; i < iters; i++) {
        uint64_t r = xs64(&s);
        /* 4 层难预测分支（不同 bit lane） */
        if (r & 0x1ULL) {
            if (r & 0x100ULL)      acc += (long)(r >> 1);
            else                   acc -= (long)(r >> 2);
        } else {
            if (r & 0x10000ULL)    acc ^= (long)(r >> 3);
            else                   acc += (long)(r >> 4);
        }
        if (r & 0x1000000ULL) {
            if (r & 0x40ULL)       acc *= 3;
            else                   acc -= 7;
        } else {
            if (r & 0x4000ULL)     acc ^= 0x5a5a;
            else                   acc += i;
        }
        /* sink：周期性把 acc 写出，阻止 DCE 同时不形成 cache miss */
        if ((i & 0xFF) == 0) g_sink[tid][0] = acc;
    }
    g_seed[tid][0] = s;
    return acc;
}

typedef struct { int tid; long cs; } warg_t;

static void *worker(void *p)
{
    warg_t *a = (warg_t *)p;
    m5_work_begin_inline();
    a->cs = branch_storm_kernel(a->tid, g_iter * 5);
    m5_work_end_inline();
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc >= 2) g_nthreads = atoi(argv[1]);
    if (argc >= 3) g_iter     = atol(argv[2]);
    if (g_nthreads <= 0 || g_nthreads > MAX_THREADS) g_nthreads = 4;
    if (g_iter    <= 0)                              g_iter    = 800;

    /* 用 tid 派生不同的 PRNG seed，4 thread 走完全不同的分支轨迹 */
    for (int i = 0; i < g_nthreads; i++)
        g_seed[i][0] = 0xDEADBEEFCAFEBABEULL ^ ((uint64_t)i * 0x9E3779B97F4A7C15ULL);

    fprintf(stderr, "mt_branch_storm: nthreads=%d iter_unit=%ld\n",
            g_nthreads, g_iter);

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
    fprintf(stderr, "mt_branch_storm: done. total=%ld\n", total);
    return 0;
}
