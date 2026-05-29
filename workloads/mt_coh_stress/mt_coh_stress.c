/*
 * mt_coh_stress.c — 全 coh 路径覆盖压力负载（用于 ref_simulator 对齐验证）
 *
 * 设计目标：在 gem5 SE 模式下若干分钟内跑完，让 confusion matrix 在
 *   L1 / L2 / LLC / DRAM / R_DIRTY / R_CLEAN / WB 多个分类上各产出 >=数十样本。
 *   任意一格非对角 ⇒ ref 对齐 bug。
 *
 * 仅使用 plain volatile load/store + sched_yield，不引入 atomic / fence /
 * mutex / cond / barrier / pthread_barrier_wait 指令（避免 probe 路径偏置，
 * 也避免 SE 模式下 spin-barrier 假死）。
 *
 * Phase 间不互等（沿用 mt_micro_coh epoch counter 风格）。R_DIRTY/R_CLEAN
 * 通过 phase 内"producer 反复写 / consumer 反复读"的时间错位自然产生。
 *
 * Build:
 *   make
 * Run:
 *   ./mt_coh_stress <nthreads> <iter_unit>
 *   推荐: ./mt_coh_stress 4 800
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define MAX_THREADS 16
#define LINE_BYTES  64

/* V9.6 ROI 闸门，与其他 µbench 同款 inline pseudo-op。 */
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

/* 各 phase 的工作集大小（cacheline 数）— 已压缩，目标几分钟内跑完 */
#define WS_L1   8        /*  512 B  -> 必命中 L1                  */
#define WS_L2   512      /*  32 KiB -> L2 命中（>L1 32K 边界附近） */
#define WS_LLC  4096     /* 256 KiB -> LLC 命中（>L2）            */
#define WS_DRAM 8192     /* 512 KiB -> 大于 LLC slice，触发 evict */

#define SHARED_LINES 32
#define MIG_LINES    16

static int  g_nthreads = 4;
static long g_iter     = 800;     /* 各 phase 单元迭代 */

/* per-thread private buffers — 全部使用 static BSS（避免 mmap，SE 模式
 * 下 mmap 区域的 functional read 会失败）。 */
static long g_priv_l1  [MAX_THREADS][WS_L1   * 8] __attribute__((aligned(LINE_BYTES)));
static long g_priv_l2  [MAX_THREADS][WS_L2   * 8] __attribute__((aligned(LINE_BYTES)));
static long g_priv_llc [MAX_THREADS][WS_LLC  * 8] __attribute__((aligned(LINE_BYTES)));
static long g_priv_dram[MAX_THREADS][WS_DRAM * 8] __attribute__((aligned(LINE_BYTES)));

/* 共享区：用于 R_DIRTY / R_CLEAN / WB / migration */
static volatile long g_shared_dirty[SHARED_LINES][8]
    __attribute__((aligned(LINE_BYTES)));
static volatile long g_shared_clean[SHARED_LINES][8]
    __attribute__((aligned(LINE_BYTES)));
static volatile long g_fs_line[8]
    __attribute__((aligned(LINE_BYTES)));
static volatile long g_mig_buf[MIG_LINES][8]
    __attribute__((aligned(LINE_BYTES)));
/* Ping-pong：两核反复 RMW 同一 word，line 在两核 L1 间窄窗口反弹，
 * 高概率触发 R_DIRTY。 */
static volatile long g_pp_word
    __attribute__((aligned(LINE_BYTES)));

/* ---------- phases ---------- */

/* P_L1: 反复触摸极小工作集 → 必命中 L1。 */
static long phase_l1(int tid)
{
    long acc = 0;
    long *a = g_priv_l1[tid];
    long n  = WS_L1 * 8;
    for (long i = 0; i < g_iter * 4; i++) {
        long idx = (i * 13) & (n - 1);
        a[idx] = a[idx] + i;
        acc += a[idx];
    }
    return acc;
}

/* P_L2: 工作集 ~ 32 KiB → 出 L1 命中 L2。 */
static long phase_l2(int tid)
{
    long acc = 0;
    long *a = g_priv_l2[tid];
    long n  = WS_L2 * 8;
    for (long i = 0; i < g_iter * 4; i++) {
        long idx = (i * 17) % n;
        a[idx] = a[idx] + i;
        acc += a[(idx + 7) % n];
    }
    return acc;
}

/* P_LLC: 工作集 ~ 256 KiB → 出 L1/L2 命中 LLC。 */
static long phase_llc(int tid)
{
    long acc = 0;
    long *a = g_priv_llc[tid];
    long n  = WS_LLC * 8;
    for (long i = 0; i < g_iter * 2; i++) {
        long idx = (i * 257) % n;
        acc += a[idx];
    }
    return acc;
}

/* P_DRAM: 工作集 > LLC slice，流式扫 → 必产生 evict + DRAM 访问。 */
static long phase_dram(int tid)
{
    long acc = 0;
    long *a = g_priv_dram[tid];
    long n  = WS_DRAM * 8;
    for (long i = 0; i < g_iter; i++) {
        long idx = (i * 1031) % n;
        acc += a[idx];
        a[idx ^ 8] = i;
    }
    return acc;
}

