/* bench_ads_ranking_proxy — 类广告精排在线服务（CTR/CVR 模型上线路径）。
 *
 * 业务参照：字节系广告精排服务（广告 ranking / 精排链路）。
 * 与现有 bench_ads_ctr 的区别：这里完整复现"线上精排"那条链路，而不是单表查找：
 *   阶段 A：multi-table sparse embedding gather（user / item / ctx 3 张表）
 *   阶段 B：feature crossing（sparse 拼接 + dense 特征点乘）
 *   阶段 C：小 MLP（2 层：32->16->1，主体是循环展开向量化乘加）
 *   阶段 D：候选打分进 topK min-heap（受用户 topK 控制，输出广告排序）
 *
 * Trace 上能看到的混合特征：
 *   - 阶段 A：随机 gather + hash 探测 → 高 L1D miss / MSHR / mr_llc
 *   - 阶段 B：dense 顺序乘加 → 高 IPC、低 miss
 *   - 阶段 C：MLP fused-multiply-add → branch low、cpi 低
 *   - 阶段 D：heap sift-down → 难预测分支 + 偶发 cache miss
 *
 * 旋钮：
 *   nthreads = argv[1]                     # 真实服务 CPU 并发度
 *   scale    = argv[2]                     # 数据规模（vocab/req 线性增长）
 *   roi      = argv[3]                     # gem5 ROI 开关，物理机默认 0
 *   seed     = argv[4]（g_tao_seed）        # 生成相似但不完全相同的 trace
 *
 * 线程同步：仅依赖 tao_bench.h 的启动 barrier 与 tao_phase_sync；
 *           各 worker 内部完全独立，没有 mutex/atomic，符合"不涉及调度"的约束。
 */
#include "tao_bench.h"

static uint64_t g_sink[TAO_MAX_THREADS];

