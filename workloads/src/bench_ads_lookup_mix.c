/* bench_ads_lookup_mix — ads-like cache-resident sparse lookup workload.
 *
 * This is a training-only companion for ads_ctr validation traces.  It avoids
 * reusing the validation implementation while covering the same missing shape:
 * hash/probe feature lookup, branch-heavy filtering, low/medium stores, and a
 * working-set sweep from L1/L2 to LLC-sized tables.
 *
 * scale controls request count.  The inner loop cycles through three table
 * sizes and probe depths, so one trace covers a small distribution instead of a
 * single point.
 */
#include "tao_bench.h"

typedef struct {
    uint32_t key;
    uint32_t val;
} entry_t;

static uint64_t g_sink[TAO_MAX_THREADS] __attribute__((aligned(TAO_LINE)));

static uint64_t mix64(uint64_t x)
{
    x ^= x >> 33;
    x *= 0xff51afd7ed558ccdULL;
    x ^= x >> 33;
    x *= 0xc4ceb9fe1a85ec53ULL;
    x ^= x >> 33;
    return x;
}

static void fill_table(entry_t *tab, size_t mask, uint64_t seed)
{
    for (size_t i = 0; i <= mask; ++i) {
        uint64_t h = mix64(seed + i * 0x9e3779b97f4a7c15ULL);
        tab[i].key = (uint32_t)h | 1u;
        tab[i].val = (uint32_t)(h >> 32);
    }
}

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    (void)nthreads;
    (void)shared;

    const size_t n_l1 = 2048;       /* 16 KiB */
    const size_t n_l2 = 32768;      /* 256 KiB */
    const size_t n_llc = 524288;    /* 4 MiB */
    entry_t *l1 = (entry_t *)tao_xaligned(n_l1 * sizeof(entry_t));
    entry_t *l2 = (entry_t *)tao_xaligned(n_l2 * sizeof(entry_t));
    entry_t *llc = (entry_t *)tao_xaligned(n_llc * sizeof(entry_t));

    uint64_t seed = 0x6a09e667f3bcc909ULL ^ ((uint64_t)tid << 32);
    fill_table(l1, n_l1 - 1, seed ^ 1);
    fill_table(l2, n_l2 - 1, seed ^ 2);
    fill_table(llc, n_llc - 1, seed ^ 3);

    long reqs = scale * 360;
    if (reqs < 360) reqs = 360;

    /* init 完成 -> 全员到齐 -> 进 ROI，开始 hot lookup loop */
    tao_phase_sync();
    tao_roi_begin();

    uint64_t acc = seed | 1u;
    uint64_t accept = 0;
    for (long r = 0; r < reqs; ++r) {
        int phase = (int)(r % 6);
        entry_t *tab;
        size_t mask;
        int probes;
        uint32_t threshold;

        if (phase < 2) {
            tab = l1; mask = n_l1 - 1; probes = 2; threshold = 0x30000000u;
        } else if (phase < 4) {
            tab = l2; mask = n_l2 - 1; probes = 4; threshold = 0x70000000u;
        } else {
            tab = llc; mask = n_llc - 1; probes = 8; threshold = 0xa0000000u;
        }

        uint64_t score = acc + (uint64_t)r * 0x9e3779b97f4a7c15ULL;
        for (int s = 0; s < 14; ++s) {
            acc = mix64(acc + (uint64_t)s + (uint64_t)phase);
            size_t pos = (size_t)acc & mask;
            uint32_t want = (uint32_t)(acc | 1u);
            uint32_t found = 0;
            for (int p = 0; p < probes; ++p) {
                entry_t e = tab[(pos + (size_t)p) & mask];
                if (((e.key ^ want) & 0xffu) == 0u) {
                    found = e.val;
                    break;
                }
                score += (uint64_t)e.val & 31u;
            }
            if (found > threshold) {
                score += (uint64_t)found * (uint64_t)(s + 3);
                accept++;
            } else if ((found ^ (uint32_t)score) & 8u) {
                score ^= (uint64_t)found << (s & 15);
            } else {
                score += (score >> 7) + (uint64_t)(phase + s);
            }
        }

        if ((r & 31) == 0) {
            size_t pos = (size_t)(score ^ acc) & (n_l2 - 1);
            l2[pos].val ^= (uint32_t)(score >> 17);
        }
        acc = score ^ (accept * 0x100000001b3ULL);
        asm volatile("" ::: "memory");
    }

    tao_roi_end();

    g_sink[tid] = acc ^ accept;
    free(l1);
    free(l2);
    free(llc);
}

TAO_BENCH_MAIN_KERNEL_ROI("ads_lookup_mix", kernel, NULL)
