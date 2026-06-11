/*
 * mt_indirect_jump.c — BR_IND（间接跳转）多核负载
 *
 * 设计目标：
 *   - 64 项函数指针表，每次循环用 PRNG 选一个调用 → 触发 BTB indirect / RAS
 *   - 表中每个函数体只做一行 ALU，函数本身被 inline barrier 阻止内联
 *   - 制造真正的间接跳转 mispredict 长尾
 *
 * ROI 内**严禁同步**：4 thread 独立 PRNG。
 *
 * 用法：./mt_indirect_jump <nthreads> <iter_unit>
 *   推荐：./mt_indirect_jump 4 800
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
#define NFUNCS      64

static int  g_nthreads = 4;
static long g_iter     = 800;

static uint64_t g_seed[MAX_THREADS][8] __attribute__((aligned(LINE_BYTES)));
static volatile long g_sink[MAX_THREADS][8] __attribute__((aligned(LINE_BYTES)));

/* noinline 强制函数有独立入口 → 真间接调用 */
#define DECL_F(N) \
    __attribute__((noinline)) static long f##N(long x) { return x * (long)(N+1) ^ ((long)(N) << 3); }

DECL_F(0)  DECL_F(1)  DECL_F(2)  DECL_F(3)  DECL_F(4)  DECL_F(5)  DECL_F(6)  DECL_F(7)
DECL_F(8)  DECL_F(9)  DECL_F(10) DECL_F(11) DECL_F(12) DECL_F(13) DECL_F(14) DECL_F(15)
DECL_F(16) DECL_F(17) DECL_F(18) DECL_F(19) DECL_F(20) DECL_F(21) DECL_F(22) DECL_F(23)
DECL_F(24) DECL_F(25) DECL_F(26) DECL_F(27) DECL_F(28) DECL_F(29) DECL_F(30) DECL_F(31)
DECL_F(32) DECL_F(33) DECL_F(34) DECL_F(35) DECL_F(36) DECL_F(37) DECL_F(38) DECL_F(39)
DECL_F(40) DECL_F(41) DECL_F(42) DECL_F(43) DECL_F(44) DECL_F(45) DECL_F(46) DECL_F(47)
DECL_F(48) DECL_F(49) DECL_F(50) DECL_F(51) DECL_F(52) DECL_F(53) DECL_F(54) DECL_F(55)
DECL_F(56) DECL_F(57) DECL_F(58) DECL_F(59) DECL_F(60) DECL_F(61) DECL_F(62) DECL_F(63)

typedef long (*fn_t)(long);

#define E(N) f##N
static const fn_t g_table[NFUNCS] = {
    E(0),  E(1),  E(2),  E(3),  E(4),  E(5),  E(6),  E(7),
    E(8),  E(9),  E(10), E(11), E(12), E(13), E(14), E(15),
    E(16), E(17), E(18), E(19), E(20), E(21), E(22), E(23),
    E(24), E(25), E(26), E(27), E(28), E(29), E(30), E(31),
    E(32), E(33), E(34), E(35), E(36), E(37), E(38), E(39),
    E(40), E(41), E(42), E(43), E(44), E(45), E(46), E(47),
    E(48), E(49), E(50), E(51), E(52), E(53), E(54), E(55),
    E(56), E(57), E(58), E(59), E(60), E(61), E(62), E(63),
};

static inline uint64_t xs64(uint64_t *s)
{
    uint64_t x = *s;
    x ^= x << 13; x ^= x >> 7; x ^= x << 17;
    *s = x; return x;
}

static long indirect_jump_kernel(int tid, long iters)
{
    uint64_t s = g_seed[tid][0];
    long acc = (long)tid;
    for (long i = 0; i < iters; i++) {
        uint64_t r = xs64(&s);
        int idx = (int)(r & (NFUNCS - 1));
        acc = g_table[idx](acc);  /* 真正的间接调用 */
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
    a->cs = indirect_jump_kernel(a->tid, g_iter * 5);
    m5_work_end_inline();
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc >= 2) g_nthreads = atoi(argv[1]);
    if (argc >= 3) g_iter     = atol(argv[2]);
    if (g_nthreads <= 0 || g_nthreads > MAX_THREADS) g_nthreads = 4;
    if (g_iter    <= 0)                              g_iter    = 800;

    for (int i = 0; i < g_nthreads; i++)
        g_seed[i][0] = 0xA5A5A5A5DEADBEEFULL ^ ((uint64_t)i * 0xBF58476D1CE4E5B9ULL);

    fprintf(stderr, "mt_indirect_jump: nthreads=%d iter_unit=%ld\n",
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
    fprintf(stderr, "mt_indirect_jump: done. total=%ld\n", total);
    return 0;
}
