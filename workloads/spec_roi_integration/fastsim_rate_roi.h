#ifndef FASTSIM_RATE_ROI_H
#define FASTSIM_RATE_ROI_H

#include <errno.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <unistd.h>

/* Same functional identity ABI as the single-thread libgomp identity marker.
 * No OpenMP runtime or synchronization semantics are added to the benchmark. */
__asm__(".pushsection .text\n"
        ".weak fastsim_wait_marker\n"
        ".type fastsim_wait_marker,@function\n"
        "fastsim_wait_marker:\n nop\n ret\n"
        ".size fastsim_wait_marker,.-fastsim_wait_marker\n"
        ".popsection\n");
extern void fastsim_wait_marker(unsigned long long, unsigned long long,
                                unsigned long long, unsigned long long,
                                unsigned long long, unsigned long long);

static void fastsim_rate_roi_ready(const char *name) {
    static int entered;
    const char *ready = getenv("FASTSIM_RATE_READY_FD");
    const char *release = getenv("FASTSIM_RATE_RELEASE_FD");
    if (entered++ || !ready || !release) return;
    char token = 'R';
    ssize_t n;
    fprintf(stderr, "[fastsim-rate] ready copy=%s phase=%s\n",
            getenv("FASTSIM_RATE_COPY"), name);
    do { n = write(atoi(ready), &token, 1); } while (n < 0 && errno == EINTR);
    if (n != 1) { perror("rate ready"); exit(EXIT_FAILURE); }
    do { n = read(atoi(release), &token, 1); } while (n < 0 && errno == EINTR);
    if (n != 1 || token != 'G') {
        fprintf(stderr, "rate gate missing release\n"); exit(EXIT_FAILURE);
    }
    close(atoi(ready)); close(atoi(release));
    unsigned long long pid = (unsigned long long)getpid();
    unsigned long long tid = (unsigned long long)syscall(SYS_gettid);
    fastsim_wait_marker(1, (pid << 32) | 1, tid, 0, 0, pid);
}
#endif
