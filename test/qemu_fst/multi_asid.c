#define _GNU_SOURCE

#include <sched.h>
#include <signal.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

#include "gem5_roi_marker.h"

enum {
    Cores = 4,
    ProcessesPerCore = 2,
    Processes = Cores * ProcessesPerCore,
};

typedef struct {
    volatile unsigned ready;
    volatile unsigned go;
    volatile unsigned entered;
    volatile unsigned blockers_ready;
} Shared;

typedef struct {
    Shared *shared;
    unsigned core;
} BlockerArgs;

static void
pin_to_core(unsigned core)
{
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(core, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0) {
        perror("sched_setaffinity");
        _exit(2);
    }
}

static void *
run_blocker(void *opaque)
{
    BlockerArgs *args = opaque;
    pin_to_core(args->core);
    while (!__atomic_load_n(&args->shared->go, __ATOMIC_ACQUIRE)) {
        sched_yield();
    }
    __atomic_add_fetch(
        &args->shared->blockers_ready, 1, __ATOMIC_ACQ_REL);
    for (;;) {
        pause();
    }
    return NULL;
}

static void
run_worker(Shared *shared, unsigned worker)
{
    const unsigned core = worker % Cores;
    pin_to_core(core);
    BlockerArgs blocker_args = {shared, core};
    pthread_t blocker;
    if (pthread_create(&blocker, NULL, run_blocker, &blocker_args) != 0) {
        perror("pthread_create");
        _exit(2);
    }
    __atomic_add_fetch(&shared->ready, 1, __ATOMIC_ACQ_REL);
    while (!__atomic_load_n(&shared->go, __ATOMIC_ACQUIRE)) {
        sched_yield();
    }
    while (__atomic_load_n(
               &shared->blockers_ready, __ATOMIC_ACQUIRE) != Processes) {
        sched_yield();
    }

    volatile uint64_t value = worker + 1;
    for (unsigned step = 0; step < 4096; ++step) {
        value = value * UINT64_C(6364136223846793005) +
            UINT64_C(1442695040888963407);
        value ^= value >> 17;
    }
    sched_yield();
    __atomic_add_fetch(&shared->entered, 1, __ATOMIC_ACQ_REL);
    for (;;) {
        for (unsigned iteration = 0; iteration < 4096; ++iteration) {
            value = value * UINT64_C(6364136223846793005) +
                UINT64_C(1442695040888963407);
            value ^= value >> 17;
        }
        sched_yield();
    }
}

int
main(void)
{
    Shared *shared = mmap(
        NULL, sizeof(*shared), PROT_READ | PROT_WRITE,
        MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    if (shared == MAP_FAILED) {
        perror("mmap");
        return 2;
    }
    shared->ready = 0;
    shared->go = 0;
    shared->entered = 0;
    shared->blockers_ready = 0;

    for (unsigned worker = 0; worker < Processes; ++worker) {
        const pid_t child = fork();
        if (child < 0) {
            perror("fork");
            return 2;
        }
        if (child == 0) {
            run_worker(shared, worker);
        }
    }

    while (__atomic_load_n(&shared->ready, __ATOMIC_ACQUIRE) != Processes) {
        sched_yield();
    }
    pin_to_core(0);
    gem5_roi_checkpoint_once("multi-asid-ready", 1);
    __atomic_store_n(&shared->go, 1, __ATOMIC_RELEASE);
    while (__atomic_load_n(&shared->entered, __ATOMIC_ACQUIRE) != Processes) {
        sched_yield();
    }
    gem5_roi_begin_once("multi-asid-measurement", 1);

    for (;;) {
        pause();
    }
}
