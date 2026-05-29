/*
 * mt_int_div.c — 整数除法长延迟多核负载
 *
 * 设计目标：
 *   - x86 idiv 是 ~20-40 cyc 长延迟（不可流水），但不属于 mem ops
 *   - 用 idiv 长依赖链填补 exec_lat 长尾分布（非 mem 来源）
 *   - 4 路独立链并行，与 mul/xor 混合避免被编译器消除
 *
 * 关键：volatile divisor 阻止编译器折叠成乘法逆元。
 *
 * ROI 内**严禁同步**：4 thread 独立寄存器流。
 *
 * 用法：./mt_int_div <nthreads> <iter_unit>
 *   推荐：./mt_int_div 4 800
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

static long g_priv[MAX_THREADS][8] __attribute__((aligned(LINE_BYTES)));

/* volatile divisors：阻止编译器优化为常量除法 (mul + shift) */
static volatile long g_div_a = 7;
static volatile long g_div_b = 13;
static volatile long g_div_c = 19;
static volatile long g_div_d = 23;

static long int_div_kernel(int tid, long iters)
{
    long *a = g_priv[tid];
    a[0] = (long)tid * 0x12345 + 0xABCDEF;
    a[1] = (long)tid * 0x67890 + 0x123456;
    a[2] = (long)tid * 0xabcde + 0x789ABC;
    a[3] = (long)tid * 0xf0f0f + 0xDEFABC;

    long x = a[0], y = a[1], z = a[2], w = a[3];
    long da = g_div_a, db = g_div_b, dc = g_div_c, dd = g_div_d;

    for (long i = 0; i < iters; i++) {
        /* 4 路独立 idiv 链 */
        x = (x + i) / da;
        y = (y * 3 + i) / db;
        z = (z ^ (i << 2)) / dc;
        w = (w + (x ^ y)) / dd;
        /* 反馈合并以避免编译器分离 */
        x ^= w;
        /* 周期性穿插 imul（也是较长 latency 的非 mem op） */
        if ((i & 0x7) == 0) {
            x = x * 0x9E3779B1L + 1;
            y = y * 0xBF58476DL + 3;
        }
    }
    a[0] = x; a[1] = y; a[2] = z; a[3] = w;
    return x ^ y ^ z ^ w;
}

typedef struct { int tid; long cs; } warg_t;

static void *worker(void *p)
{
    warg_t *a = (warg_t *)p;
    m5_work_begin_inline();
    a->cs = int_div_kernel(a->tid, g_iter * 5);
    m5_work_end_inline();
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc >= 2) g_nthreads = atoi(argv[1]);
    if (argc >= 3) g_iter     = atol(argv[2]);
    if (g_nthreads <= 0 || g_nthreads > MAX_THREADS) g_nthreads = 4;
    if (g_iter    <= 0)                              g_iter    = 800;

    fprintf(stderr, "mt_int_div: nthreads=%d iter_unit=%ld\n",
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
    fprintf(stderr, "mt_int_div: done. total=%ld\n", total);
    return 0;
}
