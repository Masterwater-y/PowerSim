/*
 * W14_branch_state_machine: parser-like branchy state machine.
 *
 * Goal: create diverse conditional branches and state transitions with
 * controllable data distribution. ROI contains no synchronization.
 *
 * Usage: ./mt_branch_state_machine <threads> <iters> <size_kb> <mode> <seed>
 *   smoke: ./mt_branch_state_machine 4 1 64 0
 */
#include "../common/pthread_harness.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define LINE_BYTES 64

typedef struct {
    uint8_t *stream;
    uint32_t hist[TAO_MAX_THREADS][8];
    uint64_t acc[TAO_MAX_THREADS];
    size_t n;
} sm_data_t;

static void state_machine_kernel(const tao_thread_ctx_t *ctx, long iter)
{
    sm_data_t *d = (sm_data_t *)ctx->user;
    uint32_t state = (uint32_t)((ctx->tid + iter + ctx->cfg->mode) & 7);
    uint64_t acc = d->acc[ctx->tid];
    uint32_t *hist = d->hist[ctx->tid];

    for (size_t i = ctx->lo; i < ctx->hi; ++i) {
        uint8_t x = d->stream[i];
        if (state == 0) {
            if (x < 32) { state = 1; acc += x; }
            else if (x < 96) { state = 2; acc ^= (uint64_t)x << 1; }
            else { state = 3; acc += (uint64_t)x * 3u; }
        } else if (state == 1) {
            if ((x & 1u) == 0) { state = 4; acc += hist[x & 7u]; }
            else { state = 0; acc ^= x; }
        } else if (state == 2) {
            if ((x ^ (uint8_t)i) & 0x20u) { state = 5; acc += x + i; }
            else { state = 6; acc -= x; }
        } else if (state == 3) {
            if (x > 220) { state = 7; acc += (uint64_t)x * x; }
            else if (x > 128) { state = 2; acc += x; }
            else { state = 1; acc ^= x; }
        } else if (state == 4) {
            state = (x & 4u) ? 3u : 5u;
            acc += (uint64_t)(x & 15u);
        } else if (state == 5) {
            state = ((x + (uint8_t)acc) & 3u) ? 6u : 0u;
            acc = (acc << 3) ^ (acc >> 2) ^ x;
        } else if (state == 6) {
            state = (x < 180) ? 2u : 7u;
            acc += (uint64_t)(x | 1u);
        } else {
            state = (x == 0 || x == 255) ? 0u : (uint32_t)(x & 7u);
            acc ^= (uint64_t)x * 0x9e37u;
        }
        hist[state & 7u]++;
    }
    d->acc[ctx->tid] = acc + state;
}

int main(int argc, char **argv)
{
    tao_harness_cfg_t cfg = {
        .name = "W14_branch_state_machine",
        .nthreads = 4,
        .iters = 2,
        .size_kb = 256,
        .mode = 0,
        .seed = UINT64_C(0xb2a4c001),
    };
    tao_parse_args(argc, argv, &cfg);

    size_t n = cfg.size_kb * 1024;
    if (n < 4096) n = 4096;

    sm_data_t d = {0};
    d.n = n;
    d.stream = (uint8_t *)tao_aligned_zalloc(LINE_BYTES, n);
    if (!d.stream) {
        fprintf(stderr, "%s: alloc failed\n", cfg.name);
        return 1;
    }

    uint64_t s = cfg.seed;
    for (size_t i = 0; i < n; ++i) {
        uint64_t r = tao_splitmix64(&s);
        if (cfg.mode == 1) {
            d.stream[i] = (uint8_t)((r & 7u) == 0 ? (r >> 8) : (32 + (r % 96)));
        } else if (cfg.mode == 2) {
            d.stream[i] = (uint8_t)((i * 17u + (r & 31u)) & 255u);
        } else {
            d.stream[i] = (uint8_t)r;
        }
    }

    fprintf(stderr, "%s: threads=%d iters=%ld size_kb=%zu n=%zu mode=%d seed=0x%llx\n",
            cfg.name, cfg.nthreads, cfg.iters, cfg.size_kb, n, cfg.mode,
            (unsigned long long)cfg.seed);

    int rc = tao_run_threads(&cfg, n, &d, state_machine_kernel);

    volatile uint64_t sink = 0;
    for (int i = 0; i < cfg.nthreads; ++i) sink += d.acc[i] + d.hist[i][0];
    fprintf(stderr, "%s: done sink=%llu\n", cfg.name, (unsigned long long)sink);
    free(d.stream);
    return rc;
}
