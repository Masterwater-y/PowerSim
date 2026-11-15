#define _GNU_SOURCE
#include <pthread.h>
#include <stdint.h>
#include <stdatomic.h>
#include "fastsim_native_roi.h"

/* Tiny input, four existing workers and the native launcher's identity gate.
 * The modes exercise actual data paths; path coverage must be audited, never
 * inferred from the mode name. No software trace/cache/timing labels emitted. */
enum { Workers = 4, Bytes = 8 * 1024 * 1024 };
static unsigned char arena[Workers][Bytes] __attribute__((aligned(4096)));
static _Atomic uint64_t shared[64] __attribute__((aligned(4096)));
static volatile uint64_t sink[Workers] __attribute__((aligned(64)));
static unsigned mode;

static uint64_t load64(const unsigned char *p)
{
    uint64_t value;
    __asm__ volatile("movq (%1), %0" : "=r"(value) : "r"(p) : "memory");
    return value;
}

static void *worker(void *unused)
{
    (void)unused;
    fastsim_native_pin_worker();
    const unsigned id = (unsigned)fastsim_native_worker_index;
    if (id >= Workers) abort();
    unsigned char *data = arena[id];
    for (unsigned i = 0; i < Bytes; i += 64) {
        data[i] = (unsigned char)(i / 64);
        data[i + 63] = (unsigned char)id;
    }
    fastsim_native_roi_ready("spr-retired-cache-small");
    uint64_t total = 0;
    for (uint64_t iteration = 0; iteration < 20000000; ++iteration) {
        unsigned offset = (unsigned)((iteration * 4096) & (Bytes - 4096));
        if (mode == 0) {
            total += load64(data + offset);
            total += load64(data + offset); /* potential active-fill follower */
            total += load64(data + (iteration & 63) * 64);
            __builtin_prefetch(data + ((offset + 4096) & (Bytes - 1)), 0, 3);
        } else if (mode == 1) {
            uint64_t value = iteration + id;
            __asm__ volatile("movq %1, (%0)" :: "r"(data + offset + 56),
                             "r"(value) : "memory");
            total += load64(data + offset + 56); /* full forwarding */
            total += load64(data + offset + 63); /* two physical cache lines */
            total += load64(data + offset + 60); /* partial forwarded bytes */
        } else {
            unsigned index = (unsigned)(iteration & 63);
            atomic_store_explicit(&shared[index], iteration + id, memory_order_relaxed);
            total += atomic_load_explicit(&shared[index], memory_order_relaxed);
            total += atomic_load_explicit(&shared[(index + 1) & 63], memory_order_relaxed);
            total += load64(data + offset);
        }
        sink[id] = total;
    }
    return NULL;
}

int main(int argc, char **argv)
{
    mode = argc > 1 ? (unsigned)strtoul(argv[1], NULL, 10) : 0;
    if (mode > 2) return 2;
    pthread_t workers[Workers];
    for (unsigned i = 0; i < Workers; ++i)
        if (pthread_create(&workers[i], NULL, worker, NULL)) return 3;
    for (unsigned i = 0; i < Workers; ++i)
        if (pthread_join(workers[i], NULL)) return 4;
    return 0;
}
