/*
 * H02_sharded_kv: holdout KV/index style workload.
 *
 * Each thread mostly works on its shard, but also probes shared hot metadata
 * and neighbor shard headers.  There are no locks or atomics in ROI; ordinary
 * shared loads/stores create coherence traffic without explicit synchronization.
 *
 * Usage:
 *   ./holdout_sharded_kv <threads> <iters> <size_kb> <mode> <seed>
 */
#include "../common/pthread_harness.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define LINE_BYTES 64
#define HOT_META 4096
#define MAX_SHARDS TAO_MAX_THREADS

typedef struct {
    uint64_t key;
    uint64_t val;
    uint32_t next;
    uint32_t tag;
} kv_node_t;

typedef struct {
    kv_node_t *nodes;
    uint32_t *heads;
    volatile uint64_t *meta;
    volatile uint64_t *shard_state;
    size_t nodes_per_shard;
    size_t total_nodes;
} h02_data_t;

static inline uint64_t rotl64(uint64_t x, unsigned r)
{
    return (x << r) | (x >> (64u - r));
}

static inline uint64_t h02_hash(uint64_t x)
{
    x ^= x >> 30;
    x *= UINT64_C(0xbf58476d1ce4e5b9);
    x ^= x >> 27;
    x *= UINT64_C(0x94d049bb133111eb);
    return x ^ (x >> 31);
}

static void h02_kernel(const tao_thread_ctx_t *ctx, long iter)
{
    h02_data_t *d = (h02_data_t *)ctx->user;
    const size_t shard = (size_t)ctx->tid;
    const size_t base = shard * d->nodes_per_shard;
    const size_t limit = base + d->nodes_per_shard;
    const size_t span = limit - base;
    uint64_t state = UINT64_C(0xd1b54a32d192ed03) ^ ((uint64_t)iter << 17) ^ shard;

    for (size_t q = 0; q < span; ++q) {
        state = h02_hash(state + q + (uint64_t)ctx->cfg->mode);
        uint32_t bucket = (uint32_t)(state & 255u);
        size_t pos = base + (d->heads[shard * 256u + bucket] % (uint32_t)span);
        uint64_t wanted = state ^ d->meta[(state >> 8) & (HOT_META - 1)];
        uint64_t acc = wanted;

        for (int step = 0; step < 4; ++step) {
            kv_node_t *node = &d->nodes[pos];
            uint64_t k = node->key;
            if (((k ^ wanted) & 15u) == 0u) {
                node->val = rotl64(node->val + acc + (uint64_t)step, 7);
                acc += node->val;
            } else {
                acc ^= rotl64(k + node->val + d->meta[(k >> 12) & (HOT_META - 1)], 13);
            }
            pos = base + (node->next % (uint32_t)span);
        }

        if ((q & 15u) == 0u) {
            size_t peer = (shard + 1u) % (size_t)ctx->cfg->nthreads;
            uint64_t peer_state = d->shard_state[peer * 8u];
            d->shard_state[shard * 8u] = acc + peer_state;
        }
        if ((q & 63u) == 9u) {
            d->meta[(q + shard * 257u) & (HOT_META - 1)] = acc;
        }
    }
}

int main(int argc, char **argv)
{
    tao_harness_cfg_t cfg = {
        .name = "H02_sharded_kv",
        .nthreads = 4,
        .iters = 2,
        .size_kb = 256,
        .mode = 2,
        .seed = UINT64_C(0x48202d02),
    };
    tao_parse_args(argc, argv, &cfg);

    size_t total_nodes = (cfg.size_kb * 1024) / sizeof(kv_node_t);
    if (total_nodes < (size_t)cfg.nthreads * 2048u) {
        total_nodes = (size_t)cfg.nthreads * 2048u;
    }
    total_nodes = (total_nodes / (size_t)cfg.nthreads) * (size_t)cfg.nthreads;

    h02_data_t d;
    d.total_nodes = total_nodes;
    d.nodes_per_shard = total_nodes / (size_t)cfg.nthreads;
    d.nodes = (kv_node_t *)tao_aligned_zalloc(LINE_BYTES, total_nodes * sizeof(kv_node_t));
    d.heads = (uint32_t *)tao_aligned_zalloc(LINE_BYTES, (size_t)cfg.nthreads * 256u * sizeof(uint32_t));
    d.meta = (volatile uint64_t *)tao_aligned_zalloc(LINE_BYTES, HOT_META * sizeof(uint64_t));
    d.shard_state = (volatile uint64_t *)tao_aligned_zalloc(LINE_BYTES, MAX_SHARDS * 8u * sizeof(uint64_t));
    if (!d.nodes || !d.heads || !d.meta || !d.shard_state) {
        fprintf(stderr, "%s: alloc failed\n", cfg.name);
        return 1;
    }

    uint64_t s = cfg.seed;
    for (size_t i = 0; i < total_nodes; ++i) {
        d.nodes[i].key = tao_splitmix64(&s);
        d.nodes[i].val = tao_splitmix64(&s);
        d.nodes[i].next = (uint32_t)(tao_splitmix64(&s) % d.nodes_per_shard);
        d.nodes[i].tag = (uint32_t)tao_splitmix64(&s);
    }
    for (int t = 0; t < cfg.nthreads; ++t) {
        for (size_t b = 0; b < 256u; ++b) {
            d.heads[(size_t)t * 256u + b] = (uint32_t)(tao_splitmix64(&s) % d.nodes_per_shard);
        }
    }
    for (size_t i = 0; i < HOT_META; ++i) d.meta[i] = tao_splitmix64(&s);
    for (size_t i = 0; i < MAX_SHARDS * 8u; ++i) d.shard_state[i] = tao_splitmix64(&s);

    fprintf(stderr, "%s: threads=%d iters=%ld size_kb=%zu nodes=%zu mode=%d seed=0x%llx\n",
            cfg.name, cfg.nthreads, cfg.iters, cfg.size_kb, total_nodes, cfg.mode,
            (unsigned long long)cfg.seed);

    int rc = tao_run_threads(&cfg, total_nodes, &d, h02_kernel);

    volatile uint64_t sink = d.nodes[0].val ^ d.nodes[total_nodes / 2].key ^ d.meta[3] ^ d.shard_state[0];
    fprintf(stderr, "%s: done sink=%llu\n", cfg.name, (unsigned long long)sink);
    free(d.nodes);
    free(d.heads);
    free((void *)d.meta);
    free((void *)d.shard_state);
    return rc;
}
