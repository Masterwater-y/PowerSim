#define _GNU_SOURCE

#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static void die_errno(const char *msg) {
  fprintf(stderr, "%s: %s\n", msg, strerror(errno));
  exit(1);
}

static void die_msg(const char *msg) {
  fprintf(stderr, "%s\n", msg);
  exit(1);
}

static uint64_t parse_u64(const char *s) {
  errno = 0;
  char *end = NULL;
  unsigned long long v = strtoull(s, &end, 0);
  if (errno != 0 || end == s || *end != 0) die_msg("invalid integer argument");
  return (uint64_t)v;
}

static uint64_t monotonic_ns(void) {
  struct timespec ts;
  if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) die_errno("clock_gettime");
  return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static inline uint64_t rotl64(uint64_t x, unsigned k) {
  return (x << k) | (x >> (64 - k));
}

static inline uint64_t mix64(uint64_t x) {
  x ^= x >> 33;
  x *= 0xff51afd7ed558ccdULL;
  x ^= x >> 33;
  x *= 0xc4ceb9fe1a85ec53ULL;
  x ^= x >> 33;
  return x;
}

struct thread_ctx {
  int tid;
  uint64_t iters;
  uint8_t *buf;
  size_t bytes;
  pthread_barrier_t *bar;
  pthread_mutex_t *lock;
  uint64_t *global_sum;
  uint64_t local_sum;
};

static void *worker_main(void *arg) {
  struct thread_ctx *ctx = (struct thread_ctx *)arg;
  uint64_t s0 = 0x9e3779b97f4a7c15ULL ^ (uint64_t)ctx->tid;
  uint64_t s1 = 0xbf58476d1ce4e5b9ULL ^ (uint64_t)(ctx->tid * 17 + 3);
  uint64_t s2 = 0x94d049bb133111ebULL ^ (uint64_t)(ctx->tid * 131 + 7);
  uint64_t local = 0;

  for (uint64_t it = 0; it < ctx->iters; it++) {
    pthread_barrier_wait(ctx->bar);

    uint64_t x = s0 + it;
    uint64_t y = s1 ^ (it * 0x100000001b3ULL);
    uint64_t z = s2 + (it << 1);
    for (int k = 0; k < 4096; k++) {
      x = rotl64(x + y, 17) ^ z;
      y = rotl64(y + z, 29) + 0x9e3779b97f4a7c15ULL;
      z = mix64(z ^ x) + y;
    }
    local ^= x ^ y ^ z;

    uint64_t m = local;
    for (size_t off = 0; off < ctx->bytes; off += 64) {
      uint64_t *p = (uint64_t *)(ctx->buf + off);
      uint64_t v = p[0];
      v ^= m;
      v *= 0xD6E8FEB86659FD93ULL;
      p[0] = v;
      m = rotl64(m + v + 0x9e3779b97f4a7c15ULL, 13);
      local += v;
    }

    pthread_mutex_lock(ctx->lock);
    *ctx->global_sum += local;
    pthread_mutex_unlock(ctx->lock);

    pthread_barrier_wait(ctx->bar);
  }

  ctx->local_sum = local;
  return NULL;
}

int main(int argc, char **argv) {
  if (argc != 3) {
    fprintf(stderr, "Usage: %s <iter> <threads>\n", argv[0]);
    return 2;
  }

  uint64_t iters = parse_u64(argv[1]);
  uint64_t threads_u = parse_u64(argv[2]);
  if (iters == 0) die_msg("iter must be > 0");
  if (threads_u == 0 || threads_u > 4096) die_msg("threads out of range");
  int threads = (int)threads_u;

  const size_t per_thread_bytes = 8u * 1024u * 1024u;

  pthread_t *t = (pthread_t *)calloc((size_t)threads, sizeof(pthread_t));
  struct thread_ctx *ctx = (struct thread_ctx *)calloc((size_t)threads, sizeof(struct thread_ctx));
  if (!t || !ctx) die_msg("oom");

  pthread_barrier_t bar;
  if (pthread_barrier_init(&bar, NULL, (unsigned)threads) != 0) die_msg("pthread_barrier_init failed");
  pthread_mutex_t lock;
  if (pthread_mutex_init(&lock, NULL) != 0) die_msg("pthread_mutex_init failed");

  uint64_t global_sum = 0;
  for (int i = 0; i < threads; i++) {
    ctx[i].tid = i;
    ctx[i].iters = iters;
    ctx[i].bytes = per_thread_bytes;
    ctx[i].bar = &bar;
    ctx[i].lock = &lock;
    ctx[i].global_sum = &global_sum;
    ctx[i].local_sum = 0;

    void *p = NULL;
    int rc = posix_memalign(&p, 64, per_thread_bytes);
    if (rc != 0 || !p) die_msg("posix_memalign failed");
    ctx[i].buf = (uint8_t *)p;
    memset(ctx[i].buf, (int)(i * 13 + 7), per_thread_bytes);
  }

  uint64_t t0 = monotonic_ns();
  for (int i = 0; i < threads; i++) {
    if (pthread_create(&t[i], NULL, worker_main, &ctx[i]) != 0) die_msg("pthread_create failed");
  }

  for (int i = 0; i < threads; i++) {
    if (pthread_join(t[i], NULL) != 0) die_msg("pthread_join failed");
  }
  uint64_t t1 = monotonic_ns();

  uint64_t checksum = global_sum;
  for (int i = 0; i < threads; i++) checksum ^= mix64(ctx[i].local_sum + (uint64_t)i);

  double elapsed_s = (double)(t1 - t0) / 1e9;
  printf("[mt_bench] iter=%" PRIu64 " threads=%d per_thread_bytes=%zu total_bytes=%zu\n", iters, threads, per_thread_bytes,
         per_thread_bytes * (size_t)threads);
  printf("[mt_bench] elapsed=%.6f s checksum=0x%016" PRIx64 "\n", elapsed_s, checksum);

  for (int i = 0; i < threads; i++) free(ctx[i].buf);
  pthread_mutex_destroy(&lock);
  pthread_barrier_destroy(&bar);
  free(ctx);
  free(t);
  return 0;
}