/* P_COH_PRODUCER_CONSUMER:
 *   tid==0  : 反复写 g_shared_dirty（保持 line 在自己 cache 上 M 状态）
 *   tid!=0  : 反复读 g_shared_dirty
 *   tid==1  : 同时也反复读 g_shared_clean（与 tid 0 共享 → S 状态）
 *
 * 时间错位下：
 *   - consumer load 在 producer 刚 store 完未被 LLC 拿走时 → R_DIRTY
 *   - 多次 load 让 line 升到 S 后再被读 → R_CLEAN / LLC
 *   - 不需要 barrier。 */
static long phase_coh(int tid)
{
    long acc = 0;
    long iters = g_iter * 4;
    if (tid == 0) {
        for (long i = 0; i < iters; i++) {
            int row = (int)(i % SHARED_LINES);
            g_shared_dirty[row][0] = i;
            g_shared_clean[row][0] = i ^ 0x5a;
            /* V9.6 档3：删除 ROI 内 sched_yield，避免 syscall 污染 latency。 */
        }
    } else {
        for (long i = 0; i < iters; i++) {
            int row = (int)(i % SHARED_LINES);
            acc += g_shared_dirty[row][0];
            acc += g_shared_clean[row][0];
        }
    }
    return acc;
}

/* P_PINGPONG: 双核 RMW（与 mt_micro_coh phase_e 相同模式，已实测产
 * R_DIRTY）。tid 0/1 反复 read-modify-write 同一 line，line 在两核 L1 间
 * 真正反弹；每次 load 到 Ruby 都可能撞上对方 M 状态 → R_DIRTY。
 * 关键：必须用 RMW（read+write）而非纯 store；纯 store 会让 owner 长时间
 * 停在写方，consumer 没机会进入；RMW 让两核轮流抢 M ownership。 */
static long phase_pingpong(int tid)
{
    long acc = 0;
    long iters = g_iter * 32;
    if (tid <= 1) {
        /* 双核 RMW —— line 在两核 L1 间反弹 */
        for (long i = 0; i < iters; i++) {
            long v = g_pp_word;
            g_pp_word = v + 1;
            acc ^= v;
        }
    } else {
        /* 其它 tid 让出周期但保持运行（不让 SE 把 cycle 全分给 tid 0/1） */
        for (long i = 0; i < iters; i++) {
            acc += i;
        }
    }
    return acc;
}

/* P_WB: false sharing —— 多核轮流 store 同一 line 的不同 lane，
 * 引发 cache-to-cache invalidate / WB。 */
static void phase_wb(int tid)
{
    int lane = tid % 8;
    for (long i = 0; i < g_iter * 2; i++) {
        g_fs_line[lane] = g_fs_line[lane] + 1;
    }
}

/* P_MIG: ownership 链式迁移（mt_micro_coh phase_f 同款，实测产 R_DIRTY）。 */
static long phase_mig(int tid)
{
    long acc = 0;
    int next = (tid + 1) % g_nthreads;
    for (long i = 0; i < g_iter * 8; i++) {
        int row = (int)(i % MIG_LINES);
        if ((i & 1) == 0) {
            g_mig_buf[row][tid % 8] = i + tid;
        } else {
            acc += g_mig_buf[row][next % 8];
        }
    }
    return acc;
}

/* ---------- worker ---------- */

typedef struct { int tid; long cs; } warg_t;

static void *worker(void *p)
{
    warg_t *a = (warg_t *)p;
    int tid = a->tid;
    long cs = 0;

    /* V9.6 ROI 起点：避开线程创建 / TLS init / pthread 启动期 syscall。 */
    m5_work_begin_inline();
    /* 顺序：先把私有 line 装入 cache，再触发跨核 coh，最后大工作集 evict。
     * phase 之间不互等，靠 yield 让相位自然错开。 */
    cs ^= phase_l1(tid);
    cs ^= phase_l2(tid);
    cs ^= phase_llc(tid);
    cs ^= phase_coh(tid);
    cs ^= phase_pingpong(tid);
    phase_wb(tid);
    cs ^= phase_mig(tid);
    cs ^= phase_dram(tid);
    m5_work_end_inline();

    a->cs = cs;
    return NULL;
}

/* ---------- main ---------- */

int main(int argc, char **argv)
{
    if (argc >= 2) g_nthreads = atoi(argv[1]);
    if (argc >= 3) g_iter     = atol(argv[2]);
    if (g_nthreads <= 0 || g_nthreads > MAX_THREADS) g_nthreads = 4;
    if (g_iter    <= 0)                              g_iter    = 800;

    fprintf(stderr, "mt_coh_stress: nthreads=%d iter_unit=%ld\n",
            g_nthreads, g_iter);

    pthread_t ths[MAX_THREADS];
    warg_t   args[MAX_THREADS];
    for (int i = 0; i < g_nthreads; i++) {
        args[i].tid = i;
        args[i].cs  = 0;
        pthread_create(&ths[i], NULL, worker, &args[i]);
    }
    long total = 0;
    for (int i = 0; i < g_nthreads; i++) {
        pthread_join(ths[i], NULL);
        total ^= args[i].cs;
    }
    fprintf(stderr,
            "mt_coh_stress: done. total=%ld dirty0=%ld fs0=%ld\n",
            total, (long)g_shared_dirty[0][0], (long)g_fs_line[0]);
    return 0;
}
