/* bench_search_index_proxy — 类搜索倒排索引检索服务（搜索/电商商品检索路径）。
 *
 * 业务参照：字节系搜索服务（头条搜索、抖音搜索、电商搜索）的核心检索路径。
 * 与 microbench 区别：完整复现"查询 → 倒排拉链合并 → BM25 打分 → topK"链路。
 *
 * 数据结构（每线程私有，没有任何共享同步）：
 *   - 词典 vocab_n：term_id ∈ [0, vocab_n)
 *   - 倒排表：每个 term 一个 posting list（docid 升序），用 varint-like 增量编码
 *     存储（每条 1-4 字节，模拟真实磁盘/内存倒排压缩）
 *   - posting_off：term -> 倒排表在 byte buffer 中的起止偏移
 *   - doc_norm：每个文档长度归一化因子（BM25 doc 部分）
 *
 * 流程：
 *   阶段 A（init，ROI 外）：随机生成 docids，编码 varint，构造倒排
 *   阶段 B（hot，ROI 内）：query loop
 *     - 取 query 的 q 个 term
 *     - 每 term：varint decode 它的 posting list，把 (docid, term_idf) 写到候选累加器
 *     - 用一个稀疏 score map（开放地址 hash, docid -> score）做合并打分
 *     - 最终结果进 topK min-heap，输出 topK 文档
 *
 * Trace 上能看到的混合特征：
 *   - varint decode：高分支 + 强数据依赖 + 短热循环（高 IPC、轻 miss）
 *   - score hash insert：开放地址 linear probe → 不规则访存、难预测分支
 *   - BM25 打分：浮点除法（idf 计算）+ 乘加（混合 div/mul/add）→ 关键 int/fp 混合
 *   - heap sift-down：难预测分支
 *
 * 旋钮：
 *   nthreads = argv[1]
 *   scale    = argv[2]       # 数据规模：n_doc / n_term / n_query
 *   roi      = argv[3]
 *   seed     = argv[4]
 *
 * 线程同步：仅启动 barrier + tao_phase_sync。每线程独立持有完整索引副本。
 */
#include "tao_bench.h"

/* log2 近似：避免链接 libm。对 trace 行为没影响，对 idf 数值精度也够用。 */
static inline float fast_log_uint(uint32_t x)
{
    if (x == 0) return 0.0f;
    int lz = __builtin_clz(x);
    int hi = 31 - lz;        /* floor(log2(x)) */
    /* 线性内插小数部分：用 x 的高位差 */
    uint32_t base = 1u << hi;
    float frac = (float)(x - base) / (float)base;
    return (float)hi * 0.69314718f + frac * 0.69314718f;
}

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

/* varint 编码：1~5 字节存 32bit 增量；返回写了多少字节。
 * 这是 elasticsearch / lucene posting list 用的真正压缩格式。 */
static inline size_t varint_encode(uint32_t v, uint8_t *buf)
{
    size_t n = 0;
    while (v >= 0x80u) {
        buf[n++] = (uint8_t)((v & 0x7fu) | 0x80u);
        v >>= 7;
    }
    buf[n++] = (uint8_t)v;
    return n;
}

