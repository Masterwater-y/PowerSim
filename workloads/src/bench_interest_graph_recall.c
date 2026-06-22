/* bench_interest_graph_recall — 类兴趣图/关系图召回。
 * 每线程私有 CSR 图上做多跳 neighbor sampling + 轻量打分：
 *   不规则邻接访问 + 属性分支 + 局部算术更新。
 * 目标：模拟推荐召回阶段的 graph recall，而不是单纯 pointer chase。
 * 注意：scale 粒度进一步调细，保证 scale=1 的完整负载低于 500k/core。 */
#include "tao_bench.h"

static uint64_t g_sink[TAO_MAX_THREADS];

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads;
    (void)shared;

    size_t nv = (size_t)scale * 256;
    size_t avg_deg = 10;
    if (nv < 512) nv = 512;
    if (nv > (1u << 19)) nv = (1u << 19);
    size_t ne = nv * avg_deg;

    uint32_t *rowptr = (uint32_t *)tao_xaligned((nv + 1) * sizeof(uint32_t));
    uint32_t *colind = (uint32_t *)tao_xaligned(ne * sizeof(uint32_t));
    uint8_t *state = (uint8_t *)tao_xaligned(nv * sizeof(uint8_t));
    float *weight = (float *)tao_xaligned(ne * sizeof(float));
    uint64_t r = 0x517cc1b727220a95ULL ^ (uint64_t)(tid + 1) * 0x9e3779b1;

    rowptr[0] = 0;
    for (size_t v = 0; v < nv; ++v) {
        rowptr[v + 1] = rowptr[v] + (uint32_t)avg_deg;
        r = r * 6364136223846793005ULL + 1442695040888963407ULL;
        state[v] = (uint8_t)(r & 7u);
    }
    for (size_t e = 0; e < ne; ++e) {
        r = r * 2862933555777941757ULL + 3037000493ULL;
        colind[e] = (uint32_t)(r % nv);
        weight[e] = (float)((int)(r & 1023u) - 512) * (1.0f / 256.0f);
    }

    size_t walks = (size_t)scale * 32;
    if (walks < 32) walks = 32;
    float acc = (float)(tid + 3);
    uint32_t v = (uint32_t)(tid % nv);
    for (size_t it = 0; it < walks; ++it) {
        for (int hop = 0; hop < 3; ++hop) {
            uint32_t lo = rowptr[v];
            uint32_t hi = rowptr[v + 1];
            uint32_t deg = hi - lo;
            r = r * 6364136223846793005ULL + 1442695040888963407ULL;
            uint32_t off = lo + (uint32_t)(r % deg);
            uint32_t nbr = colind[off];
            float w = weight[off];
            if ((state[nbr] & 1u) != 0u) {
                acc += w * (1.0f + 0.1f * (float)(state[nbr] & 3u));
            } else {
                acc -= w * 0.35f;
            }
            if ((state[v] ^ state[nbr]) & 2u) {
                v = nbr;
            } else {
                uint32_t off2 = lo + (uint32_t)((off - lo + 1u) % deg);
                v = colind[off2];
            }
        }
        acc += (float)(v & 31u) * 0.001f;
        asm volatile("" ::: "memory");
    }

    g_sink[tid] = (uint64_t)(acc * 4096.0f) ^ v;
    free(rowptr);
    free(colind);
    free(state);
    free(weight);
}

TAO_BENCH_MAIN("interest_graph_recall", kernel, NULL)
