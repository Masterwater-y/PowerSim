#ifndef SIDECAR_ROI_H
#define SIDECAR_ROI_H

/*
 * Defines the small, inherited-pipe protocol used by an instrumented workload
 * to notify its launcher of ROI preparation and start markers.
 */

#include <stdint.h>
#include <stdio.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/syscall.h>

#define SIDECAR_ROI_MAGIC 0x5343524fu
#define SIDECAR_ROI_VERSION 1u
#define SIDECAR_ROI_MARKER_MAX 64u

enum sidecar_roi_event_type {
    SIDECAR_ROI_PREPARE = 1,
    SIDECAR_ROI_BEGIN = 2
};

struct sidecar_roi_event {
    uint32_t magic;
    uint16_t version;
    uint16_t type;
    uint64_t workid;
    uint64_t monotonic_ns;
    uint64_t realtime_ns;
    uint64_t tsc;
    int32_t pid;
    int32_t tid;
    char marker[SIDECAR_ROI_MARKER_MAX];
};

static inline uint64_t sidecar_roi_clock_ns(clockid_t clock_id)
{
    struct timespec value;

    if (clock_gettime(clock_id, &value) != 0)
        return 0;
    return (uint64_t)value.tv_sec * 1000000000ull + (uint64_t)value.tv_nsec;
}

static inline uint64_t sidecar_roi_tsc(void)
{
#if defined(__x86_64__)
    unsigned int low, high;
    __asm__ volatile("rdtscp" : "=a"(low), "=d"(high) :: "rcx", "memory");
    return ((uint64_t)high << 32) | low;
#else
    return 0;
#endif
}

static inline int sidecar_roi_enabled(void)
{
    const char *value = getenv("TCSIM_NATIVE_ROI");
    return value && *value && strcmp(value, "0") != 0;
}

static inline void sidecar_roi_notify(enum sidecar_roi_event_type type,
                                      const char *marker, uint64_t workid)
{
    const char *fd_text = getenv("SIDECAR_ROI_FD");
    struct sidecar_roi_event event = {0};
    char *end = NULL;
    long fd;
    ssize_t size;

    if (!sidecar_roi_enabled() || !fd_text)
        return;
    fd = strtol(fd_text, &end, 10);
    if (!end || *end || fd < 0)
        return;

    event.magic = SIDECAR_ROI_MAGIC;
    event.version = SIDECAR_ROI_VERSION;
    event.type = (uint16_t)type;
    event.workid = workid;
    event.monotonic_ns = sidecar_roi_clock_ns(CLOCK_MONOTONIC);
    event.realtime_ns = sidecar_roi_clock_ns(CLOCK_REALTIME);
    event.tsc = sidecar_roi_tsc();
    event.pid = (int32_t)getpid();
    event.tid = (int32_t)syscall(SYS_gettid);
    if (marker)
        snprintf(event.marker, sizeof(event.marker), "%s", marker);

    do {
        size = write((int)fd, &event, sizeof(event));
    } while (size < 0 && errno == EINTR);
    if (size != (ssize_t)sizeof(event))
        return;
    if (type == SIDECAR_ROI_BEGIN) {
        const char *ack_text = getenv("SIDECAR_ROI_ACK_FD");
        char ack;
        long ack_fd;
        ssize_t ack_size;

        if (!ack_text)
            return;
        ack_fd = strtol(ack_text, &end, 10);
        if (!end || *end || ack_fd < 0)
            return;
        do {
            ack_size = read((int)ack_fd, &ack, 1);
        } while (ack_size < 0 && errno == EINTR);
    }
}

#endif
