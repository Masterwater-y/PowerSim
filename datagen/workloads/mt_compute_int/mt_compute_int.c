/*
 * mt_compute_int.c — ALU-bound 纯整数计算多核负载
 *
 * 设计目标：
 *   - 每核独立工作集 64B（一个 cacheline），稳定命中 L1
 *   - 内层都是整数 mul/xor/shift/add，长依赖链
 *   - 完全无分支误预测压力（循环计数器 well-predicted）
 *
 * V9.5：通过 m5_work_begin / m5_work_end 标记 ROI，让 tao_trace 跳过启动期，
 *       atomic 与 detailed 在 ROI 内 100% 控制流对齐。
 *
 * 预期标签分布：
 *   - SharedAttr.path_class    ≈ 100% L1
 *   - SharedAttr.coh_action    ≈ 100% L1_HIT
 *   - label_branch_mispred     ≈ 0
 *
 * 注意：依赖 RubySystem.access_backing_store=True（在 run_mt_mvp.py 里设置），
 *   否则 SE 模式 startup 阶段的 functional read 会因 BSS 布局触发 fatal
 *   (RubyPort.cc:463)。
 *
 * 用法：
 *   ./mt_compute_int <nthreads> <iter_unit>
 *   推荐：./mt_compute_int 4 800   （目标 ~100k macro / 4 threads）
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

/*
 * V9.5：m5_work_begin / m5_work_end pseudo-instruction（X86 ABI）。
 *   编码 = 0x0F 0x04 <imm16=func>，其中 func: WORK_BEGIN=0x5a, WORK_END=0x5b。
 *   gem5 src/sim/pseudo_inst.cc:workbegin/workend 解码后回调 System::workItemBegin/End。
 *   probe（tao_trace / atomic_func_trace）监听 System::workBegin/workEnd 信号开关 ROI。
 */
static inline void m5_work_begin_inline(void)
{
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005a"
                         : : : "memory");
}
static inline void m5_work_end_inline(void)
{
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005b"
                         : : : "memory");
}

#define MAX_THREADS 16
#define LINE_BYTES  64

static int  g_nthreads = 4;
static long g_iter     = 800;

/* 每核 1 cacheline = 8 个 long，永远 L1-hit */
static long g_priv[MAX_THREADS][8] __attribute__((aligned(LINE_BYTES)));

/* ALU 内核：长依赖链，避免被编译器消掉 */
static long compute_int_kernel(int tid, long iters)
{
    long *a = g_priv[tid];
    a[0] = (long)tid * 0x12345;
    a[1] = (long)tid * 0x67890;
    a[2] = (long)tid * 0xabcde;
    a[3] = (long)tid * 0xf0f0f;
    long x = a[0]; long y = a[1]; long z = a[2]; long w = a[3];
    for (long i = 0; i < iters; i++) {
        x = x * 0x9e3779b1L + 0x12345;
        y = (y ^ (y >> 13)) + i;
        z = z + (z << 5) - x;
        w = (w * 5 + y) ^ (z >> 7);
        x = x + w;
    }
    a[0] = x; a[1] = y; a[2] = z; a[3] = w;
    return x ^ y ^ z ^ w;
}

typedef struct { int tid; long cs; } warg_t;

static void *worker(void *p)
{
    warg_t *a = (warg_t *)p;
    /* V9.5：ROI 起点 —— 仅 ROI 内的 commit 进入 trace。
     *   每个 thread 各自打开 ROI，避开线程创建期的 syscall / TLS init。 */
    m5_work_begin_inline();
    a->cs = compute_int_kernel(a->tid, g_iter * 5);
    m5_work_end_inline();
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc >= 2) g_nthreads = atoi(argv[1]);
    if (argc >= 3) g_iter     = atol(argv[2]);
    if (g_nthreads <= 0 || g_nthreads > MAX_THREADS) g_nthreads = 4;
    if (g_iter    <= 0)                              g_iter    = 800;

    fprintf(stderr, "mt_compute_int: nthreads=%d iter_unit=%ld\n",
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
    fprintf(stderr, "mt_compute_int: done. total=%ld\n", total);
    return 0;
}
