/* bench_feed_ranking — 类抖音/信息流推荐排序服务。
 * 每线程独立执行 candidate ranking：
 *   sparse id embedding gather + dense feature interaction + rerank branch。
 * 目标：接近 feed ranking 在线服务的混合访存/算术/控制流特征。
 * 注意：这里把 scale 粒度进一步调细，让 scale=1 的完整负载低于 500k/core，
 * 便于采集脚本用整数 scale 逼近目标 trace 体量。 */
#include "tao_bench.h"

static uint64_t g_sink[TAO_MAX_THREADS];

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads;
    (void)shared;

    size_t dim = 32;
    size_t slots_per_item = 12;
    size_t n_embed = (size_t)scale * 256;
    size_t n_req = (size_t)scale * 16;
    if (n_embed < 512) n_embed = 512;
    if (n_req < 16) n_req = 16;
    if (n_embed > (1u << 20)) n_embed = (1u << 20);

    float *table = (float *)tao_xaligned(n_embed * dim * sizeof(float));
    uint32_t *req_ids = (uint32_t *)tao_xaligned(n_req * slots_per_item *
                                                 sizeof(uint32_t));
    uint64_t r = tao_seed_or(tid, 0,
        0x9e3779b97f4a7c15ULL ^ (uint64_t)(tid + 1) * 0x94d049bb);

    for (size_t i = 0; i < n_embed * dim; ++i) {
        r = r * 6364136223846793005ULL + 1442695040888963407ULL;
        table[i] = (float)((int)(r & 255) - 128) * (1.0f / 128.0f);
    }
    for (size_t i = 0; i < n_req * slots_per_item; ++i) {
        r = r * 2862933555777941757ULL + 3037000493ULL;
        req_ids[i] = (uint32_t)(r % n_embed);
    }

    float score_acc = (float)(tid + 1);
    for (size_t rq = 0; rq < n_req; ++rq) {
        float feat[8] = {0};
        const uint32_t *ids = req_ids + rq * slots_per_item;
        for (size_t s = 0; s < slots_per_item; ++s) {
            const float *emb = table + (size_t)ids[s] * dim;
            for (size_t d = 0; d < dim; d += 4) {
                feat[(d >> 2) & 7] += emb[d] * 0.7f + emb[d + 1] * 0.2f
                                    - emb[d + 2] * 0.1f + emb[d + 3] * 0.4f;
            }
        }

        float score = 0.0f;
        for (int k = 0; k < 8; ++k) {
            float x = feat[k] + score_acc * (0.01f * (float)(k + 1));
            if (x > 0.0f) {
                score += x * (0.6f + 0.03f * (float)k);
            } else {
                score -= x * (0.15f + 0.01f * (float)k);
            }
        }
        if ((score > 6.0f && (rq & 3u) != 0u) || ((rq + tid) & 7u) == 0u) {
            score_acc += score * 0.25f;
        } else {
            score_acc -= score * 0.05f;
        }
        asm volatile("" ::: "memory");
    }

    g_sink[tid] = (uint64_t)(score_acc * 1000.0f);
    free(table);
    free(req_ids);
}

TAO_BENCH_MAIN("feed_ranking", kernel, NULL)
