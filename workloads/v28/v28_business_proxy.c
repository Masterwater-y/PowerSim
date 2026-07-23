/*
 * v28 business-oriented functional workloads for TCSim.
 *
 * ABI: ./workload <nthreads> <scale> 1 <seed>
 *
 * Allocation, first touch, thread creation, pinning, and the only barrier are
 * outside the measured ROI.  The ROI deliberately contains no atomics,
 * locks, futexes, barriers, yields, sleeps, or explicit thread scheduling.
 * Business proxies use a large immutable shared model/table with deterministic
 * Zipf reads.  Request state and outputs are small, per-core and cache-line
 * isolated; the measured ROI never writes the shared model/table.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <xmmintrin.h>
#include <emmintrin.h>

#define V28_INT_ALU_DENSE             1
#define V28_INT_DIV_SERIAL            2
#define V28_FP_ALU_DENSE              3
#define V28_SIMD_SSE_DENSE            4
#define V28_CACHE_L1_MIXED            5
#define V28_CACHE_L2_MIXED            6
#define V28_MEMORY_SEQ_MODERATE       7
#define V28_MEMORY_RANDOM_MLP         8
#define V28_COH_READMOSTLY_SPARSE     9
#define V28_MARINE_BASE              10
#define V28_GOFEED_BASE              11
#define V28_FLINK_BASE               12
#define V28_MYSQL_BASE               13
#define V28_REDIS_BASE               14
#define V28_PYTORCH_BASE             15
#define V28_BVC_ENCODER_BASE         16
#define V28_MARINE_HELDOUT           17
#define V28_GOFEED_HELDOUT           18
#define V28_FLINK_HELDOUT            19
#define V28_MYSQL_HELDOUT            20
#define V28_REDIS_HELDOUT            21
#define V28_PYTORCH_HELDOUT          22
#define V28_BVC_ENCODER_HELDOUT      23

#ifndef V28_KIND
#error "V28_KIND must be supplied by the Makefile"
#endif
#ifndef V28_NAME
#define V28_NAME "v28_unknown"
#endif

#define V28_MAX_THREADS 32
#define LINE_BYTES 64UL
#define KiB 1024UL
#define MiB (1024UL * 1024UL)

typedef struct {
    volatile uint64_t lane[LINE_BYTES / sizeof(uint64_t)];
} private_slot_t __attribute__((aligned(LINE_BYTES)));

_Static_assert(sizeof(private_slot_t) == LINE_BYTES,
               "one private output slot must occupy exactly one cache line");

typedef struct {
    int nthreads;
    long scale;
    uint64_t seed;
} bench_cfg_t;

typedef struct {
    uint64_t *private_rw;
    size_t words_per_core;
    size_t lines_per_core;
    uint32_t *private_order;
    uint64_t *shared_ro;
    size_t shared_words;
    size_t shared_lines;
    unsigned shared_log2_lines;
    private_slot_t per_thread_slot[V28_MAX_THREADS];
} bench_state_t;

typedef struct {
    const bench_cfg_t *cfg;
    bench_state_t *state;
    pthread_barrier_t *start_barrier;
    int tid;
    uint64_t checksum;
} worker_arg_t;

static volatile uint64_t g_sink;
static int g_disable_m5;

static inline void m5_work_begin_inline(uint64_t workid, uint64_t threadid)
{
    if (g_disable_m5) return;
    /* x86-64 SysV / gem5 pseudo-inst ABI: arg0=RDI, arg1=RSI.  Supplying
     * explicit constraints keeps roi_boundaries.jsonl deterministic instead
     * of recording whatever values happen to be live in those registers. */
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005a"
                         : : "D"(workid), "S"(threadid) : "rax", "memory");
}

static inline void m5_work_end_inline(uint64_t workid, uint64_t threadid)
{
    if (g_disable_m5) return;
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005b"
                         : : "D"(workid), "S"(threadid) : "rax", "memory");
}

static inline void m5_quiesce_inline(void)
{
    if (g_disable_m5) return;
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x0001"
                         : : : "rax", "memory");
}

