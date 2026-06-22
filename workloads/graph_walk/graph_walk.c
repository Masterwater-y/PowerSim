#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define N_NODE (1u << 16)
#define DEGREE 8u
#define N_EDGE (N_NODE * DEGREE)

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
        fprintf(stderr, "usage: graph_walk [iter>0]\n");
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
    uint32_t *row = (uint32_t *)xalloc(sizeof(uint32_t) * (N_NODE + 1));
    uint32_t *col = (uint32_t *)xalloc(sizeof(uint32_t) * N_EDGE);
    uint32_t *frontier = (uint32_t *)xalloc(sizeof(uint32_t) * N_NODE);
    uint32_t *next = (uint32_t *)xalloc(sizeof(uint32_t) * N_NODE);
    uint8_t *seen = (uint8_t *)xalloc(N_NODE);
    uint64_t *rank = (uint64_t *)xalloc(sizeof(uint64_t) * N_NODE);

    for (uint32_t n = 0; n <= N_NODE; ++n)
        row[n] = n * DEGREE;
    for (uint32_t n = 0; n < N_NODE; ++n) {
        rank[n] = mix64(n + 1);
        for (uint32_t d = 0; d < DEGREE; ++d) {
            uint64_t r = mix64((uint64_t)n * 1315423911u + d * 2654435761u);
            col[row[n] + d] = (uint32_t)(r & (N_NODE - 1));
        }
    }

    uint64_t start = now_ns();
    uint64_t acc = 0x9e3779b97f4a7c15ULL;
    for (uint64_t iter = 0; iter < iters; ++iter) {
        memset(seen, 0, N_NODE);
        uint32_t f_count = 1;
        frontier[0] = (uint32_t)(mix64(iter + acc) & (N_NODE - 1));
        seen[frontier[0]] = 1;

        for (uint32_t depth = 0; depth < 16 && f_count; ++depth) {
            uint32_t n_count = 0;
            for (uint32_t i = 0; i < f_count; ++i) {
                uint32_t v = frontier[i];
                uint64_t local = rank[v] ^ acc ^ depth;
                for (uint32_t e = row[v]; e < row[v + 1]; ++e) {
                    uint32_t u = col[e];
                    local += rank[u] ^ mix64((uint64_t)u + e);
                    if (!seen[u]) {
                        seen[u] = 1;
                        if (n_count < N_NODE)
                            next[n_count++] = u;
                    } else {
                        rank[u] ^= (local >> 7) + depth;
                    }
                }
                rank[v] = mix64(local + rank[v]);
                acc ^= rank[v] + f_count;
            }
            uint32_t *tmp = frontier;
            frontier = next;
            next = tmp;
            f_count = n_count > 4096 ? 4096 : n_count;
        }

        for (uint32_t i = 0; i < N_NODE; i += 97) {
            uint32_t v = (uint32_t)(mix64(i + iter + acc) & (N_NODE - 1));
            acc ^= rank[v] + seen[v];
        }
    }

    uint64_t checksum = acc;
    for (uint32_t i = 0; i < N_NODE; i += 251)
        checksum ^= rank[i];
    double elapsed = (double)(now_ns() - start) / 1e9;
    printf("[graph_walk] iter=%" PRIu64 " nodes=%u edges=%u elapsed=%.6f checksum=0x%016" PRIx64 "\n",
           iters, N_NODE, N_EDGE, elapsed, checksum);

    free(row);
    free(col);
    free(frontier);
    free(next);
    free(seen);
    free(rank);
    return 0;
}
