#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <linux/perf_event.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

/*
 * analytics_roi
 *
 * Single-thread analytics-style workload with an explicit stable ROI.
 * The ROI is bracketed by unique NOP marker bytes so a DynamoRIO raw trace can
 * be sliced to the same interval that internal perf_event_open counters measure.
 */

#define RECORD_COUNT      (1u << 20)
#define FEATURE_COUNT     (1u << 19)
#define HOT_BIN_COUNT     (1u << 15)
#define BLOCK_RECORDS     256u

#define ROI_BEGIN_MAGIC "0f1f840042424242"
#define ROI_END_MAGIC   "0f1f840043434343"

#define ROI_BEGIN_MARKER() \
    __asm__ __volatile__( \
        ".byte 0x0f, 0x1f, 0x84, 0x00, 0x42, 0x42, 0x42, 0x42\n" \
        ::: "memory")

#define ROI_END_MARKER() \
    __asm__ __volatile__( \
        ".byte 0x0f, 0x1f, 0x84, 0x00, 0x43, 0x43, 0x43, 0x43\n" \
        ::: "memory")

static volatile uint64_t g_sink;

struct perf_counter {
    const char *name;
    uint32_t type;
    uint64_t config;
    int fd;
    uint64_t value;
};

struct perf_group_read {
    uint64_t nr;
    struct {
        uint64_t value;
        uint64_t id;
    } values[8];
};

static struct perf_counter g_counters[] = {
    {"core.cycles", PERF_TYPE_HARDWARE, PERF_COUNT_HW_CPU_CYCLES, -1, 0},
    {"core.instructions", PERF_TYPE_HARDWARE, PERF_COUNT_HW_INSTRUCTIONS, -1, 0},
    {"branch.misses", PERF_TYPE_HARDWARE, PERF_COUNT_HW_BRANCH_MISSES, -1, 0},
    {"cache.llc.load_misses", PERF_TYPE_HW_CACHE,
     PERF_COUNT_HW_CACHE_LL |
         (PERF_COUNT_HW_CACHE_OP_READ << 8) |
         (PERF_COUNT_HW_CACHE_RESULT_MISS << 16),
     -1, 0},
    {"tlb.dtlb_load_misses", PERF_TYPE_HW_CACHE,
     PERF_COUNT_HW_CACHE_DTLB |
         (PERF_COUNT_HW_CACHE_OP_READ << 8) |
         (PERF_COUNT_HW_CACHE_RESULT_MISS << 16),
     -1, 0},
};

static void die_msg(const char *msg)
{
    fprintf(stderr, "%s\n", msg);
    exit(1);
}

