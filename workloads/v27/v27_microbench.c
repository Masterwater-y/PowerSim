/*
 * v27.0-cold16 microbenchmarks.
 *
 * ABI: ./workload <nthreads> <scale> 1 <seed>
 *
 * The ROI starts after allocation, first touch and thread pinning.  With the
 * collector's --ff-atomic recipe this deliberately makes the O3/Ruby ROI a
 * cold-start measurement.  Atomic/lock/barrier semantics are intentionally
 * absent from this initial workload contract.
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

#define V27_INT_ALU_DENSE       1
#define V27_INT_DIV_SERIAL      2
#define V27_FP_ALU_DENSE        3
#define V27_SIMD_SSE_DENSE      4
#define V27_STREAM_SEQ_L2       5
#define V27_STREAM_SEQ_DRAM     6
#define V27_RANDOM_DRAM         7
#define V27_CHASE_DRAM          8
#define V27_COH_READ_SHARE      9
#define V27_COH_WRITE_SHARE    10
#define V27_COH_FALSE_SHARE    11
#define V27_COH_ASYM_RW        12
#define V27_PHASE_WS_GROW      13
#define V27_PHASE_COH_ONSET    14
#define V27_SKEW_HOT_COLD      15
#define V27_RANKING_MIX_PRIVATE 16
#define V27_PHASE_WS_SHRINK    17
#define V27_PHASE_COH_DECAY    18

#ifndef V27_KIND
#error "V27_KIND must be supplied by the Makefile"
#endif
#ifndef V27_NAME
#define V27_NAME "v27_unknown"
#endif

#define V27_MAX_THREADS 32
#define LINE_BYTES 64UL
#define KiB 1024UL
#define MiB (1024UL * 1024UL)
#define L2_WS_PER_CORE (256UL * KiB)
#define DRAM_WS_PER_CORE (16UL * MiB)
#define RANDOM_DRAM_WS_PER_CORE (8UL * MiB)
#define CHASE_DRAM_WS_PER_CORE (6UL * MiB)
#define RANKING_MIX_WS_PER_CORE (8UL * MiB)
#define PHASE_WS_PER_CORE (8UL * MiB)

typedef struct {
    int nthreads;
    long scale;
    uint64_t seed;
} bench_cfg_t;

/* A pointer-chase node occupies one complete cache line. */
typedef struct chase_node {
    struct chase_node *next;
    uint8_t padding[LINE_BYTES - sizeof(struct chase_node *)];
} chase_node_t;

typedef struct {
    uint64_t *mem;
    size_t mem_words;
    size_t mem_words_per_core;
    chase_node_t *chase;
    size_t chase_nodes;
    size_t chase_nodes_per_core;
    uint32_t *random_order;
    size_t random_lines_per_core;

    volatile uint64_t shared_word __attribute__((aligned(LINE_BYTES)));
    volatile uint8_t false_line[LINE_BYTES] __attribute__((aligned(LINE_BYTES)));
    volatile uint64_t shared_region[64][8] __attribute__((aligned(LINE_BYTES)));
    uint64_t private_line[V27_MAX_THREADS][8] __attribute__((aligned(LINE_BYTES)));
} bench_state_t;

typedef struct {
    const bench_cfg_t *cfg;
    bench_state_t *state;
    pthread_barrier_t *start_barrier;
    int tid;
    uint64_t checksum;
} worker_arg_t;

static volatile uint64_t g_sink = 0;

static inline void m5_work_begin_inline(void)
{
    if (getenv("TAO_DISABLE_M5") != NULL) return;
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005a" : : : "memory");
}

static inline void m5_work_end_inline(void)
{
    if (getenv("TAO_DISABLE_M5") != NULL) return;
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005b" : : : "memory");
}

