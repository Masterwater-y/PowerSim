#ifndef TAOGEN_PTHREAD_HARNESS_H
#define TAOGEN_PTHREAD_HARNESS_H

#include <stddef.h>
#include <stdint.h>

#define TAO_MAX_THREADS 16

typedef struct {
    const char *name;
    int nthreads;
    long iters;
    size_t size_kb;
    int mode;
    uint64_t seed;
} tao_harness_cfg_t;

typedef struct {
    const tao_harness_cfg_t *cfg;
    int tid;
    size_t lo;
    size_t hi;
    void *user;
} tao_thread_ctx_t;

typedef void (*tao_kernel_fn)(const tao_thread_ctx_t *ctx, long iter);

void tao_m5_work_begin(void);
void tao_m5_work_end(void);
uint64_t tao_splitmix64(uint64_t *x);
void *tao_aligned_zalloc(size_t align, size_t bytes);
void tao_parse_args(int argc, char **argv, tao_harness_cfg_t *cfg);
int tao_run_threads(
    const tao_harness_cfg_t *cfg,
    size_t total_items,
    void *user,
    tao_kernel_fn kernel);

#endif
