#ifndef TCSIM_GEM5_ROI_MARKER_H
#define TCSIM_GEM5_ROI_MARKER_H

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

#ifndef MAP_ANONYMOUS
#define MAP_ANONYMOUS 0x20
#endif

#if !defined(__cplusplus)
extern int setenv(const char *, const char *, int);
#endif

typedef struct {
    volatile int ready;
    volatile int start;
    volatile int warmed;
    volatile int roi_go;
    int workers;
} gem5_roi_wave_control;

static inline void
gem5_roi_qemu_hint(unsigned int command, uint64_t value)
{
#if defined(__x86_64__)
    register uint64_t payload __asm__("r15") = value;
    if (command == 0) {
        __asm__ volatile(
            ".byte 0x41, 0x0f, 0x1f, 0x47, 0x00"
            : "+r"(payload) : : "memory");
    } else if (command == 12) {
        __asm__ volatile(
            ".byte 0x41, 0x0f, 0x1f, 0x47, 0x0c"
            : "+r"(payload) : : "memory");
    } else {
        abort();
    }
#else
#error "QEMU user-only ROI hints are implemented only for x86-64"
#endif
}

static inline gem5_roi_wave_control *
gem5_roi_create_wave_control(int workers)
{
    gem5_roi_wave_control *control;
    char value[96];

    control = (gem5_roi_wave_control *)mmap(
        NULL, sizeof(*control), PROT_READ | PROT_WRITE,
        MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    if (control == MAP_FAILED) {
        perror("mmap QEMU ROI wave control");
        exit(EXIT_FAILURE);
    }
    memset(control, 0, sizeof(*control));
    control->workers = workers;
    snprintf(value, sizeof(value), "%llx",
             (unsigned long long)(uintptr_t)control);
    if (setenv("GEM5_ROI_WAVE_CONTROL", value, 1) != 0) {
        perror("setenv GEM5_ROI_WAVE_CONTROL");
        exit(EXIT_FAILURE);
    }
    return control;
}

static inline gem5_roi_wave_control *
gem5_roi_get_wave_control(void)
{
    const char *value = getenv("GEM5_ROI_WAVE_CONTROL");
    unsigned long long address;

    if (!value || sscanf(value, "%llx", &address) != 1)
        return NULL;
    return (gem5_roi_wave_control *)(uintptr_t)address;
}

static inline int
gem5_roi_worker_index(void)
{
    const char *value = getenv("GEM5_WORKER_INDEX");
    return value ? atoi(value) : 0;
}

static inline void
gem5_roi_wait_until(volatile int *value, int target)
{
    while (__atomic_load_n(value, __ATOMIC_ACQUIRE) < target)
        __asm__ volatile("pause" ::: "memory");
}

static inline void
gem5_roi_log(const char *marker, const char *operation, uint64_t workid)
{
    fprintf(stderr, "[qemu-roi] marker=%s operation=%s workid=%llu\n",
            marker, operation, (unsigned long long)workid);
    fflush(stderr);
}

static inline void
gem5_roi_checkpoint_once(const char *marker, uint64_t workid)
{
    static int fired;

    if (fired || getenv("GEM5_DISABLE_ROI_MARKER"))
        return;
    fired = 1;
    gem5_roi_log(marker, "start", workid);
    gem5_roi_qemu_hint(0, workid);
}

static inline void
gem5_roi_begin_once(const char *marker, uint64_t workid)
{
    static int fired;

    if (fired || getenv("GEM5_DISABLE_ROI_MARKER"))
        return;
    fired = 1;
    gem5_roi_log(marker, "measurement", workid);
    gem5_roi_qemu_hint(12, workid);
}

static inline void
gem5_roi_wave_checkpoint(const char *marker, uint64_t workid)
{
    static int arrived;
    gem5_roi_wave_control *control = gem5_roi_get_wave_control();

    if (arrived)
        return;
    arrived = 1;
    if (!control) {
        gem5_roi_checkpoint_once(marker, workid);
        return;
    }
    __atomic_add_fetch(&control->ready, 1, __ATOMIC_ACQ_REL);
    if (gem5_roi_worker_index() == 0) {
        gem5_roi_wait_until(&control->ready, control->workers);
        gem5_roi_checkpoint_once(marker, workid);
        __atomic_store_n(&control->start, 1, __ATOMIC_RELEASE);
    } else {
        gem5_roi_wait_until(&control->start, 1);
    }
}

static inline void
gem5_roi_wave_begin(const char *marker, uint64_t workid)
{
    static int arrived;
    gem5_roi_wave_control *control = gem5_roi_get_wave_control();

    if (arrived)
        return;
    arrived = 1;
    if (!control) {
        gem5_roi_begin_once(marker, workid);
        return;
    }
    __atomic_add_fetch(&control->warmed, 1, __ATOMIC_ACQ_REL);
    if (gem5_roi_worker_index() == 0) {
        gem5_roi_wait_until(&control->warmed, control->workers);
        gem5_roi_begin_once(marker, workid);
        __atomic_store_n(&control->roi_go, 1, __ATOMIC_RELEASE);
    } else {
        gem5_roi_wait_until(&control->roi_go, 1);
    }
}

#endif