static uint64_t parse_u64_arg(const char *s, const char *name)
{
    errno = 0;
    char *end = NULL;
    unsigned long long v = strtoull(s, &end, 0);
    if (errno != 0 || end == s || *end != '\0') {
        fprintf(stderr, "invalid %s: %s\n", name, s);
        exit(1);
    }
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

static long perf_event_open_wrap(struct perf_event_attr *attr, pid_t pid,
                                 int cpu, int group_fd, unsigned long flags)
{
    return syscall(__NR_perf_event_open, attr, pid, cpu, group_fd, flags);
}

static void perf_setup(void)
{
    const size_t n = sizeof(g_counters) / sizeof(g_counters[0]);
    int group_fd = -1;

    for (size_t i = 0; i < n; ++i) {
        struct perf_event_attr attr;
        memset(&attr, 0, sizeof(attr));
        attr.type = g_counters[i].type;
        attr.size = sizeof(attr);
        attr.config = g_counters[i].config;
        attr.disabled = (i == 0);
        attr.exclude_kernel = 1;
        attr.exclude_hv = 1;
        attr.read_format = PERF_FORMAT_GROUP | PERF_FORMAT_ID;

        int fd = (int)perf_event_open_wrap(&attr, 0, -1, group_fd, 0);
        if (fd < 0) {
            fprintf(stderr, "perf_event_open failed for %s: %s\n",
                    g_counters[i].name, strerror(errno));
            exit(2);
        }
        g_counters[i].fd = fd;
        if (i == 0)
            group_fd = fd;
    }
}

static void perf_start(void)
{
    if (ioctl(g_counters[0].fd, PERF_EVENT_IOC_RESET, PERF_IOC_FLAG_GROUP) != 0)
        die_msg("perf reset failed");
    if (ioctl(g_counters[0].fd, PERF_EVENT_IOC_ENABLE, PERF_IOC_FLAG_GROUP) != 0)
        die_msg("perf enable failed");
}

static void perf_stop(void)
{
    if (ioctl(g_counters[0].fd, PERF_EVENT_IOC_DISABLE, PERF_IOC_FLAG_GROUP) != 0)
        die_msg("perf disable failed");
}

static void perf_read_group(void)
{
    struct perf_group_read rd;
    memset(&rd, 0, sizeof(rd));
    ssize_t got = read(g_counters[0].fd, &rd, sizeof(rd));
    if (got < 0)
        die_msg("perf read failed");

    const size_t n = sizeof(g_counters) / sizeof(g_counters[0]);
    if (rd.nr != n) {
        fprintf(stderr, "perf read returned %" PRIu64 " counters, expected %zu\n", rd.nr, n);
        exit(2);
    }

    for (size_t i = 0; i < n; ++i)
        g_counters[i].value = rd.values[i].value;
}

static void perf_close_all(void)
{
    const size_t n = sizeof(g_counters) / sizeof(g_counters[0]);
    for (size_t i = 0; i < n; ++i) {
        if (g_counters[i].fd >= 0) {
            close(g_counters[i].fd);
            g_counters[i].fd = -1;
        }
    }
}

static void write_perf_json(const char *path, uint64_t warmup_iters,
                            uint64_t roi_iters, uint64_t roi_ns, uint64_t roi_begin_ns,
                            uint64_t roi_end_ns, uint64_t checksum)
{
    FILE *f = fopen(path, "w");
    if (!f) {
        fprintf(stderr, "failed to open perf json %s: %s\n", path, strerror(errno));
        exit(2);
    }

    fprintf(f, "{\n");
    fprintf(f, "  \"workload\": \"analytics_roi\",\n");
    fprintf(f, "  \"warmup_iters\": %" PRIu64 ",\n", warmup_iters);
    fprintf(f, "  \"roi_iters\": %" PRIu64 ",\n", roi_iters);
    fprintf(f, "  \"roi_begin_ns\": %" PRIu64 ",\n", roi_begin_ns);
    fprintf(f, "  \"roi_end_ns\": %" PRIu64 ",\n", roi_end_ns);
    fprintf(f, "  \"roi_elapsed_ns\": %" PRIu64 ",\n", roi_ns);
    fprintf(f, "  \"checksum\": \"0x%016" PRIx64 "\",\n", checksum);
    fprintf(f, "  \"markers\": {\n");
    fprintf(f, "    \"begin_encoding_hex\": \"%s\",\n", ROI_BEGIN_MAGIC);
    fprintf(f, "    \"end_encoding_hex\": \"%s\"\n", ROI_END_MAGIC);
    fprintf(f, "  },\n");
    fprintf(f, "  \"roi\": {\n");

    const size_t n = sizeof(g_counters) / sizeof(g_counters[0]);
    for (size_t i = 0; i < n; ++i) {
        fprintf(f, "    \"%s\": %" PRIu64 "%s\n",
                g_counters[i].name, g_counters[i].value,
                (i + 1 == n) ? "" : ",");
    }
    fprintf(f, "  }\n");
    fprintf(f, "}\n");
    fclose(f);
}

static void write_roi_timestamp_json(const char *path, uint64_t warmup_iters,
                                     uint64_t roi_iters, uint64_t roi_begin_ns,
                                     uint64_t roi_end_ns)
{
    FILE *f = fopen(path, "w");
    if (!f) {
        fprintf(stderr, "failed to open roi timestamp json %s: %s\n", path, strerror(errno));
        exit(2);
    }
    fprintf(f, "{\n");
    fprintf(f, "  \"workload\": \"analytics_roi\",\n");
    fprintf(f, "  \"warmup_iters\": %" PRIu64 ",\n", warmup_iters);
    fprintf(f, "  \"roi_iters\": %" PRIu64 ",\n", roi_iters);
    fprintf(f, "  \"roi_begin_ns\": %" PRIu64 ",\n", roi_begin_ns);
    fprintf(f, "  \"roi_end_ns\": %" PRIu64 ",\n", roi_end_ns);
    fprintf(f, "  \"roi_elapsed_ns\": %" PRIu64 ",\n", roi_end_ns - roi_begin_ns);
    fprintf(f, "  \"markers\": {\n");
    fprintf(f, "    \"begin_encoding_hex\": \"%s\",\n", ROI_BEGIN_MAGIC);
    fprintf(f, "    \"end_encoding_hex\": \"%s\"\n", ROI_END_MAGIC);
    fprintf(f, "  }\n");
    fprintf(f, "}\n");
    fclose(f);
}

static uint64_t kernel_iter(uint64_t *keys, uint64_t *payloads, uint32_t *links,
                            uint64_t *feature_table, uint64_t *hot_bins,
                            uint64_t iter, uint64_t acc)
{
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

        size_t window = (size_t)((base / BLOCK_RECORDS) * 97u + iter) & (HOT_BIN_COUNT - 1);
        for (size_t j = 0; j < 64; ++j) {
            size_t idx = (window + j * 13u) & (HOT_BIN_COUNT - 1);
            uint64_t v = hot_bins[idx];
            v = mix64(v + iter_acc + j);
            hot_bins[idx] = v;
            iter_acc += v;
        }
    }

    return acc ^ mix64(iter_acc + iter * 0x94d049bb133111ebULL);
}

