/*
 * tao_bench.h — LLMSim 统一多核多线程 workload harness。
 *
 * 设计目标（与 taogen 旧 workload 不一致问题的根治）：
 *   1. N 线程严格绑到 N 个核（CPU_SET(tid)），(core_id, thread_id) 1:1。
 *   2. 主线程也当 worker[0]（tid==0 不另起线程），所以 core0 与其它核同质、满负载。
 *   3. 每个 worker 各自 m5_work_begin / m5_work_end 标记 ROI，避开线程创建期。
 *   4. 统一单参数 SCALE 控制数据规模；启动 barrier 保证多核并发稳态。
 *
 * 用法（每个 workload 只需实现 kernel）：
 *   #include "tao_bench.h"
 *   static void kernel(int tid, int nthreads, long scale, void *shared) { ... }
 *   TAO_BENCH_MAIN("name", kernel, need_shared_bytes_fn)
 *
 * CLI：  ./bench <nthreads> <scale>
 *   nthreads : 线程数 = 核数（默认 4，<= TAO_MAX_THREADS）
 *   scale    : 单参数规模旋钮（默认 1000），含义由各 kernel 解释（迭代/元素数）
 */
#ifndef TAO_BENCH_H
#define TAO_BENCH_H

#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define TAO_MAX_THREADS 64
#define TAO_LINE 64

/* ---- m5 ROI pseudo-inst（X86）：WORK_BEGIN=0x5a / WORK_END=0x5b ----
 * 这两条是 gem5 的 pseudo-instruction（.byte 0x0F 0x04 ...），在真实 x86 上
 * 属未定义 opcode，直接执行会 SIGILL。因此用开关控制是否发射：
 *   - 开关来源是【命令行第 3 位置参数 roi_flag】，而非环境变量。
 *     原因：gem5 SE 模式不会把宿主环境变量传给目标进程，getenv 读不到；
 *     但 --workload-args 一定能传到目标程序，故用 argv 最可靠。
 *   - 物理机：默认不传第 3 参 -> g_tao_roi=0 -> 跳过 -> 不报错。
 *   - gem5 采集：--workload-args <nthreads> <scale> 1 -> g_tao_roi=1 -> 发射 ROI。
 * 开关进程级缓存，热路径无开销。 */
static int g_tao_roi = 0;   /* 0=关(默认安全), 1=开 */

static inline void tao_roi_begin(void)
{
    if (g_tao_roi != 1) return;
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005a" : : : "memory");
}
static inline void tao_roi_end(void)
{
    if (g_tao_roi != 1) return;
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005b" : : : "memory");
}

typedef void (*tao_kernel_fn)(int tid, int nthreads, long scale, void *shared);
/* 返回该 workload 需要的共享内存字节数（0 = 不需要）。可为 NULL。 */
typedef size_t (*tao_shared_bytes_fn)(int nthreads, long scale);

typedef struct {
    int tid;
    int nthreads;
    long scale;
    void *shared;
    tao_kernel_fn kernel;
    pthread_barrier_t *bar;
} tao_arg_t;

/* ---- Kernel-controlled ROI 模式 ----
 * 默认（g_tao_kernel_owns_roi=0）：worker 在 barrier 后自动 tao_roi_begin/end，
 * kernel 内部的 alloc/init 也会被 ROI 包住（旧行为，对 init 极小的 kernel 没影响）。
 * 当 kernel 通过 TAO_BENCH_MAIN_KERNEL_ROI 注册时（g_tao_kernel_owns_roi=1），
 * worker 不再自动发射 ROI；kernel 必须自行调用 tao_phase_sync() + tao_roi_begin()
 * 包住真正的 hot loop，把 alloc/init 留在 ROI 外。
 *
 * tao_phase_sync() 走的是和 worker 启动 barrier 相同的 pthread_barrier，
 * 用于让所有线程的 init 都做完后再统一进 ROI（再用一个 barrier 没必要、会复用）。
 */
static int g_tao_kernel_owns_roi = 0;
static pthread_barrier_t *g_tao_phase_bar = NULL;

static inline void tao_phase_sync(void)
{
    if (g_tao_phase_bar) pthread_barrier_wait(g_tao_phase_bar);
}

