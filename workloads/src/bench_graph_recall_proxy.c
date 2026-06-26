/* bench_graph_recall_proxy — 类兴趣图召回服务（短视频/电商 user-item 异构图召回）。
 *
 * 业务参照：字节系兴趣图召回链路（短视频/抖音/电商 推荐召回阶段）。
 * 与现有 bench_interest_graph_recall 的区别：
 *   1) 加入 thread-local visited-hash 去重（真实召回必须去重）
 *   2) 加入 topK min-heap，按 embedding dot score 取分；
 *   3) 多跳采样从 user 出发，按 degree-aware 概率游走；
 *   4) 加入 query 数量旋钮，按 scale 线性增长，控制 trace 长度。
 *
 * 流程：
 *   阶段 A：CSR 图 init（rowptr/colind/weight）+ item embedding 表 init（ROI 外）
 *   阶段 B（hot）：每个 query
 *     - 从 root user 出发 K-hop sampling（hop=2/3，每跳采 fanout 个邻居）
 *     - 邻居通过 thread-local 开放地址 hash 去重
 *     - 对去重后的候选做 embedding · query 点积打分
 *     - 进 topK min-heap，最后输出 topK
 *
 * Trace 上能看到的混合特征：
 *   - 邻接表跳转 → 高 L1D miss / dtlb miss / pointer-chase 风格
 *   - 开放地址 hash 探测 → 不规则访存 + 难预测分支（探测长度变化）
 *   - 点积打分 → 短热 dense 循环，IPC 高
 *   - heap sift-down → 难预测分支
 *
 * 旋钮：
 *   nthreads = argv[1]
 *   scale    = argv[2]    # 控制 nv / queries / hops 数据规模
 *   roi      = argv[3]
 *   seed     = argv[4]
 *
 * 线程同步：仅启动 barrier + tao_phase_sync；每线程私有 CSR 副本（不共享、不同步）。
 */
#include "tao_bench.h"

static uint64_t g_sink[TAO_MAX_THREADS];

