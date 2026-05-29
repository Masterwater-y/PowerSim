/*
 * W11_stream_mix: pthread realistic memory kernel.
 *
 * ROI excludes pthread create/join/barriers and first-touch initialization.
 * Inside ROI each thread works on a private slice, switching among several
 * load/store/FP/int-addressing phases to avoid one tiny loop dominating fv.
 *
 * Usage: ./mt_stream_mix <threads> <iters> <size_kb> <mode> <seed>
 *   smoke: ./mt_stream_mix 4 1 64 0
 */
#include "../common/pthread_harness.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define LINE_BYTES 64

typedef struct {
    double *a;
    double *b;
    double *c;
    uint32_t *idx;
    size_t n;
} stream_mix_data_t;

static void stream_mix_kernel(const tao_thread_ctx_t *ctx, long iter)
{
    stream_mix_data_t *d = (stream_mix_data_t *)ctx->user;
    const size_t lo = ctx->lo;
    const size_t hi = ctx->hi;
    const int phase = (int)((iter + ctx->tid + ctx->cfg->mode) & 7);
    const double scalar = 1.000244140625 + (double)((iter + ctx->tid) & 3);

    switch (phase) {
    case 0:
        for (size_t i = lo; i < hi; ++i) d->c[i] = d->a[i];
        break;
    case 1:
        for (size_t i = lo; i < hi; ++i) d->b[i] = scalar * d->c[i];
        break;
    case 2:
        for (size_t i = lo; i < hi; ++i) d->c[i] = d->a[i] + d->b[i];
        break;
    case 3:
        for (size_t i = lo; i < hi; ++i) d->a[i] = d->b[i] + scalar * d->c[i];
        break;
    case 4:
        for (size_t i = lo; i < hi; i += 2) d->c[i] = d->a[i] - d->b[i];
        break;
    case 5:
        for (size_t i = lo; i < hi; ++i) d->b[i] += d->a[d->idx[i]];
        break;
    case 6:
        for (size_t i = lo; i < hi; ++i) {
            double x = d->a[i] + d->b[i];
            d->c[i] = (x > 1.0) ? x * 0.5 : x + scalar;
        }
        break;
    default:
        for (size_t i = lo; i < hi; ++i) {
            size_t j = (i + 17) % d->n;
            d->a[i] = 0.75 * d->a[i] + 0.25 * d->c[j];
        }
        break;
    }
}

int main(int argc, char **argv)
{
    tao_harness_cfg_t cfg = {
        .name = "W11_stream_mix",
        .nthreads = 4,
        .iters = 2,
        .size_kb = 256,
        .mode = 0,
        .seed = UINT64_C(0x511eab1e),
    };
    tao_parse_args(argc, argv, &cfg);

    size_t n = (cfg.size_kb * 1024) / sizeof(double);
    if (n < 1024) n = 1024;

    stream_mix_data_t d;
    d.n = n;
    d.a = (double *)tao_aligned_zalloc(LINE_BYTES, n * sizeof(double));
    d.b = (double *)tao_aligned_zalloc(LINE_BYTES, n * sizeof(double));
    d.c = (double *)tao_aligned_zalloc(LINE_BYTES, n * sizeof(double));
    d.idx = (uint32_t *)tao_aligned_zalloc(LINE_BYTES, n * sizeof(uint32_t));
    if (!d.a || !d.b || !d.c || !d.idx) {
        fprintf(stderr, "%s: alloc failed\n", cfg.name);
        return 1;
    }

    uint64_t s = cfg.seed;
    for (size_t i = 0; i < n; ++i) {
        d.a[i] = (double)(int)(tao_splitmix64(&s) & 1023) / 1024.0;
        d.b[i] = (double)(int)(tao_splitmix64(&s) & 1023) / 2048.0;
        d.c[i] = 0.0;
        d.idx[i] = (uint32_t)(tao_splitmix64(&s) % n);
    }

    fprintf(stderr, "%s: threads=%d iters=%ld size_kb=%zu n=%zu mode=%d seed=0x%llx\n",
            cfg.name, cfg.nthreads, cfg.iters, cfg.size_kb, n, cfg.mode,
            (unsigned long long)cfg.seed);

    int rc = tao_run_threads(&cfg, n, &d, stream_mix_kernel);

    volatile double sink = d.a[0] + d.b[n / 2] + d.c[n - 1];
    fprintf(stderr, "%s: done sink=%f\n", cfg.name, sink);
    free(d.a);
    free(d.b);
    free(d.c);
    free(d.idx);
    return rc;
}
