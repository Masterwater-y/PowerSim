/* bench_ads_ctr — 类广告 CTR/CVR 预估服务。
 * 每线程独立处理 impression stream：
 *   sparse feature hash lookup + dense cross feature + calibration / filter branch。
 * 目标：模拟广告排序路径里的混合特征，而不是单纯哈希表 microbench。
 * 注意：scale 粒度调细，保证 scale=1 的完整负载低于 500k/core。 */
#include "tao_bench.h"

static uint64_t g_sink[TAO_MAX_THREADS];

static size_t round_up_pow2(size_t x)
{
    size_t p = 1;
    while (p < x) p <<= 1;
    return p;
}

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads;
    (void)shared;

    size_t vocab_n = (size_t)scale * 1024;
    size_t req_n = (size_t)scale * 256;
    size_t slots = 14;
    if (vocab_n < 2048) vocab_n = 2048;
    if (req_n < 256) req_n = 256;
    if (vocab_n > (1u << 20)) vocab_n = (1u << 20);
    size_t hsz = round_up_pow2(vocab_n * 2);

    uint32_t *hkey = (uint32_t *)tao_xaligned(hsz * sizeof(uint32_t));
    float *hval = (float *)tao_xaligned(hsz * sizeof(float));
    uint32_t *req_ids = (uint32_t *)tao_xaligned(req_n * slots *
                                                 sizeof(uint32_t));
    float *dense = (float *)tao_xaligned(req_n * 6 * sizeof(float));
    uint64_t r = tao_seed_or(tid, 0,
        0xd1342543de82ef95ULL ^ (uint64_t)(tid + 11) * 0x94d049bb);

    for (size_t i = 0; i < hsz; ++i) hkey[i] = UINT32_MAX;
    for (size_t i = 0; i < vocab_n; ++i) {
        r = r * 6364136223846793005ULL + 1442695040888963407ULL;
        uint32_t k = (uint32_t)(r & 0x7fffffffU);
        size_t pos = ((size_t)k * 11400714819323198485ull) & (hsz - 1);
        while (hkey[pos] != UINT32_MAX) pos = (pos + 1) & (hsz - 1);
        hkey[pos] = k;
        hval[pos] = (float)((int)(r & 1023u) - 512) * (1.0f / 256.0f);
    }

    for (size_t i = 0; i < req_n * slots; ++i) {
        r = r * 2862933555777941757ULL + 3037000493ULL;
        req_ids[i] = (uint32_t)(r & 0x7fffffffU);
    }
    for (size_t i = 0; i < req_n * 6; ++i) {
        r = r * 2862933555777941757ULL + 3037000493ULL;
        dense[i] = (float)((int)(r & 255u) - 128) * (1.0f / 128.0f);
    }

    float ctr_acc = 0.1f * (float)(tid + 1);
    for (size_t rq = 0; rq < req_n; ++rq) {
        float score = 0.0f;
        const uint32_t *ids = req_ids + rq * slots;
        const float *df = dense + rq * 6;

        for (size_t s = 0; s < slots; ++s) {
            uint32_t k = ids[s];
            size_t pos = ((size_t)k * 11400714819323198485ull) & (hsz - 1);
            while (hkey[pos] != UINT32_MAX && hkey[pos] != k) {
                pos = (pos + 1) & (hsz - 1);
            }
            if (hkey[pos] == k) {
                score += hval[pos] * (0.6f + 0.02f * (float)s);
            } else {
                score -= 0.03f * (float)(s + 1);
            }
        }

        score += df[0] * df[1] * 0.8f + df[2] * 0.5f - df[3] * 0.2f
               + df[4] * df[5] * 0.6f + ctr_acc * 0.1f;

        if (score > 1.5f) {
            ctr_acc += score * 0.08f;
        } else if (score > -0.5f) {
            ctr_acc += score * 0.02f;
        } else {
            ctr_acc -= score * 0.03f;
        }
        asm volatile("" ::: "memory");
    }

    g_sink[tid] = (uint64_t)(ctr_acc * 4096.0f);
    free(hkey);
    free(hval);
    free(req_ids);
    free(dense);
}

TAO_BENCH_MAIN("ads_ctr", kernel, NULL)