static size_t round_up_pow2(size_t x)
{
    size_t p = 1;
    while (p < x) p <<= 1;
    return p;
}

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

    size_t nv = (size_t)scale * 1024;
    if (nv < 4096) nv = 4096;
    if (nv > (1u << 19)) nv = (1u << 19);
    size_t avg_deg = 12;
    /* 每顶点最大度 ≈ 1.5*avg_deg，留 2x 余量保证 prefix sum 不被截断 */
    size_t ne = nv * (avg_deg * 2);

    size_t dim = 16;
    size_t n_query = (size_t)scale * 12;        /* 目标 scale=1 → ~250k/core */
    if (n_query < 12) n_query = 12;
    size_t hops = 3;
    size_t fanout = 8;
    size_t topk = 16;
    size_t cand_cap = hops * fanout * fanout;       /* 每 query 最多候选 */
    if (cand_cap < 64) cand_cap = 64;
    size_t hsz = round_up_pow2(cand_cap * 4);       /* visited hash */

    uint32_t *rowptr = (uint32_t *)tao_xaligned((nv + 1) * sizeof(uint32_t));
    uint32_t *colind = (uint32_t *)tao_xaligned(ne * sizeof(uint32_t));
    float    *ewght  = (float    *)tao_xaligned(ne * sizeof(float));
    uint8_t  *vstate = (uint8_t  *)tao_xaligned(nv * sizeof(uint8_t));
    float    *vemb   = (float    *)tao_xaligned(nv * dim * sizeof(float));
    uint32_t *roots  = (uint32_t *)tao_xaligned(n_query * sizeof(uint32_t));
    float    *qvec   = (float    *)tao_xaligned(n_query * dim * sizeof(float));

    /* 候选 buffer + visited hash + topK heap，每线程一份 */
    uint32_t *cand_buf = (uint32_t *)tao_xaligned(cand_cap * sizeof(uint32_t));
    uint32_t *vis      = (uint32_t *)tao_xaligned(hsz * sizeof(uint32_t));
    float    *heap_s   = (float    *)tao_xaligned(topk * sizeof(float));
    uint32_t *heap_i   = (uint32_t *)tao_xaligned(topk * sizeof(uint32_t));

    uint64_t s = g_tao_seed ^ ((uint64_t)(tid + 7) * 0x517cc1b727220a95ULL);

    /* === init（ROI 外） === */
    /* 1) 先随机分配每个顶点的 degree（[avg/2, 3*avg/2)），prefix sum 得到 rowptr */
    uint32_t *deg_arr = (uint32_t *)tao_xaligned(nv * sizeof(uint32_t));
    for (size_t v = 0; v < nv; ++v) {
        uint64_t rr = splitmix64(&s);
        size_t d = avg_deg / 2 + (size_t)(rr % avg_deg);
        deg_arr[v] = (uint32_t)d;
        vstate[v] = (uint8_t)(rr & 0xffu);
    }
    rowptr[0] = 0;
    for (size_t v = 0; v < nv; ++v) {
        uint64_t next = (uint64_t)rowptr[v] + (uint64_t)deg_arr[v];
        if (next > ne) next = ne;
        rowptr[v + 1] = (uint32_t)next;
    }
    free(deg_arr);
    /* 2) 填 colind / weight，按实际 rowptr[nv] 上界 */
    size_t real_ne = rowptr[nv];
    for (size_t e = 0; e < real_ne; ++e) {
        colind[e] = (uint32_t)(splitmix64(&s) % nv);
        ewght[e]  = (float)((int)(splitmix64(&s) & 1023u) - 512) * (1.0f / 256.0f);
    }
    for (size_t i = 0; i < nv * dim; ++i) {
        vemb[i] = (float)((int)(splitmix64(&s) & 1023u) - 512) * (1.0f / 256.0f);
    }
    for (size_t q = 0; q < n_query; ++q) {
        roots[q] = (uint32_t)(splitmix64(&s) % nv);
    }
    for (size_t i = 0; i < n_query * dim; ++i) {
        qvec[i] = (float)((int)(splitmix64(&s) & 1023u) - 512) * (1.0f / 256.0f);
    }
    for (size_t i = 0; i < hsz; ++i) vis[i] = UINT32_MAX;

    /* === ROI 开始 === */
    tao_phase_sync();
    tao_roi_begin();

    uint64_t r = s | 1ull;
    float acc = (float)(tid + 1) * 0.001f;
    for (size_t q = 0; q < n_query; ++q) {
        /* 清 visited hash（只清浅区，按 cand_cap 设定，避免 O(hsz)） */
        for (size_t i = 0; i < hsz; ++i) vis[i] = UINT32_MAX;
        size_t ncand = 0;
        uint32_t frontier[16];
        size_t fn = 1;
        frontier[0] = roots[q];

        for (size_t h = 0; h < hops && fn > 0 && ncand < cand_cap; ++h) {
            size_t next_fn = 0;
            uint32_t next_front[16];
            for (size_t fi = 0; fi < fn && ncand < cand_cap; ++fi) {
                uint32_t v = frontier[fi];
                uint32_t lo = rowptr[v];
                uint32_t hi = rowptr[v + 1];
                uint32_t deg = (hi > lo) ? (hi - lo) : 1u;
                /* 采 fanout 个邻居 */
                for (size_t k = 0; k < fanout && ncand < cand_cap; ++k) {
                    r = r * 6364136223846793005ULL + 1442695040888963407ULL;
                    uint32_t off = lo + (uint32_t)(r % deg);
                    uint32_t nb = colind[off];
                    /* visited hash（开放地址 linear probe） */
                    size_t hp = ((size_t)nb * 11400714819323198485ull) & (hsz - 1);
                    int dup = 0;
                    while (vis[hp] != UINT32_MAX) {
                        if (vis[hp] == nb) { dup = 1; break; }
                        hp = (hp + 1) & (hsz - 1);
                    }
                    if (dup) continue;
                    vis[hp] = nb;
                    cand_buf[ncand++] = nb;
                    if (next_fn < 16 && (vstate[nb] & 1u)) {
                        next_front[next_fn++] = nb;
                    }
                }
            }
            fn = next_fn;
            for (size_t i = 0; i < fn; ++i) frontier[i] = next_front[i];
        }

        /* 打分 + topK min-heap */
        for (size_t k = 0; k < topk; ++k) { heap_s[k] = -1e30f; heap_i[k] = UINT32_MAX; }
        const float *qv = qvec + q * dim;
        for (size_t c = 0; c < ncand; ++c) {
            uint32_t nb = cand_buf[c];
            const float *ev = vemb + (size_t)nb * dim;
            float score = 0.0f;
            for (size_t d = 0; d < dim; ++d) score += qv[d] * ev[d];
            if ((vstate[nb] & 2u) == 0) score -= 0.2f;
            if (score > heap_s[0]) {
                heap_s[0] = score; heap_i[0] = nb;
                size_t p = 0;
                for (;;) {
                    size_t l = 2 * p + 1, r2 = 2 * p + 2, m = p;
                    if (l < topk && heap_s[l] < heap_s[m]) m = l;
                    if (r2 < topk && heap_s[r2] < heap_s[m]) m = r2;
                    if (m == p) break;
                    float ts = heap_s[p]; heap_s[p] = heap_s[m]; heap_s[m] = ts;
                    uint32_t ti = heap_i[p]; heap_i[p] = heap_i[m]; heap_i[m] = ti;
                    p = m;
                }
            }
        }
        for (size_t k = 0; k < topk; ++k) acc += heap_s[k] * 0.0005f;
        asm volatile("" : "+r"(acc) :: "memory");
    }

    tao_roi_end();
    /* === ROI 结束 === */

    g_sink[tid] = (uint64_t)(acc * 4096.0f);
    free(rowptr); free(colind); free(ewght); free(vstate); free(vemb);
    free(roots); free(qvec); free(cand_buf); free(vis); free(heap_s); free(heap_i);
}

TAO_BENCH_MAIN_KERNEL_ROI("graph_recall_proxy", kernel, NULL)
