#define _GNU_SOURCE
#include "pthread_harness.h"

#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    tao_thread_ctx_t ctx;
    tao_kernel_fn kernel;
    pthread_barrier_t *start_barrier;
    pthread_barrier_t *end_barrier;
} worker_arg_t;

void tao_m5_work_begin(void)
{
    if (getenv("TAO_DISABLE_M5") != NULL) return;
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005a" : : : "memory");
}

void tao_m5_work_end(void)
{
    if (getenv("TAO_DISABLE_M5") != NULL) return;
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005b" : : : "memory");
}

uint64_t tao_splitmix64(uint64_t *x)
{
    uint64_t z = (*x += UINT64_C(0x9e3779b97f4a7c15));
    z = (z ^ (z >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    z = (z ^ (z >> 27)) * UINT64_C(0x94d049bb133111eb);
    return z ^ (z >> 31);
}

void *tao_aligned_zalloc(size_t align, size_t bytes)
{
    void *p = NULL;
    if (posix_memalign(&p, align, bytes) != 0) return NULL;
    memset(p, 0, bytes);
    return p;
}

void tao_parse_args(int argc, char **argv, tao_harness_cfg_t *cfg)
{
    if (argc >= 2) cfg->nthreads = atoi(argv[1]);
    if (argc >= 3) cfg->iters = atol(argv[2]);
    if (argc >= 4) cfg->size_kb = (size_t)atol(argv[3]);
    if (argc >= 5) cfg->mode = atoi(argv[4]);
    if (argc >= 6) cfg->seed = (uint64_t)strtoull(argv[5], NULL, 0);

    if (cfg->nthreads <= 0 || cfg->nthreads > TAO_MAX_THREADS) cfg->nthreads = 4;
    if (cfg->iters <= 0) cfg->iters = 1;
    if (cfg->size_kb == 0) cfg->size_kb = 256;
    if (cfg->seed == 0) cfg->seed = UINT64_C(0x123456789abcdef0);
}

static void try_pin_cpu(int tid)
{
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(tid, &set);
    /* V10: 严格保证 (core_id, thread_id) 1:1，下游 sample_steady_balanced
     * 等脚本依赖此不变量做 group-by。如失败（例如 nthreads > online cores），
     * 必须立即 abort，避免线程在多核间漂移导致采集到错位的 (core, tid)。 */
    if (pthread_setaffinity_np(pthread_self(), sizeof(set), &set) != 0) {
        fprintf(stderr,
                "FATAL: pthread_setaffinity_np(tid=%d) failed: %s. "
                "Ensure nthreads <= online CPU count.\n",
                tid, strerror(errno));
        abort();
    }
}

static void *worker_main(void *p)
{
    worker_arg_t *w = (worker_arg_t *)p;
    try_pin_cpu(w->ctx.tid);

    /* Both barriers are outside ROI. ROI contains only target kernel code. */
    pthread_barrier_wait(w->start_barrier);
    tao_m5_work_begin();
    for (long it = 0; it < w->ctx.cfg->iters; ++it) {
        w->kernel(&w->ctx, it);
    }
    tao_m5_work_end();
    pthread_barrier_wait(w->end_barrier);
    return NULL;
}

int tao_run_threads(
    const tao_harness_cfg_t *cfg,
    size_t total_items,
    void *user,
    tao_kernel_fn kernel)
{
    pthread_t th[TAO_MAX_THREADS];
    worker_arg_t args[TAO_MAX_THREADS];
    pthread_barrier_t start_barrier;
    pthread_barrier_t end_barrier;

    if (pthread_barrier_init(&start_barrier, NULL, (unsigned)cfg->nthreads) != 0) {
        fprintf(stderr, "%s: pthread_barrier_init(start) failed: %s\n",
                cfg->name, strerror(errno));
        return 1;
    }
    if (pthread_barrier_init(&end_barrier, NULL, (unsigned)cfg->nthreads) != 0) {
        fprintf(stderr, "%s: pthread_barrier_init(end) failed: %s\n",
                cfg->name, strerror(errno));
        return 1;
    }

    size_t chunk = (total_items + (size_t)cfg->nthreads - 1) / (size_t)cfg->nthreads;
    for (int tid = 0; tid < cfg->nthreads; ++tid) {
        size_t lo = (size_t)tid * chunk;
        size_t hi = lo + chunk;
        if (hi > total_items) hi = total_items;

        args[tid].ctx.cfg = cfg;
        args[tid].ctx.tid = tid;
        args[tid].ctx.lo = lo;
        args[tid].ctx.hi = hi;
        args[tid].ctx.user = user;
        args[tid].kernel = kernel;
        args[tid].start_barrier = &start_barrier;
        args[tid].end_barrier = &end_barrier;

        if (tid == 0) continue;

        int rc = pthread_create(&th[tid], NULL, worker_main, &args[tid]);
        if (rc != 0) {
            fprintf(stderr, "%s: pthread_create(%d) failed: %s\n",
                    cfg->name, tid, strerror(rc));
            return 1;
        }
    }

    worker_main(&args[0]);

    for (int tid = 1; tid < cfg->nthreads; ++tid) {
        pthread_join(th[tid], NULL);
    }
    pthread_barrier_destroy(&start_barrier);
    pthread_barrier_destroy(&end_barrier);
    return 0;
}
