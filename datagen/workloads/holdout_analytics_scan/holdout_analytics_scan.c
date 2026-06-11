/*
 * H03_analytics_scan: holdout scan/transform workload.
 *
 * The kernel mixes sequential scan, strided reuse, small LUT lookups, FP/int
 * transforms, and unsynchronized shared status rows.  ROI uses no locks,
 * atomics, sleeps, or scheduler calls.
 *
 * Usage:
 *   ./holdout_analytics_scan <threads> <iters> <size_kb> <mode> <seed>
 */
#include "../common/pthread_harness.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define LINE_BYTES 64
#define LUT_SIZE 4096
#define STATE_WORDS 1024

typedef struct {
    double *x;
    double *y;
    uint32_t *codes;
    double *lut;
    volatile uint64_t *state;
    size_t n;
} h03_data_t;

static inline uint64_t u64_from_double(double v)
{
    union {
        double d;
        uint64_t u;
    } x;
    x.d = v;
    return x.u;
}

static void h03_kernel(const tao_thread_ctx_t *ctx, long iter)
{
    h03_data_t *d = (h03_data_t *)ctx->user;
    const size_t lo = ctx->lo;
    const size_t hi = ctx->hi;
    const size_t n = d->n;
    const int mode = ctx->cfg->mode;
    double acc0 = 0.125 + (double)(ctx->tid + 1);
    double acc1 = 1.0 + (double)((iter + ctx->tid) & 7) * 0.03125;
    uint64_t bits = UINT64_C(0xfeedface12345678) ^ (uint64_t)ctx->tid;

    for (size_t i = lo; i < hi; ++i) {
        size_t left = (i == 0) ? i : i - 1;
        size_t right = (i + 17u) % n;
        uint32_t code = d->codes[(i * 13u + (size_t)iter * 7u) % n];
        double a = d->x[i];
        double b = d->x[left];
        double c = d->x[right];
        double l = d->lut[(code + (uint32_t)mode * 31u) & (LUT_SIZE - 1)];

        if ((code & 3u) == 0u) {
            acc0 += a * 0.5001220703125 + b * 0.25006103515625 + l;
            d->y[i] = acc0 - c * 0.125;
        } else if ((code & 3u) == 1u) {
            acc1 = acc1 * 1.000244140625 + c - l;
            d->y[i] = acc1 + a;
        } else if ((code & 3u) == 2u) {
            size_t j = (i + (size_t)(code & 255u) * 16u) % n;
            d->y[i] = d->x[j] + acc0 * 0.03125;
        } else {
            d->y[i] = (a > b) ? (a - b + l) : (b - a + acc1);
        }

        bits ^= u64_from_double(d->y[i]) + ((uint64_t)code << 32);
        bits = (bits << 9) | (bits >> 55);

        if ((i & 31u) == 3u) {
            size_t mine = ((size_t)ctx->tid * 64u + ((i >> 5) & 63u)) & (STATE_WORDS - 1);
            size_t peer = (((size_t)ctx->tid + 3u) * 64u + ((i >> 5) & 63u)) & (STATE_WORDS - 1);
            d->state[mine] = bits + d->state[peer];
        }
        if ((i & 127u) == 11u) {
            d->lut[(i + (size_t)ctx->tid * 193u) & (LUT_SIZE - 1)] += (double)(bits & 255u) * 0.0000152587890625;
        }
    }
}

int main(int argc, char **argv)
{
    tao_harness_cfg_t cfg = {
        .name = "H03_analytics_scan",
        .nthreads = 4,
        .iters = 2,
        .size_kb = 256,
        .mode = 3,
        .seed = UINT64_C(0x48303d03),
    };
    tao_parse_args(argc, argv, &cfg);

    size_t n = (cfg.size_kb * 1024) / sizeof(double);
    if (n < 4096) n = 4096;

    h03_data_t d;
    d.n = n;
    d.x = (double *)tao_aligned_zalloc(LINE_BYTES, n * sizeof(double));
    d.y = (double *)tao_aligned_zalloc(LINE_BYTES, n * sizeof(double));
    d.codes = (uint32_t *)tao_aligned_zalloc(LINE_BYTES, n * sizeof(uint32_t));
    d.lut = (double *)tao_aligned_zalloc(LINE_BYTES, LUT_SIZE * sizeof(double));
    d.state = (volatile uint64_t *)tao_aligned_zalloc(LINE_BYTES, STATE_WORDS * sizeof(uint64_t));
    if (!d.x || !d.y || !d.codes || !d.lut || !d.state) {
        fprintf(stderr, "%s: alloc failed\n", cfg.name);
        return 1;
    }

    uint64_t s = cfg.seed;
    for (size_t i = 0; i < n; ++i) {
        d.x[i] = (double)(int)(tao_splitmix64(&s) & 4095) / 1024.0;
        d.y[i] = 0.0;
        d.codes[i] = (uint32_t)tao_splitmix64(&s);
    }
    for (size_t i = 0; i < LUT_SIZE; ++i) {
        d.lut[i] = (double)(int)(tao_splitmix64(&s) & 1023) / 2048.0;
    }
    for (size_t i = 0; i < STATE_WORDS; ++i) d.state[i] = tao_splitmix64(&s);

    fprintf(stderr, "%s: threads=%d iters=%ld size_kb=%zu n=%zu mode=%d seed=0x%llx\n",
            cfg.name, cfg.nthreads, cfg.iters, cfg.size_kb, n, cfg.mode,
            (unsigned long long)cfg.seed);

    int rc = tao_run_threads(&cfg, n, &d, h03_kernel);

    volatile double sink = d.y[n / 4] + d.y[n / 2] + d.lut[19] + (double)(d.state[5] & 1023u);
    fprintf(stderr, "%s: done sink=%f\n", cfg.name, sink);
    free(d.x);
    free(d.y);
    free(d.codes);
    free(d.lut);
    free((void *)d.state);
    return rc;
}
