#ifndef TCSIM_GEM5_ROI_MARKER_H
#define TCSIM_GEM5_ROI_MARKER_H

/*
 * Drop-in ROI marker adapter. In gem5 it preserves address-mode pseudo-ops;
 * under the sidecar launcher it emits the same semantic markers over a pipe.
 */

#include "sidecar_roi.h"

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef MAP_ANONYMOUS
#define MAP_ANONYMOUS 0x20
#endif

#if !defined(__cplusplus)
extern int setenv(const char *, const char *, int);
extern int unsetenv(const char *);
#endif

#define GEM5_ROI_M5OP_CHECKPOINT 0x43
#define GEM5_ROI_M5OP_WORK_BEGIN 0x5a
#define GEM5_ROI_M5OP_RANGE_SIZE 0x10000

/*
 * Fixed physical base of the gem5 x86 full-system m5ops MMIO range
 * (X86Board.m5ops_base). The upstream gem5_bridge kernel module remaps this
 * page to device offset 0; when that module is absent we map the same physical
 * page through /dev/mem, which the stock gem5 x86-ubuntu image exposes and its
 * own /sbin/m5 already uses. Override with GEM5_M5OPS_BASE if a board moves it.
 */
#ifndef GEM5_ROI_M5OPS_PHYS_BASE
#define GEM5_ROI_M5OPS_PHYS_BASE 0xFFFF0000UL
#endif

typedef struct {
    volatile int ready;
    volatile int start;
    volatile int warmed;
    volatile int roi_go;
    int workers;
} gem5_roi_wave_control;

static inline unsigned long gem5_roi_m5ops_phys_base(void)
{
    const char *value = getenv("GEM5_M5OPS_BASE");
    if (value && *value)
        return strtoul(value, NULL, 0);
    return GEM5_ROI_M5OPS_PHYS_BASE;
}

static inline volatile unsigned char *gem5_roi_m5op_base(void)
{
    static volatile unsigned char *base;
    int fd;
    void *mapping;

    if (base)
        return base;
    /* Preferred path: the gem5_bridge misc device remaps the MMIO page to
     * offset 0, so the same code generates a trace under either launcher. */
    fd = open("/dev/gem5_bridge", O_RDWR | O_SYNC);
    if (fd >= 0) {
        mapping = mmap(NULL, GEM5_ROI_M5OP_RANGE_SIZE,
                       PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        close(fd);
        if (mapping == MAP_FAILED) {
            fprintf(stderr, "mmap /dev/gem5_bridge failed: %s\n",
                    strerror(errno));
            exit(EXIT_FAILURE);
        }
        base = (volatile unsigned char *)mapping;
        return base;
    }

    /* Fallback: map the fixed m5ops physical page through /dev/mem. This keeps
     * source-level ROI markers working on a stock gem5 x86 image that has no
     * gem5_bridge kernel module. */
    fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) {
        fprintf(stderr, "open /dev/gem5_bridge and /dev/mem both failed: %s\n",
                strerror(errno));
        exit(EXIT_FAILURE);
    }
    mapping = mmap(NULL, GEM5_ROI_M5OP_RANGE_SIZE, PROT_READ | PROT_WRITE,
                   MAP_SHARED, fd, (off_t)gem5_roi_m5ops_phys_base());
    close(fd);
    if (mapping == MAP_FAILED) {
        fprintf(stderr, "mmap /dev/mem at m5ops base 0x%lx failed: %s\n",
                gem5_roi_m5ops_phys_base(), strerror(errno));
        exit(EXIT_FAILURE);
    }
    base = (volatile unsigned char *)mapping;
    return base;
}

static inline void gem5_roi_m5op(uint64_t operation, uint64_t arg0,
                                 uint64_t arg1)
{
    volatile unsigned char *address =
        gem5_roi_m5op_base() + (operation << 8);

#if defined(__x86_64__)
    __asm__ volatile(
        "movq %%rax, (%[address])"
        :
        : "a"((uint64_t)0), "D"(arg0), "S"(arg1), [address] "r"(address)
        : "memory");
#else
#error "Address-mode gem5 ROI markers are implemented only for x86_64"
#endif
}

