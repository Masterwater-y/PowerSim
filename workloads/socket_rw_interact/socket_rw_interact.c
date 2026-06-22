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

#define CACHELINE_BYTES 64

struct shared_state {
  uint64_t *words;
  size_t line_count;
  int threads;
  uint64_t passes;
  pthread_barrier_t barrier;
};

struct worker_ctx {
  int tid;
  int cpu;
  struct shared_state *shared;
  uint64_t local_checksum;
};

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
  if (errno != 0 || end == s || *end != '\0') die_msg("invalid integer argument");
  return (uint64_t)v;
}

static double monotonic_sec(void) {
  struct timespec ts;
  if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) die_errno("clock_gettime");
  return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
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

static int collect_allowed_cpus(int *out, int cap) {
  cpu_set_t set;
  if (sched_getaffinity(0, sizeof(set), &set) != 0) die_errno("sched_getaffinity");
  int n = 0;
  for (int cpu = 0; cpu < CPU_SETSIZE; cpu++) {
    if (!CPU_ISSET(cpu, &set)) continue;
    if (n >= cap) die_msg("too many allowed CPUs for static buffer");
    out[n++] = cpu;
  }
  return n;
}

static void pin_current_thread(int cpu) {
  cpu_set_t set;
  CPU_ZERO(&set);
  CPU_SET(cpu, &set);
  int rc = pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
  if (rc != 0) {
    errno = rc;
    die_errno("pthread_setaffinity_np");
  }
}

static void *worker_main(void *arg) {
  struct worker_ctx *ctx = (struct worker_ctx *)arg;
  struct shared_state *shared = ctx->shared;
  const size_t words_per_line = CACHELINE_BYTES / sizeof(uint64_t);
  uint64_t checksum = 0x9e3779b97f4a7c15ULL ^ (uint64_t)(ctx->tid * 1315423911u);

  pin_current_thread(ctx->cpu);

  for (uint64_t pass = 0; pass < shared->passes; pass++) {
    const int write_mod = (ctx->tid + (int)(pass % (uint64_t)shared->threads)) % shared->threads;
    const int read_mod = (ctx->tid + (int)((pass + 1) % (uint64_t)shared->threads)) % shared->threads;

    pthread_barrier_wait(&shared->barrier);

    for (size_t line = (size_t)write_mod; line < shared->line_count; line += (size_t)shared->threads) {
      uint64_t *base = shared->words + line * words_per_line;
      uint64_t seed = mix64(checksum ^ ((uint64_t)line << 7) ^ (pass * 0x100000001b3ULL));
      for (size_t w = 0; w < words_per_line; w++) {
        uint64_t value = base[w];
        value ^= seed + (uint64_t)w * 0x9e3779b97f4a7c15ULL;
        value = rotl64(value, (unsigned)((w + ctx->tid) % 23 + 5));
        value *= 0xd6e8feb86659fd93ULL;
        base[w] = value;
        checksum ^= mix64(value + seed + (uint64_t)w);
        seed = rotl64(seed + value, 11);
      }
    }

    pthread_barrier_wait(&shared->barrier);

    for (size_t line = (size_t)read_mod; line < shared->line_count; line += (size_t)shared->threads) {
      uint64_t *base = shared->words + line * words_per_line;
      uint64_t local = checksum ^ ((uint64_t)line << 3) ^ pass;
      for (size_t w = 0; w < words_per_line; w++) {
        local ^= mix64(base[w] + (uint64_t)w * 0x94d049bb133111ebULL);
      }
      checksum = rotl64(checksum ^ local, 9) + 0x9e3779b97f4a7c15ULL;
    }

    pthread_barrier_wait(&shared->barrier);
  }

  ctx->local_checksum = checksum;
  return NULL;
}

int main(int argc, char **argv) {
  if (argc < 3 || argc > 4) {
    fprintf(stderr, "Usage: %s <passes> <threads> [shared_mb]\n", argv[0]);
    return 2;
  }

  uint64_t passes = parse_u64(argv[1]);
  uint64_t threads_u64 = parse_u64(argv[2]);
  uint64_t shared_mb = (argc >= 4) ? parse_u64(argv[3]) : 256;
  if (passes == 0) die_msg("passes must be > 0");
  if (threads_u64 == 0 || threads_u64 > 256) die_msg("threads out of range");
  if (shared_mb == 0) die_msg("shared_mb must be > 0");
  int threads = (int)threads_u64;

  int allowed[CPU_SETSIZE];
  int allowed_count = collect_allowed_cpus(allowed, CPU_SETSIZE);
  if (allowed_count < threads) {
    fprintf(stderr, "need %d CPUs in taskset/affinity, only %d available\n", threads, allowed_count);
    return 1;
  }

  size_t shared_bytes = (size_t)shared_mb * 1024u * 1024u;
  size_t line_count = shared_bytes / CACHELINE_BYTES;
  if (line_count < (size_t)threads * 8u) {
    die_msg("shared buffer too small for thread count");
  }
  shared_bytes = line_count * CACHELINE_BYTES;

  void *mem = NULL;
  if (posix_memalign(&mem, CACHELINE_BYTES, shared_bytes) != 0 || mem == NULL) {
    die_msg("posix_memalign failed");
  }
  uint64_t *words = (uint64_t *)mem;
  for (size_t i = 0; i < shared_bytes / sizeof(uint64_t); i++) {
    words[i] = mix64((uint64_t)i * 0x9e3779b97f4a7c15ULL);
  }

  struct shared_state shared;
  shared.words = words;
  shared.line_count = line_count;
  shared.threads = threads;
  shared.passes = passes;
  if (pthread_barrier_init(&shared.barrier, NULL, (unsigned)threads) != 0) {
    die_msg("pthread_barrier_init failed");
  }

  pthread_t *tids = (pthread_t *)calloc((size_t)threads, sizeof(pthread_t));
  struct worker_ctx *ctx = (struct worker_ctx *)calloc((size_t)threads, sizeof(struct worker_ctx));
  if (!tids || !ctx) die_msg("oom");

  printf("[socket_rw_interact] passes=%" PRIu64 " threads=%d shared_mb=%" PRIu64 " shared_bytes=%zu\n",
         passes, threads, shared_mb, shared_bytes);
  printf("[socket_rw_interact] allowed_cpus=");
  for (int i = 0; i < threads; i++) {
    printf("%s%d", (i == 0 ? "" : ","), allowed[i]);
  }
  printf("\n");
  fflush(stdout);

  double t0 = monotonic_sec();
  for (int i = 0; i < threads; i++) {
    ctx[i].tid = i;
    ctx[i].cpu = allowed[i];
    ctx[i].shared = &shared;
    ctx[i].local_checksum = 0;
    if (pthread_create(&tids[i], NULL, worker_main, &ctx[i]) != 0) {
      die_msg("pthread_create failed");
    }
  }

  uint64_t checksum = 0;
  for (int i = 0; i < threads; i++) {
    if (pthread_join(tids[i], NULL) != 0) {
      die_msg("pthread_join failed");
    }
    checksum ^= mix64(ctx[i].local_checksum + (uint64_t)i * 17ULL);
  }
  double t1 = monotonic_sec();

  printf("[TOTAL] elapsed = %.6f s checksum = 0x%016" PRIx64 " lines=%zu\n",
         t1 - t0, checksum, line_count);

  pthread_barrier_destroy(&shared.barrier);
  free(ctx);
  free(tids);
  free(mem);
  return 0;
}
