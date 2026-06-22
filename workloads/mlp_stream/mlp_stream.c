#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/*
 * mlp_stream
 *
 * 单核单线程：多路独立 load，暴露 MLP / MSHR 容量与 load miss 隐藏能力。
 *   - 工作集 64 MiB，远大于 L2，足以让 LLC/DRAM 路径被命中；
 *   - K 路独立追逐链 (pointer chasing)，每路彼此无依赖，可被 OoO 同时发射；
 *   - K 取 8，配合 SKL 风格 ~10 MSHR/LFB，可触发模拟器是否能正确隐藏 K 个并发 miss。
 *
 * 用法： ./mlp_stream [iter]
 */

#define BUF_BYTES (64ull * 1024ull * 1024ull)
#define WORDS     (BUF_BYTES / sizeof(uint64_t))
#define K_PARALLEL 8u
#define INNER_HOPS (1u << 18) /* 每路 256K 次跳跃 */

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
        fprintf(stderr, "usage: mlp_stream [iter>0]\n");
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

    uint64_t *buf = (uint64_t *)xalloc(BUF_BYTES);
    /* 每个 word 指向另一个 word，构造单一巨大随机环 */
    uint64_t mask = WORDS - 1ull;
    for (uint64_t i = 0; i < WORDS; ++i) {
        buf[i] = mix64(i * 2654435761ull + 0x1234) & mask;
    }

    uint64_t starts[K_PARALLEL];
    for (uint32_t k = 0; k < K_PARALLEL; ++k) {
        starts[k] = mix64((uint64_t)k * 1469598103934665603ULL) & mask;
    }

    uint64_t start = now_ns();
    uint64_t acc = 0;
    for (uint64_t iter = 0; iter < iters; ++iter) {
        uint64_t p0 = starts[0];
        uint64_t p1 = starts[1];
        uint64_t p2 = starts[2];
        uint64_t p3 = starts[3];
        uint64_t p4 = starts[4];
        uint64_t p5 = starts[5];
        uint64_t p6 = starts[6];
        uint64_t p7 = starts[7];
        for (uint32_t i = 0; i < INNER_HOPS; ++i) {
            p0 = buf[p0];
            p1 = buf[p1];
            p2 = buf[p2];
            p3 = buf[p3];
            p4 = buf[p4];
            p5 = buf[p5];
            p6 = buf[p6];
            p7 = buf[p7];
        }
        acc ^= p0 ^ p1 ^ p2 ^ p3 ^ p4 ^ p5 ^ p6 ^ p7;
        /* 让下一轮起点变化，避免被 prefetch 完全覆盖 */
        for (uint32_t k = 0; k < K_PARALLEL; ++k) {
            starts[k] = mix64(starts[k] + iter + acc) & mask;
        }
    }

    double elapsed = (double)(now_ns() - start) / 1e9;
    printf("[mlp_stream] iter=%" PRIu64 " K=%u hops=%u elapsed=%.6f acc=0x%016" PRIx64 "\n",
           iters, K_PARALLEL, INNER_HOPS, elapsed, acc);

    free(buf);
    return 0;
}