/* varint 解码：从 buf 起读出一个 32bit。返回消费字节数。 */
static inline size_t varint_decode(const uint8_t *buf, uint32_t *out)
{
    uint32_t v = 0;
    size_t n = 0;
    uint32_t shift = 0;
    for (;;) {
        uint8_t b = buf[n++];
        v |= (uint32_t)(b & 0x7fu) << shift;
        if ((b & 0x80u) == 0) break;
        shift += 7;
    }
    *out = v;
    return n;
}

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads; (void)shared;

    /* 规模设定（每线程独立持有）。值都按 scale 线性放大，方便 scale 控时长 */
    size_t n_doc    = (size_t)scale * 4096;
    size_t n_term   = (size_t)scale * 512;
    size_t n_query  = (size_t)scale * 12;        /* 目标 scale=1 → ~250k/core */
    if (n_doc   < 4096) n_doc = 4096;
    if (n_term  < 512)  n_term = 512;
    if (n_query < 12)   n_query = 12;
    if (n_doc   > (1u << 18)) n_doc = (1u << 18);
    if (n_term  > (1u << 14)) n_term = (1u << 14);
    size_t avg_post = 64;            /* 平均每 term 有多少 doc */
    size_t q_terms  = 8;             /* 每 query 多少个 term */
    size_t topk     = 16;

    /* 词典 + 文档 */
    float    *idf      = (float    *)tao_xaligned(n_term * sizeof(float));
    float    *doc_norm = (float    *)tao_xaligned(n_doc * sizeof(float));
    uint32_t *post_off = (uint32_t *)tao_xaligned((n_term + 1) * sizeof(uint32_t));
    /* posting buffer 用 byte，给足 5x avg_post 防止越界 */
    size_t post_cap = n_term * avg_post * 5;
    uint8_t  *post_buf = (uint8_t  *)tao_xaligned(post_cap);

    uint32_t *qterms = (uint32_t *)tao_xaligned(n_query * q_terms * sizeof(uint32_t));

    /* 稀疏 score map：开放地址 hash，docid -> accumulated score */
    size_t map_sz = round_up_pow2(avg_post * q_terms * 4);
    if (map_sz < 256) map_sz = 256;
    uint32_t *map_key = (uint32_t *)tao_xaligned(map_sz * sizeof(uint32_t));
    float    *map_val = (float    *)tao_xaligned(map_sz * sizeof(float));

    float    *heap_s = (float    *)tao_xaligned(topk * sizeof(float));
    uint32_t *heap_i = (uint32_t *)tao_xaligned(topk * sizeof(uint32_t));

    uint64_t s = g_tao_seed ^ ((uint64_t)(tid + 13) * 0xd1342543de82ef95ULL);

    /* === init（ROI 外） === */
    for (size_t d = 0; d < n_doc; ++d) {
        uint64_t r = splitmix64(&s);
        /* doc 长度归一化 [0.5, 1.5] */
        doc_norm[d] = 0.5f + (float)(r & 1023u) * (1.0f / 1024.0f);
    }
    /* 倒排表：每个 term 随机生成升序 docid 列表，varint 增量编码 */
    size_t off = 0;
    for (size_t t = 0; t < n_term; ++t) {
        post_off[t] = (uint32_t)off;
        /* 该 term 的 posting 长度在 [avg/2, 3*avg/2) */
        uint64_t r = splitmix64(&s);
        size_t pn = avg_post / 2 + (size_t)(r % avg_post);
        /* 生成升序 docid */
        uint32_t prev = 0;
        for (size_t i = 0; i < pn; ++i) {
            r = splitmix64(&s);
            uint32_t gap = 1u + (uint32_t)(r % (n_doc / (pn + 1) + 1));
            uint32_t doc = prev + gap;
            if (doc >= n_doc) break;
            if (off + 5 > post_cap) break;
            off += varint_encode(gap, post_buf + off);
            prev = doc;
        }
        /* idf = log(N / df+1)，模拟真实 BM25 idf */
        idf[t] = fast_log_uint((uint32_t)(n_doc / (pn + 1) + 1));
    }
    post_off[n_term] = (uint32_t)off;

    for (size_t q = 0; q < n_query; ++q) {
        for (size_t j = 0; j < q_terms; ++j) {
            qterms[q * q_terms + j] = (uint32_t)(splitmix64(&s) % n_term);
        }
    }
    for (size_t i = 0; i < map_sz; ++i) map_key[i] = UINT32_MAX;

    /* === ROI 开始 === */
    tao_phase_sync();
    tao_roi_begin();

    float acc = (float)(tid + 1) * 0.001f;
    for (size_t q = 0; q < n_query; ++q) {
        /* 1) 清 score map（轻量，只清 map_sz） */
        for (size_t i = 0; i < map_sz; ++i) map_key[i] = UINT32_MAX;

        /* 2) 遍历 q_terms 个 term，合并打分 */
        const uint32_t *qt = qterms + q * q_terms;
        for (size_t j = 0; j < q_terms; ++j) {
            uint32_t t = qt[j];
            float widf = idf[t];
            uint32_t st = post_off[t], ed = post_off[t + 1];
            uint32_t cur_doc = 0;
            size_t p = st;
            while (p < ed) {
                uint32_t gap;
                p += varint_decode(post_buf + p, &gap);
                cur_doc += gap;
                if (cur_doc >= n_doc) break;
                /* BM25 简化形式：score = idf * (1 / (k1 * doc_norm + 1))
                 * 这里故意保留一个除法，让 int_div/fp_div 都有量。 */
                float dn = doc_norm[cur_doc];
                float score = widf / (1.2f * dn + 1.0f);
                /* 写入 score map（开放地址 linear probe） */
                size_t hp = ((size_t)cur_doc * 11400714819323198485ull) & (map_sz - 1);
                while (map_key[hp] != UINT32_MAX && map_key[hp] != cur_doc) {
                    hp = (hp + 1) & (map_sz - 1);
                }
                if (map_key[hp] == UINT32_MAX) {
                    map_key[hp] = cur_doc;
                    map_val[hp] = score;
                } else {
                    map_val[hp] += score;
                }
            }
        }

        /* 3) 扫一遍 score map 进 topK min-heap */
        for (size_t k = 0; k < topk; ++k) { heap_s[k] = -1e30f; heap_i[k] = UINT32_MAX; }
        for (size_t i = 0; i < map_sz; ++i) {
            if (map_key[i] == UINT32_MAX) continue;
            float sc = map_val[i];
            if (sc > heap_s[0]) {
                heap_s[0] = sc; heap_i[0] = map_key[i];
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
    free(idf); free(doc_norm); free(post_off); free(post_buf);
    free(qterms); free(map_key); free(map_val); free(heap_s); free(heap_i);
}

TAO_BENCH_MAIN_KERNEL_ROI("search_index_proxy", kernel, NULL)
