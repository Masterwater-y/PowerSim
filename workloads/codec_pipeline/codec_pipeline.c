#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define N_BYTES (1u << 22)
#define WINDOW  64u

static uint64_t mix64(uint64_t x)
{
    x ^= x >> 30;
    x *= 0xbf58476d1ce4e5b9ULL;
    x ^= x >> 27;
    x *= 0x94d049bb133111ebULL;
    x ^= x >> 31;
    return x;
}

static uint64_t parse_u64(const char *s)
{
    errno = 0;
    char *end = NULL;
    unsigned long long v = strtoull(s, &end, 0);
    if (errno || end == s || *end != '\0' || v == 0) {
        fprintf(stderr, "usage: codec_pipeline [iter>0]\n");
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
    uint8_t *input = (uint8_t *)xalloc(N_BYTES);
    uint8_t *out = (uint8_t *)xalloc(N_BYTES + N_BYTES / 8);
    uint32_t *hist = (uint32_t *)xalloc(sizeof(uint32_t) * 256);

    uint8_t symbol = 0;
    for (uint32_t i = 0; i < N_BYTES; ++i) {
        uint64_t r = mix64(i + 0x31415926ULL);
        if ((r & 15u) == 0)
            symbol = (uint8_t)r;
        else if ((r & 3u) == 0)
            symbol = (uint8_t)(symbol + (uint8_t)(r >> 8));
        input[i] = symbol ^ (uint8_t)(r >> 17);
    }

    uint64_t start = now_ns();
    uint64_t acc = 0xcbf29ce484222325ULL;
    size_t out_len = 0;
    for (uint64_t iter = 0; iter < iters; ++iter) {
        memset(hist, 0, sizeof(uint32_t) * 256);
        out_len = 0;
        for (uint32_t base = 0; base < N_BYTES; base += WINDOW) {
            uint8_t prev = input[base];
            uint8_t run = 1;
            uint64_t local = acc ^ base ^ iter;
            uint32_t end = base + WINDOW;
            if (end > N_BYTES)
                end = N_BYTES;
            for (uint32_t i = base + 1; i < end; ++i) {
                uint8_t v = input[i] ^ (uint8_t)local;
                hist[v]++;
                if (v == prev && run < 255) {
                    run++;
                } else {
                    out[out_len++ & (N_BYTES + N_BYTES / 8 - 1)] = prev;
                    out[out_len++ & (N_BYTES + N_BYTES / 8 - 1)] = run;
                    local = mix64(local + ((uint64_t)prev << 8) + run + hist[v]);
                    prev = v;
                    run = 1;
                }
            }
            out[out_len++ & (N_BYTES + N_BYTES / 8 - 1)] = prev;
            out[out_len++ & (N_BYTES + N_BYTES / 8 - 1)] = run;

            for (uint32_t k = 0; k < 8; ++k) {
                uint32_t idx = (uint32_t)((local >> (k * 7)) & 255u);
                local ^= mix64(hist[idx] + idx + out_len);
            }
            acc ^= local + out_len;
        }

        for (uint32_t i = 0; i < N_BYTES; i += 4096) {
            input[i] ^= (uint8_t)(acc >> (i & 31));
            acc = mix64(acc + input[i] + out[i & (N_BYTES + N_BYTES / 8 - 1)]);
        }
    }

    uint64_t checksum = acc ^ out_len;
    for (uint32_t i = 0; i < 256; ++i)
        checksum ^= mix64(hist[i] + i);
    double elapsed = (double)(now_ns() - start) / 1e9;
    printf("[codec_pipeline] iter=%" PRIu64 " bytes=%u out_len=%zu elapsed=%.6f checksum=0x%016" PRIx64 "\n",
           iters, N_BYTES, out_len, elapsed, checksum);

    free(input);
    free(out);
    free(hist);
    return 0;
}
