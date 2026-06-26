/* bench_mlp_light — high-MLP, cache-resident, short-dependency light load.
 *
 * Fills a PMU-space gap: the model learns "high MSHR depth => high CPI" from
 * int_div / indirect (which carry long-latency divide / hard-to-predict
 * indirect-branch stalls), then over-predicts CPI for workloads that have
 * many in-flight loads but resolve them quickly in L1/L2.  ads_ctr's light
 * tail region (high MSHR ~13, near-zero miss, zero DRAM, CPI ~1.4) lives in
 * exactly that gap.
 *
 * Shape:
 *   - many INDEPENDENT loads per iteration over small L1/L2-resident tables
 *     => deep MSHR / high memory-level parallelism, but every access hits
 *   - no pointer chasing (independent index streams, not x.next), so the
 *     dependency chain stays short and the OoO window fills with parallel work
 *   - no integer divide, no indirect branches, no fp
 *   - target steady-state CPI ~1.0-1.5
 *
 * scale controls iteration count.
 */
#include "tao_bench.h"

static uint64_t g_sink[TAO_MAX_THREADS] __attribute__((aligned(TAO_LINE)));

/* number of independent dependency-chain lanes.  Few lanes => limited MLP =>
 * the per-lane load-to-use latency is only partly hidden => CPI rises toward
 * the ads_ctr-tail target (~1.4) while every access still hits L1. */
#define LANES 4

static uint64_t mix(uint64_t x)
{
    x ^= x >> 30;
    x *= 0xbf58476d1ce4e5b9ULL;
    x ^= x >> 27;
    x *= 0x94d049bb133111ebULL;
    x ^= x >> 31;
    return x;
}

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads;
    (void)shared;

    /* One small L1-resident table (8 KiB << 32 KiB L1D) so every access HITS
     * L1 (near-zero miss, zero DRAM).  CPI is lifted not by misses but by a
     * per-lane pointer-chase dependency chain (load-to-use), with few lanes so
     * the latency is only partly hidden by MLP. */
    const size_t n = 1024;          /* 8 KiB (uint64), L1-resident */
    uint64_t *tab = (uint64_t *)tao_xaligned(n * sizeof(uint64_t));

    uint64_t seed = tao_seed_or(tid, 0,
        0x243f6a8885a308d3ULL ^ ((uint64_t)tid << 33));
    /* each slot stores the NEXT index to visit -> pointer chase that stays
     * inside the L1-resident table (always hits) but serializes per lane. */
    for (size_t i = 0; i < n; ++i)
        tab[i] = mix(seed + i * 0x9e3779b97f4a7c15ULL) & (n - 1);

    long iters = scale * 11000;
    if (iters < 11000) iters = 11000;

    uint64_t p[LANES];
    for (int l = 0; l < LANES; ++l)
        p[l] = mix(seed + (uint64_t)l * 0x9e37u) & (n - 1);
    uint64_t acc = seed | 1u;

    /* init 完成 -> 全员到齐 -> 进 ROI，开始 chase hot loop */
    tao_phase_sync();
    tao_roi_begin();

    for (long it = 0; it < iters; ++it) {
        uint64_t sum = 0;
        /* LANES independent chains; within a lane the next address depends on
         * the value just loaded (load-to-use), so it serializes; across lanes
         * they are independent -> modest MLP (MSHR ~1). */
        for (int l = 0; l < LANES; ++l) {
            uint64_t nxt = tab[p[l]];
            sum += nxt;
            p[l] = nxt & (n - 1);       /* dependency: next addr from load */
        }
        /* small store stream into the same L1 table (stays a hit). */
        tab[it & (n - 1)] ^= (sum << 1) | 1u;
        acc += sum + (uint64_t)it;
        asm volatile("" ::: "memory");
    }

    tao_roi_end();

    g_sink[tid] = acc;
    free(tab);
}

TAO_BENCH_MAIN_KERNEL_ROI("mlp_light", kernel, NULL)
