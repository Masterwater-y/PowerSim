#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/*
 * dep_chain
 *
 * 单核单线程：长 RAW 依赖链负载，暴露 issue/forwarding/execution latency 模型。
 *   - 三条互不相关的依赖链交错（imul / xor-shift / popcount-like），每条链
 *     输出依赖上一次输出，强迫模拟器按 latency 串行；
 *   - 不访问大数组，工作集很小，分支极少，让 cycles 由 dependency 主导。
 *
 * 用法： ./dep_chain [iter]
 */

#define INNER_LOOPS (1u << 22)  /* 4M 次内层迭代 */

static uint64_t parse_u64(const char *s)
{
    errno = 0;
    char *end = NULL;
    unsigned long long v = strtoull(s, &end, 0);
    if (errno || end == s || *end != '\0' || v == 0) {
        fprintf(stderr, "usage: dep_chain [iter>0]\n");
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

int main(int argc, char **argv)
{
    uint64_t iters = argc > 1 ? parse_u64(argv[1]) : 1;

    uint64_t a = 0x9e3779b97f4a7c15ULL;
    uint64_t b = 0xbf58476d1ce4e5b9ULL;
    uint64_t c = 0x94d049bb133111ebULL;

    uint64_t start = now_ns();
    for (uint64_t iter = 0; iter < iters; ++iter) {
        uint64_t la = a, lb = b, lc = c;
        for (uint32_t i = 0; i < INNER_LOOPS; ++i) {
            /* chain A: imul-shift-add */
            la = la * 6364136223846793005ULL + 1442695040888963407ULL;
            la ^= la >> 17;

            /* chain B: xor-rot-mul */
            lb ^= lb << 13;
            lb ^= lb >> 7;
            lb ^= lb << 17;
            lb = lb * 0xff51afd7ed558ccdULL;

            /* chain C: combine prior outputs to avoid trivial parallelism */
            lc = (lc + la) ^ ((lb >> 11) | (lb << 53));
            lc = lc * 0xc4ceb9fe1a85ec53ULL;
            lc ^= lc >> 33;
        }
        a = la; b = lb; c = lc;
    }

    uint64_t elapsed_ns = now_ns() - start;
    uint64_t checksum = a ^ b ^ c;
    double elapsed = (double)elapsed_ns / 1e9;
    printf("[dep_chain] iter=%" PRIu64 " inner=%u elapsed=%.6f checksum=0x%016" PRIx64 "\n",
           iters, INNER_LOOPS, elapsed, checksum);
    return 0;
}