static uint64_t sample_checksum(const uint64_t *a, size_t count, size_t stride)
{
    uint64_t sum = 0x123456789abcdef0ULL;
    for (size_t i = 0; i < count; i += stride)
        sum = mix64(sum ^ a[i] ^ (uint64_t)i);
    return sum;
}

static void usage(const char *argv0)
{
    fprintf(stderr,
            "Usage: %s [--warmup N] [--roi-iters N] [--perf-json PATH | --no-perf]\n"
            "       %s [--roi-ts-json PATH]\n"
            "       %s --help\n",
            argv0, argv0, argv0);
}

int main(int argc, char **argv)
{
    uint64_t warmup_iters = 2;
    uint64_t roi_iters = 8;
    const char *perf_json = NULL;
    const char *roi_ts_json = NULL;
    int enable_perf = 1;

    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--warmup") == 0 && i + 1 < argc) {
            warmup_iters = parse_u64_arg(argv[++i], "warmup");
        } else if (strcmp(argv[i], "--roi-iters") == 0 && i + 1 < argc) {
            roi_iters = parse_u64_arg(argv[++i], "roi-iters");
        } else if (strcmp(argv[i], "--perf-json") == 0 && i + 1 < argc) {
            perf_json = argv[++i];
        } else if (strcmp(argv[i], "--roi-ts-json") == 0 && i + 1 < argc) {
            roi_ts_json = argv[++i];
        } else if (strcmp(argv[i], "--no-perf") == 0) {
            enable_perf = 0;
        } else if (strcmp(argv[i], "--help") == 0) {
            usage(argv[0]);
            return 0;
        } else {
            usage(argv[0]);
            return 1;
        }
    }

    if (roi_iters == 0)
        die_msg("roi-iters must be > 0");
    if (enable_perf && !perf_json)
        die_msg("--perf-json is required");

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

    printf("[analytics_roi] warmup=%" PRIu64 " roi_iters=%" PRIu64
           " begin_marker=%s end_marker=%s\n",
           warmup_iters, roi_iters, ROI_BEGIN_MAGIC, ROI_END_MAGIC);

    uint64_t acc = 0x6a09e667f3bcc909ULL;
    for (uint64_t i = 0; i < warmup_iters; ++i) {
        acc = kernel_iter(keys, payloads, links, feature_table, hot_bins, i, acc);
        g_sink ^= acc;
    }

    if (enable_perf) {
        perf_setup();
        perf_start();
    }
    ROI_BEGIN_MARKER();
    uint64_t roi_begin_ns = monotonic_ns();
    fprintf(stderr, "[analytics_roi] ROI_BEGIN_NS=%" PRIu64 "\n", roi_begin_ns);

    for (uint64_t i = 0; i < roi_iters; ++i) {
        acc = kernel_iter(keys, payloads, links, feature_table, hot_bins,
                          warmup_iters + i, acc);
        g_sink ^= acc;
    }

    uint64_t roi_end_ns = monotonic_ns();
    ROI_END_MARKER();
    fprintf(stderr, "[analytics_roi] ROI_END_NS=%" PRIu64 "\n", roi_end_ns);
    if (enable_perf) {
        perf_stop();
        perf_read_group();
        perf_close_all();
    }

    uint64_t checksum = acc ^ g_sink;
    checksum ^= sample_checksum(keys, RECORD_COUNT, 4096);
    checksum ^= sample_checksum(payloads, RECORD_COUNT, 4096);
    checksum ^= sample_checksum(feature_table, FEATURE_COUNT, 2048);
    checksum ^= sample_checksum(hot_bins, HOT_BIN_COUNT, 256);

    if (roi_ts_json) {
        write_roi_timestamp_json(roi_ts_json, warmup_iters, roi_iters,
                                 roi_begin_ns, roi_end_ns);
    }

    if (enable_perf) {
        write_perf_json(perf_json, warmup_iters, roi_iters, roi_end_ns - roi_begin_ns,
                        roi_begin_ns, roi_end_ns, checksum);
    }

    printf("[analytics_roi] roi_elapsed=%.6f s checksum=0x%016" PRIx64
           " perf_json=%s\n",
           (double)(roi_end_ns - roi_begin_ns) / 1e9, checksum,
           enable_perf ? perf_json : "<disabled>");

    free(keys);
    free(payloads);
    free(links);
    free(feature_table);
    free(hot_bins);
    return 0;
}