static inline gem5_roi_wave_control *gem5_roi_create_wave_control(int workers)
{
    gem5_roi_wave_control *control;
    char value[96];

    control = (gem5_roi_wave_control *)mmap(
        NULL, sizeof(*control), PROT_READ | PROT_WRITE,
        MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    if (control == MAP_FAILED) {
        perror("mmap gem5 ROI wave control");
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

static inline gem5_roi_wave_control *gem5_roi_get_wave_control(void)
{
    const char *value = getenv("GEM5_ROI_WAVE_CONTROL");
    unsigned long long address;

    if (!value || sscanf(value, "%llx", &address) != 1)
        return NULL;
    return (gem5_roi_wave_control *)(uintptr_t)address;
}

static inline int gem5_roi_worker_index(void)
{
    const char *value = getenv("GEM5_WORKER_INDEX");
    return value ? atoi(value) : 0;
}

static inline int gem5_roi_worker_cpu(int worker)
{
    const char *cursor = getenv("SIDECAR_TARGET_CPUS");
    int index = 0;

    if (!sidecar_roi_enabled())
        return worker;
    if (!cursor || !*cursor || worker < 0)
        return -1;
    while (*cursor) {
        char *end = NULL;
        long cpu = strtol(cursor, &end, 10);

        if (end == cursor || cpu < 0 || cpu > 1048575 ||
            (*end && *end != ','))
            return -1;
        if (index == worker)
            return (int)cpu;
        if (*end != ',')
            return -1;
        cursor = end + 1;
        index++;
    }
    return -1;
}

static inline void gem5_roi_wait_until(volatile int *value, int target)
{
    while (__atomic_load_n(value, __ATOMIC_ACQUIRE) < target)
        __asm__ volatile("pause" ::: "memory");
}

static inline void gem5_roi_emit(const char *marker, const char *operation,
                                 uint64_t workid)
{
    fprintf(stderr, "[gem5-roi] marker=%s operation=%s workid=%llu\n",
            marker, operation, (unsigned long long)workid);
    fflush(stderr);
    if (strcmp(operation, "checkpoint") != 0) {
        fprintf(stderr, "unsupported gem5 ROI operation: %s\n", operation);
        exit(EXIT_FAILURE);
    }
    gem5_roi_m5op(GEM5_ROI_M5OP_CHECKPOINT, 0, 0);
}

static inline void gem5_roi_checkpoint_once(const char *marker,
                                            uint64_t workid)
{
    static int fired;
    const char *operation;

    if (fired || (!sidecar_roi_enabled() &&
                  getenv("GEM5_DISABLE_ROI_MARKER")))
        return;
    fired = 1;
    if (sidecar_roi_enabled()) {
        sidecar_roi_notify(SIDECAR_ROI_PREPARE, marker, workid);
        return;
    }
    operation = getenv("GEM5_ROI_MARKER_OP");
    if (!operation || !*operation)
        operation = "checkpoint";
    gem5_roi_emit(marker, operation, workid);
}

static inline void gem5_roi_begin_once(const char *marker, uint64_t workid)
{
    static int fired;

    if (fired || (!sidecar_roi_enabled() &&
                  getenv("GEM5_DISABLE_ROI_MARKER")))
        return;
    fired = 1;
    if (sidecar_roi_enabled()) {
        sidecar_roi_notify(SIDECAR_ROI_BEGIN, marker, workid);
        return;
    }
    fprintf(stderr, "[gem5-roi] marker=%s operation=workbegin-serial "
                    "workid=%llu\n",
            marker, (unsigned long long)workid);
    fflush(stderr);
    gem5_roi_m5op(GEM5_ROI_M5OP_WORK_BEGIN, workid, 0);
}

static inline void gem5_roi_wave_checkpoint(const char *marker,
                                            uint64_t workid)
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

static inline void gem5_roi_wave_begin(const char *marker, uint64_t workid)
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
