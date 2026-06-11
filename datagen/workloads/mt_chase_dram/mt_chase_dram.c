/*
 * mt_chase_dram.c — DRAM-bound 多核指针追踪负载
 *
 * 设计目标：
 *   - 每核独立大链表（>> LLC），随机化 next 指针 → 无法 prefetch
 *   - 每条 load 都依赖前一条 load 结果（true dep）
 *   - 不同线程访问完全独立 buffer，无跨核共享 → 排除 coh
 *
 * V9.5：通过 m5_work_begin / m5_work_end 标记 ROI，跳过 build_chain 阶段
 *       （build_chain 含 pthread mutex / 大量分支，atomic vs detailed 易分叉）。
 *       仅 chase 阶段进入 trace，atomic 与 detailed 的纯指针追踪 100% 对齐。
 *
 * 预期标签分布：
 *   - SharedAttr.path_class    主导 LLC + DRAM
 *   - SharedAttr.coh_action    LLC_HIT / DRAM 占主
 *   - label_branch_mispred     ≈ 0
 *   - label_execution_latency  长尾（DRAM 命中带来 100+ 周期 stall）
 *
 * 工作集：每核 1 MiB（16384 nodes × 64 B），>> L2(256K) 且接近 LLC 单核份额；
 *   4 核同时跑总共占用 4 MiB > LLC 2 MiB → 强制 DRAM。
 *
 * 注意：依赖 RubySystem.access_backing_store=True（在 run_mt_mvp.py 里设置），
 *   否则 SE 模式 startup 阶段的 functional read 会因 BSS 布局变化触发 fatal
 *   (RubyPort.cc:463)。
 *
 * 用法：
 *   ./mt_chase_dram <nthreads> <iter_unit>
 *   推荐：./mt_chase_dram 4 1500
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

/*
 * V9.5：m5_work_begin / m5_work_end pseudo-instruction（X86 ABI）。
 *   编码 = 0x0F 0x04 <imm16=func>，func: WORK_BEGIN=0x5a, WORK_END=0x5b。
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
#define NODES       16384      /* 1 MiB / 核 */

struct node {
    struct node *next;
    long pad[7];
} __attribute__((aligned(LINE_BYTES)));

static int  g_nthreads = 4;
static long g_iter     = 1500;

static struct node g_chain[MAX_THREADS][NODES];

static void build_chain(int tid)
{
    int idx[NODES];
    for (int i = 0; i < NODES; i++) idx[i] = i;
    unsigned long s = 0x9e3779b97f4a7c15UL ^ ((unsigned long)tid * 0x12345);
    for (int i = NODES - 1; i > 0; i--) {
        s = s * 6364136223846793005UL + 1442695040888963407UL;
        int j = (int)((s >> 17) % (unsigned long)(i + 1));
        int t = idx[i]; idx[i] = idx[j]; idx[j] = t;
    }
    for (int i = 0; i < NODES; i++) {
        g_chain[tid][idx[i]].next = &g_chain[tid][idx[(i + 1) % NODES]];
    }
}

static long chase(int tid, long iters)
{
    struct node *p = &g_chain[tid][0];
    long acc = 0;
    for (long i = 0; i < iters; i++) {
        p = p->next;
        acc += (long)(uintptr_t)p;
    }
    return acc;
}

typedef struct { int tid; long cs; } warg_t;

static void *worker(void *p)
{
    warg_t *a = (warg_t *)p;
    build_chain(a->tid);
    /* V9.5：ROI 仅覆盖 chase（指针追踪）阶段。
     *   build_chain 含 PRNG / 数组重排，分支密集，atomic vs detailed 易分叉。
     *   chase 是纯 load-use-load 链，无条件分支（除循环计数），100% 对齐。 */
    m5_work_begin_inline();
    a->cs = chase(a->tid, g_iter * 8);
    m5_work_end_inline();
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc >= 2) g_nthreads = atoi(argv[1]);
    if (argc >= 3) g_iter     = atol(argv[2]);
    if (g_nthreads <= 0 || g_nthreads > MAX_THREADS) g_nthreads = 4;
    if (g_iter    <= 0)                              g_iter    = 1500;

    fprintf(stderr, "mt_chase_dram: nthreads=%d iter_unit=%ld nodes/th=%d\n",
            g_nthreads, g_iter, NODES);

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
    fprintf(stderr, "mt_chase_dram: done. total=%ld\n", total);
    return 0;
}
