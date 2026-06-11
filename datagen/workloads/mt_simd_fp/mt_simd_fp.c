/*
 * mt_simd_fp.c — SSE2 SIMD + FP 多核负载（gem5 X86 兼容）
 *
 * 设计目标：
 *   - SSE2 128-bit mulps/addps/divps 长依赖链 → 覆盖 SIMD + FP path
 *   - 8 路独立链并行执行，掩盖 mul/add 4-5 cyc 延迟同时填满 SIMD 端口
 *   - 工作集 ~256B/thread，永远 L1-hit，专注计算
 *   - **AVX2/FMA 在 gem5 X86 SE 模式不支持**，故降到 SSE2
 *
 * ROI 内**严禁同步**：4 thread 独立寄存器流。
 *
 * 用法：./mt_simd_fp <nthreads> <iter_unit>
 *   推荐：./mt_simd_fp 4 400
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <emmintrin.h>  /* SSE2 */

static inline void m5_work_begin_inline(void)
{
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005a" : : : "memory");
}
static inline void m5_work_end_inline(void)
{
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005b" : : : "memory");
}

#define MAX_THREADS 16
#define LINE_BYTES  64

static int  g_nthreads = 4;
static long g_iter     = 400;

/* per-thread 累加器（写回用） */
static float g_priv[MAX_THREADS][32] __attribute__((aligned(LINE_BYTES)));

__attribute__((target("sse2")))
static float simd_fp_kernel(int tid, long iters)
{
    /* 8 个独立 128-bit (4×float) 链，掩盖 mul/add 延迟 */
    __m128 a0 = _mm_set1_ps(0.001f * (tid + 1));
    __m128 a1 = _mm_set1_ps(0.002f * (tid + 1));
    __m128 a2 = _mm_set1_ps(0.003f * (tid + 1));
    __m128 a3 = _mm_set1_ps(0.004f * (tid + 1));
    __m128 a4 = _mm_set1_ps(0.005f * (tid + 1));
    __m128 a5 = _mm_set1_ps(0.006f * (tid + 1));
    __m128 a6 = _mm_set1_ps(0.007f * (tid + 1));
    __m128 a7 = _mm_set1_ps(0.008f * (tid + 1));

    const __m128 b = _mm_set1_ps(0.99999f);
    const __m128 c = _mm_set1_ps(1.00001f);

    for (long i = 0; i < iters; i++) {
        /* mulps + addps = "FMA-like" 长依赖链 */
        a0 = _mm_add_ps(_mm_mul_ps(a0, b), c);
        a1 = _mm_add_ps(_mm_mul_ps(a1, b), c);
        a2 = _mm_add_ps(_mm_mul_ps(a2, b), c);
        a3 = _mm_add_ps(_mm_mul_ps(a3, b), c);
        a4 = _mm_add_ps(_mm_mul_ps(a4, b), c);
        a5 = _mm_add_ps(_mm_mul_ps(a5, b), c);
        a6 = _mm_add_ps(_mm_mul_ps(a6, b), c);
        a7 = _mm_add_ps(_mm_mul_ps(a7, b), c);
        /* 周期性 divps：SIMD FP_DIV 长延迟样本 */
        if ((i & 0x3F) == 0) {
            a0 = _mm_div_ps(a0, c);
            a1 = _mm_div_ps(a1, c);
        }
    }

    /* 把 8 链折叠回去防止 DCE */
    __m128 s = _mm_add_ps(_mm_add_ps(_mm_add_ps(a0, a1), _mm_add_ps(a2, a3)),
                          _mm_add_ps(_mm_add_ps(a4, a5), _mm_add_ps(a6, a7)));
    _mm_store_ps(g_priv[tid], s);
    return g_priv[tid][0] + g_priv[tid][1] + g_priv[tid][2] + g_priv[tid][3];
}

typedef struct { int tid; float cs; } warg_t;

static void *worker(void *p)
{
    warg_t *a = (warg_t *)p;
    m5_work_begin_inline();
    a->cs = simd_fp_kernel(a->tid, g_iter * 5);
    m5_work_end_inline();
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc >= 2) g_nthreads = atoi(argv[1]);
    if (argc >= 3) g_iter     = atol(argv[2]);
    if (g_nthreads <= 0 || g_nthreads > MAX_THREADS) g_nthreads = 4;
    if (g_iter    <= 0)                              g_iter    = 400;

    fprintf(stderr, "mt_simd_fp: nthreads=%d iter_unit=%ld\n",
            g_nthreads, g_iter);

    pthread_t ths[MAX_THREADS];
    warg_t   args[MAX_THREADS];
    for (int i = 0; i < g_nthreads; i++) {
        args[i].tid = i; args[i].cs = 0.0f;
        pthread_create(&ths[i], NULL, worker, &args[i]);
    }
    float total = 0.0f;
    for (int i = 0; i < g_nthreads; i++) {
        pthread_join(ths[i], NULL);
        total += args[i].cs;
    }
    fprintf(stderr, "mt_simd_fp: done. total=%f\n", total);
    return 0;
}
