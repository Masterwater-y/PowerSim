#ifndef FASTSIM_NATIVE_ROI_H
#define FASTSIM_NATIVE_ROI_H

/* Collection-only gates for an existing native worker pool. No worker is
 * created here. Main/helper threads retain the launcher's CPU affinity. */
#include "fastsim_rate_roi.h"
#include <string.h>

static __thread int fastsim_native_worker_index = -1;
static __thread int fastsim_native_entered;
static unsigned fastsim_native_next_worker;

static void fastsim_native_pin_worker(void) {
    const char *text = getenv("FASTSIM_NATIVE_CPUS");
    if (!text || fastsim_native_worker_index >= 0) return;
    unsigned index = __sync_fetch_and_add(&fastsim_native_next_worker, 1);
    unsigned expected = (unsigned)atoi(getenv("FASTSIM_NATIVE_WORKERS"));
    if (index >= expected) {
        fprintf(stderr, "native gate observed extra worker\n"); exit(EXIT_FAILURE);
    }
    unsigned cpu_count = 1;
    for (const char *p = text; *p; ++p) if (*p == ',') ++cpu_count;
    /* A native producer/main may share CPU0 with the last worker (XZ).
     * Keep every OS task in the census and identity set. */
    for (unsigned i = 0; i < index % cpu_count; ++i) {
        text = strchr(text, ',');
        if (!text) { fprintf(stderr, "native CPU list too short\n"); exit(EXIT_FAILURE); }
        ++text;
    }
    int cpu = atoi(text);
    cpu_set_t mask; CPU_ZERO(&mask); CPU_SET(cpu, &mask);
    if (sched_setaffinity(0, sizeof(mask), &mask)) {
        perror("native worker affinity"); exit(EXIT_FAILURE);
    }
    fastsim_native_worker_index = (int)index;
}

static void fastsim_native_roi_ready(const char *name) {
    /* This hot-path check must precede every environment lookup. */
    if (fastsim_native_entered) return;
    const char *ready = getenv("FASTSIM_RATE_READY_FD");
    const char *release = getenv("FASTSIM_RATE_RELEASE_FD");
    const char *continuation = getenv("FASTSIM_NATIVE_CONTINUE_FD");
    if (!getenv("FASTSIM_NATIVE_CPUS") || !ready || !release) return;
    if (!continuation) { fprintf(stderr, "native gate missing continuation channel\n"); exit(EXIT_FAILURE); }
    fastsim_native_pin_worker();
    fastsim_native_entered = 1;
    fprintf(stderr, "[fastsim-native] ready worker=%d tid=%ld cpu=%d phase=%s\n",
            fastsim_native_worker_index, syscall(SYS_gettid), sched_getcpu(), name);
    char token = 'R'; ssize_t n;
    do { n = write(atoi(ready), &token, 1); } while (n < 0 && errno == EINTR);
    if (n != 1) { perror("native ready"); exit(EXIT_FAILURE); }
    do { n = read(atoi(release), &token, 1); } while (n < 0 && errno == EINTR);
    if (n != 1 || token != 'G') {
        fprintf(stderr, "native gate missing release\n"); exit(EXIT_FAILURE);
    }
    /* Descriptors belong to the process and are shared by all workers. */
    unsigned long long pid = (unsigned long long)getpid();
    unsigned long long tid = (unsigned long long)syscall(SYS_gettid);
    fastsim_wait_marker(1, (pid << 32) | tid, tid, 0, 0, pid);
    token = 'A';
    do { n = write(atoi(ready), &token, 1); } while (n < 0 && errno == EINTR);
    if (n != 1) { perror("native identity acknowledgement"); exit(EXIT_FAILURE); }
    /* A distinct pipe prevents this read stealing another worker's G token. */
    do { n = read(atoi(continuation), &token, 1); } while (n < 0 && errno == EINTR);
    if (n != 1 || token != 'C') {
        fprintf(stderr, "native gate missing identity admission\n"); exit(EXIT_FAILURE);
    }
}
#endif
