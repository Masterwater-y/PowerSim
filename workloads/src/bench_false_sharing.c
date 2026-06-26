/* bench_false_sharing — false sharing：各线程写同一 cacheline 的不同字。
 * 制造 coherence invalidation 风暴（M->I 反复弹跳）。高 inv_recv/inv_send。
 * scale = 迭代×1000。shared = 1 个 cacheline 共所有线程争抢。
 *
 * §12 phase 切换：N_PHASE=8 段，每段切换 (slot_offset, neigh_mod)，制造
 * 不同强度的 cacheline bouncing 模式。与 seed 完全解耦。 */
#include "tao_bench.h"

#define FS_N_PHASE 8

static size_t shbytes(int nthreads, long scale) { (void)nthreads; (void)scale; return TAO_LINE * 4; }

/* 每 phase 的 slot 偏移：让 thread tid 在不同 phase 落到不同字段。 */
static const int PH_SLOT_OFFSET[FS_N_PHASE] = {0, 4, 2, 6, 1, 5, 3, 7};
/* 每 phase 的 neighbor mod：决定 bouncing 半径（小=同 16B group 内争抢，大=跨 group）。 */
static const int PH_NEIGH_MOD[FS_N_PHASE] = {8, 4, 8, 4, 2, 8, 4, 2};

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    long iters = scale * 1000;
    long iters_per_phase = iters / FS_N_PHASE;
    if (iters_per_phase < 1) iters_per_phase = 1;
    volatile long *line = (volatile long *)shared;
    /* seed 入口：tid 0 在 hot loop 前写 8 路非零初值；seed=0 时跳过，
     * line 全 0（calloc by tao_xaligned），与旧版 init 行为完全一致。 */
    if (tid == 0 && g_tao_seed != 0) {
        for (int s = 0; s < 8; s++) {
            line[s] = (long)tao_seed_mix(s, 0);
        }
        __sync_synchronize();
    }
    int nm_cap = (nthreads > 8) ? 8 : nthreads;
    for (int ph = 0; ph < FS_N_PHASE; ph++) {
        int slot = (tid + PH_SLOT_OFFSET[ph]) % 8;
        int neigh_mod = PH_NEIGH_MOD[ph];
        if (neigh_mod > nm_cap) neigh_mod = nm_cap;
        if (neigh_mod < 1) neigh_mod = 1;
        for (long i = 0; i < iters_per_phase; i++) {
            line[slot] += i;             /* 同行内字段，coherence 弹跳 */
            line[slot] ^= line[(slot + 1) % neigh_mod];
        }
    }
}

TAO_BENCH_MAIN("false_sharing", kernel, shbytes)