static inline uint64_t splitmix64(uint64_t *s)
{
    uint64_t z = (*s += 0x9e3779b97f4a7c15ULL);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;

    /* 规模：三张 embedding table（user/item/ctx），每张 vocab 与 dim 不同，
     * 模拟真实精排 user/item/ctx 三类特征的取值空间差异。 */
    size_t dim_u = 32, dim_i = 32, dim_c = 16;
    size_t vocab_u = (size_t)scale * 512;
    size_t vocab_i = (size_t)scale * 4096;     /* item 字典更大 */
    size_t vocab_c = (size_t)scale * 64;       /* ctx 字典较小 */
    if (vocab_u < 1024) vocab_u = 1024;
    if (vocab_i < 4096) vocab_i = 4096;
    if (vocab_c < 256)  vocab_c = 256;
    if (vocab_u > (1u << 18)) vocab_u = (1u << 18);
    if (vocab_i > (1u << 20)) vocab_i = (1u << 20);
    if (vocab_c > (1u << 14)) vocab_c = (1u << 14);

    size_t n_req = (size_t)scale * 2;           /* request 数量；scale=1 → ~300k/core，可调 scale 2/3 覆盖 1M */
    if (n_req < 2) n_req = 2;
    size_t candidates = 8;                      /* 每 request 候选广告数（hot loop 内层） */
    size_t topk = 8;
    size_t fanout_u = 4, fanout_i = 8, fanout_c = 3;

    /* 候选广告 id 表（item vocab 子集） + dense 特征 */
    float *Tu = (float *)tao_xaligned(vocab_u * dim_u * sizeof(float));
    float *Ti = (float *)tao_xaligned(vocab_i * dim_i * sizeof(float));
    float *Tc = (float *)tao_xaligned(vocab_c * dim_c * sizeof(float));
    uint32_t *req_u = (uint32_t *)tao_xaligned(n_req * fanout_u * sizeof(uint32_t));
    uint32_t *req_c = (uint32_t *)tao_xaligned(n_req * fanout_c * sizeof(uint32_t));
    uint32_t *cand_i = (uint32_t *)tao_xaligned(n_req * candidates * fanout_i *
                                                sizeof(uint32_t));
    float *dense = (float *)tao_xaligned(n_req * 8 * sizeof(float));

    /* MLP 权重（两层）：32->16, 16->1，所有线程独立持有（精排服务每核常驻一份） */
    float *W1 = (float *)tao_xaligned(32 * 16 * sizeof(float));
    float *W2 = (float *)tao_xaligned(16 * sizeof(float));
    float *heap_score = (float *)tao_xaligned(topk * sizeof(float));
    uint32_t *heap_idx = (uint32_t *)tao_xaligned(topk * sizeof(uint32_t));

    uint64_t s = g_tao_seed ^ ((uint64_t)(tid + 1) * 0xa5a5a5a5d1342543ULL);

    /* 初始化（在 ROI 外） */
    for (size_t i = 0; i < vocab_u * dim_u; ++i) {
        Tu[i] = (float)((int)(splitmix64(&s) & 1023u) - 512) * (1.0f / 256.0f);
    }
    for (size_t i = 0; i < vocab_i * dim_i; ++i) {
        Ti[i] = (float)((int)(splitmix64(&s) & 1023u) - 512) * (1.0f / 256.0f);
    }
    for (size_t i = 0; i < vocab_c * dim_c; ++i) {
        Tc[i] = (float)((int)(splitmix64(&s) & 1023u) - 512) * (1.0f / 256.0f);
    }
    for (size_t i = 0; i < n_req * fanout_u; ++i) {
        req_u[i] = (uint32_t)(splitmix64(&s) % vocab_u);
    }
    for (size_t i = 0; i < n_req * fanout_c; ++i) {
        req_c[i] = (uint32_t)(splitmix64(&s) % vocab_c);
    }
    for (size_t i = 0; i < n_req * candidates * fanout_i; ++i) {
        cand_i[i] = (uint32_t)(splitmix64(&s) % vocab_i);
    }
    for (size_t i = 0; i < n_req * 8; ++i) {
        dense[i] = (float)((int)(splitmix64(&s) & 255u) - 128) * (1.0f / 128.0f);
    }
    for (int i = 0; i < 32 * 16; ++i)
        W1[i] = (float)((int)(splitmix64(&s) & 255u) - 128) * (1.0f / 128.0f);
    for (int i = 0; i < 16; ++i)
        W2[i] = (float)((int)(splitmix64(&s) & 255u) - 128) * (1.0f / 128.0f);

    /* === ROI 开始 === */
    tao_phase_sync();
    tao_roi_begin();

    float final_acc = (float)(tid + 1) * 0.01f;
    for (size_t rq = 0; rq < n_req; ++rq) {
        /* user / ctx pooled embedding（fanout sum） */
        float u_pool[32]; for (int d = 0; d < 32; ++d) u_pool[d] = 0.0f;
        float c_pool[16]; for (int d = 0; d < 16; ++d) c_pool[d] = 0.0f;
        const uint32_t *uids = req_u + rq * fanout_u;
        const uint32_t *cids = req_c + rq * fanout_c;
        for (size_t k = 0; k < fanout_u; ++k) {
            const float *e = Tu + (size_t)uids[k] * dim_u;
            for (int d = 0; d < 32; ++d) u_pool[d] += e[d];
        }
        for (size_t k = 0; k < fanout_c; ++k) {
            const float *e = Tc + (size_t)cids[k] * dim_c;
            for (int d = 0; d < 16; ++d) c_pool[d] += e[d];
        }
        const float *df = dense + rq * 8;

        /* 初始化 topK heap（min-heap，score 最小的在堆顶） */
        for (size_t k = 0; k < topk; ++k) {
            heap_score[k] = -1e30f; heap_idx[k] = UINT32_MAX;
        }

        const uint32_t *base = cand_i + rq * candidates * fanout_i;
        for (size_t c = 0; c < candidates; ++c) {
            /* item pooled embedding */
            float i_pool[32]; for (int d = 0; d < 32; ++d) i_pool[d] = 0.0f;
            const uint32_t *iids = base + c * fanout_i;
            for (size_t k = 0; k < fanout_i; ++k) {
                const float *e = Ti + (size_t)iids[k] * dim_i;
                for (int d = 0; d < 32; ++d) i_pool[d] += e[d];
            }
            /* feature crossing：concat (user pooled * item pooled) + ctx */
            float x[32];
            for (int d = 0; d < 32; ++d) {
                x[d] = u_pool[d] * i_pool[d] + ((d < 16) ? c_pool[d] : df[d & 7]);
            }
            /* MLP layer1: 32->16 + relu */
            float h[16];
            for (int o = 0; o < 16; ++o) {
                float acc = 0.0f;
                const float *w = W1 + o * 32;
                for (int d = 0; d < 32; ++d) acc += x[d] * w[d];
                h[o] = (acc > 0.0f) ? acc : 0.0f;
            }
            /* MLP layer2: 16->1 */
            float score = 0.0f;
            for (int o = 0; o < 16; ++o) score += h[o] * W2[o];
            score += df[0] * df[1] * 0.3f - df[2] * 0.2f;

            /* 进 topK min-heap：score 大于堆顶才换入并 sift-down */
            if (score > heap_score[0]) {
                heap_score[0] = score; heap_idx[0] = (uint32_t)c;
                /* sift-down */
                size_t p = 0;
                for (;;) {
                    size_t l = 2 * p + 1, r2 = 2 * p + 2, m = p;
                    if (l < topk && heap_score[l] < heap_score[m]) m = l;
                    if (r2 < topk && heap_score[r2] < heap_score[m]) m = r2;
                    if (m == p) break;
                    float ts = heap_score[p]; heap_score[p] = heap_score[m]; heap_score[m] = ts;
                    uint32_t ti = heap_idx[p]; heap_idx[p] = heap_idx[m]; heap_idx[m] = ti;
                    p = m;
                }
            }
        }
        for (size_t k = 0; k < topk; ++k) final_acc += heap_score[k] * 0.001f;
        asm volatile("" : "+r"(final_acc) :: "memory");
    }

    tao_roi_end();
    /* === ROI 结束 === */

    g_sink[tid] = (uint64_t)(final_acc * 4096.0f);
    free(Tu); free(Ti); free(Tc);
    free(req_u); free(req_c); free(cand_i); free(dense);
    free(W1); free(W2); free(heap_score); free(heap_idx);
}

TAO_BENCH_MAIN_KERNEL_ROI("ads_ranking_proxy", kernel, NULL)
