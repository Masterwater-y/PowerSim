/*
 * Business-shaped microarchitecture excitation workloads.
 *
 * ABI: ./workload <nthreads> <scale> 1 <seed>
 *
 * Allocation, first touch, thread creation and the start barrier are outside
 * the ROI.  The ROI contains deterministic read-mostly request, embedding and
 * index-serving kernels; it has no locks, atomics, syscalls or trace helpers.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <emmintrin.h>
#include <xmmintrin.h>

#define GOFEED_FANOUT_WIDE       1
#define GOFEED_GRAPH_PAGES_80    2
#define GOFEED_GRAPH_48M_RANDOM  3
#define GOFEED_SHARED_HOTSPOT    4
#define PYTORCH_DENSE_BATCH      5
#define PYTORCH_EMBEDDING_768K   6
#define PYTORCH_EMBEDDING_1536K  7
#define PYTORCH_EMBEDDING_48M    8
#define MYSQL_HOT_INDEX_24K      9
#define MYSQL_HOT_INDEX_48K     10
#define MYSQL_INDEX_1280K       11
#define MYSQL_TABLE_96M_SHARED  12

#ifndef BUSINESS_KIND
#error "BUSINESS_KIND must be supplied by the Makefile"
#endif
#ifndef BUSINESS_NAME
#define BUSINESS_NAME "business_excitation_unknown"
#endif

#define MAX_THREADS 32
#define LINE_BYTES 64UL
#define PAGE_BYTES 4096UL
#define KiB 1024UL
#define MiB (1024UL * 1024UL)

typedef struct {
    int nthreads;
    long scale;
    uint64_t seed;
} bench_cfg_t;

typedef struct {
    uint64_t *private_rw;
    size_t private_words;
    size_t private_lines;
    uint64_t *shared_ro;
    size_t shared_words;
    size_t shared_lines;
    volatile uint64_t result[MAX_THREADS][LINE_BYTES / sizeof(uint64_t)]
        __attribute__((aligned(LINE_BYTES)));
} bench_state_t;

typedef struct {
    const bench_cfg_t *cfg;
    bench_state_t *state;
    pthread_barrier_t *barrier;
    int tid;
    uint64_t checksum;
} worker_arg_t;

static volatile uint64_t g_sink;
static int g_disable_m5;

static inline void m5_work_begin_inline(uint64_t workid, uint64_t threadid)
{
    if (g_disable_m5) return;
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

static inline uint64_t rotl64(uint64_t x, unsigned r)
{
    return (x << r) | (x >> (64U - r));
}

static inline uint64_t mix64(uint64_t x, uint64_t y)
{
    x ^= y + UINT64_C(0x9e3779b97f4a7c15) + (x << 6) + (x >> 2);
    x = rotl64(x, 17) * UINT64_C(0xbf58476d1ce4e5b9);
    return x ^ (x >> 29);
}

static uint64_t splitmix64(uint64_t *x)
{
    uint64_t z = (*x += UINT64_C(0x9e3779b97f4a7c15));
    z = (z ^ (z >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    z = (z ^ (z >> 27)) * UINT64_C(0x94d049bb133111eb);
    return z ^ (z >> 31);
}

static void *aligned_zalloc(size_t bytes)
{
    void *p = NULL;
    if (posix_memalign(&p, PAGE_BYTES, bytes) != 0) return NULL;
    memset(p, 0, bytes);
    return p;
}

static size_t private_bytes(void)
{
#if BUSINESS_KIND == GOFEED_GRAPH_PAGES_80
    return 80UL * PAGE_BYTES;
#elif BUSINESS_KIND == PYTORCH_EMBEDDING_768K
    return 768UL * KiB;
#elif BUSINESS_KIND == PYTORCH_EMBEDDING_1536K
    return 1536UL * KiB;
#elif BUSINESS_KIND == MYSQL_HOT_INDEX_24K
    return 24UL * KiB;
#elif BUSINESS_KIND == MYSQL_HOT_INDEX_48K
    return 48UL * KiB;
#elif BUSINESS_KIND == MYSQL_INDEX_1280K
    return 1280UL * KiB;
#else
    return 64UL * KiB;
#endif
}

static size_t shared_bytes(void)
{
#if BUSINESS_KIND == GOFEED_GRAPH_48M_RANDOM || \
    BUSINESS_KIND == GOFEED_SHARED_HOTSPOT || \
    BUSINESS_KIND == PYTORCH_EMBEDDING_48M
    return 48UL * MiB;
#elif BUSINESS_KIND == MYSQL_TABLE_96M_SHARED
    return 96UL * MiB;
#elif BUSINESS_KIND == GOFEED_FANOUT_WIDE || BUSINESS_KIND == PYTORCH_DENSE_BATCH
    return 16UL * MiB;
#else
    return 1UL * MiB;
#endif
}

static void parse_args(int argc, char **argv, bench_cfg_t *cfg)
{
    cfg->nthreads = 4;
    cfg->scale = 1;
    cfg->seed = UINT64_C(0xbe51e551);
    if (argc >= 2) cfg->nthreads = atoi(argv[1]);
    if (argc >= 3) cfg->scale = atol(argv[2]);
    if (argc >= 5) cfg->seed = (uint64_t)strtoull(argv[4], NULL, 0);
    else if (argc >= 4) cfg->seed = (uint64_t)strtoull(argv[3], NULL, 0);
    if (cfg->nthreads < 1) cfg->nthreads = 1;
    if (cfg->nthreads > MAX_THREADS) cfg->nthreads = MAX_THREADS;
    if (cfg->scale < 1) cfg->scale = 1;
}

static int init_state(const bench_cfg_t *cfg, bench_state_t *st)
{
    memset(st, 0, sizeof(*st));
    st->private_words = private_bytes() / sizeof(uint64_t);
    st->private_lines = private_bytes() / LINE_BYTES;
    st->shared_words = shared_bytes() / sizeof(uint64_t);
    st->shared_lines = shared_bytes() / LINE_BYTES;
    st->private_rw = aligned_zalloc(
        st->private_words * (size_t)cfg->nthreads * sizeof(uint64_t));
    st->shared_ro = aligned_zalloc(st->shared_words * sizeof(uint64_t));
    if (!st->private_rw || !st->shared_ro) return 1;

    uint64_t s = cfg->seed;
    for (size_t i = 0; i < st->shared_words; ++i)
        st->shared_ro[i] = splitmix64(&s) | 1U;
    for (int tid = 0; tid < cfg->nthreads; ++tid) {
        uint64_t t = cfg->seed ^ ((uint64_t)(tid + 1) * UINT64_C(0xd1b54a32d192ed03));
        uint64_t *base = st->private_rw + (size_t)tid * st->private_words;
        for (size_t i = 0; i < st->private_words; ++i)
            base[i] = splitmix64(&t) | 1U;
        st->result[tid][0] = splitmix64(&t);
    }
    return 0;
}

static inline volatile uint64_t *private_mem(bench_state_t *st, int tid)
{
    return st->private_rw + (size_t)tid * st->private_words;
}

static inline size_t uniform_line(size_t lines, uint64_t key)
{
    return (size_t)(key % lines);
}

static uint64_t kernel_fanout(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    volatile const uint64_t *table = st->shared_ro;
    volatile uint64_t *request = private_mem(st, tid);
    uint64_t a = cfg->seed ^ (uint64_t)(tid + 1), b = ~a, c = a + 17U, d = b - 31U;
    size_t rounds = (size_t)cfg->scale * 8192U;
    for (size_t i = 0; i < rounds; ++i) {
        uint64_t k = mix64((uint64_t)i + ((uint64_t)tid << 32), cfg->seed);
        size_t p0 = uniform_line(st->shared_lines, k + 0U * UINT64_C(0x9e3779b97f4a7c15));
        size_t p1 = uniform_line(st->shared_lines, k + 1U * UINT64_C(0x9e3779b97f4a7c15));
        size_t p2 = uniform_line(st->shared_lines, k + 2U * UINT64_C(0x9e3779b97f4a7c15));
        size_t p3 = uniform_line(st->shared_lines, k + 3U * UINT64_C(0x9e3779b97f4a7c15));
        size_t p4 = uniform_line(st->shared_lines, k + 4U * UINT64_C(0x9e3779b97f4a7c15));
        size_t p5 = uniform_line(st->shared_lines, k + 5U * UINT64_C(0x9e3779b97f4a7c15));
        size_t p6 = uniform_line(st->shared_lines, k + 6U * UINT64_C(0x9e3779b97f4a7c15));
        size_t p7 = uniform_line(st->shared_lines, k + 7U * UINT64_C(0x9e3779b97f4a7c15));
        uint64_t v0 = table[p0 * 8U], v1 = table[p1 * 8U];
        uint64_t v2 = table[p2 * 8U], v3 = table[p3 * 8U];
        uint64_t v4 = table[p4 * 8U], v5 = table[p5 * 8U];
        uint64_t v6 = table[p6 * 8U], v7 = table[p7 * 8U];
        a = mix64(a, v0 + v4); b = mix64(b, v1 + v5);
        c = mix64(c, v2 + v6); d = mix64(d, v3 + v7);
        if ((i & 63U) == 0U) request[(i >> 6) % st->private_words] = a ^ b ^ c ^ d;
    }
    return a ^ b ^ c ^ d;
}

static uint64_t kernel_pages80(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    volatile uint64_t *graph = private_mem(st, tid);
    uint64_t x = cfg->seed ^ (uint64_t)(tid + 1), y = ~x;
    size_t rounds = (size_t)cfg->scale * 8192U;
    for (size_t i = 0; i < rounds; ++i) {
        uint64_t k = mix64(x + i, y);
        size_t p0 = (size_t)(k % 80U), p1 = (size_t)((k >> 8) % 80U);
        size_t p2 = (size_t)((k >> 16) % 80U), p3 = (size_t)((k >> 24) % 80U);
        uint64_t v0 = graph[p0 * (PAGE_BYTES / 8U)];
        uint64_t v1 = graph[p1 * (PAGE_BYTES / 8U) + 7U];
        uint64_t v2 = graph[p2 * (PAGE_BYTES / 8U) + 15U];
        uint64_t v3 = graph[p3 * (PAGE_BYTES / 8U) + 31U];
        x = mix64(x, v0 + v2); y = mix64(y, v1 + v3);
    }
    return x ^ y;
}

static uint64_t kernel_shared_random(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    volatile const uint64_t *table = st->shared_ro;
    uint64_t x = cfg->seed ^ (uint64_t)(tid + 1), y = ~x, z = x + 11U;
    size_t rounds = (size_t)cfg->scale * 8192U;
    for (size_t i = 0; i < rounds; ++i) {
        uint64_t k = mix64((uint64_t)i + ((uint64_t)tid << 37), cfg->seed);
        size_t p0 = uniform_line(st->shared_lines, k);
        size_t p1 = uniform_line(st->shared_lines, rotl64(k, 21));
        size_t p2 = uniform_line(st->shared_lines, k ^ UINT64_C(0xd1b54a32d192ed03));
        size_t p3 = uniform_line(st->shared_lines, k + UINT64_C(0x94d049bb133111eb));
        uint64_t v0 = table[p0 * 8U], v1 = table[p1 * 8U + 1U];
        uint64_t v2 = table[p2 * 8U + 2U], v3 = table[p3 * 8U + 3U];
        x = mix64(x, v0 + v2); y = mix64(y, v1 + v3); z = mix64(z, x ^ y);
    }
    return x ^ y ^ z;
}

__attribute__((target("sse2")))
static uint64_t kernel_embedding(const bench_cfg_t *cfg, bench_state_t *st, int tid,
                                 int shared)
{
    const uint64_t *words = shared
        ? st->shared_ro
        : st->private_rw + (size_t)tid * st->private_words;
    size_t lines = shared ? st->shared_lines : st->private_lines;
    const __m128i *vectors = (const __m128i *)(const void *)words;
    __m128i a = _mm_set_epi64x((long long)cfg->seed, tid + 1);
    __m128i b = _mm_set_epi64x(tid + 17, (long long)(cfg->seed ^ UINT64_C(0xd1b54a32d192ed03)));
    size_t rounds = (size_t)cfg->scale * 12288U;
    for (size_t i = 0; i < rounds; ++i) {
        uint64_t key = mix64((uint64_t)i * 5U + ((uint64_t)tid << 35), cfg->seed);
        size_t p0 = uniform_line(lines, key);
        size_t p1 = uniform_line(lines, rotl64(key, 11));
        size_t p2 = uniform_line(lines, key ^ UINT64_C(0x94d049bb133111eb));
        size_t p3 = uniform_line(lines, key + UINT64_C(0xd1b54a32d192ed03));
        __m128i x0 = _mm_load_si128(&vectors[p0 * 4U]);
        __m128i x1 = _mm_load_si128(&vectors[p1 * 4U]);
        __m128i x2 = _mm_load_si128(&vectors[p2 * 4U]);
        __m128i x3 = _mm_load_si128(&vectors[p3 * 4U]);
        a = _mm_add_epi64(a, _mm_xor_si128(x0, x2));
        b = _mm_add_epi64(b, _mm_xor_si128(x1, x3));
        a = _mm_xor_si128(a, _mm_srli_epi64(b, 13));
        b = _mm_xor_si128(b, _mm_slli_epi64(a, 7));
    }
    uint64_t out[2] __attribute__((aligned(16)));
    _mm_store_si128((__m128i *)out, _mm_xor_si128(a, b));
    return out[0] ^ out[1];
}

static uint64_t kernel_mysql_table(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    volatile const uint64_t *table = st->shared_ro;
    uint64_t query = cfg->seed ^ (uint64_t)(tid + 1), rows = 0;
    size_t rounds = (size_t)cfg->scale * 12288U;
    for (size_t i = 0; i < rounds; ++i) {
        uint64_t key = mix64(query + i * 7U, cfg->seed);
        size_t p0 = uniform_line(st->shared_lines, key);
        size_t p1 = uniform_line(st->shared_lines, rotl64(key, 23));
        size_t p2 = uniform_line(st->shared_lines, key ^ UINT64_C(0xd1b54a32d192ed03));
        uint64_t n0 = table[p0 * 8U], n1 = table[p1 * 8U + 2U];
        uint64_t tuple = table[p2 * 8U + 5U];
        query = mix64(query, n0); query = mix64(query, n1);
        if (((tuple ^ query) & 7U) != 0U)
            rows += mix64(tuple, query);
        else
            rows ^= rotl64(tuple, 17);
    }
    return query ^ rows;
}

static uint64_t kernel_hotspot(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    volatile const uint64_t *table = st->shared_ro;
    uint64_t x = cfg->seed ^ (uint64_t)(tid + 1), y = ~x;
    size_t bank_lines = st->shared_lines / 8U;
    size_t rounds = (size_t)cfg->scale * 8192U;
    for (size_t i = 0; i < rounds; ++i) {
        uint64_t k = mix64((uint64_t)i + ((uint64_t)tid << 32), cfg->seed);
        size_t b0 = uniform_line(bank_lines, k);
        size_t b1 = uniform_line(bank_lines, rotl64(k, 19));
        size_t b2 = uniform_line(bank_lines, k ^ UINT64_C(0x94d049bb133111eb));
        size_t b3 = uniform_line(bank_lines, k + UINT64_C(0xd1b54a32d192ed03));
        uint64_t v0 = table[(b0 * 8U) * 8U];
        uint64_t v1 = table[(b1 * 8U) * 8U + 1U];
        uint64_t v2 = table[(b2 * 8U) * 8U + 2U];
        uint64_t v3 = table[(b3 * 8U) * 8U + 3U];
        x = mix64(x, v0 + v2); y = mix64(y, v1 + v3);
    }
    return x ^ y;
}

__attribute__((target("sse2")))
static uint64_t kernel_dense(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    const __m128i *model = (const __m128i *)(const void *)st->shared_ro;
    __m128 a = _mm_set1_ps(0.001f * (float)(tid + 1));
    __m128 b = _mm_set1_ps(0.002f), c = _mm_set1_ps(0.003f), d = _mm_set1_ps(0.004f);
    const __m128 scale = _mm_set1_ps(1.0f / 65536.0f);
    const __m128 decay = _mm_set1_ps(0.875f);
    size_t vectors = st->shared_words / 2U;
    size_t rounds = (size_t)cfg->scale * 12288U;
    for (size_t i = 0; i < rounds; ++i) {
        uint64_t k = mix64((uint64_t)i + ((uint64_t)tid << 32), cfg->seed);
        size_t p0 = uniform_line(vectors, k), p1 = uniform_line(vectors, rotl64(k, 13));
        size_t p2 = uniform_line(vectors, rotl64(k, 29)), p3 = uniform_line(vectors, ~k);
        __m128 x0 = _mm_mul_ps(_mm_cvtepi32_ps(_mm_load_si128(&model[p0])), scale);
        __m128 x1 = _mm_mul_ps(_mm_cvtepi32_ps(_mm_load_si128(&model[p1])), scale);
        __m128 x2 = _mm_mul_ps(_mm_cvtepi32_ps(_mm_load_si128(&model[p2])), scale);
        __m128 x3 = _mm_mul_ps(_mm_cvtepi32_ps(_mm_load_si128(&model[p3])), scale);
        a = _mm_add_ps(_mm_mul_ps(a, decay), x0);
        b = _mm_add_ps(_mm_mul_ps(b, decay), x1);
        c = _mm_add_ps(_mm_mul_ps(c, decay), x2);
        d = _mm_add_ps(_mm_mul_ps(d, decay), x3);
    }
    uint64_t out[2] __attribute__((aligned(16)));
    _mm_store_si128((__m128i *)out, _mm_castps_si128(_mm_add_ps(a, _mm_add_ps(b, _mm_add_ps(c, d)))));
    return out[0] ^ out[1];
}

static uint64_t kernel_private_index(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    volatile uint64_t *index = private_mem(st, tid);
    uint64_t x = cfg->seed ^ (uint64_t)(tid + 1), rows = 0;
    size_t rounds = (size_t)cfg->scale * 12288U;
    for (size_t i = 0; i < rounds; ++i) {
        uint64_t k = mix64(x + i, cfg->seed);
        size_t p0 = uniform_line(st->private_lines, k);
        size_t p1 = uniform_line(st->private_lines, rotl64(k, 17));
        size_t p2 = uniform_line(st->private_lines, k ^ UINT64_C(0xd1b54a32d192ed03));
        uint64_t n0 = index[p0 * 8U], n1 = index[p1 * 8U + 2U], n2 = index[p2 * 8U + 4U];
        x = mix64(x, n0); x = mix64(x, n1); rows += (n2 ^ x) & 0xffffU;
        if ((n0 ^ n1) & 1U) rows ^= rotl64(n2, 11); else rows += n2;
        if ((i & 127U) == 0U) index[p0 * 8U + 7U] = rows;
    }
    return x ^ rows;
}

static uint64_t dispatch(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
#if BUSINESS_KIND == GOFEED_FANOUT_WIDE
    return kernel_fanout(cfg, st, tid);
#elif BUSINESS_KIND == GOFEED_GRAPH_PAGES_80
    return kernel_pages80(cfg, st, tid);
#elif BUSINESS_KIND == GOFEED_GRAPH_48M_RANDOM
    return kernel_shared_random(cfg, st, tid);
#elif BUSINESS_KIND == GOFEED_SHARED_HOTSPOT
    return kernel_hotspot(cfg, st, tid);
#elif BUSINESS_KIND == PYTORCH_DENSE_BATCH
    return kernel_dense(cfg, st, tid);
#elif BUSINESS_KIND == PYTORCH_EMBEDDING_768K || BUSINESS_KIND == PYTORCH_EMBEDDING_1536K
    return kernel_embedding(cfg, st, tid, 0);
#elif BUSINESS_KIND == PYTORCH_EMBEDDING_48M
    return kernel_embedding(cfg, st, tid, 1);
#elif BUSINESS_KIND == MYSQL_TABLE_96M_SHARED
    return kernel_mysql_table(cfg, st, tid);
#else
    return kernel_private_index(cfg, st, tid);
#endif
}

static void try_pin_cpu(int tid)
{
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(tid, &set);
    if (pthread_setaffinity_np(pthread_self(), sizeof(set), &set) != 0 && !g_disable_m5)
        fprintf(stderr, "%s: affinity tid=%d failed: %s\n", BUSINESS_NAME, tid, strerror(errno));
}

static void *worker_main(void *opaque)
{
    worker_arg_t *arg = (worker_arg_t *)opaque;
    try_pin_cpu(arg->tid);
    pthread_barrier_wait(arg->barrier);
    m5_work_begin_inline(0, (uint64_t)arg->tid);
    arg->checksum = dispatch(arg->cfg, arg->state, arg->tid);
    arg->state->result[arg->tid][0] = arg->checksum;
    m5_work_end_inline(0, (uint64_t)arg->tid);
    m5_quiesce_inline();
    return NULL;
}

int main(int argc, char **argv)
{
    bench_cfg_t cfg;
    bench_state_t state;
    pthread_t threads[MAX_THREADS];
    worker_arg_t args[MAX_THREADS];
    pthread_barrier_t barrier;
    uint64_t total = 0;
    g_disable_m5 = getenv("TAO_DISABLE_M5") != NULL;
    parse_args(argc, argv, &cfg);
    if (init_state(&cfg, &state) != 0) {
        fprintf(stderr, "%s: allocation failed\n", BUSINESS_NAME);
        return 1;
    }
    if (pthread_barrier_init(&barrier, NULL, (unsigned)cfg.nthreads) != 0) return 1;
    for (int tid = 0; tid < cfg.nthreads; ++tid) {
        args[tid] = (worker_arg_t){.cfg = &cfg, .state = &state, .barrier = &barrier,
                                  .tid = tid, .checksum = 0};
        if (tid > 0 && pthread_create(&threads[tid], NULL, worker_main, &args[tid]) != 0)
            return 1;
    }
    worker_main(&args[0]);
    for (int tid = 1; tid < cfg.nthreads; ++tid) pthread_join(threads[tid], NULL);
    for (int tid = 0; tid < cfg.nthreads; ++tid) total ^= args[tid].checksum + (uint64_t)tid;
    pthread_barrier_destroy(&barrier);
    free(state.private_rw);
    free(state.shared_ro);
    g_sink ^= total;
    fprintf(stderr, "%s: done checksum=%llu\n", BUSINESS_NAME,
            (unsigned long long)total);
    return 0;
}
