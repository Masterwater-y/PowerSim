/*
 * W15_indirect_dispatch: virtual-call-like indirect dispatch kernel.
 *
 * Goal: exercise indirect branch / BTB behavior with mixed int/load/store
 * operations. ROI contains no synchronization.
 *
 * Usage: ./mt_indirect_dispatch <threads> <iters> <size_kb> <mode> <seed>
 *   smoke: ./mt_indirect_dispatch 4 1 64 0
 */
#include "../common/pthread_harness.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define LINE_BYTES 64
#define NFUN 8

typedef uint64_t (*op_fn_t)(uint64_t, uint64_t *, size_t);

typedef struct {
    uint8_t *ops;
    uint64_t *vals;
    uint64_t acc[TAO_MAX_THREADS];
    size_t n;
} dispatch_data_t;

__attribute__((noinline)) static uint64_t op0(uint64_t x, uint64_t *v, size_t i)
{ return x + v[i]; }
__attribute__((noinline)) static uint64_t op1(uint64_t x, uint64_t *v, size_t i)
{ return (x ^ v[i]) * UINT64_C(11400714819323198485); }
__attribute__((noinline)) static uint64_t op2(uint64_t x, uint64_t *v, size_t i)
{ v[i] = x + (v[i] >> 3); return v[i]; }
__attribute__((noinline)) static uint64_t op3(uint64_t x, uint64_t *v, size_t i)
{ return x + v[(i * 17u + 3u) & 1023u]; }
__attribute__((noinline)) static uint64_t op4(uint64_t x, uint64_t *v, size_t i)
{ return (x << 5) ^ (x >> 2) ^ v[i]; }
__attribute__((noinline)) static uint64_t op5(uint64_t x, uint64_t *v, size_t i)
{ v[i] ^= x; return v[i] + i; }
__attribute__((noinline)) static uint64_t op6(uint64_t x, uint64_t *v, size_t i)
{ return (x & 1u) ? (x + v[i]) : (x - v[i]); }
__attribute__((noinline)) static uint64_t op7(uint64_t x, uint64_t *v, size_t i)
{ return x + ((v[i] > x) ? v[i] : (x ^ v[i])); }

static op_fn_t g_ops[NFUN] = { op0, op1, op2, op3, op4, op5, op6, op7 };

static void indirect_dispatch_kernel(const tao_thread_ctx_t *ctx, long iter)
{
    dispatch_data_t *d = (dispatch_data_t *)ctx->user;
    uint64_t acc = d->acc[ctx->tid] + (uint64_t)iter;
    const size_t mask1024 = 1023u;

    for (size_t i = ctx->lo; i < ctx->hi; ++i) {
        uint8_t op = d->ops[i];
        size_t local = (i + (size_t)ctx->tid * 13u + (size_t)iter) & mask1024;
        acc = g_ops[op & (NFUN - 1)](acc, d->vals + (i & ~mask1024), local);
        if ((op ^ (uint8_t)acc) & 0x40u) {
            acc += d->vals[i];
        } else {
            acc ^= d->vals[i];
        }
    }
    d->acc[ctx->tid] = acc;
}

int main(int argc, char **argv)
{
    tao_harness_cfg_t cfg = {
        .name = "W15_indirect_dispatch",
        .nthreads = 4,
        .iters = 2,
        .size_kb = 256,
        .mode = 0,
        .seed = UINT64_C(0x1d15da7c),
    };
    tao_parse_args(argc, argv, &cfg);

    size_t n = (cfg.size_kb * 1024) / (sizeof(uint64_t) + sizeof(uint8_t));
    if (n < 4096) n = 4096;
    n = (n + 1023u) & ~1023u;

    dispatch_data_t d = {0};
    d.n = n;
    d.ops = (uint8_t *)tao_aligned_zalloc(LINE_BYTES, n);
    d.vals = (uint64_t *)tao_aligned_zalloc(LINE_BYTES, n * sizeof(uint64_t));
    if (!d.ops || !d.vals) {
        fprintf(stderr, "%s: alloc failed\n", cfg.name);
        return 1;
    }

    uint64_t s = cfg.seed;
    for (size_t i = 0; i < n; ++i) {
        uint64_t r = tao_splitmix64(&s);
        if (cfg.mode == 1) {
            d.ops[i] = (uint8_t)((i + (r & 3u)) & (NFUN - 1));
        } else if (cfg.mode == 2) {
            d.ops[i] = (uint8_t)(((r & 15u) < 10u) ? 0u : (r & (NFUN - 1)));
        } else {
            d.ops[i] = (uint8_t)(r & (NFUN - 1));
        }
        d.vals[i] = tao_splitmix64(&s);
    }

    fprintf(stderr, "%s: threads=%d iters=%ld size_kb=%zu n=%zu mode=%d seed=0x%llx\n",
            cfg.name, cfg.nthreads, cfg.iters, cfg.size_kb, n, cfg.mode,
            (unsigned long long)cfg.seed);

    int rc = tao_run_threads(&cfg, n, &d, indirect_dispatch_kernel);

    volatile uint64_t sink = 0;
    for (int i = 0; i < cfg.nthreads; ++i) sink ^= d.acc[i];
    fprintf(stderr, "%s: done sink=%llu\n", cfg.name, (unsigned long long)sink);
    free(d.ops);
    free(d.vals);
    return rc;
}
