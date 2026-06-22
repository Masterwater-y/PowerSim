#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define N_EVENTS (1u << 18)
#define N_USERS  (1u << 16)
#define N_BUCKET (1u << 15)

typedef struct {
    uint32_t user;
    uint32_t url;
    uint32_t ts_delta;
    uint16_t method;
    uint16_t status;
    uint64_t bytes;
} Event;

typedef struct {
    uint64_t score;
    uint64_t bytes;
    uint32_t last_url;
    uint32_t flags;
} Session;

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
        fprintf(stderr, "usage: log_state [iter>0]\n");
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
    Event *events = (Event *)xalloc(sizeof(Event) * N_EVENTS);
    Session *sessions = (Session *)xalloc(sizeof(Session) * N_USERS);
    uint64_t *buckets = (uint64_t *)xalloc(sizeof(uint64_t) * N_BUCKET);
    memset(sessions, 0, sizeof(Session) * N_USERS);
    memset(buckets, 0, sizeof(uint64_t) * N_BUCKET);

    for (uint32_t i = 0; i < N_EVENTS; ++i) {
        uint64_t r = mix64(i + 0x1234abcdULL);
        events[i].user = (uint32_t)(mix64(r) & (N_USERS - 1));
        events[i].url = (uint32_t)(mix64(r + 17) & 0x3ffffu);
        events[i].ts_delta = (uint32_t)(r & 4095u);
        events[i].method = (uint16_t)((r >> 13) % 5);
        events[i].status = (uint16_t)((r & 31u) == 0 ? 500 : ((r & 7u) == 0 ? 404 : 200));
        events[i].bytes = (mix64(r + 99) & 16383u) + 64u;
    }

    uint64_t start = now_ns();
    uint64_t acc = 0x6a09e667f3bcc909ULL;
    for (uint64_t iter = 0; iter < iters; ++iter) {
        for (uint32_t i = 0; i < N_EVENTS; ++i) {
            Event e = events[(i * 2654435761u + (uint32_t)iter) & (N_EVENTS - 1)];
            Session *s = &sessions[e.user];
            uint64_t h = mix64(((uint64_t)e.user << 32) ^ e.url ^ acc);
            uint32_t b = (uint32_t)(h & (N_BUCKET - 1));

            if (e.status >= 500) {
                s->flags ^= 0x80u;
                s->score += 97 + (h & 31u);
            } else if (e.status == 404) {
                s->score += 13;
            } else {
                s->score += 1 + (e.method == 1);
            }

            if (s->last_url == e.url) {
                s->score += 11;
            } else {
                s->score ^= mix64((uint64_t)s->last_url + e.url);
                s->last_url = e.url;
            }
            s->bytes += e.bytes;
            buckets[b] += (s->score ^ s->bytes) + e.ts_delta;
            buckets[(b + 103) & (N_BUCKET - 1)] ^= mix64(buckets[b] + h);
            acc ^= mix64(s->score + buckets[b] + i);
        }
    }

    uint64_t checksum = acc;
    for (uint32_t i = 0; i < N_USERS; i += 257)
        checksum ^= mix64(sessions[i].score ^ sessions[i].bytes);
    for (uint32_t i = 0; i < N_BUCKET; i += 131)
        checksum ^= buckets[i];
    double elapsed = (double)(now_ns() - start) / 1e9;
    printf("[log_state] iter=%" PRIu64 " events=%u elapsed=%.6f checksum=0x%016" PRIx64 "\n",
           iters, N_EVENTS, elapsed, checksum);

    free(events);
    free(sessions);
    free(buckets);
    return 0;
}
