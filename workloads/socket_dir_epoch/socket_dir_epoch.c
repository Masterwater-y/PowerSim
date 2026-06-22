#define _GNU_SOURCE

#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define CACHELINE_BYTES 64
#define WORDS_PER_LINE (CACHELINE_BYTES / sizeof(uint64_t))

struct shared_state {
  uint64_t *words;
  size_t line_count;
  int threads;
  uint64_t epochs;
  uint64_t reader_rounds;
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
  uint64_t checksum = mix64((uint64_t)(ctx->tid + 1) * 0x9e3779b97f4a7c15ULL);

  pin_current_thread(ctx->cpu);

  for (uint64_t epoch = 0; epoch < shared->epochs; epoch++) {
    int writer_tid = (int)(epoch % (uint64_t)shared->threads);

    pthread_barrier_wait(&shared->barrier);

    if (ctx->tid == writer_tid) {
      /*
       * Only one writer owns the hot region in this epoch.
       * The next epoch rotates ownership to another core, which should
       * trigger directory lookup and snoop activity more predictably.
       */
      for (size_t line = 0; line < shared->line_count; line++) {
        uint64_t *base = shared->words + line * WORDS_PER_LINE;
        uint64_t value = base[0];
        value ^= mix64((uint64_t)line ^ (epoch * 0x100000001b3ULL) ^ checksum);
        value = rotl64(value + (uint64_t)ctx->tid, (unsigned)((line + epoch) % 19 + 5));
        base[0] = value;
        checksum ^= mix64(value + line + epoch);
      }
    }

    pthread_barrier_wait(&shared->barrier);

    /*
     * Readers rebuild a multi-sharer state on the same hot region before
     * ownership rotates to the next writer.
     */
    for (uint64_t round = 0; round < shared->reader_rounds; round++) {
      if (ctx->tid != writer_tid) {
        size_t start = ((size_t)ctx->tid * 1315423911u + (size_t)round * 977u) % shared->line_count;
        for (size_t offset = 0; offset < shared->line_count; offset++) {
          size_t line = start + offset;
          if (line >= shared->line_count) line -= shared->line_count;
          uint64_t *base = shared->words + line * WORDS_PER_LINE;
          checksum ^= mix64(base[0] + (uint64_t)line + (round << 8));
        }
      }
      pthread_barrier_wait(&shared->barrier);
    }
  }

  ctx->local_checksum = checksum;
  return NULL;
}

int main(int argc, char **argv) {
  if (argc < 3 || argc > 5) {
    fprintf(stderr, "Usage: %s <epochs> <threads> [hot_mb] [reader_rounds]\n", argv[0]);
    return 2;
  }

  uint64_t epochs = parse_u64(argv[1]);
  uint64_t threads_u64 = parse_u64(argv[2]);
  uint64_t hot_mb = (argc >= 4) ? parse_u64(argv[3]) : 8;
  uint64_t reader_rounds = (argc >= 5) ? parse_u64(argv[4]) : 2;
  if (epochs == 0) die_msg("epochs must be > 0");
  if (threads_u64 < 2 || threads_u64 > 256) die_msg("threads must be in [2, 256]");
  if (hot_mb == 0) die_msg("hot_mb must be > 0");
  if (reader_rounds == 0) die_msg("reader_rounds must be > 0");
  int threads = (int)threads_u64;

  int allowed[CPU_SETSIZE];
  int allowed_count = collect_allowed_cpus(allowed, CPU_SETSIZE);
  if (allowed_count < threads) {
    fprintf(stderr, "need %d CPUs in taskset/affinity, only %d available\n", threads, allowed_count);
    return 1;
  }

  size_t hot_bytes = (size_t)hot_mb * 1024u * 1024u;
  size_t line_count = hot_bytes / CACHELINE_BYTES;
  if (line_count < (size_t)threads * 1024u) {
    die_msg("hot buffer too small for thread count");
  }
  hot_bytes = line_count * CACHELINE_BYTES;

  void *mem = NULL;
  if (posix_memalign(&mem, CACHELINE_BYTES, hot_bytes) != 0 || mem == NULL) {
    die_msg("posix_memalign failed");
  }
  uint64_t *words = (uint64_t *)mem;
  for (size_t i = 0; i < hot_bytes / sizeof(uint64_t); i++) {
    words[i] = mix64((uint64_t)i * 0x9e3779b97f4a7c15ULL);
  }

  struct shared_state shared;
  shared.words = words;
  shared.line_count = line_count;
  shared.threads = threads;
  shared.epochs = epochs;
  shared.reader_rounds = reader_rounds;
  if (pthread_barrier_init(&shared.barrier, NULL, (unsigned)threads) != 0) {
    die_msg("pthread_barrier_init failed");
  }

  pthread_t *tids = (pthread_t *)calloc((size_t)threads, sizeof(pthread_t));
  struct worker_ctx *ctx = (struct worker_ctx *)calloc((size_t)threads, sizeof(struct worker_ctx));
  if (!tids || !ctx) die_msg("oom");

  printf("[socket_dir_epoch] epochs=%" PRIu64 " threads=%d hot_mb=%" PRIu64
         " reader_rounds=%" PRIu64 " hot_bytes=%zu\n",
         epochs, threads, hot_mb, reader_rounds, hot_bytes);
  printf("[socket_dir_epoch] allowed_cpus=");
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
    checksum ^= mix64(ctx[i].local_checksum + (uint64_t)i * 131ULL);
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
