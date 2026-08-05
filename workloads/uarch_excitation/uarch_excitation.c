/*
 * Source-aligned microarchitecture excitation workloads.
 *
 * ABI: ./workload <nthreads> <scale> 1 <seed>
 *
 * Setup, allocation, first touch, thread creation, affinity and the only
 * barrier are outside the ROI.  Each worker owns its data, so the capacity
 * sweeps measure core structures instead of coherence accidents.
 */
#define _GNU_SOURCE

#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef UARCH_KIND
#error "UARCH_KIND is required"
#endif
#ifndef UARCH_NAME
#define UARCH_NAME "uarch_unknown"
#endif
#ifndef UARCH_DEPTH
#define UARCH_DEPTH 1
#endif
#ifndef UARCH_FOOTPRINT
#define UARCH_FOOTPRINT 0
#endif

#define MAX_THREADS 64
#define LINE_BYTES 64UL
#define PAGE_BYTES 4096UL
#define MiB (1024UL * 1024UL)
#define STR_INNER(x) #x
#define STR(x) STR_INNER(x)

typedef struct {
    int threads;
    uint64_t scale;
    uint64_t seed;
} config_t;

typedef struct {
    const config_t *config;
    pthread_barrier_t *barrier;
    uint64_t *data;
    size_t words;
    int tid;
    uint64_t result;
} worker_t;

static volatile uint64_t sink;
static int disable_m5;

static inline void m5_work_begin(uint64_t workid, uint64_t threadid)
{
    if (disable_m5) return;
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005a"
                         : : "D"(workid), "S"(threadid) : "rax", "memory");
}

static inline void m5_work_end(uint64_t workid, uint64_t threadid)
{
    if (disable_m5) return;
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x005b"
                         : : "D"(workid), "S"(threadid) : "rax", "memory");
}

static inline void m5_quiesce(void)
{
    if (disable_m5) return;
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x0001"
                         : : : "rax", "memory");
}

static uint64_t splitmix64(uint64_t *state)
{
    uint64_t z = (*state += UINT64_C(0x9e3779b97f4a7c15));
    z = (z ^ (z >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    z = (z ^ (z >> 27)) * UINT64_C(0x94d049bb133111eb);
    return z ^ (z >> 31);
}

static void pin_cpu(int tid)
{
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET((unsigned)tid, &set);
    (void)pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
}

static void parse_args(int argc, char **argv, config_t *config)
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s <threads> <scale> [ignored] [seed]\n", argv[0]);
        exit(2);
    }
    config->threads = atoi(argv[1]);
    config->scale = strtoull(argv[2], NULL, 0);
    config->seed = argc > 4 ? strtoull(argv[4], NULL, 0) : 1;
    if (config->threads < 1 || config->threads > MAX_THREADS ||
        config->scale == 0) {
        fprintf(stderr, "%s: invalid threads/scale\n", UARCH_NAME);
        exit(2);
    }
}

static inline uint64_t add_chains(uint64_t seed, int wait_on_seed)
{
    uint64_t a = wait_on_seed ? seed : UINT64_C(0x1020304050607080);
    uint64_t b = wait_on_seed ? seed : UINT64_C(0x2131415161718191);
    uint64_t c = wait_on_seed ? seed : UINT64_C(0x32425262728292a2);
    uint64_t d = wait_on_seed ? seed : UINT64_C(0x435363738393a3b3);
    uint64_t e = wait_on_seed ? seed : UINT64_C(0x5464748494a4b4c4);
    uint64_t f = wait_on_seed ? seed : UINT64_C(0x65758595a5b5c5d5);
    uint64_t g = wait_on_seed ? seed : UINT64_C(0x768696a6b6c6d6e6);
    uint64_t h = wait_on_seed ? seed : UINT64_C(0x8797a7b7c7d7e7f7);
    /* Eight independent one-cycle chains. UARCH_DEPTH produces exactly
     * 8*depth ALU instructions. In the IQ family all chains wait on the
     * same miss result; in the ROB family they execute behind a head miss. */
    __asm__ __volatile__(
        ".rept " STR(UARCH_DEPTH) "\n\t"
        "addq $3, %0\n\taddq $5, %1\n\taddq $7, %2\n\taddq $11, %3\n\t"
        "addq $13, %4\n\taddq $17, %5\n\taddq $19, %6\n\taddq $23, %7\n\t"
        ".endr\n\t"
        : "+r"(a), "+r"(b), "+r"(c), "+r"(d),
          "+r"(e), "+r"(f), "+r"(g), "+r"(h)
        : : "cc", "memory");
    return a ^ b ^ c ^ d ^ e ^ f ^ g ^ h ^ seed;
}

