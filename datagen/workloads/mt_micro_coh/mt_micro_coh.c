/*
 * mt_micro_coh.c — coherence-only multi-thread microbench (v2).
 *
 * 与 v1 mt_micro.c 的关键差异：
 *   - 移除所有 atomic / fence / mutex / cond / barrier 指令；
 *   - 仅保留 pthread_create / pthread_join / sched_yield 三类调度系统调用；
 *   - 共享访问全部走 plain (volatile) load/store，不加 memory_order。
 *
 * 各 phase 之间不再用 pthread_barrier_wait，改用 per-thread epoch counter，
 * 让每个线程跑完自己 N 次迭代直接进入下一 phase（允许微小的相位错位，
 * 让仿真器自然观察 ownership migration）。
 *
 * Build:
 *   make                 (产出 ./mt_micro_coh，x86_64 静态链接)
 * Run (host):
 *   ./mt_micro_coh 4 25000
 * Run (gem5 SE):
 *   build/X86/gem5.opt --cmd=./mt_micro_coh --options="4 25000" ...
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

/* V9.6 ROI：与 mt_compute_int / mt_chase_dram 同款 inline pseudo-op，
 *   绕开 worker 线程创建 / TLS init / set_robust_list 等启动期 syscall。
 *   probe 内部状态机 always-update，emit 路径仅在 begin..end 之间放行。 */
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

static int  g_nthreads = 4;
static long g_iters    = 25000;

/* --------- 共享数据结构（全部 volatile，不用 atomic） --------- */

/* Phase D: false sharing 64B 行；每 thread 写 8B lane */
typedef struct {
    volatile long lane[8];
} fs_line_t;
static fs_line_t g_fs_line __attribute__((aligned(LINE_BYTES)));

/* Phase E: ping-pong word（两 thread 来回普通读写） */
static volatile long g_pp_word __attribute__((aligned(LINE_BYTES))) = 0;

/* Phase F: ownership migration —— 一段共享缓冲，链式由 t0 写、t1 读、t2 写、t3 读… */
#define MIG_LINES 16
static volatile long g_mig_buf[MIG_LINES][8] __attribute__((aligned(LINE_BYTES)));

/* Phase B: 每线程私有数组（确保 LOCAL_HIT 数据点） */
static long g_priv[MAX_THREADS][1024] __attribute__((aligned(LINE_BYTES)));

/* --------- worker phases --------- */

static long phase_a_compute(int tid)
{
    long acc = tid;
    for (long i = 0; i < g_iters; i++) {
        acc = acc * 1103515245L + 12345L;
        acc ^= (acc >> 17);
    }
    return acc;
}

static long phase_b_priv_mem(int tid)
{
    long acc = 0;
    long *a = g_priv[tid];
    for (long i = 0; i < g_iters; i++) {
        a[i & 1023] = i ^ tid;
        acc += a[(i * 7) & 1023];
    }
    return acc;
}

static long phase_c_shared_read(int tid)
{
    /* 多线程共读 thread 0 的私有缓冲 → mesi_before 倾向 S/E,
     * sharer_bucket >= 2。 */
    long acc = 0;
    long *a = g_priv[0];
    for (long i = 0; i < g_iters; i++) acc += a[i & 1023];
    (void)tid;
    return acc;
}

static void phase_d_false_sharing(int tid)
{
    /* 普通 store（非 atomic），不同 lane → 同一 cacheline 上 false sharing。
     * volatile 保证编译器不会把循环外提。 */
    int lane = tid % 8;
    for (long i = 0; i < g_iters; i++) {
        g_fs_line.lane[lane] = g_fs_line.lane[lane] + 1;
    }
}

static void phase_e_ping_pong(int tid)
{
    /* 仅 thread 0/1 参与；plain volatile RMW，无 memory_order。 */
    if (tid >= 2) return;
    for (long i = 0; i < g_iters; i++) {
        long v = g_pp_word;
        g_pp_word = v + 1;
    }
}

static void phase_f_migration(int tid)
{
    /* 让 ownership 沿 thread 链流动：tid 写第 i 行，下一 tid 读第 i 行。 */
    long acc = 0;
    int next = (tid + 1) % g_nthreads;
    for (long i = 0; i < g_iters; i++) {
        int row = (int)(i % MIG_LINES);
        if ((i & 1) == 0) {
            /* 偶数迭代：tid 写自己 lane */
            g_mig_buf[row][tid % 8] = i + tid;
        } else {
            /* 奇数迭代：读"下一 tid"刚写过的 lane */
            acc += g_mig_buf[row][next % 8];
        }
    }
    /* prevent dead-store elimination */
    g_priv[tid][0] = acc;
}

static void phase_j_yield(int tid)
{
    (void)tid;
    /* 每线程发若干次 sched_yield，给 probe 一个 syscall 锚点。 */
    for (int k = 0; k < 8; k++) sched_yield();
}

static long phase_z_cooldown(int tid)
{
    long acc = tid;
    for (long i = 0; i < (g_iters / 4 + 1); i++) {
        acc = acc * 6364136223846793005L + 1442695040888963407L;
    }
    return acc;
}

/* --------- worker --------- */

typedef struct { int tid; long checksum; } worker_arg_t;

static void *worker(void *p)
{
    worker_arg_t *a = (worker_arg_t *)p;
    int tid = a->tid;
    long cs = 0;

    /* V9.6 ROI 起点：避开线程创建 / TLS init / pthread 启动期 syscall。 */
    m5_work_begin_inline();
    cs ^= phase_a_compute(tid);
    cs ^= phase_b_priv_mem(tid);
    cs ^= phase_c_shared_read(tid);
    phase_d_false_sharing(tid);
    phase_e_ping_pong(tid);
    phase_f_migration(tid);
    cs ^= phase_z_cooldown(tid);
    m5_work_end_inline();
    /* V9.6 档3：sched_yield 段移出 ROI，避免 syscall 影响 latency 标签分布。 */
    phase_j_yield(tid);

    a->checksum = cs;
    return NULL;
}

/* --------- main --------- */

int main(int argc, char **argv)
{
    if (argc >= 2) g_nthreads = atoi(argv[1]);
    if (argc >= 3) g_iters    = atol(argv[2]);
    if (g_nthreads <= 0 || g_nthreads > MAX_THREADS) g_nthreads = 4;
    if (g_iters    <= 0)                              g_iters    = 25000;

    fprintf(stderr, "mt_micro_coh: nthreads=%d iters=%ld\n",
            g_nthreads, g_iters);

    pthread_t    ths[MAX_THREADS];
    worker_arg_t args[MAX_THREADS];
    for (int i = 0; i < g_nthreads; i++) {
        args[i].tid = i;
        args[i].checksum = 0;
        pthread_create(&ths[i], NULL, worker, &args[i]);
    }
    long total = 0;
    for (int i = 0; i < g_nthreads; i++) {
        pthread_join(ths[i], NULL);
        total ^= args[i].checksum;
    }

    /* 读出共享变量防止 DCE */
    fprintf(stderr,
            "mt_micro_coh: done. total_checksum=%ld pp_word=%ld "
            "fs_lane0=%ld\n",
            total, (long)g_pp_word, (long)g_fs_line.lane[0]);
    return 0;
}