static void tao_pin(int tid)
{
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(tid, &set);
    if (pthread_setaffinity_np(pthread_self(), sizeof(set), &set) != 0) {
        fprintf(stderr,
                "FATAL: setaffinity(tid=%d) failed: %s. nthreads<=cores?\n",
                tid, strerror(errno));
        abort();
    }
}

static void *tao_worker(void *p)
{
    tao_arg_t *a = (tao_arg_t *)p;
    tao_pin(a->tid);
    /* 全员到齐再进 ROI，保证多核并发稳态、跨核耦合真实发生。
     * kernel-owned ROI 模式下，这里只做 affinity barrier，不发射 ROI；
     * kernel 自己负责 init 之后再 tao_phase_sync() + tao_roi_begin()。 */
    pthread_barrier_wait(a->bar);
    if (!g_tao_kernel_owns_roi) tao_roi_begin();
    a->kernel(a->tid, a->nthreads, a->scale, a->shared);
    if (!g_tao_kernel_owns_roi) tao_roi_end();
    return NULL;
}

static void *tao_xaligned(size_t bytes)
{
    void *p = NULL;
    if (bytes == 0) return NULL;
    if (posix_memalign(&p, 4096, bytes) != 0) {
        fprintf(stderr, "FATAL: posix_memalign(%zu) failed\n", bytes);
        abort();
    }
    memset(p, 0, bytes);
    return p;
}

static int tao_bench_run(const char *name, int argc, char **argv,
                         tao_kernel_fn kernel, tao_shared_bytes_fn shbytes)
{
    int nthreads = 4;
    long scale = 1000;
    if (argc >= 2) nthreads = atoi(argv[1]);
    if (argc >= 3) scale = atol(argv[2]);
    if (argc >= 4) g_tao_roi = (atoi(argv[3]) == 1) ? 1 : 0;  /* 第3参开 ROI */
    if (nthreads <= 0 || nthreads > TAO_MAX_THREADS) nthreads = 4;
    if (scale <= 0) scale = 1000;

    fprintf(stderr, "[%s] nthreads=%d scale=%ld roi=%d\n",
            name, nthreads, scale, g_tao_roi);

    void *shared = NULL;
    if (shbytes) shared = tao_xaligned(shbytes(nthreads, scale));

    pthread_barrier_t bar;
    pthread_barrier_init(&bar, NULL, (unsigned)nthreads);
    g_tao_phase_bar = &bar;

    pthread_t th[TAO_MAX_THREADS];
    tao_arg_t args[TAO_MAX_THREADS];
    for (int t = 0; t < nthreads; ++t) {
        args[t].tid = t;
        args[t].nthreads = nthreads;
        args[t].scale = scale;
        args[t].shared = shared;
        args[t].kernel = kernel;
        args[t].bar = &bar;
        if (t == 0) continue;            /* 主线程自己当 worker[0] */
        if (pthread_create(&th[t], NULL, tao_worker, &args[t]) != 0) {
            fprintf(stderr, "FATAL: pthread_create(%d)\n", t);
            return 1;
        }
    }
    tao_worker(&args[0]);                 /* 主线程绑 core0 并参与计算 */
    for (int t = 1; t < nthreads; ++t) pthread_join(th[t], NULL);

    pthread_barrier_destroy(&bar);
    free(shared);
    fprintf(stderr, "[%s] done\n", name);
    return 0;
}

#define TAO_BENCH_MAIN(NAME, KERNEL, SHBYTES)                 \
    int main(int argc, char **argv)                           \
    {                                                         \
        return tao_bench_run(NAME, argc, argv, KERNEL, SHBYTES); \
    }

/* Kernel 自管 ROI：worker 不再自动 tao_roi_begin/end，kernel 必须在 init
 * 完成之后调用 tao_phase_sync() 再 tao_roi_begin()，hot loop 结束 tao_roi_end(). */
#define TAO_BENCH_MAIN_KERNEL_ROI(NAME, KERNEL, SHBYTES)      \
    int main(int argc, char **argv)                           \
    {                                                         \
        g_tao_kernel_owns_roi = 1;                            \
        return tao_bench_run(NAME, argc, argv, KERNEL, SHBYTES); \
    }

#endif /* TAO_BENCH_H */
