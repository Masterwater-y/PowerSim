/* bench_rank_score_filter — feed-ranking-like gather + score + filter.
 *
 * Covers feature gather, moderate FP score accumulation, threshold-heavy
 * branches, and a small top-k maintenance loop.  It is intentionally different
 * from bench_feed_ranking.c, which is kept as a validation workload.
 */
#include "tao_bench.h"

static uint64_t g_sink[TAO_MAX_THREADS] __attribute__((aligned(TAO_LINE)));

static uint64_t step_rng(uint64_t *s)
{
    *s = *s * 2862933555777941757ULL + 3037000493ULL;
    return *s;
}

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads;
    (void)shared;

    const size_t dim = 16;
    const size_t n_l2 = 16384;
    const size_t n_llc = 262144;
    float *tab_l2 = (float *)tao_xaligned(n_l2 * dim * sizeof(float));
    float *tab_llc = (float *)tao_xaligned(n_llc * dim * sizeof(float));
    uint32_t *ids = (uint32_t *)tao_xaligned(64 * sizeof(uint32_t));

    uint64_t rng = 0xbb67ae8584caa73bULL ^ ((uint64_t)tid << 40);
    for (size_t i = 0; i < n_l2 * dim; ++i) {
        uint64_t x = step_rng(&rng);
        tab_l2[i] = (float)((int)(x & 511u) - 255) * (1.0f / 128.0f);
    }
    for (size_t i = 0; i < n_llc * dim; ++i) {
        uint64_t x = step_rng(&rng);
        tab_llc[i] = (float)((int)(x & 511u) - 255) * (1.0f / 128.0f);
    }

    long groups = scale * 256;
    if (groups < 256) groups = 256;

    /* init 完成 -> 全员到齐 -> 进 ROI，开始 score+filter hot loop */
    tao_phase_sync();
    tao_roi_begin();

    float top[8];
    for (int i = 0; i < 8; ++i) top[i] = -1.0e30f;
    float carry = 0.01f * (float)(tid + 1);
    uint64_t accepted = 0;

    for (long g = 0; g < groups; ++g) {
        int use_llc = ((g + tid) % 4) != 0;
        size_t mask = use_llc ? (n_llc - 1) : (n_l2 - 1);
        float *tab = use_llc ? tab_llc : tab_l2;
        int slots = 8 + (int)((g + tid) & 7);

        for (int s = 0; s < slots; ++s) {
            ids[s] = (uint32_t)(step_rng(&rng) & mask);
        }

        for (int item = 0; item < 12; ++item) {
            float feat[4] = {carry, 0.0f, 0.0f, 0.0f};
            for (int s = 0; s < slots; ++s) {
                const float *emb = tab + ((ids[s] + (uint32_t)item * 17u) & mask) * dim;
                feat[0] += emb[0] * 0.35f + emb[3] * 0.12f;
                feat[1] += emb[5] * 0.23f - emb[7] * 0.08f;
                feat[2] += emb[9] * 0.19f + emb[11] * 0.05f;
                feat[3] += emb[13] * 0.31f - emb[15] * 0.11f;
            }

            float score = feat[0] * 0.7f + feat[1] * 0.4f
                        + feat[2] * feat[3] * 0.03f + carry;
            if (score > 2.5f) {
                score += feat[2] * 0.15f;
                accepted++;
            } else if (score > -1.0f) {
                score += feat[1] * 0.05f;
            } else {
                score -= feat[0] * 0.04f;
            }

            int k = 7;
            if (score > top[k]) {
                while (k > 0 && score > top[k - 1]) {
                    top[k] = top[k - 1];
                    k--;
                }
                top[k] = score;
            }
            carry = carry * 0.997f + score * 0.003f;
            asm volatile("" ::: "memory");
        }
    }

    uint64_t out = accepted;
    for (int i = 0; i < 8; ++i) out ^= (uint64_t)(top[i] * 4096.0f) << (i & 7);

    tao_roi_end();

    g_sink[tid] = out;
    free(tab_l2);
    free(tab_llc);
    free(ids);
}

TAO_BENCH_MAIN_KERNEL_ROI("rank_score_filter", kernel, NULL)
