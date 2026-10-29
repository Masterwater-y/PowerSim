#ifndef FASTSIM_SERIAL_SETUP_CHECKPOINT_H
#define FASTSIM_SERIAL_SETUP_CHECKPOINT_H

/* Instrumented libgomp only. Setup runs in KVM with no worker threads. */
#include <dirent.h>
#include <errno.h>
#include <omp.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifdef __cplusplus
extern "C" {
#endif
extern unsigned long long gomp_fastsim_thread_id(void);
extern void fastsim_wait_marker(unsigned long long, unsigned long long,
                                unsigned long long, unsigned long long,
                                unsigned long long, unsigned long long);
#ifdef __cplusplus
}
#endif

static inline void fastsim_serial_setup_outside_region(void) {
  if (omp_get_level() != 0) {
    fprintf(stderr, "serial setup checkpoint must be outside every parallel region\n");
    exit(EXIT_FAILURE);
  }
}

static inline int fastsim_serial_setup_begin(void) {
  fastsim_serial_setup_outside_region();
  int requested = omp_get_max_threads();
  if (requested < 1) exit(EXIT_FAILURE);
  /* Seed the main identity during KVM, even if setup creates no teams. */
  gomp_fastsim_thread_id();
  omp_set_dynamic(0);
  omp_set_num_threads(1);
  return requested;
}

static inline void fastsim_serial_setup_checkpoint(
    void (*checkpoint)(const char *, unsigned), const char *name,
    unsigned id, int requested) {
  fastsim_serial_setup_outside_region();
  DIR *directory = opendir("/proc/self/task");
  if (!directory) {
    perror("serial setup checkpoint: /proc/self/task");
    exit(EXIT_FAILURE);
  }
  unsigned count = 0;
  struct dirent *entry;
  errno = 0;
  while ((entry = readdir(directory))) {
    if (entry->d_name[0] >= '0' && entry->d_name[0] <= '9') ++count;
  }
  int read_error = errno;
  int close_error = closedir(directory);
  if (read_error || close_error || count != 1 || requested < 1) {
    fprintf(stderr, "serial setup checkpoint requires exactly one OS task "
                    "(tasks=%u, read_error=%d, close_error=%d)\n",
            count, read_error, close_error);
    exit(EXIT_FAILURE);
  }
  if (syscall(SYS_gettid) != (long)getpid()) {
    fprintf(stderr, "serial setup checkpoint requires the process main task\n");
    exit(EXIT_FAILURE);
  }
  printf("[fastsim-roi] serial_setup os_tasks=1 requested_threads=%d\n", requested);
  fflush(stdout);
  checkpoint(name, id);
  /* No counter reset: publish the same cached lifetime after KVM handoff. */
  long tid = syscall(SYS_gettid);
  long tgid = (long)getpid();
  if (tid <= 0 || tgid <= 0) exit(EXIT_FAILURE);
  fastsim_wait_marker(1, gomp_fastsim_thread_id(),
                     (unsigned long long)tid, 0, 0,
                     (unsigned long long)tgid);
  omp_set_num_threads(requested);
}

#endif