static uint64_t splitmix64(uint64_t *x)
{
    uint64_t z = (*x += UINT64_C(0x9e3779b97f4a7c15));
    z = (z ^ (z >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    z = (z ^ (z >> 27)) * UINT64_C(0x94d049bb133111eb);
    return z ^ (z >> 31);
}

static inline uint64_t rotl64(uint64_t x, unsigned r)
{
    return (x << r) | (x >> (64U - r));
}

static inline uint64_t mix_u64(uint64_t x, uint64_t y)
{
    x ^= y + UINT64_C(0x9e3779b97f4a7c15) + (x << 6) + (x >> 2);
    x = rotl64(x, 17) * UINT64_C(0xbf58476d1ce4e5b9);
    return x ^ (x >> 29);
}

static void *aligned_zalloc(size_t align, size_t bytes)
{
    void *p = NULL;
    if (posix_memalign(&p, align, bytes) != 0) return NULL;
    memset(p, 0, bytes);
    return p;
}

static void parse_args(int argc, char **argv, bench_cfg_t *cfg)
{
    cfg->nthreads = 4;
    cfg->scale = 1;
    cfg->seed = UINT64_C(0x28b051e55);
    if (argc >= 2) cfg->nthreads = atoi(argv[1]);
    if (argc >= 3) cfg->scale = atol(argv[2]);
    if (argc >= 5) cfg->seed = (uint64_t)strtoull(argv[4], NULL, 0);
    else if (argc >= 4) cfg->seed = (uint64_t)strtoull(argv[3], NULL, 0);
    if (cfg->nthreads < 1) cfg->nthreads = 1;
    if (cfg->nthreads > V28_MAX_THREADS) cfg->nthreads = V28_MAX_THREADS;
    if (cfg->scale < 1) cfg->scale = 1;
    if (cfg->seed == 0) cfg->seed = UINT64_C(0x28b051e55);
}

static void try_pin_cpu(int tid)
{
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(tid, &set);
    if (pthread_setaffinity_np(pthread_self(), sizeof(set), &set) != 0) {
        fprintf(stderr, "%s: pthread_setaffinity_np(tid=%d) failed: %s\n",
                V28_NAME, tid, strerror(errno));
        abort();
    }
}

static size_t bytes_per_core(void)
{
#if V28_KIND == V28_CACHE_L1_MIXED
    return 16UL * KiB;
#elif V28_KIND == V28_CACHE_L2_MIXED
    return 128UL * KiB;
#elif V28_KIND == V28_MEMORY_SEQ_MODERATE || V28_KIND == V28_MEMORY_RANDOM_MLP
    return 2UL * MiB;
#elif V28_KIND >= V28_MARINE_BASE && V28_KIND <= V28_BVC_ENCODER_BASE
    return 64UL * KiB;
#elif V28_KIND >= V28_MARINE_HELDOUT && V28_KIND <= V28_BVC_ENCODER_HELDOUT
    return 128UL * KiB;
#else
    return 0;
#endif
}

static size_t shared_bytes(void)
{
#if V28_KIND == V28_MARINE_BASE || V28_KIND == V28_MYSQL_BASE || \
    V28_KIND == V28_PYTORCH_BASE
    return 32UL * MiB;
#elif V28_KIND == V28_MARINE_HELDOUT || V28_KIND == V28_MYSQL_HELDOUT || \
      V28_KIND == V28_PYTORCH_HELDOUT
    return 64UL * MiB;
#elif V28_KIND == V28_GOFEED_BASE
    return 8UL * MiB;
#elif V28_KIND == V28_GOFEED_HELDOUT
    return 16UL * MiB;
#elif V28_KIND == V28_FLINK_BASE || V28_KIND == V28_REDIS_BASE || \
      V28_KIND == V28_BVC_ENCODER_BASE
    return 16UL * MiB;
#elif V28_KIND == V28_FLINK_HELDOUT || V28_KIND == V28_REDIS_HELDOUT || \
      V28_KIND == V28_BVC_ENCODER_HELDOUT
    return 32UL * MiB;
#else
    return 128UL * KiB;
#endif
}

static int uses_business_shared_model(void)
{
#if V28_KIND >= V28_MARINE_BASE && V28_KIND <= V28_BVC_ENCODER_HELDOUT
    return 1;
#else
    return 0;
#endif
}

static int uses_private_order(void)
{
#if V28_KIND == V28_MEMORY_RANDOM_MLP
    return 1;
#else
    return 0;
#endif
}

static int init_state(const bench_cfg_t *cfg, bench_state_t *st)
{
    memset(st, 0, sizeof(*st));
    size_t bytes = bytes_per_core();
    st->words_per_core = bytes / sizeof(uint64_t);
    st->lines_per_core = bytes / LINE_BYTES;
    st->shared_words = shared_bytes() / sizeof(uint64_t);
    st->shared_lines = st->shared_words / (LINE_BYTES / sizeof(uint64_t));
    for (size_t n = st->shared_lines; n > 1U; n >>= 1U)
        ++st->shared_log2_lines;
    st->shared_ro = aligned_zalloc(LINE_BYTES, st->shared_words * sizeof(uint64_t));
    if (!st->shared_ro) return 1;

    uint64_t rnd = cfg->seed;
    if (uses_business_shared_model()) {
        for (size_t line = 0; line < st->shared_lines; ++line)
            st->shared_ro[line * 8U] = splitmix64(&rnd) | 1U;
    } else {
        for (size_t i = 0; i < st->shared_words; ++i)
            st->shared_ro[i] = splitmix64(&rnd) | 1U;
    }
    for (int tid = 0; tid < V28_MAX_THREADS; ++tid)
        st->per_thread_slot[tid].lane[0] = splitmix64(&rnd);

    if (bytes == 0) return 0;
    size_t total_words = st->words_per_core * (size_t)cfg->nthreads;
    size_t total_lines = st->lines_per_core * (size_t)cfg->nthreads;
    st->private_rw = aligned_zalloc(LINE_BYTES, total_words * sizeof(uint64_t));
    if (uses_private_order())
        st->private_order = aligned_zalloc(
            LINE_BYTES, total_lines * sizeof(uint32_t));
    if (!st->private_rw || (uses_private_order() && !st->private_order)) return 1;
    for (int tid = 0; tid < cfg->nthreads; ++tid) {
        uint64_t s = cfg->seed ^ ((uint64_t)(tid + 1) * UINT64_C(0xd1b54a32d192ed03));
        uint64_t *mem = st->private_rw + (size_t)tid * st->words_per_core;
        uint32_t *order = st->private_order
            ? st->private_order + (size_t)tid * st->lines_per_core : NULL;
        for (size_t i = 0; i < st->words_per_core; ++i)
            mem[i] = splitmix64(&s) | 1U;
        if (order) {
            for (size_t i = 0; i < st->lines_per_core; ++i)
                order[i] = (uint32_t)(splitmix64(&s) & (st->lines_per_core - 1U));
        }
    }
    return 0;
}

static inline volatile uint64_t *core_mem(bench_state_t *st, int tid)
{
    return st->private_rw + (size_t)tid * st->words_per_core;
}

static inline const uint32_t *core_order(const bench_state_t *st, int tid)
{
    return st->private_order + (size_t)tid * st->lines_per_core;
}

static inline size_t shared_zipf_line(
    const bench_state_t *st, uint64_t key, uint64_t salt, int heldout)
{
    uint64_t z = mix_u64(key ^ UINT64_C(0x6a09e667f3bcc909), salt);
    /* Equal probability per logarithmic rank bucket and uniform selection
     * inside a bucket gives P(rank) ~= 1/rank (bounded Zipf s ~= 1). */
    unsigned bucket = (unsigned)(
        ((uint64_t)(uint32_t)z * st->shared_log2_lines) >> 32);
    size_t width = (size_t)1U << bucket;
    size_t rank = (width - 1U) + ((size_t)(z >> 32) & (width - 1U));
    /* Heldout retains a deterministic Zipf majority but exposes 1/16 extra
     * uniform tail reads over its larger table. */
    if (heldout && ((z >> 20) & 15U) == 0U)
        rank = (size_t)(z >> 24) & (st->shared_lines - 1U);
    return (rank * UINT64_C(0x9e3779b185ebca87) + salt * 131U)
         & (st->shared_lines - 1U);
}

static inline uint64_t shared_word(
    const bench_state_t *st, size_t line, size_t word)
{
    uint64_t base = st->shared_ro[line * (LINE_BYTES / sizeof(uint64_t))];
    return mix_u64(base, (word & 7U) * UINT64_C(0xd1b54a32d192ed03));
}

static uint64_t kernel_int_alu(int tid, long scale)
{
    uint64_t x = UINT64_C(0x123456789abcdef) ^ (uint64_t)(tid + 1);
    uint64_t y = x ^ UINT64_C(0xd1b54a32d192ed03);
    for (long i = 0; i < scale * 4096L; ++i) {
        x = mix_u64(x, y + (uint64_t)i);
        y = mix_u64(y, x ^ UINT64_C(0x94d049bb133111eb));
        x += rotl64(y, 11);
        y ^= rotl64(x, 23);
    }
    return x ^ y;
}

static uint64_t kernel_int_div_serial(int tid, long scale)
{
    volatile uint64_t divisor = 97U;
    uint64_t x = UINT64_C(0xfeedfacecafebeef) + (uint64_t)tid * 4099U;
    for (long i = 1; i <= scale * 2048L; ++i) {
        x = (x * UINT64_C(6364136223846793005) + (uint64_t)i + 1U) / divisor;
        x ^= x << 13;
        x += UINT64_C(0x9e3779b97f4a7c15);
    }
    return x;
}

static uint64_t kernel_fp_alu(int tid, long scale)
{
    double a = 0.001 * (double)(tid + 1), b = 0.002, c = 0.003, d = 0.004;
    for (long i = 0; i < scale * 4096L; ++i) {
        a = a * 0.9999997 + b;
        b = b * 1.0000001 + c;
        c = c * 0.9999999 + d;
        d = d * 1.0000003 + a;
    }
    return (uint64_t)((a + b + c + d) * 1000003.0);
}

__attribute__((target("sse2")))
static uint64_t kernel_simd_sse(int tid, long scale)
{
    __m128 a = _mm_set1_ps(0.001f * (float)(tid + 1));
    __m128 b = _mm_set1_ps(0.002f);
    __m128 m = _mm_set1_ps(0.99999f), c = _mm_set1_ps(1.00001f);
    float out[4] __attribute__((aligned(16)));
    for (long i = 0; i < scale * 4096L; ++i) {
        a = _mm_add_ps(_mm_mul_ps(a, m), b);
        b = _mm_add_ps(_mm_mul_ps(b, c), a);
        a = _mm_min_ps(a, _mm_set1_ps(4096.0f));
        b = _mm_max_ps(b, _mm_set1_ps(-4096.0f));
    }
    _mm_store_ps(out, _mm_add_ps(a, b));
    return (uint64_t)(out[0] + out[1] + out[2] + out[3]);
}

static uint64_t kernel_cache_mixed(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    volatile uint64_t *mem = core_mem(st, tid);
    size_t mask = st->lines_per_core - 1U;
    uint64_t x = cfg->seed ^ (uint64_t)(tid + 1);
    for (size_t i = 0; i < (size_t)cfg->scale * 16384U; ++i) {
        size_t line = (i * 17U + (size_t)tid * 29U) & mask;
        uint64_t v = mem[line * 8U];
        x = mix_u64(x, v + i);
        x = mix_u64(x, rotl64(v, 13));
        if ((i & 63U) == 0U) mem[line * 8U + 1U] = x;
    }
    return x;
}

static uint64_t kernel_memory_seq(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    volatile uint64_t *mem = core_mem(st, tid);
    size_t mask = st->lines_per_core - 1U;
    uint64_t a = (uint64_t)(tid + 1), b = cfg->seed;
    for (size_t i = 0; i < (size_t)cfg->scale * 8192U; ++i) {
        size_t p = (i * 4U + (size_t)tid * 37U) & mask;
        uint64_t v0 = mem[((p + 0U) & mask) * 8U];
        uint64_t v1 = mem[((p + 1U) & mask) * 8U];
        uint64_t v2 = mem[((p + 2U) & mask) * 8U];
        uint64_t v3 = mem[((p + 3U) & mask) * 8U];
        a = mix_u64(a, v0 ^ v2);
        b = mix_u64(b, v1 ^ v3);
        a += rotl64(b, 7);
        b ^= rotl64(a, 19);
    }
    return a ^ b;
}

static uint64_t kernel_memory_random(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    volatile uint64_t *mem = core_mem(st, tid);
    const uint32_t *order = core_order(st, tid);
    size_t mask = st->lines_per_core - 1U;
    uint64_t a = cfg->seed ^ (uint64_t)(tid + 1), b = ~a;
    for (size_t i = 0; i < (size_t)cfg->scale * 8192U; ++i) {
        uint64_t v0 = mem[(size_t)order[(i * 4U + 0U) & mask] * 8U];
        uint64_t v1 = mem[(size_t)order[(i * 4U + 1U) & mask] * 8U];
        uint64_t v2 = mem[(size_t)order[(i * 4U + 2U) & mask] * 8U];
        uint64_t v3 = mem[(size_t)order[(i * 4U + 3U) & mask] * 8U];
        a = mix_u64(a, v0 + v2);
        b = mix_u64(b, v1 + v3);
        a ^= rotl64(b, 9);
    }
    return a ^ b;
}

static uint64_t kernel_coh_readmostly_sparse(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    volatile const uint64_t *shared = st->shared_ro;
    size_t mask = st->shared_words - 1U;
    uint64_t x = cfg->seed ^ (uint64_t)(tid + 1);
    for (size_t i = 0; i < (size_t)cfg->scale * 16384U; ++i) {
        uint64_t meta = shared[(i * 13U + (size_t)tid * 7U) & mask];
        x = mix_u64(x, meta);
        x = mix_u64(x, rotl64(meta, 21));
        if ((i & 63U) == 0U)
            st->per_thread_slot[tid].lane[0] = x;
    }
    return x ^ st->per_thread_slot[tid].lane[0];
}

static uint64_t kernel_marine(const bench_cfg_t *cfg, bench_state_t *st, int tid, int heldout)
{
    volatile uint64_t *request = core_mem(st, tid);
    size_t private_mask = st->lines_per_core - 1U;
    uint64_t score = cfg->seed ^ ((uint64_t)(tid + 1) * UINT64_C(0x9e3779b97f4a7c15));
    size_t rounds = (size_t)cfg->scale * (heldout ? 2560U : 2816U);
    for (size_t i = 0; i < rounds; ++i) {
        size_t req = (i * 3U + (size_t)tid * 29U) & private_mask;
        uint64_t req0 = request[req * 8U], req1 = request[req * 8U + 1U];
        size_t l0 = shared_zipf_line(st, score ^ req0, i * 4U + 0U, heldout);
        size_t l1 = shared_zipf_line(st, score ^ req1, i * 4U + 1U, heldout);
        size_t l2 = shared_zipf_line(st, req0 + req1, i * 4U + 2U, heldout);
        size_t l3 = shared_zipf_line(st, score + req1, i * 4U + 3U, heldout);
        uint64_t a = shared_word(st, l0, 0), b = shared_word(st, l1, 2);
        uint64_t c = shared_word(st, l2, 4), d = shared_word(st, l3, 6);
        score = mix_u64(score, a + rotl64(b, 7));
        score = mix_u64(score, c ^ rotl64(d, 19));
        if ((score & (heldout ? 7U : 15U)) < 5U)
            score += (a ^ c) + rotl64(b, 11);
        else
            score ^= (b + d) ^ rotl64(c, 23);
        if ((i & 31U) == 0U)
            request[((req + 1U) & private_mask) * 8U + 4U] = score;
    }
    st->per_thread_slot[tid].lane[0] = score;
    return score;
}

static uint64_t kernel_gofeed(const bench_cfg_t *cfg, bench_state_t *st, int tid, int heldout)
{
    volatile uint64_t *request = core_mem(st, tid);
    size_t private_mask = st->lines_per_core - 1U;
    uint64_t hash = cfg->seed ^ (uint64_t)(tid + 1), out = 0;
    size_t rounds = (size_t)cfg->scale * (heldout ? 3840U : 4096U);
    for (size_t i = 0; i < rounds; ++i) {
        size_t req_line = (i * (heldout ? 3U : 2U) + (size_t)tid * 41U) & private_mask;
        uint64_t req0 = request[req_line * 8U], req1 = request[req_line * 8U + 1U];
        hash = mix_u64(hash, req0);
        hash = mix_u64(hash, req1 ^ rotl64(hash, 13));
        size_t p0 = shared_zipf_line(st, hash, i * 2U, heldout);
        size_t p1 = shared_zipf_line(st, rotl64(hash, 17), i * 2U + 1U, heldout);
        uint64_t v0 = shared_word(st, p0, 1), v1 = shared_word(st, p1, 5);
        if (((v0 ^ hash) & (heldout ? 7U : 3U)) != 0U)
            out = mix_u64(out, v0 + v1);
        else
            out ^= mix_u64(v1, hash);
        if ((i & (heldout ? 15U : 31U)) == 0U)
            request[((req_line + 1U) & private_mask) * 8U + 2U] = out;
    }
    st->per_thread_slot[tid].lane[0] = out;
    return hash ^ out;
}

static uint64_t kernel_flink(const bench_cfg_t *cfg, bench_state_t *st, int tid, int heldout)
{
    volatile uint64_t *private_state = core_mem(st, tid);
    size_t private_mask = st->lines_per_core - 1U;
    uint64_t agg = cfg->seed ^ (uint64_t)(tid + 1), emitted = 0;
    size_t rounds = (size_t)cfg->scale * (heldout ? 4096U : 4608U);
    for (size_t i = 0; i < rounds; ++i) {
        size_t event_line = shared_zipf_line(
            st, agg ^ ((uint64_t)tid << 32), i * 2U, heldout);
        size_t dim_line = shared_zipf_line(st, i + agg, i * 2U + 1U, heldout);
        uint64_t e0 = shared_word(st, event_line, 0);
        uint64_t e1 = shared_word(st, dim_line, 3);
        uint64_t key = mix_u64(e0, e1);
        size_t bucket = (size_t)(key >> 11) & private_mask;
        uint64_t state = private_state[bucket * 8U + 3U];
        agg = mix_u64(agg, state + key);
        if ((key & (heldout ? 7U : 15U)) != 0U) {
            private_state[bucket * 8U + 3U] = state + (agg & 0xffffU);
            emitted += mix_u64(e0, agg);
        } else {
            emitted ^= rotl64(key, 17);
        }
        if ((i & 63U) == 0U)
            private_state[((bucket + 5U) & private_mask) * 8U + 5U] = emitted;
    }
    st->per_thread_slot[tid].lane[0] = emitted;
    return agg ^ emitted;
}

static uint64_t kernel_mysql(const bench_cfg_t *cfg, bench_state_t *st, int tid, int heldout)
{
    volatile uint64_t *txn = core_mem(st, tid);
    size_t private_mask = st->lines_per_core - 1U;
    uint64_t q = cfg->seed ^ (uint64_t)(tid + 1), rows = 0;
    size_t rounds = (size_t)cfg->scale * (heldout ? 3584U : 3840U);
    for (size_t i = 0; i < rounds; ++i) {
        size_t query = (i * 5U + (size_t)tid * 31U) & private_mask;
        uint64_t predicate = txn[query * 8U];
        size_t p0 = shared_zipf_line(st, q ^ predicate, i * 3U, heldout);
        uint64_t n0 = shared_word(st, p0, 0);
        q = mix_u64(q, n0);
        size_t p1 = shared_zipf_line(st, n0 ^ q, i * 3U + 1U, heldout);
        uint64_t n1 = shared_word(st, p1, 1);
        q = mix_u64(q, n1);
        size_t p2 = shared_zipf_line(st, n1 ^ q, i * 3U + 2U, heldout);
        uint64_t tuple0 = shared_word(st, p2, 2);
        uint64_t tuple1 = shared_word(st, p2, 3);
        if (((tuple0 ^ q) & 7U) != 0U) rows += mix_u64(tuple0, tuple1);
        else rows ^= rotl64(tuple1, 9);
        if ((i & (heldout ? 31U : 63U)) == 0U)
            txn[((query + 1U) & private_mask) * 8U + 4U] = rows;
    }
    st->per_thread_slot[tid].lane[0] = rows;
    return q ^ rows;
}

static uint64_t kernel_redis(const bench_cfg_t *cfg, bench_state_t *st, int tid, int heldout)
{
    volatile uint64_t *request = core_mem(st, tid);
    size_t private_mask = st->lines_per_core - 1U;
    uint64_t hash = cfg->seed ^ (uint64_t)(tid + 1), reply = 0;
    size_t rounds = (size_t)cfg->scale * (heldout ? 3584U : 4096U);
    for (size_t i = 0; i < rounds; ++i) {
        size_t req = (i * 3U + (size_t)tid * 43U) & private_mask;
        hash = mix_u64(hash, request[req * 8U]);
        size_t b0 = shared_zipf_line(st, hash, i * 2U, heldout);
        size_t b1 = shared_zipf_line(st, rotl64(hash, 19), i * 2U + 1U, heldout);
        uint64_t k0 = shared_word(st, b0, 0), k1 = shared_word(st, b1, 0);
        size_t chosen = ((k0 ^ hash) < (k1 ^ hash)) ? b0 : b1;
        uint64_t v0 = shared_word(st, chosen, 2);
        uint64_t v1 = shared_word(st, chosen, 3);
        reply = mix_u64(reply, v0);
        reply = mix_u64(reply, v1);
        if (heldout) reply = mix_u64(reply, shared_word(st, chosen, 4));
        if ((i & 63U) == 0U)
            request[((req + 1U) & private_mask) * 8U + 6U] = reply;
    }
    st->per_thread_slot[tid].lane[0] = reply;
    return hash ^ reply;
}

__attribute__((target("sse2")))
static uint64_t kernel_pytorch(const bench_cfg_t *cfg, bench_state_t *st, int tid, int heldout)
{
    const __m128i mask_i = _mm_set1_epi32(255);
    const __m128 scale_v = _mm_set1_ps(1.0f / 4096.0f);
    const __m128 weight = _mm_set1_ps(heldout ? 0.6875f : 0.75f);
    const __m128 cross = _mm_set1_ps(1.0f / 32.0f);
    const __m128 zero = _mm_setzero_ps();
    volatile uint64_t *output = core_mem(st, tid);
    const __m128i *model = (const __m128i *)(const void *)st->shared_ro;
    __m128 a = _mm_set1_ps(0.001f * (float)(tid + 1));
    __m128 b = _mm_set1_ps(0.002f), c = _mm_set1_ps(0.003f);
    size_t rounds = (size_t)cfg->scale * (heldout ? 3584U : 4096U);
    for (size_t i = 0; i < rounds; ++i) {
        size_t l0 = shared_zipf_line(st, (uint64_t)tid ^ i, i * 3U, heldout);
        size_t l1 = shared_zipf_line(st, (uint64_t)tid + i * 5U, i * 3U + 1U, heldout);
        size_t l2 = shared_zipf_line(st, (uint64_t)tid + i * 11U, i * 3U + 2U, heldout);
        __m128 x0 = _mm_mul_ps(_mm_cvtepi32_ps(_mm_and_si128(
            _mm_load_si128(&model[l0 * 4U]), mask_i)), scale_v);
        __m128 x1 = _mm_mul_ps(_mm_cvtepi32_ps(_mm_and_si128(
            _mm_load_si128(&model[l1 * 4U]), mask_i)), scale_v);
        __m128 x2 = _mm_mul_ps(_mm_cvtepi32_ps(_mm_and_si128(
            _mm_load_si128(&model[l2 * 4U]), mask_i)), scale_v);
        a = _mm_add_ps(_mm_mul_ps(a, weight), x0);
        b = _mm_add_ps(_mm_mul_ps(b, weight), x1);
        c = _mm_add_ps(_mm_mul_ps(c, weight), x2);
        a = _mm_max_ps(_mm_add_ps(a, _mm_mul_ps(b, cross)), zero);
        b = _mm_add_ps(b, _mm_mul_ps(c, cross));
        if (heldout) c = _mm_add_ps(c, _mm_mul_ps(a, cross));
        a = _mm_min_ps(a, _mm_set1_ps(64.0f));
        b = _mm_min_ps(b, _mm_set1_ps(64.0f));
        c = _mm_min_ps(c, _mm_set1_ps(64.0f));
        if ((i & 63U) == 0U)
            output[((i >> 6) & (st->words_per_core - 1U))] =
                (uint64_t)_mm_cvtsi128_si64(_mm_castps_si128(a));
    }
    uint32_t out[4] __attribute__((aligned(16)));
    _mm_store_si128((__m128i *)out, _mm_castps_si128(_mm_add_ps(a, _mm_add_ps(b, c))));
    uint64_t result = (uint64_t)out[0] ^ ((uint64_t)out[1] << 32) ^ out[2] ^ out[3];
    st->per_thread_slot[tid].lane[0] = result;
    return result;
}

static uint64_t kernel_bvc(const bench_cfg_t *cfg, bench_state_t *st, int tid, int heldout)
{
    volatile uint64_t *output = core_mem(st, tid);
    size_t private_mask = st->lines_per_core - 1U;
    uint64_t cost = cfg->seed ^ (uint64_t)(tid + 1), bits = 0;
    size_t rounds = (size_t)cfg->scale * (heldout ? 2944U : 3328U);
    for (size_t i = 0; i < rounds; ++i) {
        size_t cur = shared_zipf_line(st, cost ^ i, i * 2U, heldout);
        size_t ref = shared_zipf_line(
            st, cost + (heldout ? 17U : 5U) + (i >> 4), i * 2U + 1U, heldout);
        uint64_t p0 = shared_word(st, cur, 0), p1 = shared_word(st, cur, 1);
        uint64_t r0 = shared_word(st, ref, 0), r1 = shared_word(st, ref, 1);
        uint64_t d0 = p0 ^ r0, d1 = p1 ^ r1;
        uint64_t sad = (uint64_t)__builtin_popcountll(d0)
                     + (uint64_t)__builtin_popcountll(d1);
        cost = mix_u64(cost, sad + rotl64(d0, 7));
        cost = mix_u64(cost, d1 ^ (sad << 11));
        if (sad < (heldout ? 60U : 54U)) bits += cost & 0x3ffU;
        else bits ^= rotl64(cost, 13);
        if ((i & 31U) == 0U) {
            size_t tile = ((i >> 5) + (size_t)tid * 7U) & private_mask;
            output[tile * 8U + 6U] = bits;
        }
    }
    st->per_thread_slot[tid].lane[0] = bits;
    return cost ^ bits;
}

static uint64_t dispatch_kernel(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
#if V28_KIND == V28_INT_ALU_DENSE
    return kernel_int_alu(tid, cfg->scale);
#elif V28_KIND == V28_INT_DIV_SERIAL
    return kernel_int_div_serial(tid, cfg->scale);
#elif V28_KIND == V28_FP_ALU_DENSE
    return kernel_fp_alu(tid, cfg->scale);
#elif V28_KIND == V28_SIMD_SSE_DENSE
    return kernel_simd_sse(tid, cfg->scale);
#elif V28_KIND == V28_CACHE_L1_MIXED || V28_KIND == V28_CACHE_L2_MIXED
    return kernel_cache_mixed(cfg, st, tid);
#elif V28_KIND == V28_MEMORY_SEQ_MODERATE
    return kernel_memory_seq(cfg, st, tid);
#elif V28_KIND == V28_MEMORY_RANDOM_MLP
    return kernel_memory_random(cfg, st, tid);
#elif V28_KIND == V28_COH_READMOSTLY_SPARSE
    return kernel_coh_readmostly_sparse(cfg, st, tid);
#elif V28_KIND == V28_MARINE_BASE
    return kernel_marine(cfg, st, tid, 0);
#elif V28_KIND == V28_MARINE_HELDOUT
    return kernel_marine(cfg, st, tid, 1);
#elif V28_KIND == V28_GOFEED_BASE
    return kernel_gofeed(cfg, st, tid, 0);
#elif V28_KIND == V28_GOFEED_HELDOUT
    return kernel_gofeed(cfg, st, tid, 1);
#elif V28_KIND == V28_FLINK_BASE
    return kernel_flink(cfg, st, tid, 0);
#elif V28_KIND == V28_FLINK_HELDOUT
    return kernel_flink(cfg, st, tid, 1);
#elif V28_KIND == V28_MYSQL_BASE
    return kernel_mysql(cfg, st, tid, 0);
#elif V28_KIND == V28_MYSQL_HELDOUT
    return kernel_mysql(cfg, st, tid, 1);
#elif V28_KIND == V28_REDIS_BASE
    return kernel_redis(cfg, st, tid, 0);
#elif V28_KIND == V28_REDIS_HELDOUT
    return kernel_redis(cfg, st, tid, 1);
#elif V28_KIND == V28_PYTORCH_BASE
    return kernel_pytorch(cfg, st, tid, 0);
#elif V28_KIND == V28_PYTORCH_HELDOUT
    return kernel_pytorch(cfg, st, tid, 1);
#elif V28_KIND == V28_BVC_ENCODER_BASE
    return kernel_bvc(cfg, st, tid, 0);
#elif V28_KIND == V28_BVC_ENCODER_HELDOUT
    return kernel_bvc(cfg, st, tid, 1);
#else
#error "unknown V28_KIND"
#endif
}

static void *worker_main(void *argp)
{
    worker_arg_t *arg = (worker_arg_t *)argp;
    try_pin_cpu(arg->tid);
    pthread_barrier_wait(arg->start_barrier); /* setup-only, before ROI */
    m5_work_begin_inline(0, (uint64_t)arg->tid);
    arg->checksum = dispatch_kernel(arg->cfg, arg->state, arg->tid);
    m5_work_end_inline(0, (uint64_t)arg->tid);
    /*
     * The first N-1 WORKEND events resume simulation and this core quiesces.
     * The Nth WORKEND terminates gem5 before any worker returns to
     * pthread_join/futex/cleanup.  Native runs set TAO_DISABLE_M5 and return.
     */
    m5_quiesce_inline();
    return NULL;
}

int main(int argc, char **argv)
{
    bench_cfg_t cfg;
    bench_state_t state;
    pthread_t threads[V28_MAX_THREADS];
    worker_arg_t args[V28_MAX_THREADS];
    pthread_barrier_t start_barrier;
    uint64_t total = 0;
    g_disable_m5 = getenv("TAO_DISABLE_M5") != NULL;
    parse_args(argc, argv, &cfg);
    if (init_state(&cfg, &state) != 0) {
        fprintf(stderr, "%s: allocation failed\n", V28_NAME);
        return 1;
    }
    if (pthread_barrier_init(&start_barrier, NULL, (unsigned)cfg.nthreads) != 0) return 1;
    for (int tid = 0; tid < cfg.nthreads; ++tid) {
        args[tid] = (worker_arg_t){
            .cfg=&cfg, .state=&state, .start_barrier=&start_barrier,
            .tid=tid, .checksum=0,
        };
        if (tid > 0 && pthread_create(&threads[tid], NULL, worker_main, &args[tid]) != 0)
            return 1;
    }
    worker_main(&args[0]);
    for (int tid = 1; tid < cfg.nthreads; ++tid) pthread_join(threads[tid], NULL);
    for (int tid = 0; tid < cfg.nthreads; ++tid)
        total ^= args[tid].checksum + (uint64_t)tid;
    pthread_barrier_destroy(&start_barrier);
    free(state.private_rw);
    free(state.private_order);
    free(state.shared_ro);
    g_sink ^= total;
    fprintf(stderr, "%s: done checksum=%llu sink=%llu\n", V28_NAME,
            (unsigned long long)total, (unsigned long long)g_sink);
    return 0;
}