#if UARCH_KIND == 1 || UARCH_KIND == 2
static uint64_t run_window(worker_t *worker, int iq_wait)
{
    const uint64_t dynamic_alu = 8U * UARCH_DEPTH;
    const uint64_t iterations =
        (worker->config->scale * UINT64_C(1000000) + dynamic_alu - 1) /
        dynamic_alu;
    size_t cursor = (size_t)worker->tid * 131U % worker->words;
    uint64_t total = worker->config->seed ^ (uint64_t)worker->tid;
    volatile uint64_t *chain = worker->data;
    for (uint64_t i = 0; i < iterations; ++i) {
        const uint64_t loaded = chain[cursor * (LINE_BYTES / sizeof(uint64_t))];
        const uint64_t work = add_chains(loaded, iq_wait);
        cursor = (size_t)((loaded ^ (work & 63U)) % worker->words);
        total ^= work + i;
    }
    return total ^ cursor;
}
#endif

#if UARCH_KIND == 3
static uint64_t run_dtlb(worker_t *worker)
{
    const size_t pages = UARCH_FOOTPRINT;
    const uint64_t iterations = worker->config->scale * UINT64_C(180000);
    uint64_t state = worker->config->seed ^ (uint64_t)(worker->tid + 1);
    uint64_t total = 0;
    for (uint64_t i = 0; i < iterations; ++i) {
        const size_t page = (size_t)(splitmix64(&state) % pages);
        total += worker->data[page * (PAGE_BYTES / sizeof(uint64_t))];
    }
    return total ^ state;
}
#endif

#if UARCH_KIND == 4
static uint64_t run_cache(worker_t *worker)
{
    const size_t lines = UARCH_FOOTPRINT / LINE_BYTES;
    const uint64_t iterations = worker->config->scale * UINT64_C(220000);
    uint64_t state = worker->config->seed ^ (uint64_t)(worker->tid + 1);
    uint64_t total = 0;
    for (uint64_t i = 0; i < iterations; ++i) {
        const size_t line = (size_t)(splitmix64(&state) % lines);
        total += worker->data[line * (LINE_BYTES / sizeof(uint64_t))];
    }
    return total ^ state;
}
#endif

static void *worker_main(void *opaque)
{
    worker_t *worker = opaque;
    pin_cpu(worker->tid);
    pthread_barrier_wait(worker->barrier);
    m5_work_begin(0, (uint64_t)worker->tid);
#if UARCH_KIND == 1
    worker->result = run_window(worker, 0);
#elif UARCH_KIND == 2
    worker->result = run_window(worker, 1);
#elif UARCH_KIND == 3
    worker->result = run_dtlb(worker);
#elif UARCH_KIND == 4
    worker->result = run_cache(worker);
#else
#error "unknown UARCH_KIND"
#endif
    m5_work_end(0, (uint64_t)worker->tid);
    m5_quiesce();
    return NULL;
}

int main(int argc, char **argv)
{
    config_t config;
    worker_t workers[MAX_THREADS];
    pthread_t threads[MAX_THREADS];
    pthread_barrier_t barrier;
    parse_args(argc, argv, &config);
    disable_m5 = getenv("TAO_DISABLE_M5") != NULL;
    if (pthread_barrier_init(&barrier, NULL, (unsigned)config.threads) != 0)
        return 1;

    for (int tid = 0; tid < config.threads; ++tid) {
        size_t bytes;
#if UARCH_KIND == 1 || UARCH_KIND == 2
        bytes = 32UL * MiB;
#elif UARCH_KIND == 3
        bytes = (size_t)UARCH_FOOTPRINT * PAGE_BYTES;
#else
        bytes = (size_t)UARCH_FOOTPRINT;
#endif
        void *allocation = NULL;
        if (posix_memalign(&allocation, PAGE_BYTES, bytes) != 0) return 1;
        uint64_t *data = allocation;
        size_t words = bytes / sizeof(uint64_t);
        uint64_t state = config.seed ^ (uint64_t)(tid + 1);
#if UARCH_KIND == 1 || UARCH_KIND == 2
        /* Touch only one word per line. A large odd stride over the power-of-
         * two line count is a full-cycle pointer chain, but setup costs 8x
         * fewer Atomic instructions than initializing every unused word. */
        const size_t entries = bytes / LINE_BYTES;
        const size_t mask = entries - 1;
        const size_t step = ((size_t)splitmix64(&state) | 1U) & mask;
        for (size_t line = 0; line < entries; ++line)
            data[line * (LINE_BYTES / sizeof(uint64_t))] =
                (line + step) & mask;
        words = entries;
#else
        for (size_t word = 0; word < words; ++word)
            data[word] = splitmix64(&state) % words;
#endif
        workers[tid] = (worker_t){
            .config = &config, .barrier = &barrier, .data = data,
            .words = words, .tid = tid, .result = 0,
        };
    }
    for (int tid = 1; tid < config.threads; ++tid)
        if (pthread_create(&threads[tid], NULL, worker_main, &workers[tid]) != 0)
            return 1;
    worker_main(&workers[0]);
    for (int tid = 1; tid < config.threads; ++tid)
        pthread_join(threads[tid], NULL);
    uint64_t total = 0;
    for (int tid = 0; tid < config.threads; ++tid) {
        total ^= workers[tid].result;
        free(workers[tid].data);
    }
    sink = total;
    if (disable_m5) printf("%s checksum=%llu\n", UARCH_NAME,
                           (unsigned long long)sink);
    return 0;
}
