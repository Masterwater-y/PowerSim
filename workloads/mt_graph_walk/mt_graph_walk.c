/*
 * W13_graph_walk: irregular pointer/graph walk kernel.
 *
 * Goal: cover dependent random loads, low MLP, TLB/cache misses, and
 * data-dependent branches without putting synchronization in ROI.
 *
 * Usage: ./mt_graph_walk <threads> <iters> <size_kb> <mode> <seed>
 *   smoke: ./mt_graph_walk 4 1 64 0
 */
#include "../common/pthread_harness.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define LINE_BYTES 64
#define WALK_STEPS_BASE 8

typedef struct {
    uint32_t *next0;
    uint32_t *next1;
    uint32_t *weight;
    uint64_t acc[TAO_MAX_THREADS];
    size_t n;
} graph_data_t;

static void graph_walk_kernel(const tao_thread_ctx_t *ctx, long iter)
{
    graph_data_t *d = (graph_data_t *)ctx->user;
    const size_t n = d->n;
    uint64_t acc = d->acc[ctx->tid] + (uint64_t)iter;
    uint32_t p = (uint32_t)((ctx->lo + (size_t)iter * 131u + (size_t)ctx->tid * 17u) % n);
    const int steps = WALK_STEPS_BASE + ((ctx->cfg->mode + ctx->tid) & 7);

    for (size_t i = ctx->lo; i < ctx->hi; ++i) {
        uint32_t cur = (uint32_t)(((uint64_t)p ^ (uint64_t)i) % n);
        for (int s = 0; s < steps; ++s) {
            uint32_t w = d->weight[cur];
            acc += (uint64_t)w + (uint64_t)(cur & 15u);
            if (((w ^ cur ^ (uint32_t)s) & 7u) < 3u) {
                cur = d->next0[cur];
            } else {
                cur = d->next1[cur];
            }
        }
        p = cur;
    }
    d->acc[ctx->tid] = acc ^ p;
}

int main(int argc, char **argv)
{
    tao_harness_cfg_t cfg = {
        .name = "W13_graph_walk",
        .nthreads = 4,
        .iters = 1,
        .size_kb = 256,
        .mode = 0,
        .seed = UINT64_C(0x6139a711),
    };
    tao_parse_args(argc, argv, &cfg);

    size_t n = (cfg.size_kb * 1024) / (3 * sizeof(uint32_t));
    if (n < 512) n = 512;

    graph_data_t d = {0};
    d.n = n;
    d.next0 = (uint32_t *)tao_aligned_zalloc(LINE_BYTES, n * sizeof(uint32_t));
    d.next1 = (uint32_t *)tao_aligned_zalloc(LINE_BYTES, n * sizeof(uint32_t));
    d.weight = (uint32_t *)tao_aligned_zalloc(LINE_BYTES, n * sizeof(uint32_t));
    if (!d.next0 || !d.next1 || !d.weight) {
        fprintf(stderr, "%s: alloc failed\n", cfg.name);
        return 1;
    }

    uint64_t s = cfg.seed;
    for (size_t i = 0; i < n; ++i) {
        d.next0[i] = (uint32_t)(tao_splitmix64(&s) % n);
        d.next1[i] = (uint32_t)(tao_splitmix64(&s) % n);
        d.weight[i] = (uint32_t)tao_splitmix64(&s);
    }

    fprintf(stderr, "%s: threads=%d iters=%ld size_kb=%zu n=%zu mode=%d seed=0x%llx\n",
            cfg.name, cfg.nthreads, cfg.iters, cfg.size_kb, n, cfg.mode,
            (unsigned long long)cfg.seed);

    int rc = tao_run_threads(&cfg, n, &d, graph_walk_kernel);

    volatile uint64_t sink = 0;
    for (int i = 0; i < cfg.nthreads; ++i) sink ^= d.acc[i];
    fprintf(stderr, "%s: done sink=%llu\n", cfg.name, (unsigned long long)sink);
    free(d.next0);
    free(d.next1);
    free(d.weight);
    return rc;
}
