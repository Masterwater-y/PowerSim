#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/*
 * branch_dense
 *
 * 单核单线程：高频条件分支负载，专门暴露分支方向预测器精度。
 *   - 数据相关条件分支（依赖 random pattern array），方向不易被全局历史预测；
 *   - 嵌入轻量整数计算，避免退化成纯 jcc；
 *   - 工作集放在 L1，使 cycles 主要由 branch miss / pipeline flush 决定。
 *
 * 用法： ./branch_dense [iter]
 */

#define PATTERN_BYTES (32u * 1024u)        /* 32 KiB, 整套放在 L1 */
#define INNER_LOOPS   (1u << 16)           /* 每个 iter 64K 次内层迭代 */

static uint64_t mix64(uint64_t x)
{
    x ^= x >> 33;
    x *= 0xff51afd7ed558ccdULL;
    x ^= x >> 33;
    x *= 0xc4ceb9fe1a85ec53ULL;
    x ^= x >> 33;
    return x;
}

static uint64_t parse_u64(const char *s)
{
    errno = 0;
    char *end = NULL;
    unsigned long long v = strtoull(s, &end, 0);
    if (errno || end == s || *end != '\0' || v == 0) {
        fprintf(stderr, "usage: branch_dense [iter>0]\n");
        exit(1);
    }
    return (uint64_t)v;
}

static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
}

static void *xalloc(size_t bytes)
{
    void *p = NULL;
    if (posix_memalign(&p, 64, bytes) != 0 || p == NULL) {
        perror("posix_memalign");
        exit(1);
    }
    return p;
}

int main(int argc, char **argv)
{
    uint64_t iters = argc > 1 ? parse_u64(argv[1]) : 1;

    uint8_t *pat = (uint8_t *)xalloc(PATTERN_BYTES);
    for (uint32_t i = 0; i < PATTERN_BYTES; ++i) {
        uint64_t r = mix64((uint64_t)i * 1315423911u + 0xa5a5);
        /* 三个不同 bias 的方向位，混合后强迫预测器学多模态 */
        uint8_t b0 = (r >> 7) & 1;
        uint8_t b1 = ((r >> 13) & 7) > 4;
        uint8_t b2 = ((r >> 19) & 15) > 9;
        pat[i] = (uint8_t)((b0) | (b1 << 1) | (b2 << 2));
    }

    uint64_t start = now_ns();
    uint64_t acc = 0xdeadbeefULL;
    uint64_t taken = 0;

    for (uint64_t iter = 0; iter < iters; ++iter) {
        uint32_t mask = PATTERN_BYTES - 1u;
        uint32_t idx = (uint32_t)(mix64(iter + 1) & mask);
        for (uint32_t i = 0; i < INNER_LOOPS; ++i) {
            uint8_t p = pat[idx];
            /* 三个数据相关分支，方向各自独立 */
            if (p & 1) {
                acc += (uint64_t)i * 2654435761u;
                taken++;
            } else {
                acc ^= (uint64_t)i + 0x9e3779b9u;
            }
            if (p & 2) {
                acc = (acc << 7) | (acc >> 57);
                taken++;
            } else {
                acc = (acc >> 3) ^ acc;
            }
            if (p & 4) {
                acc -= (uint64_t)idx * 1469598103934665603ULL;
                taken++;
            } else {
                acc += (uint64_t)idx ^ 0xc0ffee;
            }
            /* 索引前进路径也带一个分支 */
            if ((acc & 0xff) < 32) {
                idx = (idx + 7u) & mask;
            } else {
                idx = (idx + 13u) & mask;
            }
        }
    }

    double elapsed = (double)(now_ns() - start) / 1e9;
    printf("[branch_dense] iter=%" PRIu64 " inner=%u elapsed=%.6f acc=0x%016" PRIx64 " taken=%" PRIu64 "\n",
           iters, INNER_LOOPS, elapsed, acc, taken);

    free(pat);
    return 0;
}