static uint64_t splitmix64(uint64_t *x)
{
    uint64_t z = (*x += UINT64_C(0x9e3779b97f4a7c15));
    z = (z ^ (z >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    z = (z ^ (z >> 27)) * UINT64_C(0x94d049bb133111eb);
    return z ^ (z >> 31);
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
    cfg->seed = UINT64_C(0x27d00d5eed);
    if (argc >= 2) cfg->nthreads = atoi(argv[1]);
    if (argc >= 3) cfg->scale = atol(argv[2]);
    if (argc >= 5) cfg->seed = (uint64_t)strtoull(argv[4], NULL, 0);
    else if (argc >= 4) cfg->seed = (uint64_t)strtoull(argv[3], NULL, 0);
    if (cfg->nthreads <= 0) cfg->nthreads = 1;
    if (cfg->nthreads > V27_MAX_THREADS) cfg->nthreads = V27_MAX_THREADS;
    if (cfg->scale <= 0) cfg->scale = 1;
    if (cfg->seed == 0) cfg->seed = UINT64_C(0x27d00d5eed);
}

static void try_pin_cpu(int tid)
{
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(tid, &set);
    if (pthread_setaffinity_np(pthread_self(), sizeof(set), &set) != 0) {
        fprintf(stderr, "%s: pthread_setaffinity_np(tid=%d) failed: %s\n",
                V27_NAME, tid, strerror(errno));
        abort();
    }
}

static void first_touch_u64(uint64_t *p, size_t words, uint64_t seed)
{
    uint64_t s = seed;
    for (size_t i = 0; i < words; ++i) p[i] = splitmix64(&s) | UINT64_C(1);
}

static size_t mem_bytes_per_core(void)
{
#if V27_KIND == V27_STREAM_SEQ_L2
    return L2_WS_PER_CORE;
#elif V27_KIND == V27_STREAM_SEQ_DRAM || V27_KIND == V27_SKEW_HOT_COLD
    return DRAM_WS_PER_CORE;
#elif V27_KIND == V27_RANDOM_DRAM
    return RANDOM_DRAM_WS_PER_CORE;
#elif V27_KIND == V27_RANKING_MIX_PRIVATE
    return RANKING_MIX_WS_PER_CORE;
#elif V27_KIND == V27_PHASE_WS_GROW || V27_KIND == V27_PHASE_WS_SHRINK
    return PHASE_WS_PER_CORE;
#else
    return 0;
#endif
}

static size_t chase_bytes_per_core(void)
{
#if V27_KIND == V27_CHASE_DRAM
    return CHASE_DRAM_WS_PER_CORE;
#else
    return 0;
#endif
}

static int init_state(const bench_cfg_t *cfg, bench_state_t *st)
{
    memset(st, 0, sizeof(*st));
    st->mem_words_per_core = mem_bytes_per_core() / sizeof(uint64_t);
    st->mem_words = st->mem_words_per_core * (size_t)cfg->nthreads;
    st->chase_nodes_per_core = chase_bytes_per_core() / sizeof(chase_node_t);
    st->chase_nodes = st->chase_nodes_per_core * (size_t)cfg->nthreads;

    if (st->mem_words > 0) {
        st->mem = aligned_zalloc(LINE_BYTES, st->mem_words * sizeof(uint64_t));
        if (!st->mem) return 1;
        first_touch_u64(st->mem, st->mem_words, cfg->seed);
    }
#if V27_KIND == V27_RANDOM_DRAM || V27_KIND == V27_RANKING_MIX_PRIVATE
    st->random_lines_per_core = st->mem_words_per_core / 8U;
    st->random_order = aligned_zalloc(
        LINE_BYTES,
        (size_t)cfg->nthreads * st->random_lines_per_core * sizeof(uint32_t)
    );
    if (!st->random_order) return 1;
    for (int tid = 0; tid < cfg->nthreads; ++tid) {
        uint32_t *order = st->random_order + (size_t)tid * st->random_lines_per_core;
        uint64_t rnd = cfg->seed ^ ((uint64_t)(tid + 1) * UINT64_C(0xd1b54a32d192ed03));
        for (size_t i = 0; i < st->random_lines_per_core; ++i) order[i] = (uint32_t)i;
        for (size_t i = st->random_lines_per_core; i > 1; --i) {
            size_t j = (size_t)(splitmix64(&rnd) % i);
            uint32_t tmp = order[i - 1];
            order[i - 1] = order[j];
            order[j] = tmp;
        }
    }
#endif
    if (st->chase_nodes > 0) {
        st->chase = aligned_zalloc(LINE_BYTES, st->chase_nodes * sizeof(chase_node_t));
        if (!st->chase) return 1;
        for (int tid = 0; tid < cfg->nthreads; ++tid) {
            size_t base = (size_t)tid * st->chase_nodes_per_core;
            size_t n = st->chase_nodes_per_core;
            size_t stride = 65537U % n;
            if ((stride & 1U) == 0) stride++;
            for (size_t i = 0; i < n; ++i)
                st->chase[base + i].next = &st->chase[base + ((i + stride) % n)];
        }
    }
    for (int t = 0; t < V27_MAX_THREADS; ++t)
        for (int j = 0; j < 8; ++j)
            st->private_line[t][j] = (uint64_t)(t + 1) * 1315423911U + (uint64_t)j;
    return 0;
}

static uint64_t kernel_int_alu(int tid, long scale)
{
    uint64_t x = (uint64_t)(tid + 1) * UINT64_C(0x123456789abcdef);
    uint64_t y = x ^ UINT64_C(0x9e3779b97f4a7c15);
    uint64_t z = y + UINT64_C(0xbf58476d1ce4e5b9);
    uint64_t w = z ^ UINT64_C(0x94d049bb133111eb);
    for (long i = 0; i < scale * 4096L; ++i) {
        x = x * UINT64_C(2862933555777941757) + (uint64_t)i + 1U;
        y ^= (x >> 17) + (y << 7);
        z += (y ^ (z << 9)) + UINT64_C(0x51ed270b);
        w = (w + z) ^ (x >> 11);
    }
    return x ^ y ^ z ^ w;
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
    double a0 = 0.001 * (double)(tid + 1), a1 = 0.002 * (double)(tid + 1);
    double a2 = 0.003 * (double)(tid + 1), a3 = 0.004 * (double)(tid + 1);
    const double m0 = 1.0000001, m1 = 0.9999997;
    for (long i = 0; i < scale * 4096L; ++i) {
        a0 = a0 * m0 + a1;
        a1 = a1 * m1 + a2;
        a2 = a2 * m0 + a3;
        a3 = a3 * m1 + a0;
    }
    return (uint64_t)((a0 + a1 + a2 + a3) * 1000003.0);
}

__attribute__((target("sse2")))
static uint64_t kernel_simd_sse(int tid, long scale)
{
    __m128 a0 = _mm_set1_ps(0.001f * (float)(tid + 1));
    __m128 a1 = _mm_set1_ps(0.002f * (float)(tid + 1));
    __m128 a2 = _mm_set1_ps(0.003f * (float)(tid + 1));
    __m128 a3 = _mm_set1_ps(0.004f * (float)(tid + 1));
    const __m128 m = _mm_set1_ps(0.99999f), c = _mm_set1_ps(1.00001f);
    float out[4] __attribute__((aligned(16)));
    for (long i = 0; i < scale * 4096L; ++i) {
        a0 = _mm_add_ps(_mm_mul_ps(a0, m), c);
        a1 = _mm_add_ps(_mm_mul_ps(a1, c), m);
        a2 = _mm_add_ps(_mm_mul_ps(a2, m), a0);
        a3 = _mm_add_ps(_mm_mul_ps(a3, c), a1);
    }
    _mm_store_ps(out, _mm_add_ps(_mm_add_ps(a0, a1), _mm_add_ps(a2, a3)));
    return (uint64_t)(out[0] + out[1] + out[2] + out[3]);
}

static uint64_t stream_read_lines(const bench_state_t *st, int tid, size_t lines, long passes)
{
    volatile const uint64_t *base = st->mem + (size_t)tid * st->mem_words_per_core;
    uint64_t a = (uint64_t)tid + 1U, b = a ^ UINT64_C(0x9e3779b97f4a7c15);
    for (long pass = 0; pass < passes; ++pass) {
        volatile const uint64_t *p = base;
        volatile const uint64_t *end = base + lines * 8U;
        while (p < end) {
            a += p[0];
            b ^= p[8];
            a += p[16];
            b ^= p[24];
            p += 32;
        }
    }
    return a ^ b;
}

static uint64_t stream_ops(const bench_cfg_t *cfg, bench_state_t *st, int tid, size_t ops)
{
    size_t n = st->mem_words_per_core;
    size_t base = (size_t)tid * n;
    uint64_t acc = (uint64_t)tid + 1U;
    for (size_t i = 0; i < ops; ++i) {
        size_t idx = base + ((i * 8U) % n); /* one 64B cache line per step */
        uint64_t v = st->mem[idx];
        st->mem[idx] = v + acc + i;
        acc += v;
    }
    (void)cfg;
    return acc;
}

static uint64_t kernel_stream_seq(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
#if V27_KIND == V27_STREAM_SEQ_L2
    /* 32 full passes keep the small L2 mechanism inside the shared ROI budget. */
    return stream_read_lines(st, tid, st->mem_words_per_core / 8U, cfg->scale * 32L);
#else
    /* One complete 16 MiB line traversal: no ROI-time modulo or RMW overhead. */
    return stream_read_lines(st, tid, st->mem_words_per_core / 8U, cfg->scale);
#endif
}

static uint64_t kernel_random_dram(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    size_t lines = st->random_lines_per_core;
    size_t base = (size_t)tid * st->mem_words_per_core;
    const uint32_t *order = st->random_order + (size_t)tid * lines;
    volatile const uint64_t *mem = st->mem + base;
    uint64_t a = 0, b = UINT64_C(0x9e3779b97f4a7c15);
    for (long pass = 0; pass < cfg->scale; ++pass) {
        for (size_t i = 0; i < lines; i += 4U) {
            a += mem[(size_t)order[i] * 8U];
            b ^= mem[(size_t)order[i + 1U] * 8U];
            a += mem[(size_t)order[i + 2U] * 8U];
            b ^= mem[(size_t)order[i + 3U] * 8U];
        }
    }
    (void)cfg;
    return a ^ b;
}

static uint64_t kernel_chase(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    size_t base = (size_t)tid * st->chase_nodes_per_core;
    chase_node_t *node = &st->chase[base + ((size_t)(tid * 104729U) % st->chase_nodes_per_core)];
    uint64_t acc = (uint64_t)(uintptr_t)node;
    for (size_t i = 0; i < (size_t)cfg->scale * st->chase_nodes_per_core; ++i) {
        node = node->next;
        acc ^= (uint64_t)(uintptr_t)node;
    }
    return acc;
}

static uint64_t kernel_coh_read_share(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    uint64_t acc = 0;
    for (long i = 0; i < cfg->scale * 8192L; ++i)
        acc += st->shared_region[(i + tid) & 63L][(i >> 3) & 7L];
    return acc;
}

static uint64_t kernel_coh_write_share(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    uint64_t acc = 0;
    for (long i = 0; i < cfg->scale * 8192L; ++i) {
        st->shared_word = st->shared_word + (uint64_t)i + (uint64_t)tid;
        acc ^= st->shared_word;
    }
    return acc;
}

static uint64_t kernel_coh_false_share(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    volatile uint8_t *slot = &st->false_line[tid];
    uint64_t acc = 0;
    for (long i = 0; i < cfg->scale * 8192L; ++i) {
        *slot = (uint8_t)(*slot + (uint8_t)i + 1U);
        acc += *slot;
    }
    return acc;
}

static uint64_t kernel_coh_asym_rw(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    uint64_t acc = 0;
    for (long i = 0; i < cfg->scale * 8192L; ++i) {
        size_t row = (size_t)i & 63U;
        if (tid == 0) {
            st->shared_region[row][0] = (uint64_t)i + 1U;
            st->shared_region[row][1] = (uint64_t)i ^ UINT64_C(0x5a5a5a5a);
        } else {
            acc += st->shared_region[row][0];
            acc ^= st->shared_region[row][1];
        }
    }
    return acc;
}

static uint64_t kernel_phase_ws(const bench_cfg_t *cfg, bench_state_t *st, int tid, int grow)
{
    const size_t min_lines = (8UL * KiB) / LINE_BYTES;
    uint64_t acc = (uint64_t)tid + 1U;
    static const int shifts[] = {0, 2, 4, 6, 8, 10};
    for (size_t stage = 0; stage < sizeof(shifts) / sizeof(shifts[0]); ++stage) {
        size_t pos = grow ? stage : (sizeof(shifts) / sizeof(shifts[0]) - 1U - stage);
        size_t lines = min_lines << shifts[pos];
        /* Visit every 8 KiB--8 MiB stage once; retain an extra 4 MiB dwell
         * without exceeding the fixed cold-trace budget at scale=1. */
        const long passes = (shifts[pos] == 8) ? 2L : 1L;
        acc ^= stream_read_lines(st, tid, lines, cfg->scale * passes);
    }
    return acc;
}

static uint64_t kernel_phase_coh(const bench_cfg_t *cfg, bench_state_t *st, int tid, int onset)
{
    long half = cfg->scale * 8192L;
    uint64_t acc = 0;
    if (onset) {
        acc ^= kernel_int_alu(tid, cfg->scale);
        acc ^= kernel_coh_write_share(&(bench_cfg_t){.nthreads=cfg->nthreads, .scale=half / 8192L, .seed=cfg->seed}, st, tid);
    } else {
        acc ^= kernel_coh_write_share(&(bench_cfg_t){.nthreads=cfg->nthreads, .scale=half / 8192L, .seed=cfg->seed}, st, tid);
        acc ^= kernel_int_alu(tid, cfg->scale);
    }
    return acc;
}

static uint64_t kernel_skew_hot_cold(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    int hot = cfg->nthreads / 4;
    if (hot < 1) hot = 1;
    if (tid < hot) return stream_ops(cfg, st, tid, (size_t)cfg->scale * 4096U);
    return kernel_int_alu(tid, cfg->scale);
}

static uint64_t kernel_ranking_mix_private(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
    /* Ads-ranking-like private request streams: every core executes the same
     * number of random gathers and dense integer mixing steps.  Per-core seed
     * changes the line/branch sequence, so any CPI skew comes from cold cache
     * and shared LLC/DRAM contention rather than unequal assigned work. */
    const size_t lines = st->random_lines_per_core;
    const uint32_t *order = st->random_order + (size_t)tid * lines;
    volatile const uint64_t *mem = st->mem + (size_t)tid * st->mem_words_per_core;
    size_t ops = (size_t)cfg->scale * 8192U;
    if (ops + 2U > lines) ops = lines - 2U;
    uint64_t score = UINT64_C(0x9e3779b97f4a7c15) ^ (uint64_t)(tid + 1);
    uint64_t cross = score ^ UINT64_C(0xd1b54a32d192ed03);
    for (size_t i = 0; i < ops; ++i) {
        const uint64_t u = mem[(size_t)order[i] * 8U];
        const uint64_t v = mem[(size_t)order[i + 1U] * 8U];
        cross = (cross ^ u) * UINT64_C(0xbf58476d1ce4e5b9) + v;
        if ((cross & UINT64_C(0x80)) != 0)
            score += (u ^ (v >> 7)) + (cross << 3);
        else
            score ^= (v + (u << 11)) ^ (cross >> 5);
    }
    return score ^ cross;
}

static uint64_t dispatch_kernel(const bench_cfg_t *cfg, bench_state_t *st, int tid)
{
#if V27_KIND == V27_INT_ALU_DENSE
    return kernel_int_alu(tid, cfg->scale);
#elif V27_KIND == V27_INT_DIV_SERIAL
    return kernel_int_div_serial(tid, cfg->scale);
#elif V27_KIND == V27_FP_ALU_DENSE
    return kernel_fp_alu(tid, cfg->scale);
#elif V27_KIND == V27_SIMD_SSE_DENSE
    return kernel_simd_sse(tid, cfg->scale);
#elif V27_KIND == V27_STREAM_SEQ_L2 || V27_KIND == V27_STREAM_SEQ_DRAM
    return kernel_stream_seq(cfg, st, tid);
#elif V27_KIND == V27_RANDOM_DRAM
    return kernel_random_dram(cfg, st, tid);
#elif V27_KIND == V27_CHASE_DRAM
    return kernel_chase(cfg, st, tid);
#elif V27_KIND == V27_COH_READ_SHARE
    return kernel_coh_read_share(cfg, st, tid);
#elif V27_KIND == V27_COH_WRITE_SHARE
    return kernel_coh_write_share(cfg, st, tid);
#elif V27_KIND == V27_COH_FALSE_SHARE
    return kernel_coh_false_share(cfg, st, tid);
#elif V27_KIND == V27_COH_ASYM_RW
    return kernel_coh_asym_rw(cfg, st, tid);
#elif V27_KIND == V27_PHASE_WS_GROW
    return kernel_phase_ws(cfg, st, tid, 1);
#elif V27_KIND == V27_PHASE_COH_ONSET
    return kernel_phase_coh(cfg, st, tid, 1);
#elif V27_KIND == V27_SKEW_HOT_COLD
    return kernel_skew_hot_cold(cfg, st, tid);
#elif V27_KIND == V27_RANKING_MIX_PRIVATE
    return kernel_ranking_mix_private(cfg, st, tid);
#elif V27_KIND == V27_PHASE_WS_SHRINK
    return kernel_phase_ws(cfg, st, tid, 0);
#elif V27_KIND == V27_PHASE_COH_DECAY
    return kernel_phase_coh(cfg, st, tid, 0);
#else
#error "unknown V27_KIND"
#endif
}

static void *worker_main(void *argp)
{
    worker_arg_t *arg = (worker_arg_t *)argp;
    try_pin_cpu(arg->tid);
    pthread_barrier_wait(arg->start_barrier);
    m5_work_begin_inline();
    arg->checksum = dispatch_kernel(arg->cfg, arg->state, arg->tid);
    m5_work_end_inline();
    return NULL;
}

int main(int argc, char **argv)
{
    bench_cfg_t cfg;
    bench_state_t state;
    pthread_t th[V27_MAX_THREADS];
    worker_arg_t args[V27_MAX_THREADS];
    pthread_barrier_t start_barrier;
    uint64_t total = 0;
    parse_args(argc, argv, &cfg);
    if (init_state(&cfg, &state) != 0) {
        fprintf(stderr, "%s: allocation failed\n", V27_NAME);
        return 1;
    }
    if (pthread_barrier_init(&start_barrier, NULL, (unsigned)cfg.nthreads) != 0) return 1;
    for (int tid = 0; tid < cfg.nthreads; ++tid) {
        args[tid] = (worker_arg_t){.cfg=&cfg, .state=&state, .start_barrier=&start_barrier, .tid=tid, .checksum=0};
        if (tid > 0 && pthread_create(&th[tid], NULL, worker_main, &args[tid]) != 0) return 1;
    }
    worker_main(&args[0]);
    for (int tid = 1; tid < cfg.nthreads; ++tid) pthread_join(th[tid], NULL);
    for (int tid = 0; tid < cfg.nthreads; ++tid) total ^= args[tid].checksum + (uint64_t)tid;
    pthread_barrier_destroy(&start_barrier);
    free(state.mem);
    free(state.chase);
    free(state.random_order);
    g_sink ^= total;
    fprintf(stderr, "%s: done checksum=%llu sink=%llu\n", V27_NAME,
            (unsigned long long)total, (unsigned long long)g_sink);
    return 0;
}
