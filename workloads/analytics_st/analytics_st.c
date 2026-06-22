#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/*
 * analytics_st
 *
 * 单核单线程“类真实负载”：模拟分析型数据处理流水线，混合顺序扫描、
 * 间接访存、热点更新和整数计算。只有一个参数 iter，用来控制整体运行时间。
 *
 * 用法：
 *   ./analytics_st [iter]
 *
 * 特性：
 *   1. records 数组顺序扫描，像流式读日志/事件流；
 *   2. 通过 links 做间接索引，访问 feature_table，形成随机/半随机访存；
 *   3. 更新 hot_bins，形成较热的小工作集；
 *   4. 穿插大量整数混合计算，避免退化成纯 memcpy/纯 load-store。
 */

#define RECORD_COUNT      (1u << 20)   /* 1,048,576 */
#define FEATURE_COUNT     (1u << 19)   /* 524,288  */
#define HOT_BIN_COUNT     (1u << 15)   /* 32,768   */
#define BLOCK_RECORDS     256u

static void die_msg(const char *msg)
{
    fprintf(stderr, "%s\n", msg);
    exit(1);
}

static uint64_t parse_u64(const char *s)
{
    errno = 0;
    char *end = NULL;
    unsigned long long v = strtoull(s, &end, 0);
    if (errno != 0 || end == s || *end != '\0')
        die_msg("invalid iter argument");
    return (uint64_t)v;
}

static uint64_t monotonic_ns(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        die_msg("clock_gettime failed");
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static inline uint64_t rotl64(uint64_t x, unsigned k)
{
    return (x << k) | (x >> (64 - k));
}

static inline uint64_t mix64(uint64_t x)
{
    x ^= x >> 30;
    x *= 0xbf58476d1ce4e5b9ULL;
    x ^= x >> 27;
    x *= 0x94d049bb133111ebULL;
    x ^= x >> 31;
    return x;
}

static void *xaligned_alloc(size_t bytes)
{
    void *p = NULL;
    if (posix_memalign(&p, 64, bytes) != 0 || p == NULL)
        die_msg("posix_memalign failed");
    return p;
}

static uint64_t sample_checksum(const uint64_t *a, size_t count, size_t stride)
{
    uint64_t sum = 0x123456789abcdef0ULL;
    for (size_t i = 0; i < count; i += stride)
        sum = mix64(sum ^ a[i] ^ (uint64_t)i);
    return sum;
}

int main(int argc, char **argv)
{
    uint64_t iters = 4;
    if (argc > 1)
        iters = parse_u64(argv[1]);
    if (iters == 0)
        die_msg("iter must be > 0");

    const size_t records_bytes = RECORD_COUNT * sizeof(uint64_t);
    const size_t features_bytes = FEATURE_COUNT * sizeof(uint64_t);
    const size_t links_bytes = RECORD_COUNT * sizeof(uint32_t);
    const size_t hot_bytes = HOT_BIN_COUNT * sizeof(uint64_t);

    uint64_t *keys = (uint64_t *)xaligned_alloc(records_bytes);
    uint64_t *payloads = (uint64_t *)xaligned_alloc(records_bytes);
    uint32_t *links = (uint32_t *)xaligned_alloc(links_bytes);
    uint64_t *feature_table = (uint64_t *)xaligned_alloc(features_bytes);
    uint64_t *hot_bins = (uint64_t *)xaligned_alloc(hot_bytes);

    for (size_t i = 0; i < RECORD_COUNT; ++i) {
        keys[i] = mix64((uint64_t)i * 0x9e3779b97f4a7c15ULL + 0x100000001b3ULL);
        payloads[i] = mix64((uint64_t)i * 0xbf58476d1ce4e5b9ULL + 0x84222325ULL);
        links[i] = (uint32_t)((i * 2654435761u) & (FEATURE_COUNT - 1));
    }
    for (size_t i = 0; i < FEATURE_COUNT; ++i)
        feature_table[i] = mix64((uint64_t)i + 0xfeedface12345678ULL);
    memset(hot_bins, 0, hot_bytes);

    uint64_t total_bytes = records_bytes * 2 + features_bytes + links_bytes + hot_bytes;
    printf("[analytics_st] iter=%" PRIu64 " records=%u features=%u hot_bins=%u footprint=%" PRIu64 " bytes\n",
           iters, RECORD_COUNT, FEATURE_COUNT, HOT_BIN_COUNT, total_bytes);

    uint64_t begin = monotonic_ns();
    uint64_t acc = 0x6a09e667f3bcc909ULL;

    for (uint64_t iter = 0; iter < iters; ++iter) {
        uint64_t iter_acc = acc ^ mix64(iter + 0x9e3779b97f4a7c15ULL);

        for (size_t base = 0; base < RECORD_COUNT; base += BLOCK_RECORDS) {
            size_t end = base + BLOCK_RECORDS;
            if (end > RECORD_COUNT)
                end = RECORD_COUNT;

            for (size_t i = base; i < end; ++i) {
                uint64_t k = keys[i];
                uint64_t p = payloads[i];
                uint32_t l = links[i];

                uint64_t local = mix64(k ^ rotl64(p + iter_acc, (unsigned)((i & 15u) + 7u)));
                size_t feature_idx = (size_t)((l ^ (uint32_t)local) & (FEATURE_COUNT - 1));
                uint64_t feat = feature_table[feature_idx];

                feat ^= local + 0x9e3779b97f4a7c15ULL + (uint64_t)i;
                feat = rotl64(feat, 11) * 0xd6e8feb86659fd93ULL;
                feature_table[feature_idx] = feat;

                size_t hot_idx = (size_t)((feature_idx ^ (i >> 2) ^ iter) & (HOT_BIN_COUNT - 1));
                hot_bins[hot_idx] += (feat ^ k) + (p << 1);
                hot_bins[(hot_idx + 17) & (HOT_BIN_COUNT - 1)] ^= rotl64(local, 9);

                payloads[i] = rotl64(p ^ feat ^ iter_acc, 7) + 0x27d4eb2f165667c5ULL;
                iter_acc ^= mix64(feat + payloads[i] + hot_bins[hot_idx]);
            }

            /* 像批处理/算子收尾一样，对一个热窗口做局部聚合。 */
            size_t window = (size_t)((base / BLOCK_RECORDS) * 97u + iter) & (HOT_BIN_COUNT - 1);
            for (size_t j = 0; j < 64; ++j) {
                size_t idx = (window + j * 13u) & (HOT_BIN_COUNT - 1);
                uint64_t v = hot_bins[idx];
                v = mix64(v + iter_acc + j);
                hot_bins[idx] = v;
                iter_acc += v;
            }
        }

        acc ^= mix64(iter_acc + iter * 0x94d049bb133111ebULL);
        __asm__ volatile("" : "+r"(acc) :: "memory");
    }

    uint64_t end = monotonic_ns();
    double elapsed_s = (double)(end - begin) / 1e9;

    uint64_t checksum = acc;
    checksum ^= sample_checksum(keys, RECORD_COUNT, 4096);
    checksum ^= sample_checksum(payloads, RECORD_COUNT, 4096);
    checksum ^= sample_checksum(feature_table, FEATURE_COUNT, 2048);
    checksum ^= sample_checksum(hot_bins, HOT_BIN_COUNT, 256);

    double record_updates = (double)RECORD_COUNT * (double)iters;
    printf("[analytics_st] elapsed=%.6f s updates=%.0f checksum=0x%016" PRIx64 "\n",
           elapsed_s, record_updates, checksum);

    free(keys);
    free(payloads);
    free(links);
    free(feature_table);
    free(hot_bins);
    return 0;
}
