/* Actual libgomp fork/join, team reuse, imbalance and explicit barriers. */
#define _GNU_SOURCE
#include <omp.h>
#include <stdio.h>
#include <stdlib.h>
#include <sched.h>

#ifdef FASTSIM_FS_ROI
#include "gem5_roi_marker.h"
#endif

static volatile unsigned long values[64];

int main(int argc, char **argv)
{
    int threads = argc > 1 ? atoi(argv[1]) : 2;
    int rounds = argc > 2 ? atoi(argv[2]) : 3;
    int work = argc > 3 ? atoi(argv[3]) : 200;
    int pin = argc > 4 ? atoi(argv[4]) : 0;
    int workshare = argc > 5 ? atoi(argv[5]) : 0;
    if (threads < 1 || threads > 64 || rounds < 1 || work < 1)
        return 2;
    omp_set_dynamic(0);
    omp_set_num_threads(threads);
    if (pin) {
        cpu_set_t mask;
        CPU_ZERO(&mask);
        CPU_SET(0, &mask);
        if (sched_setaffinity(0, sizeof(mask), &mask)) return 3;
    }
#ifdef FASTSIM_FS_ROI
    gem5_roi_checkpoint_once("sync-probe-checkpoint", 1);
    gem5_roi_begin_once("sync-probe-measurement", 1);
#endif
    for (int round = 0; round < rounds; ++round) {
#pragma omp parallel
        {
            int member = omp_get_thread_num();
            if (pin) {
                cpu_set_t mask;
                CPU_ZERO(&mask);
                CPU_SET(member, &mask);
                if (sched_setaffinity(0, sizeof(mask), &mask)) abort();
            }
            unsigned long value = 0;
            for (int i = 0; i < work * (member + 1); ++i)
                value += (unsigned long)i;
            values[member] = value;
            if (workshare) {
#pragma omp for schedule(dynamic, 1)
                for (int item = 0; item < threads * 2; ++item)
                    values[member] += 0;
            }
#pragma omp barrier
            if (member == 0) {
                unsigned long sum = 0;
                for (int j = 0; j < threads; ++j)
                    sum += values[j];
                values[threads - 1] = sum;
            }
        }
    }
    unsigned long expected = 0;
    for (int j = 0; j < threads; ++j) {
        unsigned long n = (unsigned long)work * (j + 1);
        expected += n * (n - 1) / 2;
    }
    printf("sync-probe threads=%d rounds=%d result=%lu expected=%lu\n",
           threads, rounds, values[threads - 1], expected);
#ifdef FASTSIM_SYNC_HOST_CAPTURE
    extern void fastsim_sync_host_finish(void);
    fastsim_sync_host_finish();
#endif
    return values[threads - 1] != expected;
}
