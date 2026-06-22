/* bench_lookup_latency_ladder — same lookup loop across cache levels.
 *
 * The model currently needs examples where random-looking load/hash activity
 * is not always DRAM-slow.  This workload alternates dependent and independent
 * lookups over L1, L2, LLC, and DRAM-sized tables while keeping the operation
 * skeleton stable.
 */
#include "tao_bench.h"

typedef struct {
    uint64_t next;
    uint64_t payload;
} node_t;

static uint64_t g_sink[TAO_MAX_THREADS] __attribute__((aligned(TAO_LINE)));

static uint64_t mix(uint64_t x)
{
    x ^= x >> 30;
    x *= 0xbf58476d1ce4e5b9ULL;
    x ^= x >> 27;
    x *= 0x94d049bb133111ebULL;
    x ^= x >> 31;
    return x;
}

static void init_nodes(node_t *a, size_t n, uint64_t seed)
{
    for (size_t i = 0; i < n; ++i) {
        uint64_t h = mix(seed + i);
        a[i].next = h & (n - 1);
        a[i].payload = h ^ (i * 0x9e3779b97f4a7c15ULL);
    }
}

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads;
    (void)shared;

    const size_t n_l1 = 1024;       /* 16 KiB */
    const size_t n_l2 = 16384;      /* 256 KiB */
    const size_t n_llc = 262144;    /* 4 MiB */
    const size_t n_dram = 1048576;  /* 16 MiB */
    node_t *l1 = (node_t *)tao_xaligned(n_l1 * sizeof(node_t));
    node_t *l2 = (node_t *)tao_xaligned(n_l2 * sizeof(node_t));
    node_t *llc = (node_t *)tao_xaligned(n_llc * sizeof(node_t));
    node_t *dram = (node_t *)tao_xaligned(n_dram * sizeof(node_t));

    uint64_t seed = 0x3c6ef372fe94f82bULL ^ ((uint64_t)tid << 36);
    init_nodes(l1, n_l1, seed ^ 1);
    init_nodes(l2, n_l2, seed ^ 2);
    init_nodes(llc, n_llc, seed ^ 3);
    init_nodes(dram, n_dram, seed ^ 4);

    long blocks = scale * 950;
    if (blocks < 950) blocks = 950;

    uint64_t p1 = (uint64_t)tid & (n_l1 - 1);
    uint64_t p2 = ((uint64_t)tid * 17u) & (n_l2 - 1);
    uint64_t p3 = ((uint64_t)tid * 257u) & (n_llc - 1);
    uint64_t p4 = ((uint64_t)tid * 4099u) & (n_dram - 1);
    uint64_t acc = seed;

    /* init 完成 -> 全员到齐 -> 进 ROI，开始 latency-ladder hot loop */
    tao_phase_sync();
    tao_roi_begin();

    for (long b = 0; b < blocks; ++b) {
        int phase = (int)(b & 7);
        int dependent = (phase == 1 || phase == 3 || phase >= 6);
        int reps = (phase < 2) ? 24 : (phase < 4) ? 18 : (phase < 6) ? 12 : 8;

        for (int r = 0; r < reps; ++r) {
            if (phase < 2) {
                node_t x = l1[p1 & (n_l1 - 1)];
                acc += x.payload;
                p1 = dependent ? x.next : mix(acc + (uint64_t)r);
            } else if (phase < 4) {
                node_t x = l2[p2 & (n_l2 - 1)];
                acc ^= x.payload + (acc << 5);
                p2 = dependent ? x.next : mix(acc + (uint64_t)b);
            } else if (phase < 6) {
                node_t x = llc[p3 & (n_llc - 1)];
                acc += x.payload ^ (acc >> 7);
                p3 = dependent ? x.next : mix(acc + (uint64_t)(b + r));
            } else {
                node_t x = dram[p4 & (n_dram - 1)];
                acc ^= x.payload + 0x9e3779b97f4a7c15ULL;
                p4 = dependent ? x.next : mix(acc);
            }

            if ((r & 15) == 0 && phase < 6) {
                l2[(acc >> 8) & (n_l2 - 1)].payload ^= acc;
            }
        }
        asm volatile("" ::: "memory");
    }

    tao_roi_end();

    g_sink[tid] = acc ^ p1 ^ p2 ^ p3 ^ p4;
    free(l1);
    free(l2);
    free(llc);
    free(dram);
}

TAO_BENCH_MAIN_KERNEL_ROI("lookup_latency_ladder", kernel, NULL)
