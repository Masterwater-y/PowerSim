/*
 * H01_mixed_service: holdout realistic mixed pthread workload.
 *
 * ROI contains only compute and memory operations.  pthread creation,
 * affinity, and barriers are provided by the common harness outside ROI.
 *
 * Usage:
 *   ./holdout_mixed_service <threads> <iters> <size_kb> <mode> <seed>
 */
#include "../common/pthread_harness.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define LINE_BYTES 64
#define HOT_WORDS 2048
#define MAIL_WORDS 1024

typedef struct {
    uint64_t *keys;
    uint64_t *vals;
    uint32_t *idx;
    uint64_t *out;
    volatile uint64_t *hot;
    volatile uint64_t *mailbox;
    size_t n;
} h01_data_t;

static inline uint64_t mix64(uint64_t x)
{
    x ^= x >> 33;
    x *= UINT64_C(0xff51afd7ed558ccd);
    x ^= x >> 33;
    x *= UINT64_C(0xc4ceb9fe1a85ec53);
    x ^= x >> 33;
    return x;
}

static void h01_kernel(const tao_thread_ctx_t *ctx, long iter)
{
    h01_data_t *d = (h01_data_t *)ctx->user;
    const size_t lo = ctx->lo;
    const size_t hi = ctx->hi;
    const size_t n = d->n;
    const uint64_t tid = (uint64_t)ctx->tid;
    const int mode = ctx->cfg->mode;
    uint64_t acc = UINT64_C(0x9e3779b97f4a7c15) ^ (tid << 32) ^ (uint64_t)iter;

    for (size_t i = lo; i < hi; ++i) {
        uint64_t k = d->keys[i] ^ acc;
        uint32_t j = d->idx[(i + (size_t)iter * 17u) % n];
        uint64_t v = d->vals[j];
        uint64_t h = d->hot[(k >> 6) & (HOT_WORDS - 1)];

        if (((k + h + (uint64_t)mode) & 7u) < 5u) {
            acc += mix64(k + v + h);
            d->out[i] = acc ^ d->mailbox[((size_t)ctx->tid * 64u + (i & 63u)) & (MAIL_WORDS - 1)];
        } else {
            size_t j2 = (j + ((k >> 11) & 127u)) % n;
            acc ^= mix64(d->keys[j2] + h);
            d->vals[i] = (d->vals[i] + acc) ^ (v >> 3);
        }

        if ((i & 15u) == 0u) {
            size_t mine = ((size_t)ctx->tid * 64u + ((i >> 4) & 63u)) & (MAIL_WORDS - 1);
            size_t peer = (((size_t)ctx->tid + 1u) * 64u + ((i >> 4) & 63u)) & (MAIL_WORDS - 1);
            d->mailbox[mine] = acc + d->mailbox[peer];
        }

        if ((i & 31u) == 7u) {
            d->hot[(i + (size_t)ctx->tid * 131u) & (HOT_WORDS - 1)] = acc;
        }
    }
}

int main(int argc, char **argv)
{
    tao_harness_cfg_t cfg = {
        .name = "H01_mixed_service",
        .nthreads = 4,
        .iters = 2,
        .size_kb = 128,
        .mode = 1,
        .seed = UINT64_C(0x48101d01),
    };
    tao_parse_args(argc, argv, &cfg);

    size_t n = (cfg.size_kb * 1024) / sizeof(uint64_t);
    if (n < 4096) n = 4096;

    h01_data_t d;
    d.n = n;
    d.keys = (uint64_t *)tao_aligned_zalloc(LINE_BYTES, n * sizeof(uint64_t));
    d.vals = (uint64_t *)tao_aligned_zalloc(LINE_BYTES, n * sizeof(uint64_t));
    d.idx = (uint32_t *)tao_aligned_zalloc(LINE_BYTES, n * sizeof(uint32_t));
    d.out = (uint64_t *)tao_aligned_zalloc(LINE_BYTES, n * sizeof(uint64_t));
    d.hot = (volatile uint64_t *)tao_aligned_zalloc(LINE_BYTES, HOT_WORDS * sizeof(uint64_t));
    d.mailbox = (volatile uint64_t *)tao_aligned_zalloc(LINE_BYTES, MAIL_WORDS * sizeof(uint64_t));
    if (!d.keys || !d.vals || !d.idx || !d.out || !d.hot || !d.mailbox) {
        fprintf(stderr, "%s: alloc failed\n", cfg.name);
        return 1;
    }

    uint64_t s = cfg.seed;
    for (size_t i = 0; i < n; ++i) {
        d.keys[i] = tao_splitmix64(&s);
        d.vals[i] = tao_splitmix64(&s);
        d.idx[i] = (uint32_t)(tao_splitmix64(&s) % n);
        d.out[i] = 0;
    }
    for (size_t i = 0; i < HOT_WORDS; ++i) d.hot[i] = tao_splitmix64(&s);
    for (size_t i = 0; i < MAIL_WORDS; ++i) d.mailbox[i] = tao_splitmix64(&s);

    fprintf(stderr, "%s: threads=%d iters=%ld size_kb=%zu n=%zu mode=%d seed=0x%llx\n",
            cfg.name, cfg.nthreads, cfg.iters, cfg.size_kb, n, cfg.mode,
            (unsigned long long)cfg.seed);

    int rc = tao_run_threads(&cfg, n, &d, h01_kernel);

    volatile uint64_t sink = d.out[n / 3] ^ d.vals[n / 2] ^ d.hot[17] ^ d.mailbox[23];
    fprintf(stderr, "%s: done sink=%llu\n", cfg.name, (unsigned long long)sink);
    free(d.keys);
    free(d.vals);
    free(d.idx);
    free(d.out);
    free((void *)d.hot);
    free((void *)d.mailbox);
    return rc;
}
