/*
 * W12_stencil2d: pthread 2D stencil/reuse kernel.
 *
 * ROI excludes pthread create/join/barriers and first-touch initialization.
 * Each ROI iteration reads the same input grid and writes an output grid, so
 * no cross-thread barrier is required inside ROI.
 *
 * Usage: ./mt_stencil2d <threads> <iters> <size_kb> <mode> <seed>
 *   smoke: ./mt_stencil2d 4 1 64 0
 */
#include "../common/pthread_harness.h"

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define LINE_BYTES 64

typedef struct {
    float *in;
    float *out;
    size_t n;
} stencil_data_t;

static void stencil_kernel(const tao_thread_ctx_t *ctx, long iter)
{
    stencil_data_t *d = (stencil_data_t *)ctx->user;
    const size_t n = d->n;
    const size_t row_lo = ctx->lo + 1;
    const size_t row_hi = ctx->hi + 1;
    const int phase = (int)((iter + ctx->cfg->mode) & 3);

    for (size_t r = row_lo; r < row_hi; ++r) {
        const size_t base = r * n;
        const size_t up = (r - 1) * n;
        const size_t dn = (r + 1) * n;
        for (size_t c = 1; c + 1 < n; ++c) {
            float center = d->in[base + c];
            float north = d->in[up + c];
            float south = d->in[dn + c];
            float west = d->in[base + c - 1];
            float east = d->in[base + c + 1];
            float v;

            if (phase == 0) {
                v = 0.50f * center + 0.125f * (north + south + west + east);
            } else if (phase == 1) {
                float nw = d->in[up + c - 1];
                float se = d->in[dn + c + 1];
                v = 0.40f * center + 0.10f * (north + south + west + east + nw + se);
            } else if (phase == 2) {
                float grad = (east - west) + (south - north);
                v = center + 0.25f * grad;
            } else {
                float sum = north + south + west + east;
                v = (sum > center) ? (0.75f * sum - center) : (center - 0.25f * sum);
            }
            d->out[base + c] = v;
        }
    }
}

static size_t choose_grid_n(size_t size_kb)
{
    size_t target_bytes = size_kb * 1024;
    if (target_bytes < 64 * 1024) target_bytes = 64 * 1024;
    size_t cells = target_bytes / (2 * sizeof(float));
    size_t n = (size_t)sqrt((double)cells);
    if (n < 64) n = 64;
    n &= ~(size_t)1;
    return n;
}

int main(int argc, char **argv)
{
    tao_harness_cfg_t cfg = {
        .name = "W12_stencil2d",
        .nthreads = 4,
        .iters = 2,
        .size_kb = 256,
        .mode = 0,
        .seed = UINT64_C(0x57e2c11d),
    };
    tao_parse_args(argc, argv, &cfg);

    stencil_data_t d;
    d.n = choose_grid_n(cfg.size_kb);
    size_t total = d.n * d.n;
    d.in = (float *)tao_aligned_zalloc(LINE_BYTES, total * sizeof(float));
    d.out = (float *)tao_aligned_zalloc(LINE_BYTES, total * sizeof(float));
    if (!d.in || !d.out) {
        fprintf(stderr, "%s: alloc failed\n", cfg.name);
        return 1;
    }

    uint64_t s = cfg.seed;
    for (size_t i = 0; i < total; ++i) {
        d.in[i] = (float)(tao_splitmix64(&s) & 4095) / 4096.0f;
        d.out[i] = 0.0f;
    }

    fprintf(stderr, "%s: threads=%d iters=%ld size_kb=%zu n=%zu mode=%d seed=0x%llx\n",
            cfg.name, cfg.nthreads, cfg.iters, cfg.size_kb, d.n, cfg.mode,
            (unsigned long long)cfg.seed);

    int rc = tao_run_threads(&cfg, d.n - 2, &d, stencil_kernel);

    volatile float sink = d.out[d.n + 1] + d.out[(d.n / 2) * d.n + d.n / 2];
    fprintf(stderr, "%s: done sink=%f\n", cfg.name, sink);
    free(d.in);
    free(d.out);
    return rc;
}
